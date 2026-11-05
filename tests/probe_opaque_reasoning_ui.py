"""Observe a real local Claude UI with an owned, completely synthetic stream.

The fake Responses origin emits either opaque signatures or SSE comments. No
model account, existing service, user prompt, or user session is accessed.
Interactive terminal behavior is not exercised on Windows.
"""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from claude_fixture import isolated_environment, binary
from codex_stream_fixture import temporary_codex_translator
from probe_effort_roundtrip import client_arguments


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.server.requests += 1
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()

        def event(obj):
            self.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode())
            self.wfile.flush()

        try:
            event(
                {
                    "type": "response.created",
                    "response": {"id": "fixture", "model": "gpt-6.1-sol"},
                }
            )
            for i in range(5):
                if self.server.mode == "opaque":
                    obj = {
                        "id": f"reasoning_fixture_{i}",
                        "type": "reasoning",
                        "summary": [],
                    }
                    event(
                        {
                            "type": "response.output_item.added",
                            "output_index": i,
                            "item": obj,
                        }
                    )
                    event(
                        {
                            "type": "response.output_item.done",
                            "output_index": i,
                            "item": {**obj, "encrypted_content": "Z" * 8500 + str(i)},
                        }
                    )
                else:
                    self.wfile.write(b": synthetic ping\n\n")
                    self.wfile.flush()
                self.server.items += 1
                time.sleep(1)
            self.server.release.wait(30)
        except OSError:
            pass
        self.close_connection = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("proxy_binary", type=Path)
    parser.add_argument("--comments", action="store_true")
    args = parser.parse_args()
    if os.name == "nt":
        print(
            json.dumps(
                {
                    "synthetic_only": True,
                    "skipped": "Windows interactive terminal not covered",
                }
            )
        )
        return
    import fcntl
    import pty
    import select
    import struct
    import termios

    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    server.requests = 0
    server.items = 0
    server.mode = "comments" if args.comments else "opaque"
    server.release = threading.Event()
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    baseline = args.proxy_binary.resolve()
    with tempfile.TemporaryDirectory(prefix="bridge-opaque-ui-") as tmp:
        root = Path(tmp)
        work = root / "work"
        work.mkdir()
        config = root / "config"
        config.mkdir(mode=0o700)
        settings = root / "settings.json"
        model = "gpt-6.1-sol"
        settings.write_text(
            json.dumps(
                {
                    "modelPicker": {
                        "options": [
                            {
                                "model": model,
                                "label": "Synthetic fixture",
                                "behavesAs": "claude-sonnet-5-5",
                            }
                        ],
                        "replaceBuiltInOptions": True,
                    }
                }
            )
        )
        (config / ".claude.json").write_text(
            json.dumps(
                {
                    "hasCompletedOnboarding": True,
                    "projects": {str(work): {"hasTrustDialogAccepted": True}},
                }
            )
        )
        env = isolated_environment(root)
        env.update(
            CLAUDE_CONFIG_DIR=str(config),
            CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
            DISABLE_TELEMETRY="1",
            TERM="xterm-256color",
            HTTP_PROXY="http://127.0.0.1:9",
            HTTPS_PROXY="http://127.0.0.1:9",
            ALL_PROXY="http://127.0.0.1:9",
            NO_PROXY="127.0.0.1,localhost",
        )
        with temporary_codex_translator(baseline, server.server_port) as (port, key, _):
            env.update(
                ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}",
                ANTHROPIC_AUTH_TOKEN=key,
                ANTHROPIC_DEFAULT_SONNET_MODEL=model,
            )
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 42, 140, 0, 0))
            wrapper = (
                "import fcntl,os,sys,termios;fcntl.ioctl(0,termios.TIOCSCTTY,0);"
                "os.tcsetpgrp(0,os.getpgrp());os.execvpe(sys.argv[1],sys.argv[1:],os.environ)"
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    wrapper,
                    *client_arguments(str(binary()), settings, model),
                ],
                cwd=work,
                env=env,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
            )
            os.close(slave)
            try:
                output = bytearray()
                start = time.monotonic()
                sent = False
                trusted = False
                while time.monotonic() - start < 22:
                    if (
                        not trusted
                        and time.monotonic() - start > 3
                        and b"trust" in output.lower()
                    ):
                        # Accept the trust prompt for the owned, empty fixture only.
                        os.write(master, b"\x1b[B\r")
                        trusted = True
                    if not sent and time.monotonic() - start > 8:
                        os.write(master, b"Synthetic fixture request.\r")
                        sent = True
                    if select.select([master], [], [], 0.2)[0]:
                        try:
                            output.extend(os.read(master, 65536))
                        except OSError:
                            break
                    if len(output) > 4000000:
                        raise RuntimeError("Fixture output exceeded bound")
                clean = re.sub(
                    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))",
                    "",
                    output.decode(errors="replace"),
                )
                counts = re.findall(r"(?i)\b\d+(?:\.\d+)?\s*[kmb]?\s*tokens?\b", clean)
                if server.requests == 0 or server.items < 5:
                    raise RuntimeError(
                        "Synthetic stream did not reach the local client"
                    )
                print(
                    json.dumps(
                        {
                            "synthetic_only": True,
                            "mode": server.mode,
                            "empty_reasoning_items": 5
                            if server.mode == "opaque"
                            else 0,
                            "synthetic_signature_chars_each": 8501
                            if server.mode == "opaque"
                            else 0,
                            "visible_thinking_characters": 0,
                            "ui_token_counts": list(dict.fromkeys(counts))[-12:],
                            "saw_spinner": bool(re.search(r"(?i)thinking", clean)),
                            "fixture_request_count": server.requests,
                            "fixture_items_sent": server.items,
                            "process_exited": process.poll() is not None,
                            "client_output_bytes": len(output),
                            "saw_fixture_prompt": "Synthetic fixture request." in clean,
                        }
                    )
                )
            finally:
                server.release.set()
                if process.poll() is None:
                    process.terminate()
                os.close(master)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
    server.shutdown()
    server.server_close()
    worker.join(timeout=2)


if __name__ == "__main__":
    try:
        main()
    except (
        AssertionError,
        OSError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
    ):
        raise SystemExit(
            "Synthetic opaque reasoning UI probe failed; raw output withheld"
        ) from None
