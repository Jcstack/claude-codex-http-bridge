"""Verify a disposable macOS Claude evaluation boundary without model access.

Uses an owned fake Messages service, copied client and synthetic files. No
account, user project, feedback tool or external model request is used.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import tempfile
import threading

from claude_fixture import isolated_environment


def policy(root: Path, port: int, home: Path) -> str:
    if (type(port) is not int or not 1 <= port <= 65535 or any(
            not value.is_absolute() or '"' in str(value) or "\\" in str(value) or
            any(ord(char) < 32 for char in str(value)) for value in (root, home))):
        raise ValueError("Unsafe fixture path")
    return f'''(version 1)
(allow default)
(deny file-read-data (subpath "/Users"))
(deny file-read-data (subpath "{home}"))
(deny file-read-data (subpath "/Volumes"))
(deny file-read-data (subpath "/private/tmp"))
(deny file-read-data (subpath "/private/var/tmp"))
(deny file-read-data (subpath "/private/var/root"))
(deny file-read-data (subpath "/private/var/folders"))
(allow file-read-data (subpath "{root}"))
(deny file-write*)
(allow file-write* (subpath "{root}"))
(deny network*)
(allow network-outbound (remote ip "localhost:{port}"))
'''


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        if not 0 < length <= 32 << 20:
            self.send_response(413); self.end_headers(); return
        raw = self.rfile.read(length)
        body = json.loads(raw)
        if "count_tokens" in self.path:
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"input_tokens":1000}'); return
        for message in body.get("messages", []):
            parts = message.get("content") if isinstance(message, dict) else None
            for part in parts if isinstance(parts, list) else []:
                if not isinstance(part, dict) or part.get("type") != "tool_result":
                    continue
                ident = part.get("tool_use_id")
                if ident in ("outside_read", "outside_edit", "inside_read", "inside_edit"):
                    self.server.results[ident] = part.get("is_error") is True
                    if ident == "outside_read":
                        self.server.outside_leaked |= "OUTSIDE_PRIVATE_FIXTURE" in json.dumps(part.get("content"))
        step = self.server.step
        self.server.step += 1
        if step == 0:
            name, ident, args = "Read", "outside_read", {"file_path": str(self.server.outside)}
        elif step == 1:
            name, ident, args = "Edit", "outside_edit", {"file_path": str(self.server.outside),
                "old_string": "OUTSIDE_PRIVATE_FIXTURE", "new_string": "changed"}
        elif step == 2:
            name, ident, args = "Read", "inside_read", {"file_path": str(self.server.inside)}
        elif step == 3:
            name, ident, args = "Edit", "inside_edit", {"file_path": str(self.server.inside),
                "old_string": "inside-original", "new_string": "inside-updated"}
        else:
            name, ident, args = None, None, None
        block = ({"type": "tool_use", "id": ident, "name": name, "input": args} if name else
                 {"type": "text", "text": "DONE"})
        stop = "tool_use" if name else "end_turn"
        message = {"id": f"fixture_{step}", "type": "message", "role": "assistant",
                   "model": body.get("model"), "content": [], "stop_reason": None, "stop_sequence": None,
                   "usage": {"input_tokens": 1000, "output_tokens": 20}}
        initial = {**block, "input": {}} if name else {**block, "text": ""}
        delta = ({"type": "input_json_delta", "partial_json": json.dumps(args)} if name else
                 {"type": "text_delta", "text": "DONE"})
        events = [{"type": "message_start", "message": message},
                  {"type": "content_block_start", "index": 0, "content_block": initial},
                  {"type": "content_block_delta", "index": 0, "delta": delta},
                  {"type": "content_block_stop", "index": 0},
                  {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 20}},
                  {"type": "message_stop"}]
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        try:
            for event in events:
                self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
        except OSError:
            pass


def main():
    if platform.system() != "Darwin":
        print(json.dumps({"synthetic_only": True, "skipped": "macOS Seatbelt fixture"})); return
    claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
    if not claude or not Path("/usr/bin/sandbox-exec").is_file():
        raise RuntimeError("The locked client and macOS sandbox are required")
    with tempfile.TemporaryDirectory(prefix="bridge-eval-boundary-") as temporary, \
         tempfile.TemporaryDirectory(prefix="bridge-eval-outside-") as other, \
         tempfile.TemporaryDirectory(prefix=".bridge-eval-home-", dir=Path.home()) as home_temp:
        root, outside_dir = Path(temporary).resolve(), Path(other).resolve()
        home, home_fixture = Path.home().resolve(), Path(home_temp).resolve() / "fixture.txt"
        home_fixture.write_text("synthetic-home-boundary", encoding="utf-8")
        work, config = root / "work", root / "config"
        work.mkdir(); config.mkdir(mode=0o700)
        inside, outside = work / "inside.txt", outside_dir / "outside.txt"
        inside.write_text("inside-original", encoding="utf-8")
        outside.write_text("OUTSIDE_PRIVATE_FIXTURE", encoding="utf-8")
        link = root / "outside-link"
        link.symlink_to(outside)
        client = root / "claude"
        shutil.copy2(Path(claude).resolve(), client); client.chmod(0o700)
        settings = root / "settings.json"
        settings.write_text(json.dumps({"modelPicker": {"options": [
            {"model": "gpt-6.1-sol", "label": "Fixture", "behavesAs": "claude-sonnet-5-5"}],
            "replaceBuiltInOptions": True}, "permissions": {"allow": ["Read(./**)", "Edit(./**)"],
            "blockReadsOutsideWorkingDirectories": True}}), encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.inside, server.outside, server.step = inside, outside, 0
        server.outside_leaked, server.results = False, {}
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        blocked = socket.socket(); blocked.bind(("127.0.0.1", 0)); blocked.listen()
        env = isolated_environment(root)
        env.update(CLAUDE_CONFIG_DIR=str(config), ANTHROPIC_BASE_URL=f"http://127.0.0.1:{server.server_port}",
                   ANTHROPIC_AUTH_TOKEN="fixture-only", ANTHROPIC_DEFAULT_SONNET_MODEL="gpt-6.1-sol",
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_TELEMETRY="1", ENABLE_TOOL_SEARCH="false",
                   TMPDIR=str(root), CLAUDE_CODE_TMPDIR=str(root))
        try:
            # A separate local port must remain inaccessible even though the fake
            # Messages endpoint is permitted. No provider request is made.
            preflight = ("import json,socket,sys; s=socket.socket(); s.settimeout(1); "
                         "result=s.connect_ex(('127.0.0.1',int(sys.argv[1]))); "
                         "s.close(); print(json.dumps({'other_port_connected': result == 0}))")
            network_check = subprocess.run(["/usr/bin/sandbox-exec", "-p", policy(root, server.server_port, home),
                                            "/usr/bin/python3", "-c", preflight, str(blocked.getsockname()[1])],
                                           cwd=work, env=env, capture_output=True, text=True, timeout=10)
            try:
                network_observation = json.loads(network_check.stdout)
                if not isinstance(network_observation, dict) or type(network_observation.get("other_port_connected")) is not bool:
                    network_observation = None
                else:
                    network_observation = {"other_port_connected": network_observation["other_port_connected"]}
            except ValueError:
                network_observation = None
            denied = (network_check.returncode == 0 and network_observation is not None and
                      network_observation["other_port_connected"] is False)
            file_preflight = '''import json,sys
from pathlib import Path
def readable(path):
    try: Path(path).read_text(encoding="utf-8"); return True
    except OSError: return False
def writable(path):
    try: Path(path).write_text("synthetic",encoding="utf-8"); return True
    except OSError: return False
print(json.dumps([readable(path) for path in sys.argv[1:5]] + [writable(sys.argv[5])]))
'''
            file_check = subprocess.run(["/usr/bin/sandbox-exec", "-p", policy(root, server.server_port, home),
                                         "/usr/bin/python3", "-c", file_preflight, str(inside), str(outside),
                                         str(home_fixture), str(link), str(home_fixture.parent / "outside-created.txt")],
                                        cwd=work, env=env, capture_output=True, text=True, timeout=10)
            try:
                file_observation = json.loads(file_check.stdout)
                if not (isinstance(file_observation, list) and len(file_observation) == 5 and
                        all(type(value) is bool for value in file_observation)):
                    file_observation = None
            except ValueError:
                file_observation = None
            file_boundary = file_check.returncode == 0 and file_observation == [True, False, False, False, False]
            command = ["/usr/bin/sandbox-exec", "-p", policy(root, server.server_port, home), str(client),
                       "--restricted", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                       "--settings", str(settings), "--model", "gpt-6.1-sol", "--tools", "Read,Edit",
                       "--permission-mode", "dontAsk", "--max-turns", "8", "--output-format", "json",
                       "-p", "Perform the synthetic tool-boundary fixture."]
            completed = subprocess.run(command, cwd=work, env=env, capture_output=True, text=True, timeout=60)
            try:
                answer = json.loads(completed.stdout)
                success = completed.returncode == 0 and answer.get("is_error") is False
            except (ValueError, AttributeError):
                success = False
            checks = {"client_completed": success, "other_port_blocked": denied,
                      "file_read_boundary": file_boundary,
                      "outside_read_blocked": server.results.get("outside_read") is True,
                      "outside_edit_blocked": server.results.get("outside_edit") is True,
                      "outside_data_not_returned": not server.outside_leaked,
                      "outside_unchanged": outside.read_text(encoding="utf-8") == "OUTSIDE_PRIVATE_FIXTURE",
                      "inside_read_allowed": server.results.get("inside_read") is False,
                      "inside_edit_allowed": server.results.get("inside_edit") is False,
                      "inside_edited": inside.read_text(encoding="utf-8") == "inside-updated"}
            diagnostic_text = (completed.stdout + "\n" + completed.stderr).lower()
            categories = [name for name, needle in (("permissions", "permission"), ("eacces", "eacces"),
                ("authentication", "authentication"), ("network", "network"), ("settings", "settings"),
                ("sandbox", "sandbox"), ("tls", "certificate"), ("api_error", "api error")) if needle in diagnostic_text]
            preflight_error_text = (network_check.stderr + "\n" + file_check.stderr).lower()
            preflight_categories = [name for name, needle in (("xcrun", "xcrun"),
                ("python", "python"), ("permission", "permission"), ("sandbox", "sandbox"),
                ("developer_tools", "developer tools")) if needle in preflight_error_text]
            print(json.dumps({"synthetic_only": True, "checks": checks, "client_exit_code": completed.returncode,
                              "model_requests": server.step,
                              "preflight_exit_codes": {"network": network_check.returncode, "files": file_check.returncode},
                              "preflight_observations": {"network": network_observation, "files": file_observation},
                              "preflight_error_categories": preflight_categories,
                              "client_error_categories": categories if not all(checks.values()) else []}, sort_keys=True))
            if not all(checks.values()):
                raise RuntimeError("Synthetic evaluation boundary failed")
        finally:
            blocked.close(); server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        raise SystemExit("Synthetic model evaluation boundary failed; raw output withheld") from None
