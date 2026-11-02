"""TEST-ONLY tiny stdio adapter and fake MCP server; never use with real servers.

This intentionally exercises only short synthetic messages. It is not a
production transport: backpressure, framing limits and lifecycle are not
implemented to the standard required for user configurations.
"""

import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mcp_alias_policy import AliasPolicy, ProtocolError  # noqa: E402


NAMES = ["lookup.fixture", "lookup_fixture"]
UPDATED_DESCRIPTION = "Synthetic updated fixture description"


def fake_server(marker, dynamic=False, hold_refresh=False, change_description=False, initial_updated=False):
    changed = initial_updated
    for line in sys.stdin:
        message = json.loads(line)
        method, ident = message.get("method"), message.get("id")
        if ident is None:
            continue
        if method == "initialize":
            result = {"protocolVersion": message.get("params", {}).get("protocolVersion", "2025-11-25"),
                      "capabilities": {"tools": {"listChanged": True}},
                      "serverInfo": {"name": "fixture", "version": "1"}}
        elif method == "tools/list":
            names = (["lookup_fixture", "lookup.extra"] if changed and not change_description else NAMES)
            if changed:
                if hold_refresh:
                    deadline = time.monotonic() + 8
                    while not Path(marker + ".second-request").exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    if not Path(marker + ".second-request").exists():
                        Path(marker + ".hold-timeout").touch()
                Path(marker + ".refreshed").write_text("yes", encoding="utf-8")
            result = {"tools": [{"name": name, "description": UPDATED_DESCRIPTION
                                 if changed and change_description and name == "lookup.fixture" else "Synthetic fixture",
                                 "inputSchema": {"type": "object", "properties": {}}} for name in names]}
        elif method == "tools/call":
            name = message.get("params", {}).get("name")
            with open(marker, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(name) + "\n")
            result = {"content": [{"type": "text", "text": "OK"}]}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": ident,
                              "error": {"code": -32601, "message": "unknown"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}), flush=True)
        if dynamic and method == "tools/call" and name == "lookup.fixture" and not changed:
            changed = True
            print(json.dumps({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}), flush=True)


def fake_adapter(marker, dynamic=False, hold_refresh=False, change_description=False, initial_updated=False):
    policy, events = AliasPolicy("gateway"), queue.Queue()
    command = [sys.executable, __file__, "--fake-server", marker]
    if dynamic:
        command.append(("description-current" if initial_updated else "description-barrier" if hold_refresh else "description") if change_description
                       else ("dynamic-barrier" if hold_refresh else "dynamic"))
    child = subprocess.Popen(command,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, encoding="utf-8", bufsize=1)

    def read_lines(source, stream):
        try:
            for line in stream:
                events.put((source, line))
        finally:
            events.put((source, None))

    threading.Thread(target=read_lines, args=("client", sys.stdin), daemon=True).start()
    threading.Thread(target=read_lines, args=("server", child.stdout), daemon=True).start()
    try:
        while True:
            source, line = events.get()
            if line is None:
                break
            message = json.loads(line)
            destination, outgoing = (policy.client_message(message) if source == "client"
                                     else policy.server_message(message))
            if (dynamic and source == "client" and message.get("method") == "tools/call"
                    and Path(marker + ".forwarded").exists()
                    and isinstance(message.get("params"), dict)
                    and message["params"].get("name") not in policy.advertised):
                Path(marker + ".removed-call-received").touch()
                if destination == "client" and isinstance(outgoing.get("error"), dict):
                    Path(marker + ".removed-call-rejected").touch()
            if destination == "drop":
                continue
            stream = child.stdin if destination == "server" else sys.stdout
            stream.write(json.dumps(outgoing, ensure_ascii=True) + "\n")
            stream.flush()
            if (dynamic and source == "server" and destination == "client"
                    and isinstance(outgoing.get("result"), dict)
                    and isinstance(outgoing["result"].get("tools"), list)
                    and ((change_description and any(isinstance(tool, dict)
                         and tool.get("description") == UPDATED_DESCRIPTION for tool in outgoing["result"]["tools"]))
                         or (not change_description and "lookup.extra" in policy.advertised.values()))):
                # This proves only that the adapter wrote the changed list,
                # not that Claude has applied it to a subsequent request.
                Path(marker + ".forwarded").touch()
    except (BrokenPipeError, OSError, ProtocolError, ValueError):
        return 1
    finally:
        try:
            child.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=2)
    return 0


if __name__ == "__main__":
    valid = len(sys.argv) in (3, 4) and (len(sys.argv) == 3 or sys.argv[3] in (
        "dynamic", "dynamic-barrier", "description", "description-barrier", "description-current"))
    dynamic = len(sys.argv) == 4
    hold_refresh = dynamic and sys.argv[3].endswith("-barrier")
    change_description = dynamic and sys.argv[3].startswith("description")
    initial_updated = dynamic and sys.argv[3] == "description-current"
    if valid and sys.argv[1] == "--fake-server":
        fake_server(sys.argv[2], dynamic, hold_refresh, change_description, initial_updated)
    elif valid and sys.argv[1] == "--fake-adapter":
        raise SystemExit(fake_adapter(sys.argv[2], dynamic, hold_refresh, change_description, initial_updated))
    else:
        raise SystemExit("This fixture only runs its own synthetic server")
