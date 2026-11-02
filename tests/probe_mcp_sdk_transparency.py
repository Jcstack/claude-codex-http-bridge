"""Observe the locked MCP SDK's synthetic JSON-RPC and error-log behavior.

The marker is synthetic, no actual server is started, and captured messages
and logs remain in memory. This is not a complete SDK security audit.
"""

from importlib.metadata import version
import io
import json
import logging
from pathlib import Path
import sys

from mcp import types
from mcp.client import stdio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import desktop_adapter  # noqa: E402


def main():
    sample = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "fixture.tool", "arguments": {"value": "synthetic"},
                         "_meta": {"fixture": "kept"}},
              "futureExtension": {"fixture": "kept"}}
    roundtrip = types.jsonrpc_message_adapter.validate_python(sample).model_dump(by_alias=True, exclude_unset=True)
    if roundtrip.get("params") != sample["params"]:
        raise RuntimeError("Synthetic JSON-RPC parameter positive control failed")

    # Attach a logger handler to synthetic invalid input to check whether
    # validation errors may include the input. Do not print the marker,
    # raw exception, or captured log text.
    marker = "SYNTHETIC_PRIVATE_MARKER_ONLY"
    captured = io.StringIO()
    handler = logging.StreamHandler(captured)
    logger = logging.getLogger("mcp.client.stdio")
    level, propagate = logger.level, logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.ERROR)
    logger.propagate = False
    invalid = json.dumps([{"fixture": marker}])
    try:
        result = stdio._parse_line(invalid)
        unprotected_log_contains_input = marker in captured.getvalue()
        captured.seek(0)
        captured.truncate(0)
        desktop_adapter.protect_mcp_stdio_logs()
        protected_result = stdio._parse_line(invalid)
        protected_log_is_safe = (marker not in captured.getvalue()
                                 and "MCP stdio transport error; details withheld" in captured.getvalue())
    finally:
        logger.removeHandler(handler)
        logger.removeFilter(desktop_adapter._MCP_STDIO_PRIVACY_FILTER)
        logger.setLevel(level)
        logger.propagate = propagate
    if not isinstance(result, Exception) or not isinstance(protected_result, Exception):
        raise RuntimeError("Synthetic malformed-message negative control failed")
    if not protected_log_is_safe:
        raise RuntimeError("Synthetic MCP stdio privacy filter failed")
    print(json.dumps({"synthetic_only": True, "sdk_version": version("mcp"),
                      "tool_parameters_preserved": True,
                      "unknown_top_level_extension_preserved": roundtrip.get("futureExtension") == sample["futureExtension"],
                      "synthetic_invalid_input_appeared_in_unprotected_parse_log": unprotected_log_contains_input,
                      "desktop_stdio_filter_withheld_synthetic_input": protected_log_is_safe}, sort_keys=True))


if __name__ == "__main__":
    main()
