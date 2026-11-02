"""Verify that HUD Git is enabled only by the reviewed, opted-in combination.

Runs explicitly selected HUD code with fake files and a temporary home; this
is not an OS sandbox and does not use the user's project or credentials.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile

from claude_fixture import isolated_environment


ANSI = re.compile(r'\x1b\[[0-9;]*m')
GUARD_EXPORT = "export const BRIDGE_GIT_GUARD_VERSION = 'bridge-v1';\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hud-dir', type=Path, required=True)
    args = parser.parse_args()
    hud = args.hud_dir.resolve()
    package = json.loads((hud / 'package.json').read_text(encoding='utf-8'))
    if package.get('name') != 'claude-hud' or package.get('version') != '0.15.0':
        raise RuntimeError('The fixture expects the pinned HUD 0.15.0 source')
    node, git = shutil.which('node'), shutil.which('git')
    if not node or not git:
        raise RuntimeError('Node and Git are required')
    with tempfile.TemporaryDirectory(prefix='bridge-hud-guard-', dir=os.environ.get('RUNNER_TEMP')) as temporary:
        root = Path(temporary).resolve(); work = root / 'work'; work.mkdir()
        config = root / 'config'; plugin_config = config / 'plugins' / 'claude-hud'
        plugin_config.mkdir(parents=True)
        env = isolated_environment(root)
        env.update(CLAUDE_CONFIG_DIR=str(config), TMPDIR=str(root), TMP=str(root), TEMP=str(root),
                   GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0')
        global_config = root / 'empty-gitconfig'; global_config.write_text('', encoding='utf-8')
        env['GIT_CONFIG_GLOBAL'] = str(global_config)
        def run_git(*argv: str) -> None:
            subprocess.run([git, *argv], cwd=work, env=env, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=20, check=True)
        run_git('init', '-q', '-b', 'fixture-branch')
        run_git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                'commit', '-q', '--allow-empty', '-m', 'fixture')
        marker = root / 'fsmonitor-ran'
        hook = root / 'fixture-fsmonitor.cjs'
        hook.write_text("require('node:fs').writeFileSync(process.argv[2], 'fixture');\n", encoding='utf-8')
        quote = lambda value: shlex.quote(str(value).replace('\\', '/'))
        run_git('config', '--local', 'core.fsmonitor', f'{quote(node)} {quote(hook)} {quote(marker)}')
        frame = {'session_id': 'synthetic', 'cwd': str(work),
                 'workspace': {'current_dir': str(work), 'project_dir': str(work)},
                 'model': {'id': 'gpt-fixture', 'display_name': 'GPT fixture'},
                 'context_window': {'context_window_size': 872_000, 'used_percentage': 10,
                                    'current_usage': {'input_tokens': 87_000}}}
        def render(directory: Path, opted_in: bool, *, injected: bool = False) -> str:
            settings = {'lineLayout': 'expanded', 'gitStatus': {'enabled': False},
                        'claudeCodexGuardedGit': opted_in, 'display': {'showUsage': False, 'showProject': True}}
            (plugin_config / 'config.json').write_text(json.dumps(settings), encoding='utf-8')
            child_env = dict(env)
            if injected:
                child_env['CLAUDE_CODEX_HUD_GIT_GUARD'] = 'bridge-v1'
            result = subprocess.run([node, str(directory / 'hud.mjs')], input=json.dumps(frame), cwd=work,
                                    env=child_env, capture_output=True, text=True, encoding='utf-8', timeout=30, check=True)
            return ANSI.sub('', result.stdout)
        default_output = render(hud, False)
        guarded_output = render(hud, True)
        no_marker = root / 'hud-no-marker'
        shutil.copytree(hud, no_marker, ignore=shutil.ignore_patterns('.git', 'node_modules'))
        collector = no_marker / 'collector.mjs'; text = collector.read_text(encoding='utf-8')
        if text.count(GUARD_EXPORT) != 1:
            raise RuntimeError('The reviewed collector marker is not present exactly once')
        collector.write_text(text.replace(GUARD_EXPORT, '', 1), encoding='utf-8')
        missing_guard_output = render(no_marker, True, injected=True)
        guarded_avoided_hook = not marker.exists()
        run_git('status', '--porcelain')
        ordinary_git_control = marker.is_file()
        checks = {'disabled_without_opt_in': 'fixture-branch' not in default_output,
                  'enabled_with_reviewed_guard_and_opt_in': 'fixture-branch' in guarded_output,
                  'missing_guard_ignores_injected_environment': 'fixture-branch' not in missing_guard_output,
                  'guarded_hud_avoids_repo_command': guarded_avoided_hook,
                  'ordinary_git_executes_fixture_positive_control': ordinary_git_control,
                  'model_retained': all('GPT fixture' in item for item in (default_output, guarded_output, missing_guard_output)),
                  'no_error': all('[claude-hud]' not in item and 'Type /hud' not in item
                                  for item in (default_output, guarded_output, missing_guard_output))}
        print(json.dumps({'synthetic_only': True, 'checks': checks}, sort_keys=True))
        if not all(checks.values()):
            raise RuntimeError('A guarded HUD Git control failed')


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        raise SystemExit('Synthetic guarded HUD Git check failed; raw output withheld') from None
