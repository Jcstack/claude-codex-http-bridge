"""Probe a disposable observer backend against the existing subscription.

This sends one short synthetic model request and consumes subscription quota.
It never changes or stops the existing service. Only its localhost client key,
not OAuth credentials, is copied into a private temporary directory.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import ssl
import sys
import tempfile
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bridge_models
import diagnostics
import launcher
from live_support import existing_subscription
import rolling_runtime
import runtime_bundle


MARKER = 'BRIDGE_OBSERVER_OK'
MAX_RESPONSE_BYTES = 200_000


def inspect_stream(body: bytes) -> dict:
    """Inspect only a synthetic response and return fixed, non-content fields."""
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError('Synthetic response exceeded the limit')
    pieces = []
    stopped = False
    error = False
    stop_reason = None
    for line in body.decode('utf-8').splitlines():
        if not line.startswith('data: '):
            continue
        event = json.loads(line[6:])
        if not isinstance(event, dict):
            raise ValueError('Invalid synthetic event')
        event_type = event.get('type')
        stopped |= event_type == 'message_stop'
        error |= event_type == 'error'
        delta = event.get('delta')
        if isinstance(delta, dict):
            if delta.get('type') == 'text_delta' and isinstance(delta.get('text'), str):
                pieces.append(delta['text'])
            if event_type == 'message_delta':
                stop_reason = delta.get('stop_reason')
    return {'expected_output': ''.join(pieces).strip() == MARKER,
            'message_stop': stopped, 'sse_error': error,
            'end_turn': stop_reason == 'end_turn'}


def inspect_message(body: bytes) -> dict:
    """Inspect a bounded synthetic nonstream response without returning its contents."""
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError('Synthetic response exceeded the limit')
    value = json.loads(body)
    if not isinstance(value, dict) or not isinstance(value.get('content', []), list):
        raise ValueError('Invalid synthetic message')
    text = ''.join(block['text'] for block in value.get('content', [])
                   if isinstance(block, dict) and block.get('type') == 'text' and isinstance(block.get('text'), str))
    return {'expected_output': text.strip() == MARKER, 'message_complete': value.get('type') == 'message',
            'json_error': value.get('type') == 'error', 'end_turn': value.get('stop_reason') == 'end_turn'}


def run(proxy: Path, ingress: Path, installed: Path, *, nonstream: bool = False) -> dict:
    upstream = existing_subscription(installed)
    model = bridge_models.aliases(upstream['models'])['sol']
    models = {model: upstream['models'][model]}
    with tempfile.TemporaryDirectory(prefix='bridge-observer-subscription-') as temporary:
        root = Path(temporary).resolve()
        runtime_bundle.private_dir(root)
        bundle = root / 'bundle'
        runtime_bundle.pack(proxy, ingress, bundle)
        runtime_bundle.activate(root, bundle)
        with patch.object(launcher, 'ROOT', root), patch.dict(os.environ, {'CLAUDE_CODEX_STREAM_DIAGNOSTICS': '1'}):
            if rolling_runtime.process_environment(observe_streams=True).get('CLAUDE_CODEX_STREAM_DIAGNOSTICS') != '1':
                raise RuntimeError('The observer flag is not passed to the fixture')
            state = rolling_runtime.ensure(root, installed / 'runtime', upstream['state'], models)
            try:
                runtime = root / 'runtime-managed'
                key = rolling_runtime.private_key(runtime / 'front-key')
                context = ssl.create_default_context(cafile=str(runtime / 'ca.pem'))
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                     urllib.request.HTTPSHandler(context=context))
                payload = {'model': model, 'max_tokens': 1024, 'stream': not nonstream,
                           'output_config': {'effort': 'low'},
                           'messages': [{'role': 'user', 'content': f'Reply with exactly {MARKER}.'}]}
                request = urllib.request.Request(
                    f'https://127.0.0.1:{state["port"]}/v1/messages',
                    data=json.dumps(payload).encode(),
                    headers={'Authorization': 'Bearer ' + key, 'anthropic-version': '2023-06-01',
                             'content-type': 'application/json'})
                with opener.open(request, timeout=60) as response:
                    status = response.status
                    body = response.read(MAX_RESPONSE_BYTES + 1)
                result = inspect_message(body) if nonstream else inspect_stream(body)
                finished = result['message_complete'] if nonstream else result['message_stop']
                failed = result['json_error'] if nonstream else result['sse_error']
                if (status != 200 or not result['expected_output'] or not finished or
                        not result['end_turn'] or failed):
                    raise RuntimeError('Synthetic model response did not pass')
                log = runtime / state['current'] / 'proxy.log'
                completion_observed = any(
                    observation is not None and observation['outcome'] == 'completion_event' and
                    observation['phase'] in (('buffered',) if nonstream else ('stream', 'bootstrap'))
                    for observation in (diagnostics.stream_observation(line) for line in diagnostics.lines(log)))
                if not completion_observed:
                    raise RuntimeError('The owned observer did not record the healthy subscription control')
            finally:
                try:
                    rolling_runtime.stop(root)
                except Exception:
                    # Only this fixture's owned processes are eligible for
                    # cleanup, including when its own request timed out.
                    for row in state['generations'].values():
                        rolling_runtime.owned_stop(row)
                    rolling_runtime.owned_stop(state)
                    raise
    return {'synthetic_prompt': True, 'observer_requested': True, 'status_200': True,
            'completion_observed': completion_observed, 'existing_service_changed': False,
            'nonstream': nonstream, **result}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--proxy', type=Path, required=True)
    parser.add_argument('--ingress', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True, help='Existing private bridge installation')
    parser.add_argument('--nonstream', action='store_true', help='Verify buffered Messages execution')
    args = parser.parse_args()
    print(json.dumps(run(args.proxy.resolve(), args.ingress.resolve(), args.root.resolve(), nonstream=args.nonstream)))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Private subscription probe failed; raw details withheld') from None
