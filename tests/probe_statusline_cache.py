"""Observe cache counters in a disposable real-Claude status-line session.

The client sends two synthetic requests to a local fake Messages service. No
model account, user configuration, transcript, or project is used.
"""

import argparse
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
import time
import traceback

from claude_fixture import isolated_environment
from probe_parallel_stream import terminate_owned_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import statusline_adapter  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        if "count_tokens" in self.path:
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"input_tokens":100}'); return
        index = len(self.server.requests)
        self.server.requests.append(index)
        fresh, cached = (2000, 0) if index == 0 else (500, 1500)
        usage = {"input_tokens": fresh, "cache_creation_input_tokens": 0,
                 "cache_read_input_tokens": cached, "output_tokens": 5}
        message = {"id": f"fixture_{index}", "type": "message", "role": "assistant", "model": "gpt-6.1-sol",
                   "content": [], "stop_reason": None, "stop_sequence": None, "usage": usage}
        events = [{"type": "message_start", "message": message},
                  {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                  {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "DONE"}},
                  {"type": "content_block_stop", "index": 0},
                  {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}},
                  {"type": "message_stop"}]
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        try:
            for event in events:
                self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
        except OSError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hud-dir", type=Path, help="Optional reviewed HUD source for a full display check")
    parser.add_argument("--legacy-env-check", action="store_true", help="Check that Claude passes the session opt-out to the status line")
    parser.add_argument("--legacy-opt-out-check", action="store_true", help="Check the inherited opt-out against an enabled synthetic shared preference")
    args = parser.parse_args()
    if args.legacy_opt_out_check and args.hud_dir is None:
        parser.error("--legacy-opt-out-check requires --hud-dir")
    if os.name == "nt":
        print(json.dumps({"synthetic_only": True, "skipped": "Windows interactive console not covered"}))
        return
    import fcntl
    import pty
    import select
    import struct
    import termios

    claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
    if not claude:
        raise RuntimeError("The locked Claude fixture is required")
    with tempfile.TemporaryDirectory(prefix="bridge-status-cache-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
        root = Path(temporary).resolve()
        work, config = root / "work", root / "config"
        work.mkdir(); config.mkdir(mode=0o700)
        (config / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True,
            "projects": {str(work): {"hasTrustDialogAccepted": True}}}), encoding="utf-8")
        marker = root / "frames.jsonl"
        recorder = root / "record.py"
        recorder.write_text("import json,os,sys\nfrom pathlib import Path\n"
            "value=json.load(sys.stdin); cache=value.get('prompt_cache'); context=value.get('context_window') or {}\n"
            "usage=context.get('current_usage'); keys=('requests','hit_ratio','caching_observed','ttl','warm','expires_at')\n"
            "row={'cache':{key:cache.get(key) for key in keys} if isinstance(cache,dict) else None,"
            "'usage':usage if isinstance(usage,dict) else None,"
            "'opt_out_inherited':os.environ.get('CLAUDE_CODEX_STATUSLINE_CACHE_METRICS')=='0'}\n"
            "with Path(sys.argv[1]).open('a',encoding='utf-8') as out: out.write(json.dumps(row)+'\\n')\n"
            "print(json.dumps(value) if len(sys.argv)>2 else 'fixture')\n", encoding="utf-8")
        status_command = shlex.join([sys.executable, str(recorder), str(marker)])
        hud_marker = root / "hud-results.jsonl"
        adapted_marker = root / "adapted-results.jsonl"
        if args.hud_dir is not None:
            hud = args.hud_dir.resolve()
            package = json.loads((hud / "package.json").read_text(encoding="utf-8"))
            node = shutil.which("node")
            if package.get("name") != "claude-hud" or package.get("version") != "0.15.0" or not node:
                raise RuntimeError("The reviewed HUD and Node are required")
            hud_config = config / "plugins" / "claude-hud"
            hud_config.mkdir(parents=True)
            (hud_config / "config.json").write_text(json.dumps({"gitStatus": {"enabled": False},
                "display": {"showPromptCache": True, "showUsage": False}}), encoding="utf-8")
            profile = root / "profile.json"
            profile_data = {"context_windows": {"gpt-6.1-sol": 872_000}}
            if not args.legacy_opt_out_check:
                profile_data["expose_cache_metrics"] = True
            profile.write_text(json.dumps(profile_data), encoding="utf-8")
            adapter = [sys.executable, str(Path(statusline_adapter.__file__)), str(profile)]
            if args.legacy_opt_out_check:
                preference = root / "statusline-cache-metrics.json"
                preference.write_text('{"version":1,"enabled":true}\n', encoding="utf-8")
                preference.chmod(0o600)
                bootstrap = root / "adapter-bootstrap.py"
                bootstrap.write_text("import sys\nfrom pathlib import Path\n"
                    "sys.path.insert(0,sys.argv[1])\nimport statusline_adapter as adapter\n"
                    "adapter.ROOT=Path(sys.argv[2])\nassert adapter.cache_metrics_preference(adapter.ROOT)\n"
                    "sys.argv=['adapter',sys.argv[3]]\n"
                    "raise SystemExit(adapter.main())\n", encoding="utf-8")
                adapter = [sys.executable, str(bootstrap), str(Path(statusline_adapter.__file__).parent), str(root), str(profile)]
                adapted_recorder = root / "record-adapted.py"
                adapted_recorder.write_text("import json,sys\nfrom pathlib import Path\n"
                    "value=json.load(sys.stdin); row={'extension': 'claude_codex_bridge' in value,"
                    "'model': (value.get('model') or {}).get('id')=='gpt-6.1-sol'}\n"
                    "with Path(sys.argv[1]).open('a',encoding='utf-8') as out: out.write(json.dumps(row)+'\\n')\n"
                    "print(json.dumps(value))\n", encoding="utf-8")
            output_recorder = root / "record-hud.py"
            output_recorder.write_text("import json,sys\nfrom pathlib import Path\n"
                "text=sys.stdin.read(); label='Reported cache-read input (main)';"
                "row={'shown': label in text, 'fraction': label in text and '37.5%' in text,"
                "'timer': any(word in text.lower() for word in ('expired','until','5m'))}\n"
                "with Path(sys.argv[1]).open('a',encoding='utf-8') as out: out.write(json.dumps(row)+'\\n')\n"
                "sys.stdout.write(text)\n", encoding="utf-8")
            commands = [[sys.executable, str(recorder), str(marker), "passthrough"], adapter]
            if args.legacy_opt_out_check:
                commands.append([sys.executable, str(adapted_recorder), str(adapted_marker)])
            commands.extend(([node, str(hud / "hud.mjs")], [sys.executable, str(output_recorder), str(hud_marker)]))
            status_command = " | ".join(shlex.join(command) for command in commands)
        settings = root / "settings.json"
        settings.write_text(json.dumps({"modelPicker": {"options": [{"model": "gpt-6.1-sol", "label": "Fixture",
            "behavesAs": "claude-sonnet-5-5"}], "replaceBuiltInOptions": True},
            "statusLine": {"type": "command", "command": status_command}}), encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.requests = []
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        env = isolated_environment(root)
        env.update(CLAUDE_CONFIG_DIR=str(config), ANTHROPIC_BASE_URL=f"http://127.0.0.1:{server.server_port}",
                   ANTHROPIC_AUTH_TOKEN="fixture-only", ANTHROPIC_DEFAULT_SONNET_MODEL="gpt-6.1-sol",
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_TELEMETRY="1", TERM="xterm-256color")
        if args.legacy_env_check or args.legacy_opt_out_check:
            env["CLAUDE_CODEX_STATUSLINE_CACHE_METRICS"] = "0"
        command = [claude, "--restricted", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                   "--settings", str(settings), "--permission-mode", "dontAsk", "--model", "gpt-6.1-sol", "--tools", ""]
        wrapper = ("import fcntl,os,sys,termios;fcntl.ioctl(0,termios.TIOCSCTTY,0);"
                   "os.tcsetpgrp(0,os.getpgrp());os.execvpe(sys.argv[1],sys.argv[1:],os.environ)")
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 42, 140, 0, 0))
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
            collect(5)
            for index in range(2):
                os.write(master, (f"Return synthetic response {index}.\r").encode())
                end = time.monotonic() + 15
                while len(server.requests) <= index and time.monotonic() < end:
                    collect(.2)
                if len(server.requests) <= index:
                    raise RuntimeError("Synthetic client did not reach the fake service")
                collect(3)
            frames = [json.loads(line) for line in marker.read_text(encoding="utf-8").splitlines()] if marker.exists() else []
            caches = [frame["cache"] for frame in frames if isinstance(frame.get("cache"), dict)]
            usages = [frame["usage"] for frame in frames if isinstance(frame.get("usage"), dict)]
            reported = caches[-1] if caches else None
            default = statusline_adapter.corrected_payload({"prompt_cache": reported}, {})
            extended = statusline_adapter.corrected_payload({"prompt_cache": reported}, {}, expose_cache_metrics=True)
            filtered = extended.get("claude_codex_bridge", {}).get("reported_main_cache")
            expected = {"caching_observed": True, "requests": 2, "hit_ratio": .375}
            correct_counters = filtered == expected
            inferred_ttl = bool(reported and reported.get("ttl") == "5m" and reported.get("warm") is True
                                and isinstance(reported.get("expires_at"), int))
            full_usage = bool(usages and all(type(usages[-1].get(key)) is int for key in (
                "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")))
            hud_rows = [json.loads(line) for line in hud_marker.read_text(encoding="utf-8").splitlines()] if hud_marker.exists() else []
            adapted_rows = [json.loads(line) for line in adapted_marker.read_text(encoding="utf-8").splitlines()] if adapted_marker.exists() else []
            hud_correct = any(row.get("fraction") is True and row.get("timer") is False for row in hud_rows)
            hud_opted_out = bool(len(hud_rows) >= len(caches) > 0 and len(adapted_rows) >= len(caches)
                and all(row.get("shown") is False for row in hud_rows)
                and all(row.get("model") is True and row.get("extension") is False for row in adapted_rows))
            opt_out_inherited = bool(frames and all(frame.get("opt_out_inherited") is True for frame in frames))
            print(json.dumps({"synthetic_only": True, "fake_message_requests": len(server.requests), "frames": len(frames), "frames_with_usage": len(usages),
                "frames_with_prompt_cache": len(caches), "cache_read_fraction_matches_reported_tokens": correct_counters,
                "client_inferred_five_minute_ttl_without_provider_ttl": inferred_ttl,
                "bridge_omits_standard_cache_by_default": "prompt_cache" not in default and "claude_codex_bridge" not in default,
                "opt_in_omits_inferred_ttl": bool(filtered and "ttl" not in filtered and "expires_at" not in filtered),
                "last_usage_has_all_input_counters": full_usage,
                "hud_display_checked": args.hud_dir is not None,
                "hud_reported_fraction_without_timer": hud_correct if args.hud_dir is not None and not args.legacy_opt_out_check else None,
                "legacy_opt_out_hides_hud_cache": hud_opted_out if args.legacy_opt_out_check else None,
                "session_opt_out_inherited": opt_out_inherited if args.legacy_env_check or args.legacy_opt_out_check else None}, sort_keys=True))
            if len(server.requests) != 2 or not (full_usage and correct_counters and inferred_ttl and "prompt_cache" not in default):
                raise RuntimeError("Synthetic status-line control did not complete")
            if args.hud_dir is not None and not args.legacy_opt_out_check and not hud_correct:
                raise RuntimeError("Synthetic HUD cache display did not complete")
            if args.legacy_opt_out_check and not hud_opted_out:
                raise RuntimeError("Synthetic legacy opt-out did not hide the HUD cache")
            if (args.legacy_env_check or args.legacy_opt_out_check) and not opt_out_inherited:
                raise RuntimeError("Synthetic session opt-out was not inherited")
        finally:
            cleanup_ok = True
            if process is not None and process.poll() is None:
                try:
                    os.write(master, b"/exit\r")
                except OSError:
                    pass
                collect(1)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    cleanup_ok = terminate_owned_client(process, master)
            if slave is not None: os.close(slave)
            os.close(master)
            server.shutdown(); server.server_close(); thread.join(timeout=2)
            if not cleanup_ok and sys.exc_info()[0] is None:
                raise RuntimeError("Synthetic client cleanup did not complete")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        frame = traceback.extract_tb(error.__traceback__)[-1]
        print(json.dumps({"synthetic_only": True, "exception_type": type(error).__name__,
                          "exception_location": [Path(frame.filename).name, frame.name, frame.lineno]}), flush=True)
        raise SystemExit("Synthetic status-line cache observation failed; raw output withheld") from None
