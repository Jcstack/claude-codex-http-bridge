"""Observe nested agent stop ownership using a disposable client and fake model.

The fixture owns every process, session, task, and server it creates. It never
connects to a real account or project. Environment isolation is not an OS sandbox.
"""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading

from claude_fixture import isolated_environment


MODEL = "gpt-6.1-sol"
ROOT_MARKER = "ROOT_SYNTHETIC_OWNERSHIP_FIXTURE"
PARENT_MARKER = "PARENT_SYNTHETIC_OWNERSHIP_FIXTURE"
LEAF_MARKER = "LEAF_SYNTHETIC_OWNERSHIP_FIXTURE"
PARENT_CALL, LEAF_CALL, STOP_CALL = "toolu_fixture_parent", "toolu_fixture_leaf", "toolu_fixture_stop"
LEAF_WRITE = "toolu_fixture_write"
EFFECT = "synthetic leaf effect\n"


def user_text(body: dict) -> str:
    chunks = []
    for message in body.get("messages", []):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            chunks.extend(part.get("text", "") for part in content
                          if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str))
    return "\n".join(chunks)


def tool_result(body: dict, ident: str) -> dict | None:
    for message in body.get("messages", []):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "tool_result" and part.get("tool_use_id") == ident:
                    return part
    return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        if "count_tokens" in self.path:
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"input_tokens":100}'); return
        text = user_text(body)
        role = "leaf" if LEAF_MARKER in text else "parent" if PARENT_MARKER in text else "root" if ROOT_MARKER in text else "unknown"
        with self.server.lock:
            self.server.roles[role] = self.server.roles.get(role, 0) + 1
            if role == "parent":
                offered = {item.get("name") for item in body.get("tools", []) if isinstance(item, dict)}
                self.server.parent_agent_offered |= "Agent" in offered
                self.server.parent_stop_offered |= "TaskStop" in offered
        if role == "root" and self.server.mode == "root_control":
            stopped = tool_result(body, STOP_CALL)
            created = tool_result(body, PARENT_CALL)
            if stopped is not None:
                self.observe_stop(stopped)
                self.observe_hook_context(body)
                self.server.release_leaf.set()
                self.server.leaf_response_attempted.wait(5)
                self.server.leaf_tool_result_seen.wait(1)
                block = {"type": "text", "text": "ROOT_DONE"}
            elif created is not None:
                block = self.stop_created(created, "ROOT_NO_AGENT_ID")
            else:
                block = {"type": "tool_use", "id": PARENT_CALL, "name": "Agent",
                         "input": {"description": "Synthetic leaf", "prompt": LEAF_MARKER,
                                   "subagent_type": "general-purpose", "run_in_background": True}}
        elif role == "root":
            if tool_result(body, PARENT_CALL) is None:
                block = {"type": "tool_use", "id": PARENT_CALL, "name": "Agent",
                         "input": {"description": "Synthetic parent", "prompt": PARENT_MARKER,
                                   "subagent_type": "general-purpose"}}
            else:
                block = {"type": "text", "text": "ROOT_DONE"}
        elif role == "parent":
            stopped = tool_result(body, STOP_CALL)
            created = tool_result(body, LEAF_CALL)
            if stopped is not None:
                self.observe_stop(stopped)
                self.observe_hook_context(body)
                self.server.release_leaf.set()
                self.server.leaf_tool_result_seen.wait(5)
                block = {"type": "text", "text": "PARENT_DONE"}
            elif created is not None:
                block = self.stop_created(created, "PARENT_NO_AGENT_ID")
            else:
                block = {"type": "tool_use", "id": LEAF_CALL, "name": "Agent",
                         "input": {"description": "Synthetic leaf", "prompt": LEAF_MARKER,
                                   "subagent_type": "general-purpose", "run_in_background": True}}
        elif role == "leaf":
            if tool_result(body, LEAF_WRITE) is not None:
                self.server.leaf_tool_result_seen.set()
                block = {"type": "text", "text": "LEAF_DONE"}
            else:
                self.server.leaf_started.set()
                self.server.release_leaf.wait(15)
                self.server.leaf_finished.set()
                block = {"type": "tool_use", "id": LEAF_WRITE, "name": "Write",
                         "input": {"file_path": str(self.server.effect_path), "content": EFFECT}}
        else:
            block = {"type": "text", "text": "LEAF_DONE"}
        stop = "tool_use" if block["type"] == "tool_use" else "end_turn"
        message = {"id": "msg_fixture", "type": "message", "role": "assistant", "model": MODEL,
                   "content": [block], "stop_reason": stop, "stop_sequence": None,
                   "usage": {"input_tokens": 100, "output_tokens": 5}}
        self.send_response(200)
        if not body.get("stream"):
            self.send_header("Content-Type", "application/json"); self.end_headers()
            try:
                self.wfile.write(json.dumps(message).encode())
                if role == "leaf": self.server.leaf_response_written.set()
            except OSError: pass
            finally:
                if role == "leaf": self.server.leaf_response_attempted.set()
            return
        self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        start = {**block, "text": ""} if block["type"] == "text" else {**block, "input": {}}
        delta = ({"type": "text_delta", "text": block["text"]} if block["type"] == "text" else
                 {"type": "input_json_delta", "partial_json": json.dumps(block["input"])})
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
            if role == "leaf": self.server.leaf_response_written.set()
        except OSError:
            pass
        finally:
            if role == "leaf": self.server.leaf_response_attempted.set()

    def stop_created(self, created: dict, failure: str) -> dict:
        value = created.get("content")
        output = value if isinstance(value, str) else json.dumps(value)
        match = re.search(r"agentId:\s*([A-Za-z0-9_-]{4,100})", output)
        if not match:
            self.server.release_leaf.set()
            return {"type": "text", "text": failure}
        self.server.leaf_started.wait(8)
        with self.server.lock:
            self.server.agent_id_returned = True
            self.server.synthetic_task_id = match.group(1)
            self.server.leaf_running_before_stop = self.server.leaf_started.is_set() and not self.server.leaf_finished.is_set()
        return {"type": "tool_use", "id": STOP_CALL, "name": "TaskStop", "input": {"task_id": match.group(1)}}

    def observe_stop(self, stopped: dict) -> None:
        value = stopped.get("content")
        output = value if isinstance(value, str) else json.dumps(value)
        with self.server.lock:
            self.server.stop_result_seen = True
            self.server.stop_is_error = stopped.get("is_error") is True
            self.server.stop_success_text = bool(re.search(r"successfully stopped", output, re.I))
            self.server.ownership_denial = bool(re.search(r"owned.{0,256}agent.{0,256}cannot stop", output, re.I | re.S))
            owned = output.lower().find("owned")
            denial = output.lower().find("cannot stop", owned) if owned >= 0 else -1
            self.server.ownership_mentions_target = bool(
                self.server.synthetic_task_id and owned >= 0 and denial > owned
                and self.server.synthetic_task_id in output[owned:denial])

    def observe_hook_context(self, body: dict) -> None:
        serialized = json.dumps(body, ensure_ascii=True)
        with self.server.lock:
            self.server.failure_hook_context_seen = "FIXTURE_FAILURE_HOOK_CONTEXT" in serialized
            self.server.batch_hook_context_seen = "FIXTURE_BATCH_HOOK_CONTEXT" in serialized


