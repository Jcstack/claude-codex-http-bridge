"""Synthetic stdio capabilities for the real Claude client contract test."""

import argparse
import base64
import io
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from mcp.server.mcpserver import MCPServer
from native_services import NativeServices
from native_tools import create_server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("native", "extra"))
    parser.add_argument("root", type=Path)
    parser.add_argument("workspace", type=Path)
    args = parser.parse_args()
    if args.kind == "extra":
        server = MCPServer("Fixture additional plugin", log_level="ERROR")
        @server.tool()
        def fixture_ping() -> str:
            """Return the synthetic plugin coexistence marker."""
            return "EXTRA_PLUGIN_READY"
    else:
        class FixtureClient:
            async def async_json(self, path, _payload=None):
                if path == "/v1/models": return {"data": [{"id": "gpt-image-9.2"}]}
                output = io.BytesIO(); Image.new("RGB", (84, 84), "blue").save(output, format="PNG")
                return {"data": [{"b64_json": base64.b64encode(output.getvalue()).decode()}]}
        services = NativeServices(args.root, args.workspace, client=FixtureClient())
        async def voice(*_args, **_kwargs): return {"transcript": "VOICE_FIXTURE_READY", "microphone_used": False}
        services.realtime_exchange = voice
        server = create_server(services)
    server.run()


if __name__ == "__main__": main()
