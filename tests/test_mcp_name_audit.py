"""Offline MCP-name audit checks; fixture data is synthetic."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

import mcp_name_audit


class MCPNameAuditTest(unittest.TestCase):
    def test_dotted_tool_and_server_name_collisions(self):
        names = {"gateway.a", "gateway_a", "same"}
        rows = [("same", "lookup.fixture"), ("same", "lookup_fixture"),
                ("gateway.a", "query"), ("gateway_a", "query")]
        result = mcp_name_audit.analyze(names, rows, show_names=True)
        self.assertEqual(result["possible_server_name_collision_groups"], 1)
        self.assertEqual(result["possible_tool_alias_collision_groups"], 2)
        self.assertFalse(result["complete_compatibility_proven"])
        self.assertFalse(result["mcp_servers_contacted"])
        self.assertEqual({row["alias"] for row in result["tool_collisions"]},
                         {"mcp__same__lookup_fixture", "mcp__gateway_a__query"})

    def test_full_alias_collisions_cross_separator_boundaries(self):
        rows = [("a__b", "c"), ("a", "b__c")]
        result = mcp_name_audit.analyze({"a__b", "a"}, rows)
        self.assertEqual(result["possible_server_name_collision_groups"], 0)
        self.assertEqual(result["possible_tool_alias_collision_groups"], 1)

    def test_exact_duplicates_are_not_distinct_originals(self):
        result = mcp_name_audit.analyze({"same"}, [("same", "tool"), ("same", "tool")])
        self.assertEqual(result["tool_names_inspected"], 2)
        self.assertEqual(result["possible_tool_alias_collision_groups"], 0)

    def test_non_ascii_and_overlong_names_are_not_silently_certified(self):
        result = mcp_name_audit.analyze({"非ascii", "safe"},
            [("safe", "动作"), ("safe", "x" * 140)])
        self.assertEqual(result["unverified_server_names"], 1)
        self.assertEqual(result["unverified_tool_names"], 1)
        self.assertEqual(result["over_128_character_tool_aliases"], 1)
        self.assertFalse(result["complete_compatibility_proven"])

    def test_cli_withholds_config_secrets_and_names_by_default(self):
        with tempfile.TemporaryDirectory(prefix="bridge-mcp-audit-") as temporary:
            root = Path(temporary)
            config = root / "config.json"
            inventory = root / "inventory.json"
            config.write_text(json.dumps({"mcpServers": {"sensitive.server": {"env": {
                "TOKEN": "PRIVATE_SENTINEL"}}, "sensitive_server": {"url": "PRIVATE_ENDPOINT"}}}), encoding="utf-8")
            inventory.write_text(json.dumps({"servers": [{"name": "sensitive.server", "tools": [
                {"name": "private.action", "description": "PRIVATE_DESCRIPTION"}, "private_action"]}]}), encoding="utf-8")
            stdout, stderr = StringIO(), StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = mcp_name_audit.main(["--config", str(config), "--inventory", str(inventory)])
            self.assertEqual(code, 0)
            output = stdout.getvalue() + stderr.getvalue()
            for value in ("PRIVATE_SENTINEL", "PRIVATE_ENDPOINT", "PRIVATE_DESCRIPTION", "sensitive.server", "private.action", str(root)):
                self.assertNotIn(value, output)
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["possible_server_name_collision_groups"], 1)
            self.assertEqual(result["possible_tool_alias_collision_groups"], 1)
            self.assertTrue(result["tool_inventory_supplied"])

    def test_invalid_explicit_input_does_not_expose_content(self):
        with tempfile.TemporaryDirectory(prefix="bridge-mcp-audit-") as temporary:
            config = Path(temporary) / "invalid.json"
            config.write_text('PRIVATE_SENTINEL {', encoding="utf-8")
            stdout, stderr = StringIO(), StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = mcp_name_audit.main(["--config", str(config)])
            self.assertEqual(code, 1)
            self.assertNotIn("PRIVATE_SENTINEL", stdout.getvalue() + stderr.getvalue())
            self.assertNotIn(str(config), stdout.getvalue() + stderr.getvalue())

    def test_absent_inventory_and_empty_servers_are_explicit(self):
        with tempfile.TemporaryDirectory(prefix="bridge-mcp-audit-") as temporary:
            root = Path(temporary)
            config, inventory = root / "config.json", root / "inventory.json"
            config.write_text('{"mcpServers": {}}', encoding="utf-8")
            inventory.write_text('{"servers": [{"name": "a.b", "tools": []}, {"name": "a_b", "tools": []}]}', encoding="utf-8")
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(mcp_name_audit.main(["--config", str(config)]), 0)
            self.assertFalse(json.loads(stdout.getvalue())["tool_inventory_supplied"])
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(mcp_name_audit.main(["--config", str(config), "--inventory", str(inventory)]), 0)
            self.assertEqual(json.loads(stdout.getvalue())["possible_server_name_collision_groups"], 1)


if __name__ == "__main__":
    unittest.main()
