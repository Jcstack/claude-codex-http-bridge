"""Synthetic resume-cache observations must not expose inputs or invent hits."""

import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_dynamic_toolsearch import cache_resume_facts  # noqa: E402


def request(key, content, *, instructions="PRIVATE_INSTRUCTIONS", tools=None):
    body = {"prompt_cache_key": key, "instructions": instructions,
            "tools": tools if tools is not None else [{"name": "PRIVATE_TOOL"}], "input": content}
    return json.dumps(body, separators=(",", ":")).encode(), body


class ResumeCacheObservationTests(unittest.TestCase):
    def test_same_key_and_append_only_inputs_are_reported_without_contents(self):
        initial = [request("PRIVATE_KEY", ["PRIVATE_A"]), request("PRIVATE_KEY", ["PRIVATE_A", "PRIVATE_B"])]
        resumed = [request("PRIVATE_KEY", ["PRIVATE_A", "PRIVATE_B", "PRIVATE_C"])]
        report = cache_resume_facts(initial, resumed)
        self.assertTrue(all(report.values()))
        self.assertNotIn("PRIVATE", json.dumps(report))

    def test_changed_key_fields_or_history_are_not_treated_as_equal(self):
        initial = [request("PRIVATE_KEY", ["PRIVATE_A"]), request("PRIVATE_KEY", ["PRIVATE_A", "PRIVATE_B"])]
        resumed = [request("PRIVATE_OTHER", ["PRIVATE_A", "PRIVATE_C"], instructions="OTHER", tools=[])]
        report = cache_resume_facts(initial, resumed)
        self.assertFalse(report["cache_key_same_across_resume"])
        self.assertFalse(report["initial_and_resumed_tools_identical"])
        self.assertFalse(report["initial_and_resumed_instructions_identical"])
        self.assertTrue(report["initial_first_input_prefix_of_resumed"])
        self.assertFalse(report["initial_last_input_prefix_of_resumed"])
        self.assertTrue(report["provider_cache_not_tested"])
        self.assertNotIn("PRIVATE", json.dumps(report))

    def test_missing_key_or_requests_do_not_create_a_positive_observation(self):
        report = cache_resume_facts([request(None, [])], [request(None, [])])
        self.assertFalse(report["cache_key_same_across_resume"])
        self.assertFalse(report["initial_cache_key_present"])
        self.assertFalse(cache_resume_facts([], [request("PRIVATE_KEY", [])])["complete"])


if __name__ == "__main__":
    unittest.main()
