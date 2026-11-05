"""Record four synthetic screens from the integrity-locked Claude Code client.

The client talks through a disposable translator to a scripted loopback origin.
It has an owned home, workspace, status line and empty MCP configuration. Real
accounts, user configuration, installed services and provider APIs are not used.
This POSIX terminal recorder is a demo generator, not a compatibility test.
"""

import argparse
import codecs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time

import pyte


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "tests"))
from claude_fixture import binary, isolated_environment
from live_support import temporary_translator
from probe_parallel_stream import terminate_owned_client


MODELS = ("gpt-6.1-sol", "gpt-6.1-terra")


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= 2 * 1024 * 1024:
            self.send_error(413)
            return
        body = json.loads(self.rfile.read(size))
        inputs = body.get("input", [])
        result = any(isinstance(item, dict) and item.get("type") == "function_call_output"
                     and "SYNTHETIC_READ_OK" in str(item.get("output")) for item in inputs)
        prompt = "Read sample.txt" in json.dumps(inputs)
        tools = {item.get("name") for item in body.get("tools", []) if isinstance(item, dict)}
        response = {"id": "resp_synthetic_demo", "model": body.get("model"), "status": "in_progress"}
        events = [{"type": "response.created", "response": response}]
        if prompt and not result and "Read" in tools:
            self.server.read_requested = True
            self.server.read_model = body.get("model")
            arguments = json.dumps({"file_path": str(self.server.work / "sample.txt")})
            item = {"id": "item_demo", "type": "function_call", "call_id": "call_demo",
                    "name": "Read", "arguments": arguments, "status": "completed"}
            events.extend([
                {"type": "response.output_item.added", "output_index": 0,
                 "item": {**item, "arguments": "", "status": "in_progress"}},
                {"type": "response.function_call_arguments.delta", "output_index": 0,
                 "item_id": item["id"], "delta": arguments},
                {"type": "response.function_call_arguments.done", "output_index": 0,
                 "item_id": item["id"], "arguments": arguments},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
            ])
        else:
            self.server.read_verified = self.server.read_verified or result
            text = ("Read verified. Claude Code ran the local tool; the model response is scripted."
                    if result else "Ready. This is a synthetic local demo; no provider was called.")
            item = {"id": "message_demo", "type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}]}
            events.extend([
                {"type": "response.output_item.added", "output_index": 0,
                 "item": {**item, "content": [], "status": "in_progress"}},
                {"type": "response.content_part.added", "output_index": 0, "content_index": 0,
                 "part": {"type": "output_text", "text": ""}},
                {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": text},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
            ])
        events.append({"type": "response.completed", "response": {**response, "status": "completed",
                       "output": [item], "usage": {"input_tokens": 100, "output_tokens": 20}}})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for event in events:
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("proxy_binary", type=Path, help="An independently reviewed translator; never copied into the repository")
    args = parser.parse_args()
    if os.name == "nt":
        raise RuntimeError("The terminal recorder requires POSIX; the generated assets are portable")
    import fcntl
    import pty
    import select
    import struct
    import termios

    client = binary()
    with tempfile.TemporaryDirectory(prefix="harnesslift-demo-") as temporary:
        root = Path(temporary).resolve()
        env = isolated_environment(root)
        work = Path(env["HOME"]) / "sample-project"
        config = root / "config"
        work.mkdir()
        config.mkdir(mode=0o700)
        (work / "sample.txt").write_text("SYNTHETIC_READ_OK\nOnly this disposable file is read.\n", encoding="utf-8")
        statusline = root / "statusline.py"
        statusline.write_text('import json,sys\njson.load(sys.stdin)\n'
                             'print("HarnessLift | synthetic demo | quota unavailable | fixture status line")\n', encoding="utf-8")
        settings = root / "settings.json"
        settings.write_text(json.dumps({"modelPicker": {"options": [
            {"model": model, "label": label, "behavesAs": "claude-sonnet-5-5"}
            for model, label in zip(MODELS, ("Sol (synthetic demo)", "Terra (synthetic demo)"))],
            "replaceBuiltInOptions": True}, "statusLine": {"type": "command",
            "command": shlex.join([sys.executable, str(statusline)])}}), encoding="utf-8")
        (config / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True,
            "projects": {str(work): {"hasTrustDialogAccepted": True}}}), encoding="utf-8")
        env.update(CLAUDE_CONFIG_DIR=str(config), CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                   DISABLE_TELEMETRY="1", TERM="xterm-256color", HTTP_PROXY="http://127.0.0.1:9",
                   HTTPS_PROXY="http://127.0.0.1:9", ALL_PROXY="http://127.0.0.1:9", NO_PROXY="127.0.0.1,localhost")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        server.daemon_threads = True
        server.work, server.read_requested, server.read_verified = work, False, False
        server.read_model = None
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        spec = {"context_window": 500000, "supports_search_tool": True, "input_modalities": ["text"],
                "supported_reasoning_levels": [{"effort": effort} for effort in ("low", "medium", "high", "xhigh", "max")]}
        catalog = {model: spec for model in MODELS}
        upstream = {"state": {"port": server.server_port}, "key": "synthetic-demo-upstream", "models": catalog}
        screens = []
        try:
            with temporary_translator(args.proxy_binary.resolve(), upstream, MODELS[0], models=catalog) as (port, key):
                env.update(ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}", ANTHROPIC_AUTH_TOKEN=key,
                           ANTHROPIC_DEFAULT_SONNET_MODEL=MODELS[0])
                master, slave = pty.openpty()
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 25, 112, 0, 0))
                screen = pyte.Screen(112, 25)
                stream = pyte.Stream(screen)
                decoder = codecs.getincrementaldecoder("utf-8")("replace")
                wrapper = ("import fcntl,os,sys,termios;fcntl.ioctl(0,termios.TIOCSCTTY,0);"
                           "os.tcsetpgrp(0,os.getpgrp());os.execvpe(sys.argv[1],sys.argv[1:],os.environ)")
                command = [str(client), "--restricted", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                           "--settings", str(settings), "--permission-mode", "dontAsk", "--tools", "Read",
                           "--allowedTools", "Read", "--model", MODELS[0]]
                process = None
                try:
                    process = subprocess.Popen([sys.executable, "-c", wrapper, *command], cwd=work, env=env,
                        stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
                    os.close(slave)
                    slave = -1

                    def collect(seconds):
                        deadline = time.monotonic() + seconds
                        size = 0
                        while time.monotonic() < deadline:
                            if select.select([master], [], [], min(.1, max(0, deadline - time.monotonic())))[0]:
                                try:
                                    data = os.read(master, 65536)
                                except OSError:
                                    break
                                size += len(data)
                                if size > 2 * 1024 * 1024:
                                    raise RuntimeError("Synthetic terminal output exceeded its bound")
                                stream.feed(decoder.decode(data))

                    def snapshot(title, duration):
                        lines = [line.rstrip().replace(str(root), "<demo>") for line in screen.display]
                        if any("jcstack" in line.lower() or "sk-local-" in line for line in lines):
                            raise RuntimeError("Unexpected private-looking text in owned fixture")
                        screens.append({"title": title, "duration_ms": duration, "lines": lines})

                    collect(5)
                    if any("trust" in line.lower() for line in screen.display):
                        os.write(master, b"\x1b[B\r")
                        collect(3)
                    snapshot("01 / Keep Claude Code as your harness", 4500)
                    os.write(master, b"/model\r")
                    collect(2)
                    snapshot("02 / Choose a model inside the session", 7500)
                    os.write(master, b"\x1b[B\r")
                    collect(1)
                    os.write(master, b"Read sample.txt and report what happened.\r")
                    deadline = time.monotonic() + 20
                    while not server.read_verified and time.monotonic() < deadline:
                        collect(.5)
                    collect(3)
                    snapshot("03 / Claude Code executes the local Read tool", 11000)
                    collect(3)
                    snapshot("04 / Status-line plugins stay in the harness", 7000)
                    if not server.read_requested or not server.read_verified or server.read_model != MODELS[1]:
                        # Owned synthetic text only; never dump a real session.
                        (Path(tempfile.gettempdir()) / "harnesslift-demo-debug.txt").write_text(
                            "\n".join(screen.display), encoding="utf-8")
                        raise RuntimeError("The local tool round trip was not observed")
                finally:
                    if slave != -1:
                        os.close(slave)
                    if process is not None and process.poll() is None:
                        if not terminate_owned_client(process, master):
                            raise RuntimeError("Owned demo client cleanup did not complete")
                    os.close(master)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
        version = json.loads((client.parent / "package.json").read_text(encoding="utf-8"))["version"]
        result = {"synthetic_only": True, "provider_calls": 0, "real_claude_client": version,
                  "transport": "disposable Responses translator and scripted loopback origin",
                  "status_line": "owned fixture; not the Claude HUD plugin",
                  "read_tool_round_trip_verified": True, "selected_demo_model": server.read_model, "screens": screens}
        (ROOT / "demo-screens.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print("Recorded owned synthetic Claude client screens; provider calls: 0")


if __name__ == "__main__":
    main()
