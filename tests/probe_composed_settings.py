"""Exercise the real launcher and locked client with only owned fake endpoints.

The service, account, model, credentials, hooks and working directory are fake.
The fixture does not contact a provider or use the user's Claude configuration.
Environment isolation is not an operating-system sandbox.
"""

from contextlib import redirect_stderr
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from unittest.mock import patch

from claude_fixture import isolated_environment

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import launcher  # noqa: E402
import runtime_bundle  # noqa: E402


MODEL = "gpt-6.1-sol"
TOKEN = "fixture-local-token"
CALL = "toolu_fixture_denied"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        token = self.headers.get("Authorization") == "Bearer " + TOKEN or self.headers.get("x-api-key") == TOKEN
        if "count_tokens" in self.path:
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"input_tokens":100}'); return
        denied = None
        for item in body.get("messages", []):
            content = item.get("content", []) if isinstance(item, dict) else []
            if isinstance(content, list):
                denied = next((part for part in content if isinstance(part, dict)
                               and part.get("type") == "tool_result" and part.get("tool_use_id") == CALL), denied)
        with self.server.lock:
            self.server.messages += 1
            self.server.received_fake_key |= token
            self.server.denied_seen |= denied is not None
            self.server.denied_error |= isinstance(denied, dict) and denied.get("is_error") is True
        block = ({"type": "tool_use", "id": CALL, "name": "Bash",
                  "input": {"command": "echo fixture > denied-effect.txt"}} if denied is None
                 else {"type": "text", "text": "DONE"})
        stop = "tool_use" if denied is None else "end_turn"
        message = {"id": "msg_fixture", "type": "message", "role": "assistant", "model": MODEL,
                   "content": [block], "stop_reason": stop, "stop_sequence": None,
                   "usage": {"input_tokens": 100, "output_tokens": 5}}
        self.send_response(200)
        if not body.get("stream"):
            self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps(message).encode()); return
        self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        start = {**block, "input": {}} if denied is None else {**block, "text": ""}
        delta = ({"type": "input_json_delta", "partial_json": json.dumps(block["input"])} if denied is None
                 else {"type": "text_delta", "text": "DONE"})
        events = [{"type": "message_start", "message": {**message, "content": [], "stop_reason": None}},
                  {"type": "content_block_start", "index": 0, "content_block": start},
                  {"type": "content_block_delta", "index": 0, "delta": delta},
                  {"type": "content_block_stop", "index": 0},
                  {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 5}},
                  {"type": "message_stop"}]
        try:
            for event in events:
                self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
        except OSError:
            pass


class Captured(Exception):
    pass


class FakeProcess:
    def __init__(self, *args, **kwargs):
        captured["command"] = args[0]
        captured["env"] = kwargs["env"]

    def wait(self, **_kwargs):
        return 0

    def poll(self):
        return 0


captured = {}


def capture_exec(_path, command, env):
    captured["command"] = command
    captured["env"] = env
    raise Captured()


