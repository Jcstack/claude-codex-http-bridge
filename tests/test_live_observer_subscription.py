"""Check the synthetic SSE assertion independently of a real account."""

import json
import unittest

from tests.live_observer_subscription import MAX_RESPONSE_BYTES, inspect_stream


def frame(value: dict) -> bytes:
    return ('data: ' + json.dumps(value) + '\n\n').encode()


class ObserverSubscriptionStreamTest(unittest.TestCase):
    def test_reassembles_split_text_and_requires_completion(self):
        parts = [frame({'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': text}})
                 for text in ('BRIDGE_', 'OBSERVER_', 'OK')]
        parts += [frame({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}}),
                  frame({'type': 'message_stop'})]
        self.assertEqual(inspect_stream(b''.join(parts)),
                         {'expected_output': True, 'message_stop': True, 'sse_error': False, 'end_turn': True})
        self.assertFalse(inspect_stream(b''.join(parts[:-1]))['message_stop'])

    def test_errors_and_limits_are_not_reported_as_success(self):
        self.assertTrue(inspect_stream(frame({'type': 'error'}))['sse_error'])
        with self.assertRaises(ValueError):
            inspect_stream(b'x' * (MAX_RESPONSE_BYTES + 1))
        with self.assertRaises(ValueError):
            inspect_stream(b'data: not-json\n\n')


if __name__ == '__main__':
    unittest.main()
