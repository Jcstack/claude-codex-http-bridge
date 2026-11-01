"""Observe native-compaction progress at a disposable Claude Messages boundary.

The fake Responses origin and opaque compaction capsule are synthetic. This
does not measure model latency or contact a provider.
"""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import urllib.request

from live_support import temporary_translator

MODEL = "gpt-6.1-sol"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        self.server.trigger_seen = any(item.get("type") == "compaction_trigger" for item in body.get("input", []))
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        events = [
            {"type": "response.created", "response": {"id": "resp_fixture", "model": MODEL}},
            {"type": "response.compaction.compacting", "output_index": 0},
        ]
        try:
            for event in events:
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode()); self.wfile.flush()
            self.server.progress_sent.set()
            if not self.server.release.wait(15):
                return
            terminal = {"type": "response.completed", "response": {"id": "resp_fixture", "model": MODEL,
                "status": "completed", "output": [{"type": "compaction", "encrypted_content": "synthetic-capsule"}],
                "usage": {"input_tokens": 100, "output_tokens": 10}}}
            self.wfile.write(("data: " + json.dumps(terminal) + "\n\n").encode()); self.wfile.flush()
        except OSError:
            pass


def main() -> int:
    if len(sys.argv) != 2:
        raise RuntimeError("Usage: probe_native_compact_progress.py /path/to/proxy")
    binary = Path(sys.argv[1]).resolve()
    if not binary.is_file():
        raise RuntimeError("Candidate proxy is missing")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.progress_sent, server.release, server.trigger_seen = threading.Event(), threading.Event(), False
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    spec = {"context_window": 500000, "input_modalities": ["text"], "supports_search_tool": True,
            "supported_reasoning_levels": [{"effort": level} for level in ("low", "medium", "high", "xhigh", "max")]}
    upstream = {"state": {"port": server.server_port}, "key": "synthetic-local-upstream", "models": {MODEL: spec}}
    first_body = threading.Event()
    body = {"model": MODEL, "max_tokens": 100, "stream": True,
            "messages": [{"role": "user", "content": "Synthetic compact fixture"}]}

    def client(port, key):
        request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/messages", data=json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                     "anthropic-version": "2023-06-01", "X-Codex-Bridge-Native-Compaction": "1",
                     "X-Claude-Code-Request-Class": "compaction"})
        events = []
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=20) as response:
            for line in response:
                if line.strip():
                    first_body.set()
                if line.startswith(b"data: "):
                    events.append(json.loads(line[6:]))
        return events

    try:
        with temporary_translator(binary, upstream, MODEL) as (port, key):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(client, port, key)
                if not server.progress_sent.wait(10):
                    raise RuntimeError("Synthetic upstream did not send progress")
                before_completion = first_body.wait(1)
                server.release.set()
                events = pending.result(timeout=15)
            if not server.trigger_seen or not events or events[-1].get("type") != "message_stop":
                raise RuntimeError("Synthetic compaction did not complete correctly")
            print(json.dumps({"mock_only": True, "native_trigger_seen": True, "upstream_progress_sent": True,
                              "downstream_body_before_completion": before_completion, "completed": True,
                              "provider_latency_not_measured": True}, sort_keys=True))
        return 0
    finally:
        server.release.set(); server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, TimeoutError):
        raise SystemExit("Synthetic native-compaction progress probe failed") from None
