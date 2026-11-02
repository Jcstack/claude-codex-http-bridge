"""Check the standalone synthetic fixture's public Go module graph with OSV."""

import json
from pathlib import Path
import subprocess
import sys
import urllib.request


def module_graph():
    result = subprocess.run(["go", "list", "-m", "-json", "all"], cwd=Path(__file__).resolve().parent,
                            capture_output=True, text=True, timeout=30, check=True)
    decoder = json.JSONDecoder()
    offset, modules = 0, []
    while offset < len(result.stdout):
        while offset < len(result.stdout) and result.stdout[offset].isspace():
            offset += 1
        if offset == len(result.stdout): break
        value, offset = decoder.raw_decode(result.stdout, offset)
        if value.get("Main"): continue
        if value.get("Replace") is not None or not isinstance(value.get("Path"), str) or not isinstance(value.get("Version"), str):
            raise ValueError("Unsupported fixture module graph")
        modules.append((value["Path"], value["Version"]))
    if not modules or len(modules) > 100: raise ValueError("Invalid fixture module graph size")
    return modules


def main():
    modules = module_graph()
    query = {"queries": [{"package": {"ecosystem": "Go", "name": name}, "version": version.removeprefix("v")}
                         for name, version in modules]}
    request = urllib.request.Request("https://api.osv.dev/v1/querybatch", data=json.dumps(query).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = response.read(2 * 1024 * 1024 + 1)
    if len(payload) > 2 * 1024 * 1024: raise ValueError("OSV response too large")
    results = json.loads(payload).get("results")
    if not isinstance(results, list) or len(results) != len(modules): raise ValueError("Incomplete OSV response")
    findings = []
    for (name, version), row in zip(modules, results):
        if not isinstance(row, dict) or not isinstance(row.get("vulns", []), list):
            raise ValueError("Invalid OSV result")
        for advisory in row.get("vulns", []):
            if not isinstance(advisory, dict) or not isinstance(advisory.get("id"), str):
                raise ValueError("Invalid OSV advisory")
            findings.append({"module": name, "version": version, "id": advisory["id"]})
    print(json.dumps({"public_modules_checked": len(modules), "advisories": findings}, sort_keys=True))
    return 1 if findings else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        raise SystemExit("Synthetic validator dependency audit failed; raw details withheld") from None