def run_case(claude: str, mode: str, observe_hooks: bool) -> dict:
    with tempfile.TemporaryDirectory(prefix="bridge-nested-stop-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
        root = Path(temporary).resolve(); work = root / "work"; config = root / "config"
        work.mkdir(); config.mkdir(mode=0o700)
        settings = root / "settings.json"
        config_data = {"modelPicker": {"options": [{"model": MODEL, "label": "Fixture",
            "behavesAs": "claude-sonnet-5-5"}], "replaceBuiltInOptions": True}}
        hook_marker = root / "hook-observations.jsonl"
        if observe_hooks:
            hook = root / "fixture-hook.py"
            hook.write_text('import json,re,sys\nfrom pathlib import Path\n'
                'data=json.load(sys.stdin); event=data.get("hook_event_name"); calls=data.get("tool_calls",[])\n'
                'selected=[x for x in calls if isinstance(x,dict) and x.get("tool_name")=="TaskStop"] if isinstance(calls,list) else []\n'
                'text=json.dumps(data.get("error") if event=="PostToolUseFailure" else [x.get("tool_response") for x in selected])\n'
                'matched=bool(re.search(r"owned.{0,256}agent.{0,256}cannot stop",text,re.I|re.S))\n'
                'row={"event":event if event in ("PostToolUseFailure","PostToolBatch") else "other",'
                '"taskstop":data.get("tool_name")=="TaskStop" or bool(selected),"ownership":matched}\n'
                'with Path(sys.argv[1]).open("a",encoding="utf-8") as out: out.write(json.dumps(row)+"\\n")\n'
                'if matched and row["taskstop"]:\n'
                ' marker="FIXTURE_FAILURE_HOOK_CONTEXT" if event=="PostToolUseFailure" else "FIXTURE_BATCH_HOOK_CONTEXT"\n'
                ' print(json.dumps({"hookSpecificOutput":{"hookEventName":event,"additionalContext":marker}}))\n'
                'else: print("{}")\n', encoding="utf-8")
            command = {"type": "command", "command": sys.executable, "args": [str(hook), str(hook_marker)], "timeout": 5}
            config_data["hooks"] = {"PostToolUseFailure": [{"matcher": "TaskStop", "hooks": [command]}],
                                    "PostToolBatch": [{"hooks": [command]}]}
        settings.write_text(json.dumps(config_data), encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.lock = threading.Lock(); server.roles = {}; server.mode = mode
        server.leaf_started = threading.Event(); server.leaf_finished = threading.Event(); server.release_leaf = threading.Event()
        server.leaf_response_written = threading.Event(); server.leaf_response_attempted = threading.Event()
        server.leaf_tool_result_seen = threading.Event(); server.effect_path = work / "synthetic-leaf-effect.txt"
        server.parent_agent_offered = server.parent_stop_offered = False
        server.stop_result_seen = server.stop_is_error = server.stop_success_text = server.ownership_denial = False
        server.agent_id_returned = server.leaf_running_before_stop = False
        server.synthetic_task_id = None; server.ownership_mentions_target = False
        server.failure_hook_context_seen = server.batch_hook_context_seen = False
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        env = isolated_environment(root)
        env.update(CLAUDE_CONFIG_DIR=str(config), ANTHROPIC_BASE_URL=f"http://127.0.0.1:{server.server_port}",
            ANTHROPIC_AUTH_TOKEN="fixture-only", ANTHROPIC_DEFAULT_SONNET_MODEL=MODEL,
            CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_TELEMETRY="1")
        command = [claude, "--restricted", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--settings", str(settings), "--permission-mode", "dontAsk", "--model", MODEL,
            "--tools", "Agent,TaskStop,Write", "--allowedTools", "Agent,TaskStop,Write", "--output-format", "json",
            "--max-turns", "8", "-p", ROOT_MARKER]
        try:
            completed = subprocess.run(command, cwd=work, env=env, capture_output=True, text=True, timeout=70)
            try:
                final = json.loads(completed.stdout)
                client_completed = completed.returncode == 0 and not final.get("is_error") and "ROOT_DONE" in final.get("result", "")
            except (ValueError, AttributeError, TypeError):
                client_completed = False
            hook_rows = [json.loads(line) for line in hook_marker.read_text(encoding="utf-8").splitlines()] if hook_marker.exists() else []
            report = {"synthetic_only": True, "direct_fake_messages": True, "client_completed": client_completed,
                "root_request_seen": server.roles.get("root", 0) > 0, "parent_request_seen": server.roles.get("parent", 0) > 0,
                "leaf_request_seen": server.roles.get("leaf", 0) > 0, "parent_agent_offered": server.parent_agent_offered,
                "parent_stop_offered": server.parent_stop_offered, "nested_agent_id_returned": server.agent_id_returned,
                "leaf_running_before_stop": server.leaf_running_before_stop, "stop_result_seen": server.stop_result_seen,
                "stop_reported_error": server.stop_is_error, "stop_success_text": server.stop_success_text,
                "ownership_denial_pattern": server.ownership_denial,
                "ownership_segment_mentions_target": server.ownership_mentions_target,
                "leaf_response_written_after_release": server.leaf_response_written.is_set(),
                "leaf_tool_result_seen": server.leaf_tool_result_seen.is_set(),
                "leaf_effect_created": server.effect_path.is_file() and server.effect_path.read_text(encoding="utf-8") == EFFECT,
                "hook_observation_enabled": observe_hooks,
                "failure_hook_saw_taskstop": any(row.get("event") == "PostToolUseFailure" and row.get("taskstop") for row in hook_rows),
                "batch_hook_saw_taskstop": any(row.get("event") == "PostToolBatch" and row.get("taskstop") for row in hook_rows),
                "failure_hook_saw_ownership": any(row.get("event") == "PostToolUseFailure" and row.get("taskstop") and row.get("ownership") for row in hook_rows),
                "batch_hook_saw_ownership": any(row.get("event") == "PostToolBatch" and row.get("taskstop") and row.get("ownership") for row in hook_rows),
                "failure_hook_context_seen": server.failure_hook_context_seen,
                "batch_hook_context_seen": server.batch_hook_context_seen}
            return report
        finally:
            server.release_leaf.set(); server.shutdown(); server.server_close(); thread.join(timeout=2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect-ownership-denial", action="store_true", help="Require the pinned client's observed nested denial")
    parser.add_argument("--observe-hooks", action="store_true", help="Observe synthetic failure and batch hooks")
    parser.add_argument("--expect-hook-observation", action="store_true", help="Require the pinned client's observed hook contexts")
    args = parser.parse_args()
    if args.expect_hook_observation and not args.observe_hooks:
        parser.error("--expect-hook-observation requires --observe-hooks")
    claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
    if not claude: raise RuntimeError("The locked Claude fixture is required")
    nested = run_case(claude, "nested", args.observe_hooks)
    control = run_case(claude, "root_control", args.observe_hooks)
    print(json.dumps({"synthetic_only": True, "nested": nested, "root_control": control}, sort_keys=True))
    common = ("client_completed", "root_request_seen", "leaf_request_seen", "nested_agent_id_returned",
              "leaf_running_before_stop", "stop_result_seen")
    if (not all(nested[key] and control[key] for key in common) or not nested["parent_request_seen"] or
            not nested["parent_agent_offered"] or not nested["parent_stop_offered"] or
            control["stop_reported_error"] or not control["stop_success_text"] or control["leaf_effect_created"]):
        raise RuntimeError("Synthetic nested-agent controls did not complete")
    if args.expect_ownership_denial and not (nested["stop_reported_error"] and nested["ownership_denial_pattern"]
                                             and nested["leaf_tool_result_seen"] and nested["leaf_effect_created"]):
        raise RuntimeError("The pinned client's nested stop behavior changed")
    if args.expect_hook_observation and not (nested["failure_hook_saw_ownership"] and nested["batch_hook_saw_ownership"]
            and nested["failure_hook_context_seen"] and nested["batch_hook_context_seen"]
            and control["batch_hook_saw_taskstop"] and not control["failure_hook_saw_taskstop"]
            and not control["batch_hook_saw_ownership"]):
        raise RuntimeError("The pinned client's synthetic hook behavior changed")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        raise SystemExit("Synthetic nested-agent observation failed; raw output withheld") from None
