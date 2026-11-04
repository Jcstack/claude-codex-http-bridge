"""Observe a synthetic background-response resume request through a local proxy.

This is a routing probe only: it does not contact OpenAI, a subscription account,
or a real response, and cannot establish a provider's entitlement or behavior.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import json
from pathlib import Path
import threading
import urllib.error
import urllib.request

from codex_stream_fixture import temporary_codex_translator


class FakeUpstream(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.server.received += 1
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        self.server.received += 1
        self.send_response(200)
        self.end_headers()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("proxy_binary", type=Path)
    args = parser.parse_args()
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
    upstream.received = 0
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    try:
        with temporary_codex_translator(args.proxy_binary.resolve(), upstream.server_port) as (port, key, _log):
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/responses/resp_fixture?stream=true&starting_after=1",
                headers={"Authorization": "Bearer " + key})
            try:
                with opener.open(request, timeout=5) as response:
                    status = response.status
            except urllib.error.HTTPError as error:
                status = error.code
                error.close()
            if status != 404 or upstream.received != 0:
                raise RuntimeError("The local response-resume route differs from the pinned baseline")
            print(json.dumps({"mock_only": True, "local_resume_status": status,
                              "fake_upstream_requests": upstream.received,
                              "provider_resume_not_tested": True}))
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, urllib.error.URLError):
        raise SystemExit("Synthetic response-resume routing probe failed; raw output withheld") from None
