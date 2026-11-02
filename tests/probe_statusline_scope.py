"""Observe status-line source isolation with owned clients and a fake model.

All settings, commands, sessions, working directories and endpoints are
synthetic. This temporary environment is not an operating-system sandbox.
"""

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from claude_fixture import isolated_environment
from probe_parallel_stream import terminate_owned_client
from probe_statusline_cache import Handler

sys.path.insert(0, os.environ.get("CLAUDE_CODEX_TEST_LAUNCHER_ROOT") or str(Path(__file__).resolve().parents[1]))
import launcher  # noqa: E402


MODEL = "gpt-6.1-sol"


def run_case(claude, scope, wrap, *, legacy=False):
    import fcntl
    import pty
    import select
    import struct
    import termios

    with tempfile.TemporaryDirectory(prefix="bridge-status-scope-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
        root = Path(temporary).resolve(); work = root / "work"; config = root / "config"; ui = root / "ui"
        for directory in (config, ui): directory.mkdir(mode=0o700)
        main_worktree = None
        if scope == "worktree_override":
            git = shutil.which("git")
            if not git: raise RuntimeError("Git is required for the synthetic worktree")
            main_worktree = root / "main"; main_worktree.mkdir(mode=0o700)
            template = root / "empty-template"; template.mkdir(mode=0o700)
            git_env = isolated_environment(root)
            git_env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                           GIT_TEMPLATE_DIR=str(template), GIT_TERMINAL_PROMPT="0")
            git_prefix = [git, "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgsign=false"]
            for arguments in (["init", "--quiet", str(main_worktree)],
                              ["-C", str(main_worktree), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                               "commit", "--quiet", "--allow-empty", "-m", "fixture"],
                              ["-C", str(main_worktree), "worktree", "add", "--quiet", "--detach", str(work)]):
                subprocess.run([*git_prefix, *arguments], env=git_env, capture_output=True, check=True, timeout=20)
        else:
            work.mkdir(mode=0o700)
        adapter = root / "adapter"; adapter.mkdir(mode=0o700)
        source = Path(launcher.__file__).resolve().parent
        for name in ("statusline_adapter.py", "session_models.py", "codex_usage.py", "accounts.py",
                     "api_profile.py", "windows_acl.py"):
            shutil.copyfile(source / name, adapter / name)
        (config / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True,
            "projects": {str(work): {"hasTrustDialogAccepted": True}}}), encoding="utf-8")
        marker = root / "statusline-ran"; explicit_marker = root / "explicit-statusline-ran"
        project_marker = root / "project-statusline-ran"
        recorder = root / "statusline.py"
        recorder.write_text("import pathlib,sys\nsys.stdin.read()\npathlib.Path(sys.argv[1]).write_text('fixture')\nprint('fixture')\n",
                            encoding="utf-8")
        status = {"type": "command", "command": shlex.join([sys.executable, str(recorder), str(marker)])}
        (config / "settings.json").write_text(json.dumps({"statusLine": status}), encoding="utf-8")
        if scope in ("project_override", "local_override", "parent_override", "worktree_override"):
            project_root = root if scope == "parent_override" else main_worktree if scope == "worktree_override" else work
            project_config = project_root / ".claude"; project_config.mkdir()
            name = "settings.local.json" if scope in ("local_override", "worktree_override") else "settings.json"
            project_status = {"type": "command", "command": shlex.join([sys.executable, str(recorder), str(project_marker)])}
            (project_config / name).write_text(json.dumps({"statusLine": project_status}), encoding="utf-8")
        base = root / "base.json"
        initial = {"modelPicker": {"options": [{"model": MODEL, "label": "Fixture",
            "behavesAs": "claude-sonnet-5-5"}], "replaceBuiltInOptions": True}}
        if scope == "restricted_explicit":
            initial["statusLine"] = {"type": "command", "command": shlex.join([sys.executable, str(recorder), str(explicit_marker)])}
        base.write_text(json.dumps(initial), encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler); server.requests = []
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        env = isolated_environment(root)
        env.update(CLAUDE_CONFIG_DIR=str(config), ANTHROPIC_BASE_URL=f"http://127.0.0.1:{server.server_port}",
            ANTHROPIC_AUTH_TOKEN="fixture-only", ANTHROPIC_DEFAULT_SONNET_MODEL=MODEL,
            CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_TELEMETRY="1", TERM="xterm-256color")
        flags = (["--restricted"] if scope in ("restricted", "restricted_explicit") else
                 ["--setting-sources", scope] if scope in ("project", "user") else [])
        if scope == "restricted_env": env["CLAUDE_CODE_RESTRICTED"] = "1"
        if scope == "restricted_env_true": env["CLAUDE_CODE_RESTRICTED"] = "true"
        with patch.dict(os.environ, env, clear=True), patch.object(launcher, "ROOT", adapter):
            allowed = (True if legacy else launcher.user_status_line_allowed(flags)
                       and not launcher.project_status_line_may_override(flags, work))
            settings, _ = (launcher.session_settings(base, {MODEL: 500000}, ui, route="api",
                                                     include_user_status_line=allowed) if wrap else (base, []))
        command = [claude, *flags, "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                   "--settings", str(settings), "--permission-mode", "dontAsk", "--model", MODEL, "--tools", ""]
        wrapper = ("import fcntl,os,sys,termios;fcntl.ioctl(0,termios.TIOCSCTTY,0);"
                   "os.tcsetpgrp(0,os.getpgrp());os.execvpe(sys.argv[1],sys.argv[1:],os.environ)")
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        process = None
        try:
            process = subprocess.Popen([sys.executable, "-c", wrapper, *command], cwd=work, env=env,
                                       stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
            os.close(slave); slave = None
            def collect(seconds):
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    ready, _, _ = select.select([master], [], [], min(.2, max(0, end - time.monotonic())))
                    if ready:
                        try: os.read(master, 65536)
                        except OSError: break
            collect(3)
            os.write(master, b"Return the synthetic response.\r")
            deadline = time.monotonic() + 25
            while not server.requests and time.monotonic() < deadline: collect(.2)
            if not server.requests:
                raise RuntimeError("Synthetic client did not reach its fake service")
            collect(3)
            return {"scope": scope, "bridge_wrapped": wrap, "fake_request_seen": True,
                    "global_statusline_ran": marker.exists(), "explicit_statusline_ran": explicit_marker.exists(),
                    "project_statusline_ran": project_marker.exists()}
        finally:
            cleanup_ok = True
            if process is not None and process.poll() is None:
                try: os.write(master, b"/exit\r")
                except OSError: pass
                collect(1)
                try: process.wait(timeout=3)
                except subprocess.TimeoutExpired: cleanup_ok = terminate_owned_client(process, master)
            if slave is not None: os.close(slave)
            os.close(master); server.shutdown(); server.server_close(); thread.join(timeout=2)
            if not cleanup_ok and sys.exc_info()[0] is None:
                raise RuntimeError("Synthetic client cleanup did not complete")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    expectations = parser.add_mutually_exclusive_group()
    expectations.add_argument("--expect-promotion", action="store_true", help="Exercise the earlier unconditional wrapper")
    expectations.add_argument("--expect-isolated", action="store_true", help="Require scoped user settings")
    expectations.add_argument("--expect-project-promotion", action="store_true", help="Observe project/local status-line precedence")
    expectations.add_argument("--expect-project-respected", action="store_true", help="Require native project/local precedence")
    expectations.add_argument("--observe-parent-scope", action="store_true", help="Observe parent project-settings discovery")
    expectations.add_argument("--observe-worktree-scope", action="store_true", help="Observe linked-worktree local settings")
    expectations.add_argument("--expect-worktree-respected", action="store_true", help="Require linked-worktree local precedence")
    expectations.add_argument("--observe-true-restricted", action="store_true", help="Observe the true spelling for restricted mode")
    args = parser.parse_args()
    if os.name == "nt":
        print(json.dumps({"synthetic_only": True, "skipped": "Windows interactive console not covered"})); return
    claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
    if not claude: raise RuntimeError("The locked client is required")
    scopes = (("restricted", "project", "normal") if args.expect_promotion else
              ("project_override", "local_override") if args.expect_project_promotion or args.expect_project_respected else
              ("parent_override",) if args.observe_parent_scope else
              ("worktree_override",) if args.observe_worktree_scope or args.expect_worktree_respected else
              ("restricted_env_true",) if args.observe_true_restricted else
              ("restricted", "project", "user", "restricted_explicit", "restricted_env", "restricted_env_true", "normal"))
    results = [run_case(claude, scope, wrap, legacy=args.expect_promotion or args.expect_project_promotion)
               for scope in scopes for wrap in (False, True)]
    print(json.dumps({"synthetic_only": True, "cases": results}, sort_keys=True))
    values = {(row["scope"], row["bridge_wrapped"]): row["global_statusline_ran"] for row in results}
    if not (args.expect_project_promotion or args.expect_project_respected or args.observe_parent_scope
            or args.observe_worktree_scope or args.expect_worktree_respected or args.observe_true_restricted) and not (not values[("restricted", False)] and not values[("project", False)]
            and values[("normal", False)] and values[("normal", True)]):
        raise RuntimeError("Synthetic status-line source controls did not hold")
    if args.expect_promotion and not (values[("restricted", True)] and values[("project", True)]):
        raise RuntimeError("Synthetic status-line promotion was not reproduced")
    if args.expect_isolated:
        if any(values[(scope, wrapped)] for scope in ("restricted", "project", "restricted_env", "restricted_env_true") for wrapped in (False, True)):
            raise RuntimeError("Disabled synthetic user status line was promoted")
        if not all(values[(scope, wrapped)] for scope in ("normal", "user") for wrapped in (False, True)):
            raise RuntimeError("Enabled synthetic user status line was lost")
        explicit = [row for row in results if row["scope"] == "restricted_explicit"]
        if not all(row["explicit_statusline_ran"] and not row["global_statusline_ran"] for row in explicit):
            raise RuntimeError("Explicit synthetic launch status line was not preserved")
    if args.expect_project_promotion:
        for scope in scopes:
            native = next(row for row in results if row["scope"] == scope and not row["bridge_wrapped"])
            bridge = next(row for row in results if row["scope"] == scope and row["bridge_wrapped"])
            if not (native["project_statusline_ran"] and not native["global_statusline_ran"]
                    and bridge["global_statusline_ran"] and not bridge["project_statusline_ran"]):
                raise RuntimeError("Synthetic project status-line precedence changed")
    if args.expect_project_respected:
        for scope in scopes:
            for row in (row for row in results if row["scope"] == scope):
                if not (row["project_statusline_ran"] and not row["global_statusline_ran"]):
                    raise RuntimeError("Synthetic project status-line precedence was not preserved")
    if args.expect_worktree_respected:
        if not all(row["project_statusline_ran"] and not row["global_statusline_ran"] for row in results):
            raise RuntimeError("Synthetic linked-worktree status-line precedence was not preserved")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        raise SystemExit("Synthetic status-line scope observation failed; raw output withheld") from None
