"""Exercise Claude's interactive model picker with synthetic settings only.

This POSIX pseudoterminal fixture uses an empty disposable home and workspace.
It never edits a user's settings, starts a real model request, or prints UI text.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from claude_fixture import isolated_environment
from probe_claude_client import FixtureServer, Handler


MODEL = "gpt-6.1-sol"
ALPHA, BETA, GAMMA = (b"FIXTUREALPHA", b"FIXTUREBETA", b"FIXTUREGAMMA")


def picker(include_gamma):
    rows = [(MODEL, ALPHA), ("gpt-6.1-terra", BETA)]
    if include_gamma:
        rows.append(("gpt-6-sol", GAMMA))
    return {"modelPicker": {"options": [{"model": model, "label": label.decode(),
                                         "behavesAs": "claude-sonnet-5-5"} for model, label in rows],
                            "replaceBuiltInOptions": True}}


def write_settings(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    os.replace(temporary, path)


def run_case(claude, mode):
    import fcntl
    import pty
    import select
    import struct
    import termios

    server = FixtureServer(("127.0.0.1", 0), Handler)
    server.requests = []
    server.mode = "text"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="bridge-picker-reload-") as temporary:
            root = Path(temporary)
            config, work = root / "config", root / "work"
            config.mkdir(mode=0o700)
            work.mkdir(mode=0o700)
            launch, user = root / "launch.json", config / "settings.json"
            changed, hook = root / "changed", root / "hook.py"
            hook.write_text("import pathlib,sys\npathlib.Path(sys.argv[1]).write_text('changed')\n", encoding="utf-8")
            hooks = {"hooks": {"ConfigChange": [{"matcher": "user_settings", "hooks": [
                {"type": "command", "command": sys.executable, "args": [str(hook), str(changed)], "timeout": 10}]}]}}
            write_settings(launch, picker(False) if mode == "launch" else {})
            write_settings(user, {**(picker(False) if mode == "user" else {}), **hooks})
            profile = {"hasCompletedOnboarding": True, "projects": {str(work): {"hasTrustDialogAccepted": True}}}
            write_settings(config / ".claude.json", profile)
            env = isolated_environment(root)
            env.update(CLAUDE_CONFIG_DIR=str(config), ANTHROPIC_BASE_URL=f"http://127.0.0.1:{server.server_port}",
                       ANTHROPIC_AUTH_TOKEN="fixture-only", ANTHROPIC_DEFAULT_SONNET_MODEL=MODEL,
                       CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_TELEMETRY="1", TERM="xterm-256color")
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 42, 140, 0, 0))
            wrapper = ("import fcntl,os,sys,termios;fcntl.ioctl(0,termios.TIOCSCTTY,0);"
                       "os.tcsetpgrp(0,os.getpgrp());os.execvpe(sys.argv[1],sys.argv[1:],os.environ)")
            command = [claude, "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                       "--settings", str(launch), "--model", MODEL, "--tools", "", "--permission-mode", "dontAsk"]
            try:
                process = subprocess.Popen([sys.executable, "-c", wrapper, *command], cwd=work, env=env,
                    stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
            except BaseException:
                os.close(master)
                raise
            finally:
                os.close(slave)

            def collect(seconds, required=()):
                output = bytearray()
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    ready, _, _ = select.select([master], [], [], min(.2, max(0, deadline - time.monotonic())))
                    if ready:
                        try:
                            output.extend(os.read(master, 65536))
                        except OSError:
                            break
                        if len(output) > 2 * 1024 * 1024:
                            raise RuntimeError("Synthetic terminal output exceeded its bound")
                        if required and all(value in output for value in required):
                            break
                return bytes(output)

            try:
                initial = collect(5)
                if b"trust" in initial.lower():
                    # The prompt defaults to No; only the owned empty fixture is trusted.
                    os.write(master, b"\x1b[B\r")
                    collect(4)
                assert process.poll() is None, "Synthetic client exited during startup"
                os.write(master, b"/model\r")
                first = collect(15, (ALPHA, BETA))
                assert ALPHA in first and BETA in first and GAMMA not in first, "Initial synthetic picker was not visible"
                os.write(master, b"\x1b")
                collect(1)
                if mode == "user":
                    write_settings(user, {**picker(True), **hooks})
                    deadline = time.monotonic() + 12
                    while not changed.exists() and time.monotonic() < deadline:
                        time.sleep(.1)
                    assert changed.exists(), "Synthetic user settings change was not observed"
                else:
                    write_settings(launch, picker(True))
                    time.sleep(3)
                # The hook can finish before the client has finished applying
                # the file-watcher update. Reopen the picker while waiting for
                # an eventual update; the same client must remain alive.
                attempts = 0
                deadline = time.monotonic() + (30 if mode == "user" else 5)
                second = b""
                while time.monotonic() < deadline:
                    attempts += 1
                    os.write(master, b"/model\r")
                    second = collect(min(8, max(0, deadline - time.monotonic())),
                                     (ALPHA, BETA, GAMMA) if mode == "user" else (ALPHA, BETA))
                    if mode != "user" or (ALPHA in second and BETA in second and GAMMA in second):
                        break
                    os.write(master, b"\x1b")
                    collect(.5)
                assert ALPHA in second and BETA in second, "Reopened synthetic picker was not visible"
                assert (GAMMA in second) == (mode == "user"), "Synthetic picker reload differed from the expected source behavior"
                assert process.poll() is None, "Synthetic client exited during picker test"
                assert not server.requests, "Synthetic picker unexpectedly sent a model request"
                return {"case": mode, "initial_models_visible": True, "new_model_visible": GAMMA in second,
                        "same_client_alive": True, "picker_open_attempts": attempts}
            finally:
                if process.poll() is None:
                    process.terminate()
                # Closing our pty unblocks a client that is already trying to exit.
                os.close(master)
                if process.poll() is None:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def main():
    if os.name == "nt":
        print(json.dumps({"mock_only": True, "skipped": "Windows interactive console is not covered"}))
        return
    claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
    if not claude:
        raise RuntimeError("The locked Claude client is required")
    results = []
    for mode in ("launch", "user"):
        try:
            results.append(run_case(claude, mode))
        except (OSError, ValueError, RuntimeError, AssertionError, subprocess.SubprocessError) as error:
            print("Failed synthetic picker case: " + mode, file=sys.stderr)
            allowed = {"Synthetic client exited during startup", "Initial synthetic picker was not visible",
                       "Synthetic user settings change was not observed", "Reopened synthetic picker was not visible",
                       "Synthetic picker reload differed from the expected source behavior",
                       "Synthetic client exited during picker test", "Synthetic picker unexpectedly sent a model request"}
            detail = str(error)
            if detail in allowed: print("Synthetic picker failure stage: " + detail, file=sys.stderr)
            raise
    print(json.dumps({"mock_only": True, "cases": results}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, AssertionError, subprocess.SubprocessError):
        raise SystemExit("Synthetic picker probe failed; raw output withheld") from None
