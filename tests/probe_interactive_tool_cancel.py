"""Interrupt between two owned tool effects, then continue in the same client.

The real POSIX terminal client uses an empty home, synthetic hooks and two
owned translators. This does not load an existing user's settings or accounts.
"""

import argparse
from contextlib import ExitStack
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time

from claude_fixture import binary, isolated_environment
from codex_stream_fixture import temporary_codex_translator
from probe_native_retry_cancel import Origin, control_request
from probe_parallel_stream import Handler, TARGETS, terminate_owned_client


CONTINUE = "Continue the owned task using successful tool history."


class ToolOrigin(Handler):
    event = Origin.event
    complete = Origin.complete

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        if not 0 < length <= 2000000:
            self.send_error(400)
            return
        body = json.loads(self.rfile.read(length))
        main = any(tool.get("name") == "Write" or isinstance(tool.get("function"), dict)
                   and tool["function"].get("name") == "Write"
                   for tool in body.get("tools", []) if isinstance(tool, dict))
        if not main:
            self.complete(self.server.model, "BRIDGE_CONTROL_OK")
            return
        self.server.new_turn_present.append(CONTINUE in json.dumps(body.get("input", [])))
        self.respond(body)


HOOK = '''import json,os,pathlib,secrets,sys,urllib.request
event=json.load(sys.stdin)
data=event.get("tool_input",{})
try:
    path=os.path.normcase(str(pathlib.Path(data["file_path"]).resolve()))
except (KeyError,TypeError,ValueError,OSError):
    path=""
allowed={os.path.normcase(str(pathlib.Path(sys.argv[2],name).resolve())):label for label,name in (("A","owned-a.txt"),("B","owned-b.txt"))}
label=allowed.get(path)
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
if event.get("hook_event_name")=="PreToolUse":
    permitted=event.get("tool_name")=="Write" and label is not None and data.get("content")=="SYNTHETIC"
    if permitted:
        try:
            opener.open("http://127.0.0.1:"+sys.argv[3]+"/fixture-gate/"+label,timeout=50).close()
        except OSError:
            permitted=False
    print(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow" if permitted else "deny","permissionDecisionReason":"Owned synthetic fixture only"}}))
elif label is not None:
    marker=pathlib.Path(sys.argv[1],secrets.token_hex(16))
    with marker.open("x",encoding="utf-8") as handle:
        handle.write(label)
    try:
        opener.open("http://127.0.0.1:"+sys.argv[3]+"/fixture-effect/"+label,timeout=5).close()
    except OSError:
        pass
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("proxy_binary", type=Path)
    parser.add_argument("--origin-binary", type=Path, required=True)
    parser.add_argument("--family", choices=("sol", "astra"), default="sol")
    parser.add_argument("--interrupt-key", choices=("escape", "ctrl-c"), default="escape")
    parser.add_argument("--client-binary", type=Path,
                        help="Optional second path, required to match the locked client byte for byte")
    args = parser.parse_args()
    if os.name == "nt":
        raise RuntimeError("This fixture requires a POSIX controlling terminal")
    import fcntl
    import pty
    import select
    import struct
    import termios

    claude = binary()
    if args.client_binary is not None:
        supplied = args.client_binary.resolve(strict=True)
        with supplied.open("rb") as alternate, claude.open("rb") as locked:
            matching = hashlib.file_digest(alternate, "sha256").digest() == hashlib.file_digest(locked, "sha256").digest()
        if not matching:
            raise RuntimeError("The supplied client does not match the integrity-locked fixture")
        claude = supplied
    model, behaves = (("gpt-6.1-sol", "claude-sonnet-5-5") if args.family == "sol"
                      else ("gpt-6-astra", "claude-opus-5-5"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), ToolOrigin)
    server.model, server.mode, server.resume_interrupted = model, "ctrl_c_between_effects", True
    server.requests, server.calls, server.new_turn_present = [], {}, []
    server.effects = {label: threading.Event() for label in TARGETS}
    server.phase_ready, server.release, server.gate_returned = (threading.Event() for _ in range(3))
    server.gate_lock, server.gate_first = threading.Lock(), None
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    process = master = None
    phase, output, succeeded = "setup", bytearray(), False
    try:
        with tempfile.TemporaryDirectory(prefix="bridge-interactive-tools-") as temporary, ExitStack() as stack:
            root = Path(temporary).resolve()
            root.chmod(0o700)
            work = root / "work"; work.mkdir()
            server.work = work
            config = root / "config"; config.mkdir(mode=0o700)
            markers = root / "markers"; markers.mkdir(mode=0o700)
            (config / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True,
                "projects": {str(work): {"hasTrustDialogAccepted": True}}}), encoding="utf-8")
            hook = root / "hook.py"; hook.write_text(HOOK, encoding="utf-8")
            settings = root / "settings.json"
            settings.write_text(json.dumps({"modelPicker": {"options": [
                {"model": model, "label": "Fixture", "behavesAs": behaves}], "replaceBuiltInOptions": True},
                "hooks": {event: [{"matcher": "Write", "hooks": [{"type": "command", "command": sys.executable,
                    "args": [str(hook), str(markers), str(work), str(server.server_port)], "timeout": 45}]}]
                    for event in ("PreToolUse", "PostToolUse")}}), encoding="utf-8")
            inner, inner_key, _ = stack.enter_context(temporary_codex_translator(
                args.origin_binary.resolve(), server.server_port, managed_policy=True, model=model))
            port, key, _ = stack.enter_context(temporary_codex_translator(
                args.proxy_binary.resolve(), inner, upstream_prefix="/v1", upstream_key=inner_key,
                managed_policy=True, model=model))
            env = isolated_environment(root)
            env.update(CLAUDE_CONFIG_DIR=str(config), ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}",
                ANTHROPIC_AUTH_TOKEN=key, ANTHROPIC_DEFAULT_SONNET_MODEL=model, ANTHROPIC_DEFAULT_HAIKU_MODEL=model,
                CLAUDE_CODE_MAX_RETRIES="3", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_TELEMETRY="1",
                TERM="xterm-256color", HTTP_PROXY="http://127.0.0.1:9", HTTPS_PROXY="http://127.0.0.1:9",
                ALL_PROXY="http://127.0.0.1:9", NO_PROXY="127.0.0.1,localhost")
            command = [str(claude), "--restricted", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--settings", str(settings), "--permission-mode", "dontAsk", "--tools", "Write", "--model", model,
                "--debug-file", str(root / "debug.log")]
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 42, 140, 0, 0))
            wrapper = ("import fcntl,os,sys,termios;fcntl.ioctl(0,termios.TIOCSCTTY,0);"
                       "os.tcsetpgrp(0,os.getpgrp());os.execvpe(sys.argv[1],sys.argv[1:],os.environ)")
            try:
                process = subprocess.Popen([sys.executable, "-c", wrapper, *command], cwd=work, env=env,
                    stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
            finally:
                os.close(slave)

            def stop_client():
                nonlocal master
                server.release.set()
                try:
                    if process.poll() is None and not terminate_owned_client(process, master):
                        raise RuntimeError("The owned interactive tool client did not stop")
                finally:
                    if master is not None:
                        os.close(master)
                        master = None

            stack.callback(stop_client)

            def clean():
                return re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))", "",
                              output.decode(errors="replace"))

            def pump_until(predicate, seconds):
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("The owned interactive tool client exited before continuation")
                    if predicate():
                        return
                    if select.select([master], [], [], .1)[0]:
                        output.extend(os.read(master, 65536))
                        if len(output) > 2000000:
                            raise RuntimeError("Owned interactive tool output exceeded its bound")
                raise RuntimeError("The interactive tool fixture did not reach its boundary")

            def counts():
                labels = [path.read_text(encoding="utf-8") for path in markers.iterdir() if path.is_file()]
                return {label: labels.count(label) for label in TARGETS}

            phase = "prompt_ready"
            pump_until(lambda: "❯" in clean(), 30)
            os.write(master, b"Write SYNTHETIC to owned-a.txt and owned-b.txt once each.\r")
            phase = "between_tool_effects"
            pump_until(lambda: server.phase_ready.is_set() and server.gate_first in TARGETS
                       and server.effects[server.gate_first].is_set(), 35)
            first = server.gate_first
            second = next(label for label in TARGETS if label != first)
            assert counts() == {first: 1, second: 0}, "The second effect escaped its gate"
            assert len(server.requests) == 1, "The initial tool turn issued unexpected requests"
            assert control_request(port, key, model), "Independent control failed before tool cancellation"
            output.clear()
            os.write(master, b"\x1b" if args.interrupt_key == "escape" else b"\x03")
            phase = "input_ready_after_interrupt"
            pump_until(lambda: "❯" in clean(), 15)
            server.release.set()
            phase = "blocked_gate_settled"
            pump_until(server.gate_returned.is_set, 5)
            assert counts() == {first: 1, second: 0}, "Cancellation allowed a blocked effect"
            assert len(server.requests) == 1, "Cancellation issued another inference request"
            assert control_request(port, key, model), "Tool cancellation disrupted independent work"
            output.clear()
            os.write(master, b"\x15" + CONTINUE.encode() + b"\r")
            phase = "same_process_continuation"
            pump_until(lambda: "DONE" in clean(), 45)
            assert len(server.requests) == 3, "Continuation used unexpected inference attempts"
            assert server.new_turn_present == [False, True, True], "Continuation was not the new user turn"
            resumed = server.requests[1]
            assert resumed["effects_before_request"] == [first], "An effect escaped before the continuation request"
            assert resumed["successful_results"] == [first], "Continuation lost a successful tool result"
            assert counts() == {"A": 1, "B": 1}, "Continuation lost or repeated a tool effect"
            assert server.requests[-1]["successful_results"] == ["A", "B"], "Continuation lost final tool results"
            assert sorted(path.name for path in work.iterdir()) == sorted(TARGETS.values()), "Unexpected workspace effects"
            assert all((work / name).read_text(encoding="utf-8") == "SYNTHETIC" for name in TARGETS.values()), "Tool content changed"
            result = {"synthetic_only": True, "two_layers": True, "family_mapping": args.family,
                "interrupt_key": args.interrupt_key, "same_process_continued": process.poll() is None,
                "cancelled_before_second_effect": True, "successful_tool_history_retained": True,
                "effects_once_each": True, "independent_requests_completed": True,
                "byte_identical_alternate_client_path": args.client_binary is not None}
            phase = "cleanup"
        print(json.dumps(result))
        succeeded = True
    finally:
        if not succeeded:
            print(json.dumps({"synthetic_only": True, "failed_phase": phase,
                "main_requests": len(server.requests), "new_turn_checks": server.new_turn_present,
                "tool_history": server.requests,
                "process_exited": process.poll() is not None if process is not None else None}))
        server.release.set()
        if process is not None and process.poll() is None and not terminate_owned_client(process, master):
            raise RuntimeError("The owned interactive tool client did not stop")
        if master is not None:
            os.close(master)
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, AssertionError, subprocess.SubprocessError):
        raise SystemExit("Owned interactive tool cancellation failed; raw details withheld") from None
