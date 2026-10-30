"""Opt-in window-scoped community computer use, owned by the Claude harness."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import sys
import tarfile
import time
import urllib.request

import desktop_scope
import runtime_bundle

VERSION = "1.3.0"
ARCHIVE_SHA = "e800b756424118c9afc9d18950a91a12e954831fbecb6ea2ac060967d325dc1c"
ARCHIVE_URL = "https://github.com/opensymph/open-computer-use/releases/download/v1.3.0/opensymph-open-computer-use-1.3.0.tgz"
BINARIES = {
    "darwin": ("Open Computer Use.app/Contents/MacOS/OpenComputerUse", "16d7d32c085065a7cf92745789785630786d5ba13aef3a3242fd91d7c2257361"),
    "windows-x86_64": ("windows/amd64/open-computer-use.exe", "6bafd71418b85d83ace19937bcae4a4934a153244c14926ffd13e3fbac4da40f"),
    "windows-arm64": ("windows/arm64/open-computer-use.exe", "19e4dbaeb164dd306b39dc028eb34294bd2eb9fa87fc5e9e00db04a55746ca3f"),
}


def asset() -> tuple[str, str]:
    target = runtime_bundle.target()
    key = "darwin" if target.startswith("darwin-") else target
    if key not in BINARIES: raise desktop_scope.ScopeError("Scoped desktop requires macOS or Windows; Linux display fallback is disabled")
    return BINARIES[key]


def executable(root: Path) -> Path:
    name, digest = asset()
    base = root / "desktop" / VERSION
    path = base / name
    if any(p.is_symlink() for p in (base, root / "desktop", path, *path.parents[:len(Path(name).parts)-1])):
        raise desktop_scope.ScopeError("Desktop executable directories must not be symlinked")
    runtime_bundle.safe_file(path, limit=100 << 20)
    if runtime_bundle.file_hash(path) != digest: raise desktop_scope.ScopeError("Reviewed desktop binary checksum mismatch")
    return path


def install(root: Path, archive: Path | None = None):
    name, _ = asset()
    directory = root / "desktop"; runtime_bundle.private_dir(directory)
    if archive is None:
        archive = directory / ("release-" + ARCHIVE_SHA + ".tgz")
        if not archive.exists():
            with urllib.request.urlopen(ARCHIVE_URL, timeout=60) as response:
                raw = response.read((32 << 20) + 1)
            if len(raw) > 32 << 20 or hashlib.sha256(raw).hexdigest() != ARCHIVE_SHA:
                raise desktop_scope.ScopeError("Desktop archive checksum or size mismatch")
            runtime_bundle.write_file(archive, raw, 0o600)
    if archive.is_symlink() or not archive.is_file() or archive.stat().st_size > 32 << 20 or runtime_bundle.file_hash(archive) != ARCHIVE_SHA:
        raise desktop_scope.ScopeError("Desktop archive does not match the reviewed release")
    base = directory / VERSION; runtime_bundle.private_dir(base)
    prefix = "package/dist/"
    with tarfile.open(archive, "r:gz") as package:
        for member in package.getmembers():
            if not member.name.startswith(prefix): continue
            relative = member.name[len(prefix):]
            if not (relative == name or (sys.platform == "darwin" and relative.startswith("Open Computer Use.app/"))): continue
            parts = Path(relative)
            if parts.is_absolute() or ".." in parts.parts or "\\" in relative or not (member.isfile() or member.isdir()):
                raise desktop_scope.ScopeError("Unsafe desktop archive member")
            destination = base / parts
            runtime_bundle.private_dir(destination if member.isdir() else destination.parent)
            if member.isfile():
                if member.size > 100 << 20: raise desktop_scope.ScopeError("Desktop archive member exceeds the size bound")
                with package.extractfile(member) as source:
                    runtime_bundle.write_file(destination, source.read(), 0o700 if relative == name else 0o600)
    executable(root)
    print("Reviewed desktop runtime installed. No window selected and no OS permission changed.")


def selection(root: Path, workspace: Path | None = None) -> dict | None:
    path = root / "desktop.json"
    if not path.exists(): return None
    runtime_bundle.safe_file(path, limit=16384)
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or value.get("format") != 1 or type(value.get("enabled")) is not bool or
        not isinstance(value.get("workspace"), str) or not isinstance(value.get("identity"), dict) or
        set(value["identity"]) != {"id", "pid", "created", "executable"} or
        type(value["identity"]["id"]) is not int or type(value["identity"]["pid"]) is not int):
        raise desktop_scope.ScopeError("Invalid private desktop selection")
    if not value["enabled"] or (workspace is not None and str(workspace.resolve()) != value["workspace"]): return None
    return value


def child_environment() -> dict:
    allowed = ("PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT", "COMSPEC", "PATHEXT", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "TEMP", "TMP")
    result = {name: os.environ[name] for name in allowed if name in os.environ}
    result["OPEN_COMPUTER_USE_DISABLE_APP_AGENT_PROXY"] = "1"
    for flag in ("OPEN_COMPUTER_USE_ALLOW_GLOBAL_POINTER_FALLBACKS", "OPEN_COMPUTER_USE_WINDOWS_ALLOW_FOREGROUND_INPUT",
                 "OPEN_COMPUTER_USE_WINDOWS_ALLOW_APP_LAUNCH", "OPEN_COMPUTER_USE_WINDOWS_ALLOW_FOCUS_ACTIONS",
                 "OPEN_COMPUTER_USE_WINDOWS_ALLOW_UIA_TEXT_FALLBACK"):
        result[flag] = "0"
    return result


class DesktopSession:
    def __init__(self, root: Path, workspace: Path, *, scope=None, configuration=None):
        self.root, self.workspace = root.resolve(), workspace.resolve()
        self.configuration = configuration or selection(root, workspace)
        if self.configuration is None: raise desktop_scope.ScopeError("Select a window in this workspace before enabling desktop tools")
        self.scope = scope or desktop_scope.inventory()
        self.queue = asyncio.Queue(); self.worker = None; self.lock = asyncio.Lock()
        self.receipt = None

    def check(self, *, keyboard=False):
        if selection(self.root, self.workspace) != self.configuration:
            raise desktop_scope.ScopeError("Desktop selection changed or was disabled; start a new Claude launch")
        identity = self.configuration["identity"]
        if self.scope.identity(identity["id"], keyboard=keyboard) != identity:
            raise desktop_scope.ScopeError("Window or process identity changed; select the window again")

    def lease(self):
        directory = self.root / "desktop"; runtime_bundle.private_dir(directory)
        digest = hashlib.sha256(runtime_bundle.canonical(self.configuration["identity"])).hexdigest()
        path = directory / ("lease-" + digest + ".lock")
        if path.is_symlink(): raise desktop_scope.ScopeError("Desktop lease must not be symlinked")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        handle = os.fdopen(fd, "r+b", buffering=0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                if os.fstat(handle.fileno()).st_size == 0: handle.write(b"0")
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close(); raise desktop_scope.ScopeError("This selected window is already leased by another bridge session") from None
        return handle

    async def _run(self):
        from mcp import Client, StdioServerParameters
        # One owner task enters and exits the SDK's anyio task group. Requests
        # never enter its cancel scope from a different MCP request task.
        lease = None
        try:
            lease = self.lease()
            from contextlib import asynccontextmanager
            @asynccontextmanager
            async def backend():
                if os.name == "nt":
                    from desktop_windows import WindowsWorker
                    yield WindowsWorker(executable(self.root), child_environment(), self.configuration["identity"]["id"], self.scope)
                else:
                    params = StdioServerParameters(command=str(executable(self.root)), args=["mcp"], env=child_environment(), cwd=self.workspace)
                    async with Client(params, read_timeout_seconds=30) as client: yield client
            async with backend() as client:
                while True:
                    job = await self.queue.get()
                    if job is None: return
                    name, arguments, future = job
                    if future.cancelled(): continue
                    try:
                        self.check(keyboard=name in ("type_text", "press_key"))
                        result = await client.call_tool(name, arguments)
                        self.check()
                        if result.is_error: raise desktop_scope.ScopeError("Desktop action failed; observe again. No foreground fallback was attempted")
                        if not future.done(): future.set_result(result)
                    except Exception:
                        if not future.done(): future.set_exception(desktop_scope.ScopeError("Scoped desktop call failed; verify window visibility, identity and OS permissions"))
        except Exception:
            # Startup failures must also complete a queued caller. Never echo
            # raw child exceptions, input strings, window trees or file paths.
            while not self.queue.empty():
                job = self.queue.get_nowait()
                if job is not None and not job[2].done(): job[2].set_exception(desktop_scope.ScopeError("Reviewed desktop runtime could not start in this scope"))
        finally:
            if lease: lease.close()

    async def call(self, name, arguments):
        if self.worker is not None and self.worker.done(): raise desktop_scope.ScopeError("Desktop worker ended; start a new Claude launch")
        future = asyncio.get_running_loop().create_future()
        self.queue.put_nowait((name, arguments, future))
        if self.worker is None: self.worker = asyncio.create_task(self._run())
        try:
            async with asyncio.timeout(40): return await future
        except asyncio.CancelledError:
            # Input may already have been sent. Stop the private child and
            # invalidate the observation; never replay a cancelled action.
            self.receipt = None
            await self.close()
            raise
        except TimeoutError:
            self.receipt = None
            await self.close()
            raise desktop_scope.ScopeError("Desktop call exceeded its budget; no action was retried") from None

    async def close(self):
        if self.worker is not None and not self.worker.done():
            self.worker.cancel()
            try: await self.worker
            except asyncio.CancelledError: pass

    async def observe(self):
        from mcp import types
        from PIL import Image
        async with self.lock:
            self.receipt = None; self.check()
            result = await self.call("get_window_state", {"window": {"id": self.configuration["identity"]["id"]},
                "include_screenshot": True, "include_text": True, "text_limit": 12000, "max_tree_nodes": 1000})
            values = []
            for block in result.content:
                if block.type == "text":
                    try: values.append(json.loads(block.text))
                    except ValueError: pass
            metadata = next((value for value in values if isinstance(value, dict) and isinstance(value.get("window"), dict)), None)
            if metadata is None or metadata["window"].get("id") != self.configuration["identity"]["id"]:
                raise desktop_scope.ScopeError("Desktop returned a different window; the observation was withheld")
            images = [block for block in result.content if block.type == "image"]
            if len(images) != 1: raise desktop_scope.ScopeError("A scoped screenshot is required; no coordinate or whole-display fallback is used")
            import base64
            raw = base64.b64decode(images[0].data, validate=True)
            with Image.open(io.BytesIO(raw)) as image:
                if image.width*image.height > 32_000_000: raise desktop_scope.ScopeError("Desktop screenshot exceeds the pixel bound")
                width, height = image.size
            screenshots = metadata.get("screenshots", [])
            shot = screenshots[0].get("id") if len(screenshots) == 1 else None
            if not isinstance(shot, str): raise desktop_scope.ScopeError("Scoped screenshot identity is missing")
            token = secrets.token_urlsafe(24)
            self.receipt = (token, time.monotonic()+60, shot, width, height)
            return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps({"observation": token,
                "expires_in_seconds": 60, "scope": "one selected window", "image_width": width, "image_height": height})), *result.content])

    def action_arguments(self, action: str, arguments: dict) -> dict:
        allowed = {"click": {"element_index", "x", "y", "click_count", "mouse_button"}, "set_value": {"element_index", "value"},
            "type_text": {"text"}, "press_key": {"key"}, "scroll": {"element_index", "direction", "pages", "x", "y", "scrollX", "scrollY"},
            "drag": {"from_x", "from_y", "to_x", "to_y"}}
        if action not in allowed or not isinstance(arguments, dict) or set(arguments)-allowed[action]:
            raise desktop_scope.ScopeError("Unsupported desktop action or argument; app, window, focus and global-method overrides are not accepted")
        result = dict(arguments)
        for key in ("element_index", "value", "text", "key"):
            if key in result and (not isinstance(result[key], str) or not (0 if key == "value" else 1) <= len(result[key]) <= 16000):
                raise desktop_scope.ScopeError("Desktop string arguments exceed their bounds")
        if action == "set_value" and not {"element_index", "value"} <= set(result): raise desktop_scope.ScopeError("set_value requires an observed element index and value")
        if action == "type_text" and "text" not in result: raise desktop_scope.ScopeError("type_text requires text")
        if action == "press_key" and not re.fullmatch(r"(?:Return|Tab|Escape|BackSpace|Delete|Left|Right|Up|Down|Home|End|Page_Up|Page_Down|space|(?:ctrl|cmd)\+[al])", result.get("key", "")):
            raise desktop_scope.ScopeError("Use navigation keys, ctrl/cmd+a or ctrl/cmd+l; application/global shortcuts are outside this window policy")
        for key in ("x", "y", "from_x", "from_y", "to_x", "to_y", "pages", "scrollX", "scrollY"):
            if key in result and (type(result[key]) not in (int, float) or not math.isfinite(result[key])):
                raise desktop_scope.ScopeError("Desktop coordinates must be finite numbers")
        for x, y in (("x", "y"), ("from_x", "from_y"), ("to_x", "to_y")):
            if x in result or y in result:
                if not (x in result and y in result and 0 <= result[x] < self.receipt[3] and 0 <= result[y] < self.receipt[4]):
                    raise desktop_scope.ScopeError("Coordinates must lie inside the latest selected-window screenshot")
        if action == "drag" and not {"from_x", "from_y", "to_x", "to_y"} <= set(result): raise desktop_scope.ScopeError("drag requires two observed coordinate pairs")
        if action == "click":
            if ("element_index" in result) == ("x" in result): raise desktop_scope.ScopeError("Click one observed element or one coordinate pair")
            if result.get("click_count", 1) not in (1, 2) or result.get("mouse_button", "left") not in ("left", "right", "middle"):
                raise desktop_scope.ScopeError("Unsupported click count or mouse button")
            result["click_method"] = "accessibility" if ("element_index" in result and result.get("click_count", 1) == 1
                and result.get("mouse_button", "left") == "left") else "app_post"
        if action == "scroll":
            if "element_index" in result:
                if result.get("direction") not in ("up", "down", "left", "right") or not 0 < result.get("pages", 1) <= 10:
                    raise desktop_scope.ScopeError("Element scrolling requires a direction and 0–10 pages")
            elif not {"x", "y"} <= set(result) or not ({"scrollX", "scrollY"} & set(result)):
                raise desktop_scope.ScopeError("Coordinate scroll requires an observed point and pixel delta")
            if any(abs(result.get(key, 0)) > 10000 for key in ("scrollX", "scrollY")): raise desktop_scope.ScopeError("Scroll delta exceeds the window policy")
        result["window"] = {"id": self.configuration["identity"]["id"]}
        if action in ("click", "scroll", "drag"): result["screenshotId"] = self.receipt[2]
        return result

    async def act(self, observation: str, action: str, arguments: dict):
        async with self.lock:
            if self.receipt is None or not hmac.compare_digest(observation, self.receipt[0]) or time.monotonic() > self.receipt[1]:
                raise desktop_scope.ScopeError("Observe this selected window again; the observation is missing, expired or already consumed")
            self.check(keyboard=action in ("type_text", "press_key"))
            payload = self.action_arguments(action, arguments)
            self.receipt = None  # Consume before dispatch, including errors/cancellation.
            return await self.call(action, payload)


def main(arguments: list[str], *, root: Path) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "windows", "select", "status", "disable"))
    parser.add_argument("window", nargs="?", type=int)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args(arguments)
    if args.action == "install": install(root, args.archive); return 0
    if args.action == "windows":
        print(json.dumps(desktop_scope.inventory().windows(), ensure_ascii=False)); return 0
    if args.action == "select":
        if args.window is None: raise desktop_scope.ScopeError("Usage: claude-codex desktop select WINDOW_ID")
        import capabilities
        capabilities.selected_server(root); capabilities.selected_python(root); executable(root)
        identity = desktop_scope.inventory().identity(args.window)
        value = {"format": 1, "enabled": True, "workspace": str(Path.cwd().resolve()), "identity": identity}
        runtime_bundle.write_file(root / "desktop.json", runtime_bundle.canonical(value), 0o600)
        print("One visible window selected for future launches in this workspace. Its screenshot/text will enter model context only after a Claude-approved tool call.")
        return 0
    if args.action == "disable":
        path = root / "desktop.json"
        if path.exists():
            value = selection(root)
            if value:
                value["enabled"] = False; runtime_bundle.write_file(path, runtime_bundle.canonical(value), 0o600)
        print("Desktop scope disabled, including further calls from existing bridge desktop sessions")
        return 0
    value = selection(root)
    print(json.dumps({"selected": value is not None, "workspace_matches": bool(value and value["workspace"] == str(Path.cwd().resolve())),
                      "window_id": value["identity"]["id"] if value else None, "global_pointer_fallback": False,
                      "automatic_activation": False, "microphone": False}))
    return 0
