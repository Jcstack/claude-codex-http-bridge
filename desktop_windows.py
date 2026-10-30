"""Pinned OCU native worker protocol with exactly one dispatch per action.

The upstream long-lived Windows MCP backend retries crashed action workers.
Use its reviewed `op` entry directly instead: no MCP-parent retry/fallback.
Window/element bindings, leases, consent and receipts belong to our wrapper.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import secrets

from desktop_scope import ScopeError


class WindowsWorker:
    def __init__(self, binary, environment, window_id, scope):
        self.binary, self.environment, self.window_id, self.scope = binary, environment, window_id, scope
        self.snapshot = None
        self.bounds = None
        self.capture_mode = "wgc"

    async def operation(self, payload):
        # Auto capture in upstream includes screen-region GDI, which can show
        # an unrelated overlapping window. Force a window-only capture mode,
        # including the post-action snapshot performed inside the worker.
        payload = {**payload, "env_flags": {"OPEN_COMPUTER_USE_WINDOWS_CAPTURE": self.capture_mode}}
        process = await asyncio.create_subprocess_exec(str(self.binary), "op", env=self.environment,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            limit=32 << 20)
        try:
            process.stdin.write(json.dumps(payload).encode()+b"\n")
            await process.stdin.drain(); process.stdin.close()
            # Bounded read even if a crashed/corrupt worker emits garbage.
            chunks, size = [], 0
            async with asyncio.timeout(30):
                while True:
                    chunk = await process.stdout.read(65536)
                    if not chunk: break
                    size += len(chunk)
                    if size > 32 << 20: raise ScopeError("Native desktop worker exceeded its response bound; no action was retried")
                    chunks.append(chunk)
                await process.wait()
            if process.returncode: raise ScopeError("Native desktop worker ended with an unknown action outcome; observe before continuing")
            try: value = json.loads(b"".join(chunks))
            except ValueError: raise ScopeError("Native desktop worker returned incomplete data; no action was retried") from None
            if not isinstance(value, dict) or value.get("ok") is not True:
                raise ScopeError("Native desktop action was not confirmed; observe before continuing. No worker retry or foreground fallback occurred")
            return value
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    def result(self, snapshot, *, store=False):
        from mcp import types
        from PIL import Image
        if not isinstance(snapshot, dict) or snapshot.get("windowHandle") != self.window_id:
            raise ScopeError("Native worker returned a different selected window")
        encoded = snapshot.get("screenshotPngBase64", "")
        if not isinstance(encoded, str) or len(encoded) > 24 << 20: raise ScopeError("Invalid scoped desktop image")
        try:
            raw = base64.b64decode(encoded, validate=True)
            with Image.open(io.BytesIO(raw), formats=("PNG",)) as image:
                if image.format != "PNG" or image.width*image.height > 32_000_000: raise ScopeError("Invalid scoped screenshot dimensions/format")
                width, height = image.size
        except (ValueError, OSError): raise ScopeError("Scoped screenshot is unavailable; no whole-display fallback is used") from None
        metadata = {"window": {"id": self.window_id}, "accessibility": {"tree": "\n".join(snapshot.get("treeLines", []))},
                    "screenshots": [{"id": secrets.token_urlsafe(18), "width": width, "height": height}]}
        if store:
            self.snapshot = snapshot
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(metadata)),
            types.ImageContent(type="image", data=encoded, mime_type="image/png")])

    async def call_tool(self, name, arguments):
        if arguments.get("window", {}).get("id") != self.window_id:
            raise ScopeError("The Windows worker cannot change its selected window")
        if name == "get_window_state":
            self.snapshot = None
            before = self.scope.bounds(self.window_id)
            for mode in ("wgc", "print"):
                self.capture_mode = mode
                try:
                    value = await self.operation({"tool": name, "windowId": self.window_id, "include_screenshot": True,
                        "text_limit": 12000, "max_tree_nodes": 1000, "max_tree_depth": 64})
                    result = self.result(value.get("snapshot"), store=True)
                    break
                except ScopeError:
                    if mode == "print": raise
            after = self.scope.bounds(self.window_id)
            if before != after: raise ScopeError("Window moved during observation; observe again")
            self.bounds = after
            return result
        snapshot, self.snapshot = self.snapshot, None
        if snapshot is None or self.scope.bounds(self.window_id) != self.bounds:
            raise ScopeError("Window changed or this native observation was already consumed; observe again")
        allowed = {"click", "set_value", "type_text", "press_key", "scroll", "drag"}
        if name not in allowed: raise ScopeError("Unknown native window action")
        payload = {k: v for k, v in arguments.items() if k not in ("window", "screenshotId", "element_index")}
        payload.update({"tool": name, "windowId": self.window_id, "windowBounds": snapshot.get("windowBounds")})
        if "element_index" in arguments:
            records = {str(row.get("index")): row for row in snapshot.get("elements", []) if isinstance(row, dict)}
            record = records.get(arguments["element_index"])
            if record is None: raise ScopeError("Element index did not come from this window observation")
            payload["element"] = record
        if name == "click":
            payload.setdefault("click_count", 1); payload.setdefault("mouse_button", "left")
        if name in ("scroll", "drag") and "element" not in payload: payload["input_method"] = "app_post"
        value = await self.operation(payload)  # Exactly once, including crashes/timeouts/truncated output.
        return self.result(value.get("snapshot"))
