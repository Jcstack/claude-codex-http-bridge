"""Check the synthetic long-wait observer cannot be kept alive indefinitely."""

from io import BytesIO
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe_reasoning_retry_wait as probe  # noqa: E402


class SyntheticWaitTests(unittest.TestCase):
    def test_heartbeats_do_not_defeat_total_deadline(self):
        response = BytesIO(b": keepalive\n: keepalive\n")
        with self.assertRaises(TimeoutError):
            probe.collect_stream(response, time.monotonic() - 91)

    def test_counts_heartbeats_and_completion(self):
        response = BytesIO(b': keepalive\ndata: {"type":"message_stop"}\n')
        self.assertEqual(probe.collect_stream(response, time.monotonic()), (1, True))


if __name__ == "__main__":
    unittest.main()
