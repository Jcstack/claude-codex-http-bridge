"""Classify only the known synthetic upstream WebSocket fixture failure."""

import json
from pathlib import Path
import re
import sys


NAMES = {"TestCodexWebsockets_KeepalivePingDuringUpload_WithSession",
         "TestCodexWebsockets_KeepalivePingDuringUpload_Sessionless",
         "TestCodexWebsockets_KeepalivePingDuringUpload_NonstreamSessionless"}


def summarize(text):
    lines = text.splitlines()
    failures = [(index, match.group(1)) for index, line in enumerate(lines)
                if (match := re.search(r"--- FAIL: (\S+)", line))]
    known = sum(name in NAMES and any("use of closed network connection" in line
                                      for line in lines[index + 1:index + 6])
                for index, name in failures)
    fatal = any("panic:" in line or "fatal error:" in line for line in lines)
    return {"fixture_failures": len(failures), "known_closed_connection_shapes": known,
            "unexpected_or_fatal_failure": fatal or not failures or known != len(failures)}


def main(path):
    report = summarize(Path(path).read_text(encoding="utf-8", errors="replace"))
    print(json.dumps(report, sort_keys=True))
    return int(report["unexpected_or_fatal_failure"])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
