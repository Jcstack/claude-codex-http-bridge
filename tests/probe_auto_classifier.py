"""Exercise the native client-side auto classifier with owned synthetic actions.

No model account, existing service, user settings or user workspace is used.
The fake reviewer returns both permission outcomes; it does not claim model
safety accuracy or an Anthropic server-side safeguard entitlement.
"""

import argparse
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from unittest.mock import patch

from claude_fixture import binary, isolated_environment

sys.path.insert(0, os.environ.get("CLAUDE_CODEX_TEST_LAUNCHER_ROOT") or str(Path(__file__).resolve().parents[1]))
import launcher
from codex_stream_fixture import temporary_codex_translator


MAIN, REVIEW = "gpt-6-astra", "gpt-6.1-sol"
DENIAL = "OWNED_CLASSIFIER_DENIAL"
DONE = "OWNED_AUTO_CLASSIFIER_DONE"


class VerificationFailure(RuntimeError):
    """A fixed fixture assertion; never includes native output or request data."""


def verify(condition, reason):
    if not condition:
        raise VerificationFailure(reason)


class Origin(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def event(self, value, *, responses):
        prefix = "" if responses else "event: " + value["type"] + "\n"
        self.wfile.write((prefix + "data: " + json.dumps(value) + "\n\n").encode())
        self.wfile.flush()

    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        if not 0 < length <= 2000000:
            self.send_error(400)
            return
        body = json.loads(self.rfile.read(length))
        if "count_tokens" in self.path:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"input_tokens":1000}')
            return
        responses = self.server.responses
        model = body.get("model")
        if model not in (MAIN, REVIEW):
            self.send_error(400)
            return
        self.server.models.append(model)
        if len(self.server.models) > 16:
            self.send_error(500)
            return
        if not responses:
            self.server.server_safeguards_requested |= "safeguards" in body
        results = ([item for item in body.get("input", [])
                    if isinstance(item, dict) and item.get("type") == "function_call_output"] if responses else
                   [part for item in body.get("messages", []) if isinstance(item, dict)
                    and isinstance(item.get("content"), list) for part in item["content"]
                    if isinstance(part, dict) and part.get("type") == "tool_result"])
        if model == MAIN and results:
            self.server.tool_result_seen = True
            self.server.tool_result_error |= any(part.get("is_error") is True for part in results)
            self.server.denial_reason_seen |= DENIAL in json.dumps(results)
            text = json.dumps(results).lower()
            self.server.tool_error_hints.update(label for label, needles in (
                ("unknown_tool", ("no such tool", "tool is not available", "unknown tool")),
                ("missing", ("not found", "no such file", "enoent", "does not exist")),
                ("permission", ("permission", "denied", "not allowed")),
                ("validation", ("invalid", "required", "validation")),
                ("shell", ("bash", "shell", "powershell")),
                ("syntax", ("syntax", "parse", "unterminated")),
                ("process", ("spawn", "exit code", "exit status")),
            ) if any(needle in text for needle in needles))
        if model == REVIEW:
            text = ("<block>no</block>" if self.server.verdict == "allow" else
                    f"<category>Owned fixture policy</category><reason>{DENIAL}</reason><block>yes</block>")
            tool = False
        else:
            tool, text = not results, DONE
        try:
            if responses:
                self.send_responses(body, model, tool, text)
            else:
                self.send_messages(body, model, tool, text)
        except OSError:
            pass

    def send_messages(self, body, model, tool, text):
        block = ({"type": "tool_use", "id": "owned-command", "name": "Bash",
                  "input": {"command": self.server.command, "timeout": 10000,
                            "description": "Append one owned synthetic marker"}} if tool else
                 {"type": "text", "text": text})
        stop = "tool_use" if tool else "end_turn"
        message = {"id": "owned-message", "type": "message", "role": "assistant", "model": model,
            "content": [block], "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 1000, "output_tokens": 8}}
        self.send_response(200)
        if not body.get("stream"):
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(message).encode())
            return
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        start = {**block, "input": {}} if tool else {**block, "text": ""}
        delta = ({"type": "input_json_delta", "partial_json": json.dumps(block["input"])} if tool else
                 {"type": "text_delta", "text": text})
        for event in (
            {"type": "message_start", "message": {**message, "content": [], "stop_reason": None}},
            {"type": "content_block_start", "index": 0, "content_block": start},
            {"type": "content_block_delta", "index": 0, "delta": delta},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 8}},
            {"type": "message_stop"},
        ):
            self.event(event, responses=False)

    def send_responses(self, body, model, tool, text):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        response = {"id": "owned-response", "model": model, "status": "in_progress"}
        self.event({"type": "response.created", "response": response}, responses=True)
        if tool:
            arguments = json.dumps({"command": self.server.command, "timeout": 10000,
                                    "description": "Append one owned synthetic marker"})
            item = {"id": "owned-tool", "type": "function_call", "call_id": "owned-command",
                    "name": "Bash", "arguments": arguments, "status": "completed"}
            events = [
                {"type": "response.output_item.added", "output_index": 0,
                 "item": {**item, "arguments": "", "status": "in_progress"}},
                {"type": "response.function_call_arguments.delta", "output_index": 0,
                 "item_id": item["id"], "delta": arguments},
                {"type": "response.function_call_arguments.done", "output_index": 0,
                 "item_id": item["id"], "arguments": arguments},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
            ]
        else:
            part = {"type": "output_text", "text": text, "annotations": []}
            item = {"id": "owned-text", "type": "message", "role": "assistant", "status": "completed", "content": [part]}
            events = [
                {"type": "response.output_item.added", "output_index": 0, "item": {**item, "content": []}},
                {"type": "response.content_part.added", "output_index": 0, "content_index": 0,
                 "part": {"type": "output_text", "text": ""}},
                {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": text},
                {"type": "response.output_text.done", "output_index": 0, "content_index": 0, "text": text},
                {"type": "response.content_part.done", "output_index": 0, "content_index": 0, "part": part},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
            ]
        for event in events:
            self.event(event, responses=True)
        self.event({"type": "response.completed", "response": {**response, "status": "completed", "output": [item],
                   "usage": {"input_tokens": 1000, "output_tokens": 8}}}, responses=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verdict", choices=("allow", "deny"), required=True)
    parser.add_argument("--route", choices=("subscription", "api"), default="subscription")
    parser.add_argument("--deny-rule", action="store_true")
    parser.add_argument("--proxy-binary", type=Path)
    parser.add_argument("--origin-binary", type=Path)
    args = parser.parse_args()
    if args.origin_binary and not args.proxy_binary:
        parser.error("--origin-binary requires --proxy-binary")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    server.models, server.verdict, server.responses = [], args.verdict, bool(args.proxy_binary)
    server.tool_result_seen = server.tool_result_error = server.denial_reason_seen = False
    server.tool_error_hints = set()
    server.server_safeguards_requested = False
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with tempfile.TemporaryDirectory(prefix="bridge-auto-classifier-") as temporary, ExitStack() as stack:
            root = Path(temporary).resolve();root.chmod(0o700)
            work = root / "work";work.mkdir()
            config = root / "config";config.mkdir(mode=0o700)
            marker = work / "owned.txt"
            script = f'from pathlib import Path; p=Path({marker.as_posix()!r}); p.open("a").write("SYNTHETIC\\n")'
            server.command = shlex.join([Path(sys.executable).as_posix(), "-I", "-S", "-c", script])
            settings = root / "settings.json"
            value = {"modelPicker": {"options": [
                {"model": MAIN, "label": "Fixture", "behavesAs": "claude-opus-5-5"},
                {"model": REVIEW, "label": "Review fixture", "behavesAs": "claude-sonnet-5-5"}],
                "replaceBuiltInOptions": True}}
            if args.deny_rule:
                value["permissions"] = {"deny": ["Bash"]}
            settings.write_text(json.dumps(value), encoding="utf-8")
            port, key = server.server_port, "fixture-only"
            if args.origin_binary:
                port, key, _ = stack.enter_context(temporary_codex_translator(args.origin_binary.resolve(),
                    port, managed_policy=True, model=MAIN, additional_models=(REVIEW,)))
            if args.proxy_binary:
                port, key, _ = stack.enter_context(temporary_codex_translator(args.proxy_binary.resolve(),
                    port, upstream_prefix="/v1" if args.origin_binary else "",
                    upstream_key=key if args.origin_binary else "fixture-upstream",
                    managed_policy=True, model=MAIN, additional_models=(REVIEW,)))
            environment = isolated_environment(root)
            environment.update(CLAUDE_CONFIG_DIR=str(config), ANTHROPIC_DEFAULT_SONNET_MODEL=REVIEW,
                ANTHROPIC_DEFAULT_OPUS_MODEL=MAIN, ANTHROPIC_DEFAULT_HAIKU_MODEL=REVIEW,
                CLAUDE_CODE_AUTO_MODE_MODEL=REVIEW, DISABLE_TELEMETRY="1",
                CLAUDE_CODE_MAX_RETRIES="2",
                HTTP_PROXY="http://127.0.0.1:9", HTTPS_PROXY="http://127.0.0.1:9",
                ALL_PROXY="http://127.0.0.1:9", NO_PROXY="127.0.0.1,localhost")
            with patch.dict(os.environ, environment, clear=True):
                environment = launcher.claude_environment(port, key, MAIN, args.route, available_models={MAIN, REVIEW})
            verify(environment["CLAUDE_CODE_AUTO_MODE_SERVER"] == "0", "The route did not select native client review")
            if os.name == "nt" and shutil.which("git"):
                git = Path(shutil.which("git")).resolve()
                candidates = [parent / "bin/bash.exe" for parent in git.parents]
                bash = next((path for path in candidates if path.is_file()), None)
                verify(bash is not None, "The owned Windows action requires Git Bash")
                environment["CLAUDE_CODE_GIT_BASH_PATH"] = str(bash)
            command = [str(binary()), "--restricted", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--settings", str(settings), "--permission-mode", "auto", "--tools", "Bash", "--model", MAIN,
                "--max-turns", "3", "--output-format", "json", "-p", "Run the owned synthetic action once, then report its result."]
            run = subprocess.run(command, cwd=work, env=environment, stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, timeout=90)
            reviews = server.models.count(REVIEW)
            effects = marker.read_text(encoding="utf-8").splitlines() if marker.exists() else []
            print(json.dumps({"synthetic_only": True, "native_exit_code": run.returncode,
                "classifier_requests": reviews, "tool_result_seen": server.tool_result_seen,
                "tool_result_error": server.tool_result_error, "tool_error_hints": sorted(server.tool_error_hints),
                "owned_effect_count": len(effects)}), flush=True)
            verify(run.returncode == 0, "The native auto-mode client exited with an error")
            try:
                result = json.loads(run.stdout)
            except ValueError:
                raise VerificationFailure("The native auto-mode client returned no JSON result") from None
            verify(result.get("is_error") is False, "The native auto-mode client did not complete")
            verify(str(result.get("result", "")).strip() == DONE, "The native auto-mode result changed")
            verify("this session isn't eligible" not in run.stderr.lower(), "The incompatible server-review notice remained")
            denied = args.verdict == "deny" or args.deny_rule
            verify(effects == ([] if denied else ["SYNTHETIC"]), "Auto classification lost, duplicated or permitted a denied effect")
            verify(server.tool_result_seen, "The native harness lost its tool result")
            if not args.deny_rule:
                verify(1 <= reviews <= 3, "The classifier was skipped or repeatedly failed to parse")
                if args.verdict == "deny":
                    verify(server.denial_reason_seen, "The native denial did not preserve the classifier's verdict")
            verify(sorted(p.name for p in work.iterdir()) == ([] if denied else ["owned.txt"]), "Unexpected workspace effects")
            verify(not server.server_safeguards_requested, "Native server safeguards were requested in the direct control")
            print(json.dumps({"synthetic_only": True, "route_configuration": args.route, "through_codex": bool(args.proxy_binary),
                "two_layers": bool(args.origin_binary), "classifier_verdict": args.verdict, "explicit_deny_rule": args.deny_rule,
                "classifier_requests": reviews, "effects": len(effects), "native_auto_mode_completed": True,
                "tool_result_retained": True, "classifier_denial_reason_retained": server.denial_reason_seen if args.verdict == "deny" and not args.deny_rule else None,
                "server_review_notice_absent": True, "server_safeguard_results_fabricated": False}))
    finally:
        server.shutdown();server.server_close();worker.join(timeout=2)


if __name__ == "__main__":
    try:
        main()
    except VerificationFailure as error:
        raise SystemExit("Owned auto-classifier verification failed: " + str(error)) from None
    except (OSError, RuntimeError, ValueError, AssertionError, subprocess.SubprocessError):
        raise SystemExit("Owned auto-classifier verification failed; raw details withheld") from None
