"""The synthetic validator audit fails closed without revealing local paths."""

from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parent / "validator-fixture" / "audit.py"
spec = importlib.util.spec_from_file_location("validator_dependency_audit", SOURCE)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class FakeResponse:
    def __init__(self, value):
        self.value = json.dumps(value).encode()

    def __enter__(self): return self
    def __exit__(self, *_args): return None
    def read(self, _size): return self.value


class ValidatorDependencyAuditTests(unittest.TestCase):
    def test_advisory_and_clean_responses_are_distinguished(self):
        modules = [("example.invalid/public-module", "v1.2.3")]
        for advisories, expected in (([], 0), ([{"id": "GO-SYNTHETIC-1"}], 1)):
            output = io.StringIO()
            response = FakeResponse({"results": [{"vulns": advisories}]})
            with patch.object(audit, "module_graph", return_value=modules), \
                 patch.object(audit.urllib.request, "urlopen", return_value=response), redirect_stdout(output):
                self.assertEqual(audit.main(), expected)
            report = json.loads(output.getvalue())
            self.assertEqual(report["public_modules_checked"], 1)
            self.assertEqual(len(report["advisories"]), len(advisories))

    def test_missing_or_malformed_results_never_report_clean(self):
        for body in ({"results": []}, {"results": [None]}, {"results": [{"vulns": [None]}]}):
            with self.subTest(body=body), patch.object(audit, "module_graph", return_value=[("example.invalid/public", "v1")]), \
                 patch.object(audit.urllib.request, "urlopen", return_value=FakeResponse(body)):
                with self.assertRaises(ValueError): audit.main()

    def test_module_graph_rejects_local_replacements_without_printing_paths(self):
        graph = '{"Main":true,"Path":"fixture"}\n' + json.dumps({
            "Path": "example.invalid/public", "Version": "v1", "Replace": {"Dir": "PRIVATE_SYNTHETIC_PATH"}})
        with patch.object(audit.subprocess, "run", return_value=SimpleNamespace(stdout=graph)):
            with self.assertRaisesRegex(ValueError, "Unsupported fixture module graph"):
                audit.module_graph()


if __name__ == "__main__":
    unittest.main()