def run(claude, *, bare=False):
    with tempfile.TemporaryDirectory(prefix="bridge-composed-settings-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
        root = Path(temporary).resolve(); work = root / "work"; config = root / "config"; runtime = root / "runtime"
        for directory in (work, config, runtime):
            runtime_bundle.private_dir(directory)
        session = runtime / "session-fixture"; runtime_bundle.private_dir(session)
        launcher.private_json(session / "key", {"unused": True})
        (session / "key").write_text(TOKEN, encoding="utf-8")
        servers = [ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
        threads = []
        for server in servers:
            server.lock = threading.Lock(); server.messages = 0; server.received_fake_key = False
            server.denied_seen = server.denied_error = False
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start(); threads.append(thread)
        urls = [f"http://127.0.0.1:{server.server_port}" for server in servers]
        hook = root / "hook.py"
        hook.write_text("import pathlib,sys\npathlib.Path(sys.argv[1]).write_text('fixture')\n", encoding="utf-8")
        def hook_entry(name):
            return {"hooks": [{"type": "command", "command": sys.executable,
                               "args": [str(hook), str(root / name)], "timeout": 10}]}
        base = root / "base.json"
        launcher.private_json(base, {"modelPicker": {"options": [{"model": MODEL, "label": "Fixture",
            "behavesAs": "claude-sonnet-5-5"}], "replaceBuiltInOptions": True},
            "modelSettings": {MODEL: {"autoCompactWindow": 400000}},
            "hooks": {"UserPromptSubmit": [hook_entry("bridge-hook")]}})
        (config / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": urls[1]},
            "hooks": {"UserPromptSubmit": [hook_entry("global-hook")]}}), encoding="utf-8")
        custom = root / "custom.json"
        custom.write_text(json.dumps({"permissions": {"deny": ["Bash"]},
            "hooks": {"UserPromptSubmit": [hook_entry("custom-hook")]},
            "modelSettings": {MODEL: {"autoCompactWindow": 300000, "effortLevel": "high"}},
            "env": {"FIXTURE_THEME": "blue"}}), encoding="utf-8")
        env = isolated_environment(root)
        env.update(CLAUDE_CONFIG_DIR=str(config), ANTHROPIC_BASE_URL=urls[1], ANTHROPIC_AUTH_TOKEN=TOKEN,
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_TELEMETRY="1")
        state = {"port": servers[0].server_port, "session": "session-fixture"}
        prepared = "\t".join([MODEL, "500000", "400000", str(base), json.dumps({MODEL: 500000})])
        arguments = ["--settings", str(custom), "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                     "--tools", "Bash", "--allowedTools", "Bash", "--permission-mode", "dontAsk",
                     "--output-format", "json", "--max-turns", "4", "-p", "Synthetic fixture."]
        if bare:
            arguments.insert(2, "--bare")
        captured.clear()
        try:
            with patch.dict(os.environ, env, clear=True), patch.object(launcher, "ROOT", root), \
                    patch.object(launcher, "ensure_service", return_value=(runtime, state, False)), \
                    patch.object(launcher, "service_environment", return_value={}), \
                    patch.object(launcher, "helper_output", return_value=prepared), redirect_stderr(StringIO()):
                if os.name == "nt":
                    real_popen = subprocess.Popen
                    def windows_popen(*args, **kwargs):
                        if args and isinstance(args[0], list) and args[0] and args[0][0] == claude:
                            return FakeProcess(*args, **kwargs)
                        return real_popen(*args, **kwargs)
                    with patch.object(launcher.subprocess, "Popen", windows_popen):
                        launcher.run_session("claude", MODEL, arguments, claude, route="api", experimental_settings=True)
                else:
                    with patch.object(launcher.os, "execvpe", capture_exec):
                        try:
                            launcher.run_session("claude", MODEL, arguments, claude, route="api", experimental_settings=True)
                        except Captured:
                            pass
            command, client_env = captured["command"], captured["env"]
            if command.count("--settings") != 1:
                raise RuntimeError("Launcher did not pass exactly one settings file")
            settings = json.loads(Path(command[command.index("--settings") + 1]).read_text())
            structure = (settings["env"]["ANTHROPIC_BASE_URL"] == urls[0]
                         and settings["env"]["ANTHROPIC_AUTH_TOKEN"] == TOKEN
                         and "ANTHROPIC_AUTH_TOKEN" not in client_env
                         and settings["env"]["FIXTURE_THEME"] == "blue"
                         and settings["modelSettings"][MODEL] == {"autoCompactWindow": 300000, "effortLevel": "high"}
                         and len(settings["hooks"]["UserPromptSubmit"]) == 2)
            process = subprocess.run(command, env=client_env, cwd=work, capture_output=True, text=True, timeout=60)
            try:
                response = json.loads(process.stdout)
                completed = process.returncode == 0 and not response.get("is_error") and "DONE" in response.get("result", "")
            except (ValueError, AttributeError, TypeError):
                completed = False
            result = {"client_completed": completed, "one_composed_file": True, "settings_structure_valid": structure,
                      "first_received_fake_key": servers[0].received_fake_key,
                      "second_endpoint_messages": servers[1].messages,
                      "denied_result_seen": servers[0].denied_seen, "denied_result_is_error": servers[0].denied_error,
                      "denied_effect_created": (work / "denied-effect.txt").exists(),
                      "all_three_hooks_ran": all((root / name).exists() for name in ("bridge-hook", "custom-hook", "global-hook")),
                      "any_hooks_ran": any((root / name).exists() for name in ("bridge-hook", "custom-hook", "global-hook")),
                      "bare": bare}
            if not (completed and structure and result["first_received_fake_key"] and not result["second_endpoint_messages"]
                    and result["denied_result_seen"] and result["denied_result_is_error"]
                    and not result["denied_effect_created"]
                    and (not result["any_hooks_ran"] if bare else result["all_three_hooks_ran"])):
                raise RuntimeError("Synthetic composed-settings contract failed")
            # A malformed launch file must not expose the synthetic token to a
            # lower-priority destination if the client ignores that file.
            configured = Path(command[command.index("--settings") + 1])
            configured.write_text('{"invalid":', encoding="utf-8")
            servers[0].received_fake_key = servers[1].received_fake_key = False
            subprocess.run(command, env=client_env, cwd=work, capture_output=True, text=True, timeout=60)
            result["invalid_settings_leaked_fake_key"] = servers[0].received_fake_key or servers[1].received_fake_key
            if result["invalid_settings_leaked_fake_key"]:
                raise RuntimeError("Invalid synthetic settings exposed the synthetic token")
            semantic_cases = [
                {**settings, "permissions": {"deny": "not-a-list"}},
                {**settings, "modelSettings": {MODEL: {"autoCompactWindow": 1}}},
                {**settings, "modelPicker": {"options": "not-a-list"}},
            ]
            for value in semantic_cases:
                configured.write_text(json.dumps(value), encoding="utf-8")
                servers[1].received_fake_key = False
                subprocess.run(command, env=client_env, cwd=work, capture_output=True, text=True, timeout=60)
                if servers[1].received_fake_key:
                    raise RuntimeError("Malformed synthetic settings redirected the synthetic token")
            result["semantic_negative_cases"] = len(semantic_cases)
            return result
        finally:
            for server in servers: server.shutdown(); server.server_close()
            for thread in threads: thread.join(timeout=2)


if __name__ == "__main__":
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--bare", action="store_true", help="Exercise Claude Code's synthetic bare mode")
        args = parser.parse_args()
        executable = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
        if not executable:
            raise RuntimeError("The locked Claude fixture is required")
        print(json.dumps({"synthetic_only": True, "result": run(executable, bare=args.bare)}, sort_keys=True))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        raise SystemExit("Synthetic composed-settings contract failed; raw output withheld") from None
