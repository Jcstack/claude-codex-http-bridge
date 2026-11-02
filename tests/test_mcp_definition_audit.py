"""Privacy and uncertainty controls for the offline definition comparison."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import mcp_definition_audit as audit
from tests.claude_fixture import isolated_environment


NAME = "mcp__fixture__lookup"
PRIVATE = "PRIVATE_SYNTHETIC_SENTINEL"


def tool(description=PRIVATE, schema=None, name=NAME):
    return {"name": name, "description": description,
            "input_schema": schema if schema is not None else {"type": "object", "properties": {}}}


def record(entries):
    return {"type": "attachment", "attachment": {"type": "deferred_tools_record", "entries": entries}}


class DefinitionAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="definition-audit-test-")
        self.root = Path(self.temporary.name)
        self.session = self.root / "synthetic.jsonl"
        self.inventory = self.root / "inventory.json"
        self.addCleanup(self.temporary.cleanup)

    def write(self, events, tools):
        self.session.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        self.inventory.write_text(json.dumps({"tools": tools}), encoding="utf-8")

    def run_audit(self, *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = audit.main(["--session-file", str(self.session), "--inventory", str(self.inventory), *extra])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_changed_description_is_counted_without_private_contents(self):
        self.write([record([tool()])], [tool("SYNTHETIC_CHANGED")])
        original = self.session.read_bytes()
        code, stdout, stderr = self.run_audit()
        report = json.loads(stdout)
        self.assertEqual(code, 0)
        self.assertEqual(report["description_differences"], 1)
        self.assertEqual(report["result"], "differences_found")
        self.assertEqual(report["input_schema_differences"], 0)
        self.assertFalse(report["complete_compatibility_proven"])
        self.assertFalse(report["mcp_server_contacted"])
        self.assertNotIn(PRIVATE, stdout + stderr)
        self.assertNotIn(NAME, stdout + stderr)
        self.assertEqual(self.session.read_bytes(), original)

    def test_schema_key_order_is_ignored_but_value_change_is_detected(self):
        old_schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        same_schema = {"properties": {"x": {"type": "string"}}, "type": "object"}
        self.write([record([tool(schema=old_schema)])], [tool(schema=same_schema)])
        self.assertEqual(json.loads(self.run_audit()[1])["matching_compared_fields"], 1)
        self.write([record([tool(schema=old_schema)])], [tool(schema={"type": "array"})])
        report = json.loads(self.run_audit("--show-names")[1])
        self.assertEqual(report["input_schema_differences"], 1)
        self.assertEqual(report["changed_names"], [NAME])
        self.assertNotIn(PRIVATE, json.dumps(report))

    def test_ambiguous_repeated_names_never_compare_as_safe(self):
        self.write([record([tool("A")]), record([tool("B")])], [tool("B")])
        report = json.loads(self.run_audit()[1])
        self.assertEqual(report["ambiguous_names"], 1)
        self.assertEqual(report["compared_names"], 0)
        self.assertEqual(report["result"], "no_comparable_definitions")
        self.write([record([tool("A")])], [tool("A"), tool("A")])
        self.assertEqual(json.loads(self.run_audit()[1])["ambiguous_names"], 1)

    def test_non_attachment_and_other_tools_are_not_treated_as_matches(self):
        forged = {"type": "user", "attachment": {"type": "deferred_tools_record", "entries": [tool()]}}
        self.write([forged, record([tool(name="Read"), tool()])], [tool()])
        report = json.loads(self.run_audit()[1])
        self.assertEqual(report["deferred_records"], 1)
        self.assertEqual(report["unrecognized_retained_entries"], 1)
        self.assertEqual(report["matching_compared_fields"], 1)

    def test_absent_and_incomplete_fields_remain_unknown(self):
        self.write([record([tool(), {"name": "mcp__fixture__incomplete"}])],
                   [{"name": "mcp__fixture__incomplete"}])
        report = json.loads(self.run_audit()[1])
        self.assertEqual(report["retained_missing_from_snapshot"], 1)
        self.assertEqual(report["incomplete_definitions"], 1)
        self.assertEqual(report["compared_names"], 0)
        self.assertEqual(report["result"], "no_comparable_definitions")

    def test_duplicate_keys_and_incomplete_session_are_rejected_privately(self):
        self.write([record([tool()])], [tool()])
        self.inventory.write_text('{"tools":[],"tools":["' + PRIVATE + '"]}', encoding="utf-8")
        code, stdout, stderr = self.run_audit()
        self.assertEqual(code, 1)
        self.assertNotIn(PRIVATE, stdout + stderr)
        self.write([record([tool()])], [tool()])
        self.session.write_bytes(self.session.read_bytes().rstrip(b"\n"))
        self.assertEqual(self.run_audit()[0], 1)

    def test_symlink_file_is_refused(self):
        self.write([record([tool()])], [tool()])
        link = self.root / "link.jsonl"
        try:
            link.symlink_to(self.session)
        except OSError:
            self.skipTest("Symlinks unavailable")
        self.session = link
        self.assertEqual(self.run_audit()[0], 1)

    def test_changed_open_file_is_detected(self):
        self.write([record([tool()])], [tool()])
        handle, before = audit.open_regular(self.inventory, audit.MAX_INVENTORY_BYTES)
        with handle:
            self.inventory.write_text('{"tools": []}', encoding="utf-8")
            with self.assertRaises(ValueError):
                audit.unchanged(handle, before)

    def test_real_launcher_dispatch_reads_only_synthetic_inputs(self):
        self.write([record([tool()])], [tool("SYNTHETIC_CHANGED")])
        bridge = self.root / "unused-bridge"
        env = isolated_environment(self.root)
        env["CLAUDE_CODEX_BRIDGE_HOME"] = str(bridge)
        launcher = Path(__file__).resolve().parents[1] / "launcher.py"
        result = subprocess.run([sys.executable, str(launcher), "mcp-definitions", "--session-file", str(self.session),
                                 "--inventory", str(self.inventory)], cwd=self.root, env=env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["description_differences"], 1)
        self.assertNotIn(PRIVATE, result.stdout + result.stderr)
        self.assertNotIn(NAME, result.stdout + result.stderr)
        self.assertFalse(bridge.exists())


if __name__ == "__main__":
    unittest.main()
