"""Check targeted effort-error protection and negative cases through a fake origin."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import http.client
import json
from pathlib import Path
import threading

from live_support import temporary_translator


MODEL = "gpt-6.1-sol"
EFFORT_ERROR = "output_config.effort: Extra inputs are not permitted"
OTHER_ERROR = "context_management: Extra inputs are not permitted"


class Origin(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.calls += 1
        payload = json.dumps({"error": {"type": "invalid_request_error", "message": self.server.message}}).encode()
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    binary = args.binary.resolve()
    if not binary.is_file():
        raise RuntimeError("A candidate translator is required")
    origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    origin.calls, origin.message = 0, EFFORT_ERROR
    thread = threading.Thread(target=origin.serve_forever, daemon=True)
    thread.start()
    spec = {"context_window": 500000, "supports_search_tool": True, "input_modalities": ["text"],
            "supported_reasoning_levels": [{"effort": value} for value in ("low", "medium", "high", "xhigh", "max")]}
    upstream = {"state": {"port": origin.server_port}, "key": "synthetic-local-upstream", "models": {MODEL: spec}}
    try:
        with temporary_translator(binary, upstream, MODEL) as (port, key):
            def run(body, error):
                origin.message = error
                before = origin.calls
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
                try:
                    connection.request("POST", "/v1/messages", json.dumps(body),
                                       {"Authorization": "Bearer " + key, "Content-Type": "application/json",
                                        "anthropic-version": "2023-06-01"})
                    response = connection.getresponse()
                    value = json.loads(response.read())
                    message = value.get("error", {}).get("message", "")
                    if origin.calls <= before or response.status != 400:
                        raise RuntimeError("Synthetic upstream-error control failed")
                    return message
                finally:
                    connection.close()

            base = {"model": MODEL, "max_tokens": 64, "messages": [{"role": "user", "content": "fixture"}]}
            explicit = {**base, "output_config": {"effort": "max"}}
            checks = {}
            for streaming in (False, True):
                message = run({**explicit, "stream": streaming}, EFFORT_ERROR)
                checks[f"explicit_effort_guarded_stream_{streaming}"] = "not downgraded" in message and "/effort" in message
            message = run({**base, "stream": True}, EFFORT_ERROR)
            checks["unset_effort_keeps_original_error"] = EFFORT_ERROR in message
            message = run({**explicit, "stream": True}, OTHER_ERROR)
            checks["unrelated_error_kept"] = OTHER_ERROR in message
            system = {**base, "stream": True, "messages": [*base["messages"],
                      {"role": "system", "content": [], "output_config": {"effort": "max"}}]}
            message = run(system, EFFORT_ERROR)
            checks["system_effort_guarded"] = "not downgraded" in message and "/effort" in message
            if not all(checks.values()):
                raise RuntimeError("Synthetic targeted effort guard contract failed: "
                                   + ", ".join(name for name, passed in checks.items() if not passed))
            print(json.dumps({"synthetic_only": True, **checks}, sort_keys=True))
    finally:
        origin.shutdown()
        origin.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, http.client.HTTPException):
        raise SystemExit("Synthetic effort guard contract failed; raw output withheld") from None
