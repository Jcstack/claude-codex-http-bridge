"""Observe a synthetic Streamable HTTP MCP notification connection recovery.

All services and credentials are fixture-owned. This does not connect any real
plugin or provider and does not change a user's Claude configuration.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import json
import os
from pathlib import Path
import queue
import shutil
import socket
import subprocess
import tempfile
import threading

from claude_fixture import isolated_environment
from probe_mcp_alias_candidate import MODEL, run_dynamic_client


OLD = "mcp__gateway__lookup"
NEW = "mcp__gateway__lookup_extra"
REFRESH = "mcp__gateway__refresh"
SESSION = "synthetic-mcp-session"


class McpHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def reply(self, status, body=b"", content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Mcp-Session-Id", SESSION)
        self.end_headers()
        if body:
            self.wfile.write(body)
            self.wfile.flush()

    def do_POST(self):
        message = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        ident, method = message.get("id"), message.get("method")
        if ident is None:
            self.reply(202)
            return
        if method == "initialize":
            result = {"protocolVersion": message.get("params", {}).get("protocolVersion", "2025-11-25"),
                      "capabilities": {"tools": {"listChanged": True}},
                      "serverInfo": {"name": "synthetic", "version": "1"}}
        elif method == "tools/list":
            self.server.list_count += 1
            names = ["lookup", "refresh", "lookup_extra"] if self.server.changed.is_set() else ["lookup", "refresh"]
            result = {"tools": [{"name": name, "description": "Synthetic fixture",
                                 "inputSchema": {"type": "object", "properties": {}}} for name in names]}
        elif method == "tools/call":
            name = message.get("params", {}).get("name")
            if name not in ("lookup", "lookup_extra", "refresh"):
                self.reply(400)
                return
            self.server.calls.append(name)
            result = {"content": [{"type": "text", "text": "OK"}]}
        else:
            self.reply(200, json.dumps({"jsonrpc": "2.0", "id": ident,
                                        "error": {"code": -32601, "message": "synthetic unknown"}}).encode())
            return
        if (method == "tools/call" and name == "refresh" and self.server.changed.is_set()
                and self.server.request_notification):
            notice = {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
            answer = {"jsonrpc": "2.0", "id": ident, "result": result}
            payload = ("data: " + json.dumps(notice) + "\n\ndata: " + json.dumps(answer) + "\n\n").encode()
            self.server.request_notification_sent = True
            self.reply(200, payload, "text/event-stream")
            return
        self.reply(200, json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}).encode())
        if method == "tools/call" and name == "lookup" and not self.server.transition_started.is_set():
            self.server.transition_started.set()
            threading.Thread(target=self.server.transition, daemon=True).start()

    def do_GET(self):
        with self.server.lock:
            self.server.get_count += 1
            number = self.server.get_count
            reject = (self.server.disconnect_mode and number >= 2
                      and (number in (2, 3) or self.server.request_notification))
            if reject:
                self.server.failed_retries += 1
                if self.server.failed_retries == 2:
                    self.server.retries_seen.set()
        if reject:
            self.reply(503)
            return
        events = queue.Queue()
        with self.server.lock:
            self.server.streams.append(events)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Mcp-Session-Id", SESSION)
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(b": synthetic connection\n\n")
            self.wfile.flush()
            self.server.stream_open.set()
            while not self.server.stopping.is_set():
                try:
                    item = events.get(timeout=1)
                except queue.Empty:
                    continue
                if item is None:
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return
                self.wfile.write(("data: " + json.dumps(item) + "\n\n").encode())
                self.wfile.flush()
        except OSError:
            pass
        finally:
            with self.server.lock:
                if events in self.server.streams:
                    self.server.streams.remove(events)
            if number == 1:
                self.server.first_stream_closed.set()


class ModelHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        if "count_tokens" in self.path:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"input_tokens":100}')
            return
        server = self.server
        step = server.requests
        server.requests += 1
        names = {tool.get("name") for tool in body.get("tools", []) if isinstance(tool, dict)}
        server.new_offered |= NEW in names
        if step == 0:
            server.initial_old_offered = OLD in names
            server.initial_stream_seen = server.mcp.stream_open.wait(12)
            name, text = OLD, ""
        elif (server.request_notification and server.mcp.changed.is_set()
              and not server.synthetic_refresh_called and REFRESH in names):
            server.synthetic_refresh_called = True
            name, text = REFRESH, ""
        elif not server.new_called and NEW in names:
            server.new_called = True
            name, text = NEW, ""
        else:
            name, text = None, "DONE" if server.new_called else "REFRESH_PENDING"
        calling = name is not None
        message = {"id": f"msg_fixture_{step}", "type": "message", "role": "assistant", "model": MODEL,
                   "content": [], "stop_reason": None, "stop_sequence": None,
                   "usage": {"input_tokens": 100, "output_tokens": 5}}
        block = ({"type": "tool_use", "id": f"fixture_call_{step}", "name": name, "input": {}} if calling
                 else {"type": "text", "text": ""})
        delta = ({"type": "input_json_delta", "partial_json": "{}"} if calling
                 else {"type": "text_delta", "text": text})
        events = [{"type": "message_start", "message": message},
                  {"type": "content_block_start", "index": 0, "content_block": block},
                  {"type": "content_block_delta", "index": 0, "delta": delta},
                  {"type": "content_block_stop", "index": 0},
                  {"type": "message_delta", "delta": {"stop_reason": "tool_use" if calling else "end_turn"},
                   "usage": {"output_tokens": 5}}, {"type": "message_stop"}]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for event in events:
                self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
        except OSError:
            pass


def run_case(claude, disconnect_mode, request_notification=False):
    with tempfile.TemporaryDirectory(prefix="bridge-mcp-stream-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
        root = Path(temporary).resolve()
        work, config = root / "work", root / "config"
        work.mkdir()
        config.mkdir(mode=0o700)
        marker = root / "transition-complete"
        mcp = ThreadingHTTPServer(("127.0.0.1", 0), McpHandler)
        mcp.daemon_threads = True
        mcp.disconnect_mode, mcp.get_count, mcp.failed_retries, mcp.list_count = disconnect_mode, 0, 0, 0
        mcp.request_notification, mcp.request_notification_sent = request_notification, False
        mcp.calls, mcp.streams, mcp.lock = [], [], threading.Lock()
        mcp.changed, mcp.stream_open, mcp.retries_seen = threading.Event(), threading.Event(), threading.Event()
        mcp.first_stream_closed = threading.Event()
        mcp.transition_started, mcp.stopping = threading.Event(), threading.Event()
        mcp.notification_queued = False

        def transition():
            if disconnect_mode:
                with mcp.lock:
                    for stream in list(mcp.streams):
                        stream.put(None)
                mcp.first_stream_closed.wait(5)
                mcp.retries_seen.wait(20)
            mcp.changed.set()
            notice = {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
            with mcp.lock:
                for stream in list(mcp.streams):
                    stream.put(notice)
                    mcp.notification_queued = True
            marker.touch()

        mcp.transition = transition
        model = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
        model.requests, model.mcp = 0, mcp
        model.request_notification, model.synthetic_refresh_called = request_notification, False
        model.initial_old_offered = model.initial_stream_seen = model.new_offered = model.new_called = False
        threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (mcp, model)]
        for thread in threads:
            thread.start()
        mcp_config = root / "mcp.json"
        mcp_config.write_text(json.dumps({"mcpServers": {"gateway": {"type": "http",
            "url": f"http://127.0.0.1:{mcp.server_port}/mcp"}}}), encoding="utf-8")
        settings = root / "settings.json"
        settings.write_text(json.dumps({"modelPicker": {"options": [{"model": MODEL, "label": "Fixture",
            "behavesAs": "claude-sonnet-5-5"}], "replaceBuiltInOptions": True}}), encoding="utf-8")
        env = isolated_environment(root)
        env.update(CLAUDE_CONFIG_DIR=str(config), ANTHROPIC_BASE_URL=f"http://127.0.0.1:{model.server_port}",
                   ANTHROPIC_AUTH_TOKEN="fixture-only", ANTHROPIC_DEFAULT_SONNET_MODEL=MODEL,
                   ENABLE_TOOL_SEARCH="false", MCP_CONNECTION_NONBLOCKING="0",
                   CLAUDE_CODE_MCP_STARTUP_WAIT_MS="5000", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                   DISABLE_TELEMETRY="1")
        command = [claude, "--restricted", "--strict-mcp-config", "--mcp-config", str(mcp_config), "--settings", str(settings),
                   "--permission-mode", "dontAsk", "--model", MODEL, "--tools", "", "--allowedTools", ",".join([OLD, NEW, REFRESH]),
                   "--output-format", "json", "--max-turns", "8"]
        try:
            completed = run_dynamic_client(command, work, env, marker, delay=0.5, forwarded_timeout=30)
            expected_calls = ["lookup", "refresh", "lookup_extra"] if request_notification else ["lookup", "lookup_extra"]
            result = {"synthetic_only": True, "http_mcp": True, "disconnect_mode": disconnect_mode,
                      "request_related_notification": request_notification,
                      "initial_tool_offered": model.initial_old_offered, "initial_event_stream_seen": model.initial_stream_seen,
                      "initial_event_stream_closed": mcp.first_stream_closed.is_set(),
                      "old_tool_called": bool(mcp.calls and mcp.calls[0] == "lookup"), "event_stream_gets": mcp.get_count,
                      "failed_retries": mcp.failed_retries, "list_requests": mcp.list_count,
                      "notification_queued_on_stream": mcp.notification_queued,
                      "request_related_notification_sent": mcp.request_notification_sent,
                      "synthetic_refresh_tool_called": model.synthetic_refresh_called,
                      "new_tool_offered": model.new_offered, "new_tool_called": mcp.calls == expected_calls,
                      "completed": completed}
            if not (model.initial_old_offered and model.initial_stream_seen and result["old_tool_called"]
                    and marker.exists() and (not disconnect_mode or
                        (mcp.first_stream_closed.is_set() and mcp.failed_retries >= 2))):
                print(json.dumps(result, sort_keys=True), flush=True)
                raise RuntimeError("Synthetic HTTP MCP control was not fully exercised")
            if not disconnect_mode and not (mcp.notification_queued and model.new_offered
                                            and result["new_tool_called"] and completed):
                print(json.dumps(result, sort_keys=True), flush=True)
                raise RuntimeError("Healthy synthetic HTTP MCP notification control failed")
            if request_notification and not (not mcp.notification_queued and mcp.request_notification_sent and model.new_offered
                                             and result["new_tool_called"] and completed):
                print(json.dumps(result, sort_keys=True), flush=True)
                raise RuntimeError("Synthetic request-related notification did not refresh the client")
            return result
        finally:
            mcp.stopping.set()
            with mcp.lock:
                for stream in list(mcp.streams):
                    stream.put(None)
            for server in (model, mcp):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disconnect", action="store_true")
    parser.add_argument("--request-notification", action="store_true")
    args = parser.parse_args()
    if args.request_notification and not args.disconnect:
        raise RuntimeError("The request-related control requires the disconnected stream")
    claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
    if not claude:
        raise RuntimeError("The locked Claude fixture is required")
    result = run_case(claude, args.disconnect, args.request_notification)
    print(json.dumps(result, sort_keys=True))
    if (os.environ.get("GITHUB_ACTIONS") == "true" and args.disconnect and not args.request_notification
            and not result["new_tool_offered"]):
        print("::warning::Synthetic MCP event-stream retries ended before a changed tool was offered; inspect the observation.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        raise SystemExit("Synthetic HTTP MCP recovery probe failed; raw output withheld") from None
