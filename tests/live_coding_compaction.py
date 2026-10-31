"""Verify a real coding/tool workflow after native compaction and model change.

Consumes subscription quota in an owned disposable workspace. No user project,
transcript, account, or running process is changed. This is a workflow gate,
not a model-quality benchmark or a comparison with an unavailable Claude model.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_client import NativeClient


SOURCE = '''from collections import OrderedDict
import time

class TTLCache:
    def __init__(self, max_entries, ttl, clock=time.monotonic):
        self.max_entries = max_entries
        self.ttl = ttl
        self.clock = clock
        self.items = OrderedDict()

    def put(self, key, value):
        self.items[key] = (value, self.clock() + self.ttl)
        if len(self.items) > self.max_entries:
            self.items.popitem(last=True)

    def get(self, key, default=None):
        value, deadline = self.items.get(key, (default, None))
        if deadline is not None and deadline < self.clock():
            del self.items[key]
            return default
        self.items.move_to_end(key)
        return value
'''

README = '''# Offline cache repair fixture

Repair cache.py using the Python standard library. Keep the public TTLCache
constructor, put(key, value), and get(key, default=None) API. This is an in-memory
LRU cache. Entries expire exactly at their deadline. Updating an existing key
refreshes its deadline and makes it most recently used. Expired entries must be
removed before a capacity eviction. A missing or expired key returns the exact
default object, and a miss does not change the order of valid entries.

Only edit cache.py. Do not edit this README or the public acceptance test. The
operator supplies additional requirements in the conversation. Do not use the
network, install dependencies, create agents, or access files outside this
disposable workspace. Run python -m unittest -v before reporting completion.
'''

PUBLIC_TEST = '''import unittest
from cache import TTLCache

class Smoke(unittest.TestCase):
    def test_store_and_expiry(self):
        now = [10.0]
        cache = TTLCache(2, 5, clock=lambda: now[0])
        cache.put("key", "value")
        self.assertEqual(cache.get("key"), "value")
        now[0] = 15
        self.assertIsNone(cache.get("key"))

    def test_missing_and_capacity(self):
        cache = TTLCache(1, 5, clock=lambda: 0)
        self.assertIsNone(cache.get("missing"))
        cache.put("a", 1); cache.put("b", 2)
        self.assertIsNone(cache.get("a"))
        self.assertEqual(cache.get("b"), 2)
'''

POLICY = '''Additional authoritative requirements, kept in this conversation:
Copy values defensively with deepcopy both when storing and returning them,
including nested Unicode data. Never copy a missing-key default. Capacity must
be a positive int and must reject bool. TTL must be a finite positive int or
float and must reject bool. Invalid capacity or TTL raises ValueError. Validate
every clock sample as finite; NaN or infinity raises ValueError before changing
the cache. Clamp valid backwards clock movement to the latest observed time so
an expired entry cannot reappear. A successful get updates LRU order without
extending TTL. No changes outside cache.py, no network, and no extra agents.
Do not implement yet. Acknowledge these requirements and retain them for the
next coding turn after compaction.'''

HIDDEN_TEST = '''import unittest
from cache import TTLCache

class Contract(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.cache = TTLCache(2, 10, clock=lambda: self.now[0])

    def test_exact_expiry(self):
        self.cache.put("a", 1); self.now[0] = 110
        self.assertIsNone(self.cache.get("a"))

    def test_read_does_not_extend_ttl(self):
        self.cache.put("a", 1); self.now[0] = 109
        self.assertEqual(self.cache.get("a"), 1)
        self.now[0] = 110; self.assertIsNone(self.cache.get("a"))

    def test_read_updates_lru(self):
        self.cache.put("a", 1); self.cache.put("b", 2)
        self.cache.get("a"); self.cache.put("c", 3)
        self.assertIsNone(self.cache.get("b")); self.assertEqual(self.cache.get("a"), 1)

    def test_missing_default_identity(self):
        default = {"nested": []}
        self.assertIs(self.cache.get("missing", default), default)

    def test_expired_default_identity(self):
        default = []
        self.cache.put("a", 1); self.now[0] = 110
        self.assertIs(self.cache.get("a", default), default)

    def test_miss_does_not_change_lru(self):
        self.cache.put("a", 1); self.cache.put("b", 2)
        self.cache.get("missing"); self.cache.put("c", 3)
        self.assertIsNone(self.cache.get("a")); self.assertEqual(self.cache.get("b"), 2)

    def test_replacement_refreshes_expiry(self):
        self.cache.put("a", 1); self.now[0] = 105
        self.cache.put("a", 2); self.now[0] = 110
        self.assertEqual(self.cache.get("a"), 2)
        self.now[0] = 115; self.assertIsNone(self.cache.get("a"))

    def test_replacement_updates_lru(self):
        self.cache.put("a", 1); self.cache.put("b", 2)
        self.cache.put("a", 3); self.cache.put("c", 4)
        self.assertIsNone(self.cache.get("b")); self.assertEqual(self.cache.get("a"), 3)

    def test_expired_entries_before_lru_eviction(self):
        self.cache.put("a", 1); self.now[0] = 105; self.cache.put("b", 2)
        self.cache.get("a"); self.now[0] = 111; self.cache.put("c", 3)
        self.assertEqual(self.cache.get("b"), 2); self.assertEqual(self.cache.get("c"), 3)

    def test_nested_input_is_copied(self):
        original = {"数据": [{"emoji": "🟣"}]}
        self.cache.put("unicode", original); original["数据"][0]["emoji"] = "wrong"
        self.assertEqual(self.cache.get("unicode"), {"数据": [{"emoji": "🟣"}]})

    def test_nested_output_is_copied(self):
        self.cache.put("unicode", {"data": ["🟣"]})
        self.cache.get("unicode")["data"].append("wrong")
        self.assertEqual(self.cache.get("unicode"), {"data": ["🟣"]})

    def test_backwards_clock_cannot_revive_entry(self):
        self.cache.put("a", 1); self.now[0] = 120
        self.cache.get("missing"); self.now[0] = 105
        self.assertIsNone(self.cache.get("a"))

    def test_invalid_capacity(self):
        for value in [0, -1, True, False, 1.5, "2", None]:
            with self.subTest(value=value), self.assertRaises(ValueError): TTLCache(value, 10)

    def test_invalid_ttl(self):
        for value in [0, -1, True, False, float("nan"), float("inf"), "2", None]:
            with self.subTest(value=value), self.assertRaises(ValueError): TTLCache(2, value)

    def test_fractional_ttl(self):
        cache = TTLCache(2, 0.5, clock=lambda: self.now[0])
        cache.put("a", 1); self.now[0] = 100.5
        self.assertIsNone(cache.get("a"))

    def test_invalid_clock_does_not_change_existing_data(self):
        self.cache.put("a", 1)
        for value in [float("nan"), float("inf")]:
            self.now[0] = value
            with self.assertRaises(ValueError): self.cache.put("b", 2)
        self.now[0] = 101
        self.assertEqual(self.cache.get("a"), 1); self.assertIsNone(self.cache.get("b"))
'''


def run_contract(work: Path, *, baseline=False) -> dict:
    # The assertions live outside the model's workspace until coding is over.
    script = HIDDEN_TEST + '''\nif __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Contract)
    result = unittest.TestResult(); suite.run(result)
    import json
    print(json.dumps({"tests":result.testsRun,"failures":len(result.failures),"errors":len(result.errors)}))
'''
    result = subprocess.run([sys.executable, "-c", script], cwd=work,
                            capture_output=True, text=True, timeout=20)
    if result.returncode: raise RuntimeError("Owned acceptance runner failed; raw output withheld")
    metric = json.loads(result.stdout)
    if not baseline and (metric["tests"] != 16 or metric["failures"] or metric["errors"]):
        raise RuntimeError("Coding continuation failed independent acceptance: " + json.dumps(metric))
    return metric


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--claude-bin", type=Path)
    parser.add_argument("--model", default="sol")
    parser.add_argument("--recall-model", default="terra")
    args = parser.parse_args(); root = args.root.resolve()
    before = NativeClient(root)
    origin = (root / "runtime/service.json").read_bytes()
    with tempfile.TemporaryDirectory(prefix="bridge-coding-compact-") as temporary:
        private = Path(temporary); private.chmod(0o700)
        work = private / "work"; work.mkdir(); config = private / "claude"; config.mkdir(mode=0o700)
        for name, text in {"cache.py":SOURCE, "README.md":README, "test_public.py":PUBLIC_TEST}.items():
            (work/name).write_text(text, encoding="utf-8")
        protected = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in work.iterdir() if p.name != "cache.py"}
        baseline = run_contract(work, baseline=True)
        if not baseline["failures"] and not baseline["errors"]:
            raise RuntimeError("Broken baseline unexpectedly passed independent acceptance")
        allowed = ("PATH","HOME","TMPDIR","LANG","SYSTEMROOT","COMSPEC","PATHEXT", "LOCALAPPDATA","APPDATA","USERPROFILE","TEMP","TMP")
        env = {key:os.environ[key] for key in allowed if key in os.environ}
        env.update(CLAUDE_CODEX_BRIDGE_HOME=str(root), CLAUDE_CONFIG_DIR=str(config),
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_TELEMETRY="1")
        if args.claude_bin: env["CLAUDE_CODEX_CLAUDE_BIN"] = str(args.claude_bin.resolve())
        sid = str(uuid.uuid4()); seen = set()
        stages = [
            ("inspect", "Read README.md and cache.py. Describe the repairs briefly; do not edit or run anything yet.", "Read"),
            ("policy", POLICY, ""),
            ("compact", "/compact Preserve the full coding task and every additional operator policy exactly, including validation, copying, clock rollback, LRU and file/network restrictions.", ""),
            ("implement", "Implement the agreed repair now, including every operator requirement from before compaction. Use ordinary local Read/Edit/Write/Bash tools. Run the public unittest suite with the available Python interpreter. Edit only cache.py. Do not use agents or the network.", "Read,Edit,Write,Bash"),
        ]
        for index, (stage, prompt, tools) in enumerate(stages):
            selected = args.recall_model if stage == "implement" else args.model
            command = [sys.executable, str(root/"launcher.py"), "--native-compaction", "--no-native-tools",
                "--model", selected, "--effort", "max", "--restricted", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--tools", tools, "--permission-mode", "dontAsk", "--max-turns", "20",
                "--output-format", "stream-json", "--verbose", "--session-id" if index == 0 else "--resume", sid]
            if tools: command.extend(["--allowedTools", *tools.split(",")])
            started = time.monotonic()
            result = subprocess.run([*command, "-p"], input=prompt, cwd=work, env=env,
                                    capture_output=True, text=True, timeout=240)
            if result.returncode or len(result.stdout) > 16 << 20:
                raise RuntimeError("Synthetic coding stage failed: " + stage + "; raw output withheld")
            events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
            final = next((event for event in reversed(events) if event.get("type") == "result"), {})
            if not final or final.get("is_error"): raise RuntimeError("Coding stage returned an error: " + stage)
            used = {block["name"] for event in events for block in event.get("message",{}).get("content",[])
                    if isinstance(block,dict) and block.get("type") == "tool_use" and block.get("name") in {"Read","Edit","Write","Bash"}}
            seen.update(used)
            print(json.dumps({"stage":stage,"model_alias":selected,"seconds":round(time.monotonic()-started,3),"tools":sorted(used)}),flush=True)
        native_carrier = any("codex-native-context-v1:" in p.read_text(encoding="utf-8") for p in (config/"projects").glob("*/*.jsonl"))
        if not native_carrier: raise RuntimeError("Coding fixture did not cross a native compact boundary")
        if not {"Read","Bash"}.issubset(seen) or not {"Edit","Write"}.intersection(seen):
            raise RuntimeError("The real client did not exercise the expected coding tools")
        for name, digest in protected.items():
            if hashlib.sha256((work/name).read_bytes()).hexdigest() != digest:
                raise RuntimeError("The coding fixture altered protected acceptance input")
        result = run_contract(work)
        after = NativeClient(root)
        if ((before.state["pid"],before.state["port"]) != (after.state["pid"],after.state["port"]) or
                origin != (root/"runtime/service.json").read_bytes()):
            raise RuntimeError("Production listener metadata changed during the owned fixture")
        print(json.dumps({"native_compaction":True,"independent_acceptance":result,
            "model_switch_requested":args.model != args.recall_model,"tools":sorted(seen),
            "origin_and_ingress_preserved":True,"quality_parity_claimed":False}),flush=True)


if __name__ == "__main__": main()
