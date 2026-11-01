"""Credential-free parallel-tool interruption gate using a real Claude client.

An owned Responses origin drives the reviewed translator. Independent hook
markers count every successful effect; no user project or model account is used.
"""

import argparse
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import threading

from live_support import temporary_translator


MODEL = "gpt-6.1-sol"
TARGETS = {"A": "owned-a.txt", "B": "owned-b.txt"}


def terminal_client(command, work, env):
    """Own a POSIX controlling terminal so its Ctrl-C reaches only this fixture."""
    import pty
    master, slave = pty.openpty()
    wrapper = ("import fcntl,os,sys,termios;"
               "fcntl.ioctl(0,termios.TIOCSCTTY,0);"
               "os.tcsetpgrp(0,os.getpgrp());"
               "os.execvpe(sys.argv[1],sys.argv[1:],os.environ)")
    try:
        process = subprocess.Popen([sys.executable, "-c", wrapper, *command], cwd=work, env=env,
            stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    return process, master


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        prefix = "/fixture-effect/"
        label = self.path.removeprefix(prefix) if self.path.startswith(prefix) else ""
        if label not in TARGETS:
            self.send_response(404)
        else:
            self.server.effects[label].set()
            self.send_response(204)
        self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        outputs = [item for item in body.get("input", [])
                   if isinstance(item, dict) and item.get("type") == "function_call_output"]
        observed = {self.server.calls.get(item.get("call_id")) for item in outputs}
        observed &= set(TARGETS)
        self.server.requests.append({"acknowledged": sorted(observed)})
        count = len(self.server.requests)
        first = count == 1
        missing = [name for name in TARGETS if name not in observed]
        response = {"id": f"response_{count}", "model": MODEL, "status": "in_progress"}
        events = [{"type": "response.created", "response": response}]
        if missing:
            items = []
            for index, label in enumerate(missing):
                call_id = f"call_{label}_{count}"
                self.server.calls[call_id] = label
                args = json.dumps({"file_path": (self.server.work / TARGETS[label]).as_posix(), "content": "SYNTHETIC"})
                item = {"id": f"item_{label}_{count}", "type": "function_call", "call_id": call_id,
                        "name": "Write", "arguments": args, "status": "completed"}
                items.append(item)
                events.append({"type": "response.output_item.added", "output_index": index,
                               "item": {**item, "arguments": "", "status": "in_progress"}})
            # Interleave argument events so both calls are open before either is done.
            for index, item in enumerate(items):
                events.append({"type": "response.function_call_arguments.delta", "output_index": index,
                               "item_id": item["id"], "delta": item["arguments"]})
            for index, item in enumerate(items):
                events.append({"type": "response.function_call_arguments.done", "output_index": index,
                               "item_id": item["id"], "arguments": item["arguments"]})
            for index, item in enumerate(items):
                events.append({"type": "response.output_item.done", "output_index": index, "item": item})
            output = items
        else:
            item = {"id": f"message_{count}", "type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": "DONE", "annotations": []}]}
            events.extend([
                {"type": "response.output_item.added", "output_index": 0,
                 "item": {**item, "content": [], "status": "in_progress"}},
                {"type": "response.content_part.added", "output_index": 0, "content_index": 0,
                 "part": {"type": "output_text", "text": ""}},
                {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "DONE"},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
            ])
            output = [item]
        events.append({"type": "response.completed", "response": {**response, "status": "completed", "output": output,
                      "usage": {"input_tokens": 1000, "output_tokens": 10}}})

        # For the first response exactly two function calls are interleaved.
        cuts = {"healthy": 10, "eof_before_first": 7, "eof_after_first": 8,
                "eof_after_both": 9, "error_after_first": 8, "failed_after_both": 9,
                "error_after_completed": 10, "cancel_before_items": 7, "cancel_after_both": 9,
                "ctrl_c_before_items": 7, "ctrl_c_after_first_item": 8, "ctrl_c_after_both": 9}
        cutoff = cuts[self.server.mode] if first else len(events)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            if self.server.mode in ("cancel_after_both", "ctrl_c_after_both") and not first:
                self.server.phase_ready.set()
                self.server.release.wait(30)
                return
            for event in events[:cutoff]:
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
            if first and self.server.mode in ("cancel_before_items", "ctrl_c_before_items", "ctrl_c_after_first_item"):
                self.server.phase_ready.set()
                self.server.release.wait(30)
            elif first and self.server.mode.startswith("eof_"):
                self.connection.shutdown(socket.SHUT_WR)
            elif first and self.server.mode not in ("healthy",):
                err = {"type": "server_error", "code": "request_timeout", "message": "Synthetic parallel-stream failure"}
                failure = ({"type": "response.failed", "response": {"status": "failed", "error": err}}
                           if self.server.mode.startswith("failed_") else {"type": "error", "error": err})
                self.wfile.write(("data: " + json.dumps(failure) + "\n\n").encode())
                self.wfile.flush()
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("proxy_binary", type=Path)
    args = parser.parse_args()
    claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
    if not claude:
        raise RuntimeError("The locked Claude client is required")
    modes = ("healthy", "eof_before_first", "eof_after_first", "eof_after_both",
             "error_after_first", "failed_after_both", "error_after_completed",
             "cancel_before_items", "cancel_after_both")
    if os.name != "nt":
        modes += ("ctrl_c_before_items", "ctrl_c_after_first_item", "ctrl_c_after_both")
    metrics = []
    for mode in modes:
        with tempfile.TemporaryDirectory(prefix="bridge-parallel-stream-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
            root = Path(temporary).resolve()
            root.chmod(0o700)
            work = root / "work"
            work.mkdir()
            config = root / "config"
            config.mkdir(mode=0o700)
            markers = root / "markers"
            markers.mkdir(mode=0o700)
            hook = root / "hook.py"
            hook.write_text('''import json,os,pathlib,secrets,sys,urllib.request
event=json.load(sys.stdin)
data=event.get("tool_input",{})
try:
    path=os.path.normcase(str(pathlib.Path(data["file_path"]).resolve()))
except (KeyError,TypeError,ValueError,OSError):
    path=""
allowed={os.path.normcase(str(pathlib.Path(sys.argv[2],name).resolve())):label for label,name in (("A","owned-a.txt"),("B","owned-b.txt"))}
label=allowed.get(path)
if event.get("hook_event_name")=="PreToolUse":
    permitted=event.get("tool_name")=="Write" and label is not None and data.get("content")=="SYNTHETIC"
    print(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow" if permitted else "deny","permissionDecisionReason":"Owned synthetic fixture only"}}))
elif label is not None:
    marker=pathlib.Path(sys.argv[1],secrets.token_hex(16))
    with marker.open("x",encoding="utf-8") as handle:
        handle.write(label)
    try:
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        opener.open("http://127.0.0.1:"+sys.argv[3]+"/fixture-effect/"+label,timeout=5).close()
    except OSError:
        pass
''', encoding="utf-8")
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server.requests = []
            server.calls = {}
            server.mode = mode
            server.work = work
            server.effects = {label: threading.Event() for label in TARGETS}
            server.phase_ready = threading.Event()
            server.release = threading.Event()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            settings = root / "settings.json"
            settings.write_text(json.dumps({"modelPicker": {"options": [
                {"model": MODEL, "label": "Fixture", "behavesAs": "claude-sonnet-5-5"}], "replaceBuiltInOptions": True},
                "hooks": {event: [{"matcher": "Write", "hooks": [{"type": "command", "command": sys.executable,
                    "args": [str(hook), str(markers), str(work), str(server.server_port)], "timeout": 7}]}]
                    for event in ("PreToolUse", "PostToolUse")}}), encoding="utf-8")
            allowed = ("PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT", "COMSPEC", "PATHEXT",
                       "LOCALAPPDATA", "APPDATA", "USERPROFILE", "TEMP", "TMP")
            env = {key: os.environ[key] for key in allowed if key in os.environ}
            env.update(CLAUDE_CONFIG_DIR=str(config), CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_TELEMETRY="1")
            spec = {"context_window": 500000, "supports_search_tool": True, "input_modalities": ["text"],
                    "supported_reasoning_levels": [{"effort": level} for level in ("low", "medium", "high", "xhigh", "max")]}
            upstream = {"state": {"port": server.server_port}, "key": "synthetic-local-upstream", "models": {MODEL: spec}}
            stack = ExitStack()
            process = None
            terminal_master = None
            try:
                port, key = stack.enter_context(temporary_translator(args.proxy_binary.resolve(), upstream, MODEL))
                env.update(ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}", ANTHROPIC_AUTH_TOKEN=key,
                    ANTHROPIC_DEFAULT_SONNET_MODEL=MODEL, ANTHROPIC_DEFAULT_HAIKU_MODEL=MODEL)
                command = [claude, "--restricted", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                    "--settings", str(settings), "--permission-mode", "dontAsk", "--output-format", "json",
                    "--model", MODEL, "--tools", "Write", "--add-dir", work.as_posix(), "--max-turns", "6",
                    "-p", "Write SYNTHETIC to owned-a.txt and owned-b.txt once each."]
                terminal_interrupt = mode.startswith("ctrl_c_")
                terminal_isig = None
                cancelled = mode.startswith("cancel_") or terminal_interrupt
                if cancelled:
                    if terminal_interrupt:
                        process, terminal_master = terminal_client(command, work, env)
                    else:
                        process = subprocess.Popen(command, cwd=work, env=env, stdin=subprocess.DEVNULL,
                                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    if mode.endswith("after_both"):
                        assert all(server.effects[label].wait(20) for label in TARGETS), "Parallel effects did not finish"
                    assert server.phase_ready.wait(20), "Cancellation boundary was not reached"
                    assert process.poll() is None, "Client exited before the cancellation boundary"
                    if terminal_interrupt:
                        import termios
                        attributes = termios.tcgetattr(terminal_master)
                        terminal_isig = bool(attributes[3] & termios.ISIG)
                        interrupt_byte = attributes[6][termios.VINTR]
                        if terminal_isig and interrupt_byte not in (b"\x03", 3):
                            raise RuntimeError("Fixture terminal has a different interrupt character")
                        os.write(terminal_master, b"\x03")
                    else:
                        process.terminate()
                    process.communicate(timeout=10)
                    server.release.set()
                    final = None
                    returncode = process.returncode
                else:
                    result = subprocess.run(command, cwd=work, env=env, capture_output=True, text=True, timeout=60)
                    final = json.loads(result.stdout)
                    returncode = result.returncode
                labels = [path.read_text(encoding="utf-8") for path in markers.iterdir() if path.is_file()]
                counts = {label: labels.count(label) for label in TARGETS}
                acknowledged = set().union(*(set(row["acknowledged"]) for row in server.requests))
                succeeded = None if cancelled else returncode == 0 and final.get("is_error") is False
                try:
                    assert server.requests, "Client never reached the synthetic endpoint"
                    assert all(count <= 1 for count in counts.values()), "A parallel tool effect was duplicated"
                    if mode == "healthy":
                        assert succeeded, "Healthy parallel fixture did not complete successfully"
                    if mode.endswith("before_items"):
                        assert counts == {"A": 0, "B": 0}, "An effect ran after cancellation before tool completion"
                        assert not any((work / name).exists() for name in TARGETS), "Cancellation created a target"
                    if mode == "ctrl_c_after_first_item":
                        assert counts["B"] == 0 and not (work / TARGETS["B"]).exists(), "An incomplete tool ran after Ctrl-C"
                        if counts["A"] == 1:
                            assert (work / TARGETS["A"]).read_text(encoding="utf-8") == "SYNTHETIC", "First effect was corrupted"
                    if mode.endswith("after_both") and cancelled:
                        assert counts == {"A": 1, "B": 1}, "Cancellation lost or repeated a completed effect"
                        assert acknowledged == set(TARGETS), "Cancellation follow-up omitted a completed tool result"
                    if succeeded is True:
                        assert all(counts[label] == 1 and (work / name).read_text(encoding="utf-8") == "SYNTHETIC"
                                   for label, name in TARGETS.items()), "Client reported success without both effects"
                        assert acknowledged == set(TARGETS), "Continuation omitted a completed tool result"
                    elif not cancelled:
                        assert final.get("is_error") is True or returncode != 0, "Client termination was ambiguous"
                except (AssertionError, OSError) as error:
                    print(json.dumps({"failed_case": mode, "reason": str(error) if isinstance(error, AssertionError) else "target_unavailable",
                        "reported_success": succeeded, "requests": len(server.requests), "effects": counts,
                        "acknowledged": sorted(acknowledged)}), flush=True)
                    raise
                metrics.append({"case": mode, "reported_success": succeeded, "client_terminated_by_fixture": cancelled,
                                "terminal_ctrl_c": terminal_interrupt,
                                "terminal_isig_enabled": terminal_isig,
                                "requests": len(server.requests),
                                "effects": counts, "acknowledged": sorted(acknowledged), "at_most_once": True})
            finally:
                server.release.set()
                if process is not None and process.poll() is None:
                    if terminal_master is not None:
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                    process.communicate(timeout=10)
                if terminal_master is not None:
                    os.close(terminal_master)
                stack.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
    print(json.dumps({"mock_only": True, "through_translator": True,
                      "terminal_ctrl_c_exercised": os.name != "nt", "cases": metrics}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, AssertionError, subprocess.TimeoutExpired):
        raise SystemExit("Synthetic parallel stream gate failed; raw output withheld") from None
