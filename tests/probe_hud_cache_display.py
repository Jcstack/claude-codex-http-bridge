"""Exercise reported cache-read display through the real HUD with synthetic frames.

The explicitly chosen plugin runs with a temporary home and filtered environment;
that is not an OS sandbox. No model, account, real transcript or user config is used.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

from claude_fixture import isolated_environment

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import statusline_adapter  # noqa: E402

ANSI = re.compile(r'\x1b\[[0-9;]*m')
LABEL = 'Reported cache-read input (main)'


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hud-dir', type=Path, required=True)
    args = parser.parse_args()
    hud = args.hud_dir.resolve()
    package = json.loads((hud / 'package.json').read_text(encoding='utf-8'))
    if package.get('name') != 'claude-hud' or package.get('version') != '0.15.0':
        raise RuntimeError('The fixture expects the pinned HUD 0.15.0 source')
    node = shutil.which('node')
    if not node: raise RuntimeError('Node is required')
    with tempfile.TemporaryDirectory(prefix='bridge-hud-cache-', dir=os.environ.get('RUNNER_TEMP')) as temporary:
        root = Path(temporary).resolve(); config = root / 'config'; plugin = config / 'plugins' / 'claude-hud'
        plugin.mkdir(parents=True)
        env = isolated_environment(root)
        env.update(CLAUDE_CONFIG_DIR=str(config), TMPDIR=str(root), TMP=str(root), TEMP=str(root))
        def render(frame: dict, enabled: bool = True) -> str:
            settings = {'gitStatus': {'enabled': False}, 'display': {'showPromptCache': enabled, 'showUsage': False}}
            (plugin / 'config.json').write_text(json.dumps(settings), encoding='utf-8')
            p = subprocess.run([node, str(hud / 'hud.mjs')], input=json.dumps(frame), cwd=root, env=env,
                               capture_output=True, text=True, encoding='utf-8', timeout=30, check=True)
            return ANSI.sub('', p.stdout)
        frame = {'session_id': 'synthetic', 'cwd': str(root),
                 'workspace': {'current_dir': str(root), 'project_dir': str(root)},
                 'model': {'id': 'gpt-fixture', 'display_name': 'GPT fixture'},
                 'context_window': {'current_usage': {'input_tokens': 1_000, 'cache_creation_input_tokens': 0,
                                                       'cache_read_input_tokens': 2_000}},
                 'prompt_cache': {'requests': 2, 'caching_observed': True, 'hit_ratio': .375,
                                  'ttl': '5m', 'warm': True, 'expires_at': int(time.time()) + 300}}
        windows = {'gpt-fixture': 872_000}
        baseline = render(statusline_adapter.corrected_payload(frame, windows))
        extended_frame = statusline_adapter.corrected_payload(frame, windows, expose_cache_metrics=True)
        extended = render(extended_frame)
        disabled = render(extended_frame, enabled=False)
        incomplete = dict(extended_frame)
        incomplete['claude_codex_bridge'] = {'reported_main_cache': {'requests': 2, 'caching_observed': True}}
        missing = render(incomplete)
        invalid = dict(extended_frame)
        invalid['claude_codex_bridge'] = {'reported_main_cache': {'requests': 2, 'caching_observed': True, 'hit_ratio': 1.1}}
        invalid_output = render(invalid)
        zero = dict(extended_frame)
        zero['claude_codex_bridge'] = {'reported_main_cache': {'requests': 1, 'caching_observed': False, 'hit_ratio': 0}}
        zero_output = render(zero)
        native = render(frame)
        checks = {'default_keeps_extension_hidden': LABEL not in baseline,
                  'opted_in_reports_token_fraction': LABEL in extended and '37.5%' in extended,
                  'no_inferred_expiry_or_warmth': all(x not in extended.lower() for x in ('expired', 'until', '5m')),
                  'plugin_setting_disables_display': LABEL not in disabled,
                  'missing_and_invalid_ratios_hidden': LABEL not in missing and LABEL not in invalid_output,
                  'zero_is_labeled_as_reported': LABEL in zero_output and '0.0%' in zero_output,
                  'native_cache_display_preserved': LABEL not in native and 'Cache' in native,
                  'model_remains_visible': all('GPT fixture' in x for x in (baseline, extended, native))}
        print(json.dumps({'synthetic_only': True, 'checks': checks}, sort_keys=True))
        if not all(checks.values()): raise RuntimeError('A synthetic HUD cache display check failed')


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        raise SystemExit('Synthetic HUD cache display failed; raw output withheld') from None
