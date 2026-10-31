"""Probe the local GPT-Live primary route without starting voice or using a mic.

Requires the installed capability environment and an existing managed service.
A WebSocket upgrade alone does not establish model/account entitlement.
"""

import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_client import NativeClient
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus


async def probe(root: Path):
    client = NativeClient(root)
    result = {"protocol": "gpt_live_primary", "session_start_sent": False,
              "microphone_used": False, "entitlement_tested": False}
    try:
        async with connect(client.base.replace("https://", "wss://", 1) + "/v1/live/sessions",
                ssl=client.context, additional_headers={"Authorization": "Bearer " + client.key},
                proxy=None, open_timeout=20, close_timeout=3, ping_interval=None,
                max_size=65536, compression=None):
            result["websocket_upgrade"] = True
    except InvalidStatus as error:
        result.update(websocket_upgrade=False, http_status=error.response.status_code)
        if error.response.status_code == 404:
            result["blocker"] = "local_primary_route_missing_or_unrecognized"
    except (OSError, TimeoutError):
        result.update(websocket_upgrade=False, transport_error=True)
    print(json.dumps(result))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    asyncio.run(probe(parser.parse_args().root))


if __name__ == "__main__": main()
