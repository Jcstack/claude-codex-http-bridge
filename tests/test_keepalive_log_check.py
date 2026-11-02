"""Do not reclassify unrelated test failures as a synthetic close race."""

import unittest

from tests.check_keepalive_baseline import summarize


class KeepaliveLogCheckTests(unittest.TestCase):
    def test_only_the_known_closed_connection_shape_is_accepted(self):
        name = "TestCodexWebsockets_KeepalivePingDuringUpload_NonstreamSessionless"
        good = f"--- FAIL: {name} (0.00s)\n  fixture: Execute() failed: use of closed network connection\nFAIL\n"
        report = summarize(good)
        self.assertEqual(report["fixture_failures"], 1)
        self.assertEqual(report["known_closed_connection_shapes"], 1)
        self.assertFalse(report["unexpected_or_fatal_failure"])
        self.assertTrue(summarize(good + "panic: PRIVATE_MESSAGE\n")["unexpected_or_fatal_failure"])
        self.assertTrue(summarize(good.replace("use of closed network connection", "timeout"))["unexpected_or_fatal_failure"])
        self.assertTrue(summarize(good + "--- FAIL: TestOther (0.00s)\n")["unexpected_or_fatal_failure"])
        self.assertTrue(summarize("compiler failure PRIVATE_MESSAGE")["unexpected_or_fatal_failure"])
        self.assertNotIn("PRIVATE_MESSAGE", str(summarize(good + "panic: PRIVATE_MESSAGE\n")))


if __name__ == "__main__":
    unittest.main()
