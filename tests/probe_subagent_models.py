"""Exercise Claude subagent routing against a synthetic local Messages server.

The harness, agent inputs and responses are disposable fixtures. No credential,
user transcript or model account is used.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge_models  # noqa: E402
import launcher  # noqa: E402


PARENT_MODEL = "gpt-6.1-sol"
DEFAULT_CHILD_MODEL = "gpt-6.1-terra"
ALIAS_CHILD_MODEL = "gpt-6-astra"


class FixtureServer(ThreadingHTTPServer):
    requests: list[dict]
    invoked: bool
    alias: str | None
    agent_type: str
    lock: threading.Lock


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        pass

    def do_GET(self) -> None:
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        if "count_tokens" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"input_tokens":1000}')
            return
        with self.server.lock:
            self.server.requests.append({"model": body.get("model"),
                                         "agent_header": bool(self.headers.get("x-claude-code-agent-id"))})
            first = not self.server.invoked
            self.server.invoked = True
        if first:
            agent_input = {"description": "Synthetic check", "prompt": "Respond with OK.",
                           "subagent_type": self.server.agent_type}
            if self.server.alias:
                agent_input["model"] = self.server.alias
            block = {"type": "tool_use", "id": "toolu_fixture", "name": "Agent", "input": agent_input}
            stop = "tool_use"
        else:
            block = {"type": "text", "text": "OK"}
            stop = "end_turn"
        message = {"id": "msg_fixture", "type": "message", "role": "assistant", "model": body.get("model"),
                   "content": [block], "stop_reason": stop, "stop_sequence": None,
                   "usage": {"input_tokens": 1000, "output_tokens": 20}}
        self.send_response(200)
        if not body.get("stream"):
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(message).encode())
            return
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        start = {**block, "text": ""} if block["type"] == "text" else {**block, "input": {}}
        delta = ({"type": "text_delta", "text": block["text"]} if block["type"] == "text" else
                 {"type": "input_json_delta", "partial_json": json.dumps(block["input"])})
        events = [
            {"type": "message_start", "message": {**message, "content": [], "stop_reason": None}},
            {"type": "content_block_start", "index": 0, "content_block": start},
            {"type": "content_block_delta", "index": 0, "delta": delta},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 20}},
            {"type": "message_stop"},
        ]
        for event in events:
            self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
            self.wfile.flush()


def run_case(claude: str, *, alias: str | None, force: bool = False, custom_agent: bool = False) -> dict:
    with tempfile.TemporaryDirectory(prefix="bridge-subagent-fixture-") as temporary:
        root = Path(temporary)
        work = root / "work"
        work.mkdir()
        config = root / "config"
        config.mkdir(mode=0o700)
        settings = root / "settings.json"
        models = (PARENT_MODEL, DEFAULT_CHILD_MODEL, ALIAS_CHILD_MODEL)
        settings.write_text(json.dumps({"modelPicker": {"options": [
            {"model": model, "label": "Synthetic fixture", "behavesAs": bridge_models.behaves_as(model)}
            for model in models], "replaceBuiltInOptions": True},
            "modelSettings": {model: {"autoCompactWindow": 256000} for model in models}}), encoding="utf-8")
        server = FixtureServer(("127.0.0.1", 0), Handler)
        server.requests = []
        server.invoked = False
        server.alias = alias
        server.agent_type = "fixture-worker" if custom_agent else "general-purpose"
        server.lock = threading.Lock()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT", "COMSPEC",
               "PATHEXT", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "TEMP", "TMP") if key in os.environ}
        env.update({"CLAUDE_CONFIG_DIR": str(config), "ANTHROPIC_AUTH_TOKEN": "fixture-only",
                    "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                    "ANTHROPIC_DEFAULT_OPUS_MODEL": ALIAS_CHILD_MODEL,
                    "ANTHROPIC_DEFAULT_SONNET_MODEL": ALIAS_CHILD_MODEL,
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL": PARENT_MODEL,
                    "CLAUDE_CODE_SUBAGENT_MODEL": DEFAULT_CHILD_MODEL,
                    "CLAUDE_CODE_ATTRIBUTION_HEADER": "0", "CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1",
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1"})
        if force:
            env["CLAUDE_CODE_SUBAGENT_MODEL_FORCE"] = "1"
        with patch.dict(os.environ, env, clear=True):
            env = launcher.claude_environment(server.server_port, "fixture-only", PARENT_MODEL,
                available_models={PARENT_MODEL, DEFAULT_CHILD_MODEL, ALIAS_CHILD_MODEL})
        try:
            agent_args = ["--agents", json.dumps({"fixture-worker": {"description": "Synthetic check",
                "prompt": "Respond with OK.", "model": ALIAS_CHILD_MODEL}})] if custom_agent else []
            result = subprocess.run([claude, "--restricted", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--permission-mode", "dontAsk", "--output-format", "json", "--model", PARENT_MODEL,
                "--settings", str(settings), *agent_args,
                "--tools", "Agent", "--allowedTools", "Agent", "--max-turns", "5", "-p", "Run the synthetic agent."],
                cwd=work, env=env, capture_output=True, text=True, timeout=60)
            if result.returncode:
                raise RuntimeError("Isolated Claude subagent fixture exited unsuccessfully")
            final = json.loads(result.stdout)
            if final.get("is_error"):
                raise RuntimeError("Isolated Claude subagent fixture reported an error")
            child_models = [request["model"] for request in server.requests if request["agent_header"]]
            return {"parent_model_retained": bool(server.requests) and server.requests[0]["model"] == PARENT_MODEL,
                    "child_models": child_models}
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def main() -> int:
    claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
    if not claude:
        raise RuntimeError("Install Claude Code before running the subagent fixture")
    default = run_case(claude, alias=None)
    alias = run_case(claude, alias="opus")
    same_family = run_case(claude, alias="sonnet")
    forced = run_case(claude, alias="opus", force=True)
    custom = run_case(claude, alias=None, custom_agent=True)
    result = {"parent_model_retained": all(case["parent_model_retained"] for case in (default, alias, same_family, forced, custom)),
              "subagent_default_model": default["child_models"] == [DEFAULT_CHILD_MODEL],
              "other_family_alias_takes_precedence": alias["child_models"] == [ALIAS_CHILD_MODEL],
              "same_family_alias_keeps_parent_model": same_family["child_models"] == [PARENT_MODEL],
              "agent_definition_full_model_id": custom["child_models"] == [ALIAS_CHILD_MODEL],
              "forced_subagent_model": forced["child_models"] == [DEFAULT_CHILD_MODEL]}
    print(json.dumps(result, sort_keys=True))
    return 0 if all(result.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
