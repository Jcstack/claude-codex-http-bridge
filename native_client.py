"""Use an already-running private subscription endpoint without reading OAuth.

Never starts a service, follows a redirect, uses environment HTTP proxies, or
returns upstream error bodies. Callers own consent and any artifact storage.
"""

from __future__ import annotations

import json
from pathlib import Path
import ssl
import urllib.error
import urllib.parse
import urllib.request

import api_profile
import rolling_runtime


class NativeRouteError(RuntimeError):
    def __init__(self, status: int | None, category: str):
        self.status = status
        self.category = category
        super().__init__(f"Native subscription route failed: status={status}, category={category}")


class NativeClient:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.state = rolling_runtime.state(self.root)
        if self.state is None:
            raise NativeRouteError(None, "managed_service_required")
        rolling_runtime.endpoint_status(self.root, self.state)
        self.runtime = self.root / "runtime-managed"
        self.key = rolling_runtime.private_key(self.runtime / "front-key")
        self.base = f'https://127.0.0.1:{self.state["port"]}'
        context = ssl.create_default_context(cafile=str(self.runtime / "ca.pem"))
        self.context = context
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context), api_profile.NoRedirect())

    def validate_path(self, path: str):
        parsed = urllib.parse.urlsplit(path)
        decoded = urllib.parse.unquote(parsed.path)
        if (parsed.scheme or parsed.netloc or not path.startswith("/v1/") or
                parsed.fragment or "\\" in decoded or any(ord(c) < 32 for c in path + decoded) or
                any(part in (".", "..") for part in decoded.split("/"))):
            raise NativeRouteError(None, "invalid_local_route")

    def open(self, path: str, payload: dict | None = None, *, timeout: float = 180):
        self.validate_path(path)
        request = urllib.request.Request(self.base + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json"})
        try:
            return self.opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            raw = error.read(65536)
            error.close()
            category = "upstream_error"
            try:
                value = json.loads(raw).get("error", {})
                kind = value.get("type") if isinstance(value, dict) else None
                if kind in ("not_supported_error", "invalid_request_error", "authentication_error",
                            "permission_error", "rate_limit_error", "server_error", "api_error"):
                    category = kind
            except (ValueError, AttributeError):
                pass
            raise NativeRouteError(error.code, category) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise NativeRouteError(None, "transport_error") from None

    def json(self, path: str, payload: dict | None = None, *, limit: int = 32 << 20, timeout: float = 180):
        with self.open(path, payload, timeout=timeout) as response:
            raw = response.read(limit + 1)
        if len(raw) > limit:
            raise NativeRouteError(None, "response_limit")
        try:
            value = json.loads(raw)
        except ValueError:
            raise NativeRouteError(None, "invalid_json") from None
        if not isinstance(value, dict):
            raise NativeRouteError(None, "invalid_response")
        return value

    async def async_json(self, path: str, payload: dict | None = None, *, limit: int = 32 << 20):
        import httpx2
        self.validate_path(path)
        try:
            async with httpx2.AsyncClient(verify=self.context, trust_env=False, timeout=None,
                                         follow_redirects=False) as client:
                async with client.stream("POST" if payload is not None else "GET", self.base + path,
                        json=payload, headers={"Authorization": "Bearer " + self.key}) as response:
                    chunks, length = [], 0
                    async for chunk in response.aiter_bytes():
                        length += len(chunk)
                        if length > limit:
                            raise NativeRouteError(None, "response_limit")
                        chunks.append(chunk)
                    if not 200 <= response.status_code < 300:
                        raise NativeRouteError(response.status_code, "upstream_error")
        except httpx2.HTTPError:
            raise NativeRouteError(None, "transport_error") from None
        try:
            result = json.loads(b"".join(chunks))
        except ValueError:
            raise NativeRouteError(None, "invalid_json") from None
        if not isinstance(result, dict):
            raise NativeRouteError(None, "invalid_response")
        return result

    def realtime(self, model: str):
        from websockets.asyncio.client import connect
        query = urllib.parse.urlencode({"model": model})
        return connect(self.base.replace("https://", "wss://", 1) + "/v1/realtime?" + query,
            ssl=self.context, additional_headers={"Authorization": "Bearer " + self.key},
            proxy=None, open_timeout=30, close_timeout=5, ping_interval=20, ping_timeout=None,
            max_size=4 << 20, compression=None)

    def events(self, path: str, payload: dict, *, timeout: float = 180):
        with self.open(path, {**payload, "stream": True}, timeout=timeout) as response:
            while True:
                line = response.readline((4 << 20) + 1)
                if not line:
                    return
                if len(line) > 4 << 20:
                    raise NativeRouteError(None, "event_limit")
                if line.startswith(b"data:"):
                    raw = line[5:].strip()
                    if raw and raw != b"[DONE]":
                        try:
                            value = json.loads(raw)
                        except ValueError:
                            raise NativeRouteError(None, "invalid_event") from None
                        if isinstance(value, dict):
                            yield value
