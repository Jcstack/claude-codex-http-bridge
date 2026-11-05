"""Observe fake credential selection via a disposable origin's trace header.

No trace value or credential leaves this process. This is not account pinning.
"""

import argparse
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import re
import sys
import threading
import time
from unittest.mock import patch
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_stream_fixture as fixture  # noqa: E402
from probe_reasoning_retry_wait import Origin  # noqa: E402

original_write_text = Path.write_text


def with_fake_management(path, data, *args, **kwargs):
    if path.name == "config.yaml" and path.parent.name.startswith(
        "bridge-codex-stream-fixture-"
    ):
        old = "management: {allow-remote: false, disable-control-panel: true, disable-auto-update-panel: true}"
        new = "management: {allow-remote: false, disable-control-panel: true, disable-auto-update-panel: true, secret-key: synthetic-management-only}"
        data = data.replace(old, new)
    return original_write_text(path, data, *args, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    server.bodies = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with (
            patch.object(Path, "write_text", new=with_fake_management),
            fixture.temporary_codex_translator(
                args.baseline.resolve(), server.server_port, managed_policy=True
            ) as (port, key, log),
        ):
            payload = {
                "model": "gpt-6.1-sol",
                "stream": True,
                "input": [{"role": "user", "content": "synthetic"}],
            }

            def selected_index():
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/responses",
                    data=json.dumps(payload).encode(),
                    headers={
                        "authorization": "Bearer " + key,
                        "content-type": "application/json",
                    },
                )
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                    request, timeout=8
                ) as response:
                    trace = response.headers.get("X-CPA-TRACE-ID", "")
                    response.read(1_000_000)
                match = re.fullmatch(r"\d{14}-([a-z0-9]+)-[0-9a-f-]{36}", trace)
                return match.group(1) if match else ""

            original = selected_index()
            same = selected_index()
            if not original or same != original:
                raise AssertionError("Synthetic trace identity was absent or unstable")

            def management_indexes():
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v0/management/auth-files",
                    headers={"authorization": "Bearer synthetic-management-only"},
                )
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                    request, timeout=8
                ) as response:
                    data = json.load(response)
                return {
                    item.get("auth_index")
                    for item in data.get("files", [])
                    if isinstance(item, dict)
                    and item.get("provider") == "codex"
                    and not item.get("disabled")
                }

            original_listed = original in management_indexes()
            config = log.parent / "config.yaml"
            text = config.read_text(encoding="utf-8")
            if text.count('api-key: "fixture-upstream"') != 1:
                raise AssertionError("Unexpected disposable fixture configuration")
            config.write_text(
                text.replace('api-key: "fixture-upstream"', 'api-key: "fixture-local"'),
                encoding="utf-8",
            )
            deadline = time.monotonic() + 25
            changed = ""
            while time.monotonic() < deadline:
                time.sleep(0.25)
                changed = selected_index()
                if changed and changed != original:
                    break
            checks = {
                "trace_present": bool(original),
                "same_fake_selection_stable": same == original,
                "changed_fake_selection_detected": bool(
                    changed and changed != original
                ),
                "fake_api_key_absent_from_auth_files": not original_listed
                and changed not in management_indexes(),
            }
            print(json.dumps({"mock_only": True, **checks}))
            if not all(checks.values()):
                raise AssertionError("Synthetic trace selection was not observed")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, OSError, RuntimeError, ValueError):
        raise SystemExit(
            "Synthetic trace selection gate failed; raw output withheld"
        ) from None
