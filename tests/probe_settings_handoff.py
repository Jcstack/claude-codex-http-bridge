"""Measure settings reload between two owned fake gateways, including TLS.

No real account, user config, external gateway or installed service is used.
This is a client-version-specific contract probe, not a live-session migration.
"""

import json
import os
from pathlib import Path
import queue
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from claude_fixture import isolated_environment


MODEL = "gpt-6.1-sol"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        if self.headers.get("Authorization") != "Bearer " + self.server.token:
            self.server.bad_auth += 1
            self.send_response(401)
            self.end_headers()
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        if "count_tokens" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"input_tokens":1000}')
            return
        self.server.requests.append(body)
        message = {"id": "fixture_" + str(len(self.server.requests)), "type": "message", "role": "assistant",
                   "model": body.get("model"), "content": [], "stop_reason": None,
                   "stop_sequence": None, "usage": {"input_tokens": 1000, "output_tokens": 10}}
        events = [
            {"type": "message_start", "message": message},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "FIXTURE"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 10}},
            {"type": "message_stop"},
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for index, event in enumerate(events):
                self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
                if self.server.pause_first and len(self.server.requests) == 1 and index == 2:
                    self.server.admitted.set()
                    if not self.server.release.wait(30):
                        return
        except OSError:
            pass


def write_settings(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    os.replace(temporary, path)


def run_case(claude, ingress, mode):
    with tempfile.TemporaryDirectory(prefix="bridge-settings-handoff-") as temporary:
        root = Path(temporary)
        work = root / "work"
        config = root / "config"
        tls = root / "tls"
        for directory in (work, config, tls):
            directory.mkdir(mode=0o700)
        if os.name == "nt":
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            import windows_acl
            windows_acl.lock([tls])
        environment = isolated_environment(root)
        subprocess.run([str(ingress), "--root", str(tls), "--init-tls"], env=environment,
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        servers = [ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(tls / "server.pem", tls / "server-key.pem")
        servers[1].socket = context.wrap_socket(servers[1].socket, server_side=True)
        threads = []
        for index, server in enumerate(servers):
            server.requests = []
            server.token = "synthetic-gateway-token-" + str(index)
            server.bad_auth = 0
            server.pause_first = index == 0 and mode == "in_flight"
            server.admitted = threading.Event()
            server.release = threading.Event()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            threads.append(thread)
        process = None
        try:
            # The generated CA must not already be trusted by the OS.
            try:
                with socket.create_connection(("127.0.0.1", servers[1].server_port), timeout=5) as raw:
                    with ssl.create_default_context().wrap_socket(raw, server_hostname="127.0.0.1"):
                        raise AssertionError("Synthetic CA was unexpectedly trusted")
            except ssl.SSLCertVerificationError:
                pass
            urls = ["http://127.0.0.1:" + str(servers[0].server_port),
                    "https://127.0.0.1:" + str(servers[1].server_port)]
            user_settings = config / "settings.json"
            launch_settings = root / "launch.json"
            marker, changed = root / "prompt-marker", root / "change-marker"
            hook = root / "hook.py"
            hook.write_text("import pathlib,sys\npathlib.Path(sys.argv[1]).write_text(sys.argv[2])\n", encoding="utf-8")

            def hook_settings(label):
                def call(path, value):
                    return {"type": "command", "command": sys.executable,
                            "args": [str(hook), str(path), value], "timeout": 10}
                return {"hooks": {"UserPromptSubmit": [{"hooks": [call(marker, label)]}],
                                  "ConfigChange": [{"matcher": "user_settings", "hooks": [call(changed, "changed")]}]}}

            picker = {"modelPicker": {"options": [{"model": MODEL, "label": "Fixture",
                       "behavesAs": "claude-sonnet-5-5"}], "replaceBuiltInOptions": True}}
            initial_route = {"ANTHROPIC_BASE_URL": urls[0], "ANTHROPIC_AUTH_TOKEN": servers[0].token}
            new_route = {"ANTHROPIC_BASE_URL": urls[1], "ANTHROPIC_AUTH_TOKEN": servers[1].token,
                         "NODE_TLS_REJECT_UNAUTHORIZED": "1"}
            if mode != "missing_ca":
                new_route["NODE_EXTRA_CA_CERTS"] = str(tls / "ca.pem")
            launch = dict(picker)
            if mode in ("launch_priority", "launch_file_edit"):
                launch["env"] = initial_route
            write_settings(launch_settings, launch)
            write_settings(user_settings, hook_settings("A"))
            environment.update(CLAUDE_CONFIG_DIR=str(config), **initial_route,
                ANTHROPIC_DEFAULT_SONNET_MODEL=MODEL, ANTHROPIC_DEFAULT_HAIKU_MODEL=MODEL,
                CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_TELEMETRY="1",
                CLAUDE_CODE_ATTRIBUTION_HEADER="0", API_TIMEOUT_MS="5000", NO_PROXY="127.0.0.1,localhost")
            command = [claude, "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                       "--settings", str(launch_settings), "--model", MODEL, "--tools", "",
                       "--permission-mode", "dontAsk", "--input-format", "stream-json",
                       "--output-format", "stream-json", "--verbose", "-p"]
            if mode == "restricted":
                command.insert(1, "--restricted")
            process = subprocess.Popen(command, cwd=work, env=environment, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
            events = queue.Queue()

            def reader():
                for line in process.stdout:
                    try:
                        events.put(json.loads(line))
                    except ValueError:
                        pass
                events.put({"type": "fixture_eof"})

            reader_thread = threading.Thread(target=reader, daemon=True)
            reader_thread.start()

            def send(text):
                process.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
                process.stdin.flush()

            def result(timeout=20):
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        event = events.get(timeout=max(0, deadline - time.monotonic()))
                    except queue.Empty:
                        return False
                    if event.get("type") == "fixture_eof":
                        return False
                    if event.get("type") == "result":
                        return event.get("is_error") is False

            send("Synthetic first turn")
            if mode == "in_flight":
                assert servers[0].admitted.wait(20), "First synthetic stream did not reach its barrier"
            else:
                assert result(), "First synthetic turn did not finish"
            if mode == "launch_file_edit":
                write_settings(launch_settings, {**picker, "env": new_route})
                time.sleep(3)
            else:
                write_settings(user_settings, {**hook_settings("B"), "env": new_route})
                if mode == "restricted":
                    time.sleep(3)
                else:
                    deadline = time.monotonic() + 12
                    while not changed.exists() and time.monotonic() < deadline:
                        time.sleep(0.1)
                    assert changed.exists(), "Synthetic user settings change was not observed"
            if mode == "in_flight":
                servers[0].release.set()
                assert result(), "Synthetic in-flight turn failed after settings changed"
            before = [len(server.requests) for server in servers]
            send("Synthetic second turn")
            succeeded = result(timeout=15)
            delta = [len(server.requests) - before[index] for index, server in enumerate(servers)]
            assert not any(server.bad_auth for server in servers), "A synthetic gateway received the wrong credential"
            assert process.poll() is None, "Synthetic client unexpectedly exited"
            if mode in ("user_env", "in_flight"):
                assert succeeded and delta == [0, 1], "Changed user settings did not route the next turn to TLS"
                assert any("Synthetic first turn" in json.dumps(row.get("messages")) for row in servers[1].requests), \
                    "Synthetic conversation history was not retained"
                assert marker.read_text() == "B", "Changed user hook did not run"
            elif mode == "missing_ca":
                assert not succeeded and delta == [0, 0], "Untrusted synthetic TLS gateway unexpectedly succeeded"
            else:
                assert succeeded and delta == [1, 0], "Higher-priority or restricted setting unexpectedly changed route"
                if mode == "launch_priority":
                    assert marker.read_text() == "B", "User setting change was not otherwise applied"
                if mode == "restricted":
                    assert not marker.exists(), "Restricted mode unexpectedly ran the user hook"
            return {"case": mode, "same_client_alive": True, "next_turn_succeeded": succeeded,
                    "original_requests": delta[0], "new_tls_requests": delta[1],
                    "new_gateway_received_wrong_auth": False}
        finally:
            servers[0].release.set()
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            for server in servers:
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=2)


def main():
    if len(sys.argv) != 2:
        raise RuntimeError("Specify the private fixture bundle")
    claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
    if not claude:
        raise RuntimeError("The locked Claude client is required")
    ingress = Path(sys.argv[1]) / ("bridge-ingress.exe" if os.name == "nt" else "bridge-ingress")
    cases = ("user_env", "in_flight", "missing_ca", "launch_priority", "launch_file_edit", "restricted")
    results = []
    for case in cases:
        try:
            results.append(run_case(claude, ingress, case))
        except (OSError, ValueError, RuntimeError, AssertionError, subprocess.SubprocessError):
            print("Failed synthetic case: " + case, file=sys.stderr)
            raise
    print(json.dumps({"mock_only": True, "cases": results}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, AssertionError, subprocess.SubprocessError):
        raise SystemExit("Synthetic settings handoff probe failed; raw output withheld") from None
