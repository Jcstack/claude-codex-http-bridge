"""Check native retry cancellation and server advice using only owned processes.

POSIX cancellation uses a controlling terminal's Ctrl-C. Windows uses explicit
termination of the disposable child and does not claim console-key coverage.
No model account, existing service, user workspace, or credential file is used.
"""

import argparse
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request

from claude_fixture import binary, isolated_environment
from codex_stream_fixture import temporary_codex_translator
from probe_parallel_stream import terminal_client, terminate_owned_client


RECOVERED = "BRIDGE_RETRY_RECOVERED"
CONTROL = "BRIDGE_CONTROL_OK"


class Origin(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def event(self, value):
        self.wfile.write(("data: " + json.dumps(value) + "\n\n").encode())
        self.wfile.flush()

    def complete(self, model, text):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        item = {"type": "message", "id": "fixture-message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}]}
        for value in (
            {"type": "response.created", "response": {"id": "fixture", "model": model}},
            {"type": "response.output_item.added", "output_index": 0, "item": {**item, "content": []}},
            {"type": "response.content_part.added", "output_index": 0, "content_index": 0,
             "part": {"type": "output_text", "text": ""}},
            {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": text},
            {"type": "response.content_part.done", "output_index": 0, "content_index": 0, "part": item["content"][0]},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": {"id": "fixture", "model": model, "status": "completed",
             "output": [item], "usage": {"input_tokens": 1000, "output_tokens": 4}}},
        ):
            self.event(value)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        main = any(tool.get("name") == "Write" or isinstance(tool.get("function"), dict) and
                   tool["function"].get("name") == "Write" for tool in body.get("tools", []) if isinstance(tool, dict))
        try:
            if not main:
                self.complete(body.get("model"), CONTROL)
                return
            self.server.semantics.append(json.dumps({key: body.get(key) for key in
                ("model", "instructions", "input", "tools", "tool_choice", "reasoning", "prompt_cache_key")}, sort_keys=True))
            count = len(self.server.semantics)
            if count == 1:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.event({"type": "response.created", "response": {"id": "fixture", "model": body.get("model")}})
                item = {"id": "fixture-reasoning", "type": "reasoning", "summary": []}
                self.event({"type": "response.output_item.added", "output_index": 0, "item": item})
                self.event({"type": "response.output_item.done", "output_index": 0,
                            "item": {**item, "encrypted_content": "A" * 8501}})
                self.server.first_sent.set()
                if self.server.mode == "active":
                    while not self.server.release.wait(.1):
                        self.wfile.write(b": synthetic transport control\n\n")
                        self.wfile.flush()
                else:
                    self.connection.shutdown(socket.SHUT_WR)
                return
            if count == 2:
                self.send_response(self.server.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Retry-After", str(self.server.delay))
                self.send_header("x-should-retry", "false" if self.server.mode == "veto" else "true")
                self.end_headers()
                error = {"type": "server_error", "message": "Synthetic retry wait"}
                if self.server.status == 429:
                    error.update(type="rate_limit_error", code="turn_rate_limit_exceeded")
                self.wfile.write(json.dumps({"error": error}).encode())
                self.wfile.flush()
                self.server.second_at = time.monotonic()
                self.server.second_sent.set()
                return
            self.server.third_at = time.monotonic()
            self.complete(body.get("model"), RECOVERED)
        except OSError:
            if self.server.mode == "active":
                self.server.origin_closed.set()
        finally:
            self.close_connection = True


def wait_for_native_error(process, debug, status=None):
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        if debug.exists():
            text = debug.read_text(encoding="utf-8", errors="replace")[-500000:]
            matched = (any("stream started" in line.lower() for line in text.splitlines()) if status is None else
                       any("api error" in line.lower() and str(status) in line for line in text.splitlines()))
            if matched:
                return
        if process.poll() is not None:
            break
        threading.Event().wait(.05)
    raise RuntimeError("The isolated client did not reach the native error boundary")


def control_request(port, key, model):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    payload = {"model": model, "stream": False, "max_tokens": 64,
               "messages": [{"role": "user", "content": "Owned independent control"}]}
    request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/messages", data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with opener.open(request, timeout=10) as response:
        value = json.loads(response.read(65536))
    return value.get("stop_reason") == "end_turn" and any(
        isinstance(block, dict) and block.get("text") == CONTROL for block in value.get("content", []))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("proxy_binary", type=Path)
    parser.add_argument("--origin-binary", type=Path, required=True)
    parser.add_argument("--mode", choices=("active", "backoff", "advice", "veto"), required=True)
    parser.add_argument("--status", type=int, choices=(429, 503), default=429)
    parser.add_argument("--family", choices=("sol", "astra"), default="sol")
    parser.add_argument("--expect-veto-loss", action="store_true")
    args = parser.parse_args()
    if args.expect_veto_loss and args.mode != "veto":
        parser.error("--expect-veto-loss requires --mode veto")
    model, behaves = ("gpt-6.1-sol", "claude-sonnet-5-5") if args.family == "sol" else ("gpt-6-astra", "claude-opus-5-5")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    server.mode = args.mode
    server.status = args.status
    server.delay = 15 if args.mode == "backoff" else 3
    server.semantics = []
    server.first_sent = threading.Event()
    server.second_sent = threading.Event()
    server.origin_closed = threading.Event()
    server.release = threading.Event()
    server.second_at = server.third_at = None
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    process = terminal = None
    try:
        with tempfile.TemporaryDirectory(prefix="bridge-native-retry-cancel-") as temporary, ExitStack() as stack:
            root = Path(temporary)
            root.chmod(0o700)
            work = root / "work"; work.mkdir()
            config = root / "config"; config.mkdir(mode=0o700)
            settings = root / "settings.json"
            settings.write_text(json.dumps({"modelPicker": {"options": [{"model": model, "label": "Fixture", "behavesAs": behaves}],
                "replaceBuiltInOptions": True}}), encoding="utf-8")
            inner, inner_key, _ = stack.enter_context(temporary_codex_translator(
                args.origin_binary.resolve(), server.server_port, managed_policy=True, model=model))
            port, key, _ = stack.enter_context(temporary_codex_translator(
                args.proxy_binary.resolve(), inner, upstream_prefix="/v1", upstream_key=inner_key,
                managed_policy=True, model=model))
            env = isolated_environment(root)
            env.update(CLAUDE_CONFIG_DIR=str(config), ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}",
                ANTHROPIC_AUTH_TOKEN=key, ANTHROPIC_DEFAULT_SONNET_MODEL=model, ANTHROPIC_DEFAULT_HAIKU_MODEL=model,
                CLAUDE_CODE_MAX_RETRIES="3", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_TELEMETRY="1")
            debug = root / "debug.log"
            command = [str(binary()), "--restricted", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--settings", str(settings), "--permission-mode", "dontAsk", "--tools", "Write", "--model", model,
                "--debug-file", str(debug), "--output-format", "json", "--max-turns", "3", "-p", "Return the synthetic response text."]
            cancel = args.mode in ("active", "backoff")
            if cancel and os.name != "nt":
                process, terminal = terminal_client(command, work, env)
            else:
                process = subprocess.Popen(command, cwd=work, env=env, stdin=subprocess.DEVNULL,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                if cancel:
                    boundary = server.first_sent if args.mode == "active" else server.second_sent
                    assert boundary.wait(35), "The synthetic cancellation boundary was not reached"
                    wait_for_native_error(process, debug, args.status if args.mode == "backoff" else None)
                    assert control_request(port, key, model), "Independent control failed before cancellation"
                    assert process.poll() is None, "The client exited before cancellation"
                    if terminal is not None:
                        os.write(terminal, b"\x03")
                    else:
                        process.terminate()
                    process.communicate(timeout=10)
                    if args.mode == "active":
                        assert server.origin_closed.wait(5), "Client cancellation did not close the owned origin stream"
                    assert len(server.semantics) == (1 if args.mode == "active" else 2), "Cancelled work issued another inference request"
                    assert control_request(port, key, model), "Cancellation disrupted another request"
                    succeeded = None
                else:
                    stdout, stderr = process.communicate(timeout=70)
                    if len(stdout) > 2000000 or len(stderr) > 2000000:
                        raise RuntimeError("Synthetic client output exceeded the fixture bound")
                    final = json.loads(stdout)
                    succeeded = process.returncode == 0 and final.get("is_error") is False
                    if args.mode == "veto" and not args.expect_veto_loss:
                        assert not succeeded and len(server.semantics) == 2, "The client repeated a vetoed request"
                    else:
                        assert succeeded and len(server.semantics) == 3, "Native recovery did not complete"
                        assert str(final.get("result", "")).strip() == RECOVERED, "Native recovery lost the synthetic result"
                    if args.mode == "advice":
                        assert server.third_at is not None and server.second_at is not None and server.third_at - server.second_at >= server.delay - .05, "The native client retried before the server wait elapsed"
                assert all(row == server.semantics[0] for row in server.semantics[1:]), "Native retries changed the requested semantics"
                assert not list(work.iterdir()), "The cancellation fixture produced a workspace effect"
                print(json.dumps({"synthetic_only": True, "two_layers": True, "mode": args.mode,
                    "family_mapping": args.family, "upstream_refusal_status": args.status,
                    "main_attempts": len(server.semantics), "reported_success": succeeded,
                    "interrupt_method": ("terminal_ctrl_c" if terminal is not None else "owned_child_termination") if cancel else None,
                    "origin_connection_closed": server.origin_closed.is_set() if args.mode == "active" else None,
                    "independent_requests_completed": True if cancel else None,
                    "server_wait_honored": True if args.mode == "advice" else None,
                    "veto_honored": not args.expect_veto_loss if args.mode == "veto" else None,
                    "request_semantics_preserved": True, "workspace_effects_absent": True}))
            finally:
                if process.poll() is None and not terminate_owned_client(process, terminal):
                    raise RuntimeError("Owned native client did not stop")
                if terminal is not None:
                    os.close(terminal)
                    terminal = None
                server.release.set()
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, AssertionError, subprocess.TimeoutExpired):
        raise SystemExit("Synthetic native cancellation/advice gate failed; raw details withheld") from None
