"""Experimental MCP name mapping policy; this is not an MCP transport.

This module is deliberately not installed or connected to user servers. A
transport must enforce strict JSON-RPC framing, serialize access to the policy,
and stop on protocol errors. See MCP_NAMING.md for the current test scope.
"""

import re


_SAFE = re.compile(r"[A-Za-z0-9_-]+\Z", re.ASCII)
_RESERVED = "zZ0_"
_MAX_FULL_NAME = 128
_MAX_ORIGINAL_BYTES = 4096
_MAX_CANCELLED = 10_000


class ProtocolError(ValueError):
    """The transport must fail closed; it must not forward this message."""


def alias_for(original, server):
    """Return a deterministic, collision-free Claude-facing *tool* name.

    The reserved prefix separates encoded names from unchanged safe names.
    Escaping is reversible, unlike truncation or a hash, and keeps the
    alphanumeric parts readable. An overlong result is rejected; it is never
    shortened or silently mapped to another tool.
    """
    if not isinstance(server, str) or not _SAFE.fullmatch(server):
        raise ProtocolError("The effective server namespace must be safe ASCII")
    if not isinstance(original, str) or not original:
        raise ProtocolError("Tool name must be a nonempty string")
    try:
        raw = original.encode("utf-8", errors="strict")
    except UnicodeError:
        raise ProtocolError("Tool name is not valid UTF-8") from None
    if len(raw) > _MAX_ORIGINAL_BYTES:
        raise ProtocolError("Tool name is too long")
    if _SAFE.fullmatch(original) and not original.startswith(_RESERVED):
        alias = original
    else:
        pieces = []
        for byte in raw:
            if 48 <= byte <= 57 or 65 <= byte <= 90 or 97 <= byte <= 122 or byte == 45:
                pieces.append(chr(byte))
            elif byte == 95:
                pieces.append("_u")
            elif byte == 46:
                pieces.append("_d")
            else:
                pieces.append(f"_x{byte:02X}")
        alias = _RESERVED + "".join(pieces)
    if len("mcp__" + server + "__" + alias) > _MAX_FULL_NAME:
        raise ProtocolError("Full Claude tool name exceeds the supported limit")
    return alias


def _id_key(value):
    # bool is an int in Python, but is not a valid JSON-RPC request ID.
    if type(value) not in (int, str):
        raise ProtocolError("Request ID must be a string or integer")
    return (type(value), value)


def _error(ident, message):
    return {"jsonrpc": "2.0", "id": ident,
            "error": {"code": -32602, "message": message}}


class AliasPolicy:
    """In-memory policy for one client and one explicitly wrapped MCP server.

    Each method returns (destination, message), where destination is `server`,
    `client`, or `drop`. Calls must be serialized with server events by the transport.
    No configuration, credentials, logs or network access are used here.
    """

    def __init__(self, server):
        if not isinstance(server, str) or not _SAFE.fullmatch(server):
            raise ProtocolError("The effective server namespace must be safe ASCII")
        self.server = server
        self.epoch = 0
        self.list_started = False
        self.advertised = {}
        self.pending = {}
        self.cancelled = set()

    def _invalidate(self):
        self.epoch += 1
        self.advertised.clear()

    def client_message(self, message):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise ProtocolError("Invalid JSON-RPC message")
        if "method" not in message:
            # Response to a server-originated request. It has the opposite ID
            # namespace and must never affect the client's pending requests.
            _id_key(message.get("id"))
            return "server", message
        method = message["method"]
        if not isinstance(method, str):
            raise ProtocolError("Invalid method")
        has_id = "id" in message
        if not has_id:
            if method in ("tools/list", "tools/call"):
                raise ProtocolError("Tool methods require request IDs")
            if method == "notifications/cancelled":
                params = message.get("params")
                request_id = params.get("requestId") if isinstance(params, dict) else None
                try:
                    cancelled_key = _id_key(request_id)
                except ProtocolError:
                    cancelled_key = None
                if cancelled_key in self.pending:
                    if len(self.cancelled) >= _MAX_CANCELLED:
                        raise ProtocolError("Cancelled request tracking limit reached")
                    kind, epoch = self.pending.pop(cancelled_key)
                    self.cancelled.add(cancelled_key)
                    if kind == "list" and epoch == self.epoch:
                        self._invalidate()
                        self.list_started = False
            return "server", message
        ident = message["id"]
        key = _id_key(ident)
        if key in self.pending or key in self.cancelled:
            raise ProtocolError("Client request ID is already in flight or cancelled")
        if method == "tools/list":
            params = message.get("params", {})
            if not isinstance(params, dict):
                return "client", _error(ident, "Invalid tool-list parameters")
            cursor = params.get("cursor")
            if cursor is None:
                self._invalidate()
                self.list_started = True
            elif not isinstance(cursor, str) or not cursor or not self.list_started:
                return "client", _error(ident, "Tool-list page has no valid initial request")
            self.pending[key] = ("list", self.epoch)
            return "server", message
        if method == "tools/call":
            params = message.get("params")
            alias = params.get("name") if isinstance(params, dict) else None
            if not isinstance(alias, str) or alias not in self.advertised:
                return "client", _error(ident, "Tool is not in the current advertised list")
            original = self.advertised[alias]
            self.pending[key] = ("other", self.epoch)
            return "server", {**message, "params": {**params, "name": original}}
        self.pending[key] = ("other", self.epoch)
        return "server", message

    def server_message(self, message):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise ProtocolError("Invalid JSON-RPC message")
        if "method" in message:
            if not isinstance(message["method"], str):
                raise ProtocolError("Invalid method")
            if message["method"] == "notifications/tools/list_changed":
                self._invalidate()
                self.list_started = False
            return "client", message
        key = _id_key(message.get("id"))
        if key in self.cancelled:
            # A late reply cannot be confused with a new request. Never expose
            # its unrewritten tool list or silently accept ID reuse.
            return "drop", None
        if key not in self.pending:
            raise ProtocolError("Unexpected server response ID")
        kind, epoch = self.pending.pop(key)
        if kind != "list":
            return "client", message
        ident = message["id"]
        if epoch != self.epoch:
            return "client", _error(ident, "Tool list changed while the request was pending")
        if "error" in message and "result" not in message:
            return "client", message
        if "error" in message or "result" not in message:
            self._invalidate()
            self.list_started = False
            return "client", _error(ident, "Invalid upstream tool-list response")
        result = message.get("result")
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list):
            self._invalidate()
            self.list_started = False
            return "client", _error(ident, "Invalid upstream tool list")
        rewritten, additions = [], {}
        try:
            for tool in tools:
                if not isinstance(tool, dict):
                    raise ProtocolError("Invalid tool entry")
                original = tool.get("name")
                alias = alias_for(original, self.server)
                previous = additions.get(alias, self.advertised.get(alias))
                if previous is not None and previous != original:
                    raise ProtocolError("Tool alias collision")
                additions[alias] = original
                rewritten.append({**tool, "name": alias})
        except ProtocolError:
            self._invalidate()
            self.list_started = False
            return "client", _error(ident, "Tool list cannot be mapped safely")
        self.advertised.update(additions)
        return "client", {**message, "result": {**result, "tools": rewritten}}
