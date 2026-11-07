"""Cancel an owned interactive Claude turn and continue in the same process.

Only disposable homes, fake credentials and two local translators are used.
POSIX terminal keys are covered; Windows console behavior is not inferred.
"""

import argparse
from contextlib import ExitStack
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
from probe_native_retry_cancel import Origin, RECOVERED, control_request
from probe_parallel_stream import terminate_owned_client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("proxy_binary", type=Path)
    parser.add_argument("--origin-binary", type=Path, required=True)
    parser.add_argument("--mode", choices=("active", "backoff"), required=True)
    parser.add_argument("--family", choices=("sol", "astra"), default="sol")
    parser.add_argument("--interrupt-key", choices=("escape", "ctrl-c"), default="escape")
    args = parser.parse_args()
    if os.name == "nt":
        print(json.dumps({"synthetic_only": True, "skipped": "Windows interactive console is not covered"}))
        return
    import fcntl
    import pty
    import select
    import struct
    import termios

    model, behaves = ("gpt-6.1-sol", "claude-sonnet-5-5") if args.family == "sol" else ("gpt-6-astra", "claude-opus-5-5")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    server.mode, server.status, server.delay = args.mode, 429, 15 if args.mode == "backoff" else 3
    server.semantics = []
    server.first_sent = threading.Event()
    server.second_sent = threading.Event()
    server.origin_closed = threading.Event()
    server.release = threading.Event()
    server.second_at = server.third_at = None
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    process = master = None
    phase, output, succeeded = "setup", bytearray(), False
    try:
        with tempfile.TemporaryDirectory(prefix="bridge-interactive-cancel-") as temporary, ExitStack() as stack:
            root = Path(temporary).resolve()
            root.chmod(0o700)
            work = root / "work"; work.mkdir()
            config = root / "config"; config.mkdir(mode=0o700)
            (config / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True,
                "projects": {str(work): {"hasTrustDialogAccepted": True}}}), encoding="utf-8")
            settings = root / "settings.json"
            settings.write_text(json.dumps({"modelPicker": {"options": [
                {"model": model, "label": "Fixture", "behavesAs": behaves}], "replaceBuiltInOptions": True}}), encoding="utf-8")
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
            debug = root / "debug.log"
            command = [str(binary()), "--restricted", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--settings", str(settings), "--permission-mode", "dontAsk", "--tools", "Write", "--model", model,
                "--debug-file", str(debug)]
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
                        raise RuntimeError("The owned interactive client did not stop")
                finally:
                    if master is not None:
                        os.close(master)
                        master = None

            # Stop the writer before removing its home or shutting down its gateways.
            stack.callback(stop_client)

            def clean():
                return re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))", "",
                              output.decode(errors="replace"))

            def pump_until(predicate, seconds):
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("The owned interactive client exited before continuation")
                    if predicate():
                        return
                    if select.select([master], [], [], .1)[0]:
                        output.extend(os.read(master, 65536))
                        if len(output) > 2000000:
                            raise RuntimeError("Owned interactive output exceeded its bound")
                raise RuntimeError("The owned interactive client did not reach the expected boundary")

            def native_boundary():
                event = server.first_sent if args.mode == "active" else server.second_sent
                if not event.is_set() or not debug.exists():
                    return False
                text = debug.read_text(encoding="utf-8", errors="replace")[-500000:].lower()
                return "stream started" in text if args.mode == "active" else any(
                    "api error" in line and "429" in line for line in text.splitlines())

            phase = "prompt_ready"
            pump_until(lambda: "❯" in clean(), 30)
            os.write(master, b"Start the owned cancellation fixture.\r")
            phase = "initial_request"
            pump_until(native_boundary, 40)
            assert control_request(port, key, model), "Independent control failed before interactive cancellation"
            before = len(server.semantics)
            assert before == (1 if args.mode == "active" else 2), "The intended interactive boundary was missed"
            assert all(row == server.semantics[0] for row in server.semantics[1:]), "Initial native retries changed semantics"
            output.clear()
            os.write(master, b"\x1b" if args.interrupt_key == "escape" else b"\x03")
            if args.mode == "active":
                phase = "origin_connection_close"
                pump_until(server.origin_closed.is_set, 5)
            phase = "input_ready_after_interrupt"
            pump_until(lambda: "❯" in clean(), 15)
            assert len(server.semantics) == before, "Cancellation issued another inference request"
            assert control_request(port, key, model), "Interactive cancellation disrupted an independent request"
            output.clear()
            os.write(master, b"\x15Continue the owned fixture without using tools.\r")
            phase = "same_process_continuation"
            pump_until(lambda: RECOVERED in clean(), 45)
            assert len(server.semantics) == 3, "Interactive continuation used unexpected inference attempts"
            next_input = json.loads(server.semantics[before])["input"]
            assert "Continue the owned fixture without using tools." in json.dumps(next_input), "The recovered response did not belong to the new user turn"
            if args.mode == "active":
                assert server.semantics[1] == server.semantics[2], "Continuation retries changed the new turn's semantics"
            assert not list(work.iterdir()), "Interactive cancellation caused a workspace effect"
            result = {"synthetic_only": True, "two_layers": True, "family_mapping": args.family,
                "mode": args.mode, "interrupt_key": args.interrupt_key, "same_process_continued": process.poll() is None,
                "active_origin_connection_closed": server.origin_closed.is_set() if args.mode == "active" else None,
                "independent_requests_completed": True, "unexpected_cancel_retry_absent": True,
                "native_retry_semantics_preserved": True, "workspace_effects_absent": True}
            phase = "cleanup"
        print(json.dumps(result))
        succeeded = True
    finally:
        if not succeeded:
            lower = output.decode(errors="replace").lower()
            print(json.dumps({"synthetic_only": True, "failed_phase": phase, "main_requests": len(server.semantics),
                "trust_text_seen": "trust" in lower, "login_text_seen": "login" in lower,
                "process_exited": process.poll() is not None if process is not None else None}))
        server.release.set()
        if process is not None and process.poll() is None:
            if not terminate_owned_client(process, master):
                raise RuntimeError("The owned interactive client did not stop")
        if master is not None:
            os.close(master)
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, AssertionError, subprocess.SubprocessError):
        raise SystemExit("Owned interactive cancellation failed; raw details withheld") from None
