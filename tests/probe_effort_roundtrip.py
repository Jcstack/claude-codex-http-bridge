"""Check Claude effort controls through a translator and fake Responses origin.

The client, home, workspace, credentials and origin are disposable. This does
not establish a real model's entitlement or actual reasoning behavior.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading

from claude_fixture import isolated_environment
from live_support import temporary_translator


FAMILIES = {"sol": ("gpt-6.1-sol", "claude-sonnet-5-5"),
            "terra": ("gpt-6.1-terra", "claude-sonnet-5-5"),
            "astra": ("gpt-6-astra", "claude-opus-5-5"),
            "luna": ("gpt-6-luna", "claude-haiku-5-5")}
LEVELS = ("low", "medium", "high", "xhigh", "max")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        self.server.bodies.append(body)
        self.server.requests.append({"model": body.get("model"),
                                     "effort": (body.get("reasoning") or {}).get("effort")})
        item = {"id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "DONE", "annotations": []}]}
        response = {"id": "resp_fixture", "model": self.server.model, "status": "in_progress"}
        events = [
            {"type": "response.created", "response": response},
            {"type": "response.output_item.added", "output_index": 0,
             "item": {**item, "content": [], "status": "in_progress"}},
            {"type": "response.content_part.added", "output_index": 0, "content_index": 0,
             "part": {"type": "output_text", "text": ""}},
            {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "DONE"},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": {**response, "status": "completed", "output": [item],
                          "usage": {"input_tokens": 100, "output_tokens": 5}}},
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for event in events:
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
        except OSError:
            pass


def client_arguments(claude, settings, model):
    return [claude, "--restricted", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--settings", str(settings), "--permission-mode", "dontAsk", "--model", model, "--tools", "Read"]


def interactive(claude, settings, work, config, environment, server, model):
    if os.name == "nt":
        return {"skipped": "Windows interactive console is not covered"}
    import fcntl
    import pty
    import select
    import struct
    import termios
    import time

    profile = {"hasCompletedOnboarding": True, "projects": {str(work): {"hasTrustDialogAccepted": True}}}
    (config / ".claude.json").write_text(json.dumps(profile), encoding="utf-8")
    environment = {**environment, "TERM": "xterm-256color"}
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 42, 140, 0, 0))
    wrapper = ("import fcntl,os,sys,termios;fcntl.ioctl(0,termios.TIOCSCTTY,0);"
               "os.tcsetpgrp(0,os.getpgrp());os.execvpe(sys.argv[1],sys.argv[1:],os.environ)")
    try:
        process = subprocess.Popen([sys.executable, "-c", wrapper, *client_arguments(claude, settings, model)],
                                   cwd=work, env=environment, stdin=slave, stdout=slave, stderr=slave,
                                   start_new_session=True)
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)

    def collect(seconds):
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
        return bytes(output)

    try:
        initial = collect(5)
        if b"trust" in initial.lower():
            # Only the owned empty fixture workspace is trusted.
            os.write(master, b"\x1b[B\r")
            collect(4)
        results = {}
        requests_at_start = len(server.bodies)
        for level in LEVELS:
            if process.poll() is not None:
                print(json.dumps({"synthetic_only": True, "failure_phase": "interactive_client_exited",
                                  "completed_effort_checks": len(results)}))
                raise RuntimeError("Synthetic interactive client exited")
            os.write(master, ("/effort " + level + "\r").encode())
            collect(2)
            before = len(server.requests)
            os.write(master, ("Emit fixture response " + level + ".\r").encode())
            collect(5)
            sent = server.requests[before:]
            results[level] = len(sent) == 1 and sent[0] == {"model": model, "effort": level}
        bodies = server.bodies[requests_at_start:]
        results["request_count_matches"] = len(bodies) == len(LEVELS)
        if not results["request_count_matches"]:
            print(json.dumps({"synthetic_only": True, "failure_phase": "interactive_request_count",
                              "request_count": len(bodies), "expected_count": len(LEVELS),
                              "effort_checks": dict(results)}))
        keys = [body.get("prompt_cache_key") for body in bodies]
        pairs = list(zip(bodies, bodies[1:]))
        results["cache_key_present_and_stable"] = all(isinstance(key, str) and key for key in keys) and len(set(keys)) == 1
        results["tool_definitions_present_and_stable"] = (
            bool(bodies) and isinstance(bodies[0].get("tools"), list) and bool(bodies[0]["tools"])
            and all(before.get("tools") == after.get("tools") for before, after in pairs))
        results["input_items_preserve_structural_prefix"] = (
            bool(bodies) and isinstance(bodies[0].get("input"), list) and bool(bodies[0]["input"])
            and all(isinstance(before.get("input"), list) and isinstance(after.get("input"), list)
                    and after["input"][:len(before["input"])] == before["input"] for before, after in pairs))
        return results
    finally:
        if process.poll() is None:
            process.terminate()
        os.close(master)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--family", choices=tuple(FAMILIES), default="sol")
    args = parser.parse_args()
    binary = args.binary.resolve()
    model, behaves_as = FAMILIES[args.family]
    claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
    if not claude or not binary.is_file():
        raise RuntimeError("The locked Claude fixture and candidate proxy are required")
    with tempfile.TemporaryDirectory(prefix="bridge-effort-roundtrip-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
        root = Path(temporary).resolve()
        work, config = root / "work", root / "config"
        work.mkdir()
        config.mkdir(mode=0o700)
        settings = root / "settings.json"
        settings.write_text(json.dumps({"modelPicker": {"options": [{"model": model, "label": "Fixture",
            "behavesAs": behaves_as}], "replaceBuiltInOptions": True}}), encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.model = model
        server.requests = []
        server.bodies = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        environment = isolated_environment(root)
        environment.update(CLAUDE_CONFIG_DIR=str(config), CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                           DISABLE_TELEMETRY="1", CLAUDE_CODE_TMPDIR=str(root), TMPDIR=str(root),
                           TMP=str(root), TEMP=str(root))
        spec = {"context_window": 500000, "supports_search_tool": True, "input_modalities": ["text"],
                "supported_reasoning_levels": [{"effort": value} for value in LEVELS]}
        upstream = {"state": {"port": server.server_port}, "key": "synthetic-local-upstream", "models": {model: spec}}
        try:
            with temporary_translator(binary, upstream, model) as (port, key):
                environment.update(ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}", ANTHROPIC_AUTH_TOKEN=key,
                                   ANTHROPIC_DEFAULT_SONNET_MODEL=model)
                cli = {}
                for level in LEVELS:
                    before = len(server.requests)
                    try:
                        result = subprocess.run([*client_arguments(claude, settings, model), "--output-format", "json",
                                                 "--effort", level, "-p", "Return the fixture response."],
                                                cwd=work, env=environment, capture_output=True, text=True, timeout=45)
                    except subprocess.TimeoutExpired:
                        print(json.dumps({"synthetic_only": True, "failure_phase": "cli_timeout", "effort": level}))
                        raise
                    try:
                        message = json.loads(result.stdout)
                        successful = result.returncode == 0 and not message.get("is_error") and "DONE" in message.get("result", "")
                    except (ValueError, AttributeError, TypeError):
                        successful = False
                    sent = server.requests[before:]
                    cli[level] = successful and len(sent) == 1 and sent[0] == {"model": model, "effort": level}
                tui = interactive(claude, settings, work, config, environment, server, model)
                print(json.dumps({"synthetic_only": True, "family": args.family,
                                  "cli_roundtrip": cli, "interactive_roundtrip": tui}, sort_keys=True))
                if not all(cli.values()) or ("skipped" not in tui and not all(tui.values())):
                    raise RuntimeError("Synthetic effort round trip failed")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        raise SystemExit("Synthetic effort round-trip gate failed; raw output withheld") from None
