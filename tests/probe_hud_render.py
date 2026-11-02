"""Exercise a selected HUD with synthetic bridge status frames and a fake transcript.

The HUD code executes with a temporary home and filtered environment, not an
OS sandbox. No real sessions, model accounts, credentials, or prompts are used.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from claude_fixture import isolated_environment

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import statusline_adapter  # noqa: E402


ANSI = re.compile(r'\x1b\[[0-9;]*m')


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
        raise RuntimeError('Node and Git are required for this synthetic fixture')
    with tempfile.TemporaryDirectory(prefix='bridge-hud-render-', dir=os.environ.get('RUNNER_TEMP')) as temporary:
        root = Path(temporary).resolve()
        work, config = root / 'work', root / 'config'
        work.mkdir(); (config / 'plugins' / 'claude-hud').mkdir(parents=True)
        env = isolated_environment(root)
        env.update(CLAUDE_CONFIG_DIR=str(config), GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0',
                   TMPDIR=str(root), TMP=str(root), TEMP=str(root))
        global_config = root / 'empty-gitconfig'
        global_config.write_text('', encoding='utf-8')
        env['GIT_CONFIG_GLOBAL'] = str(global_config)
        settings = {'lineLayout': 'expanded', 'gitStatus': {'enabled': True}, 'display': {
            'contextValue': 'both', 'showModel': True, 'showProject': True, 'showContextBar': True,
            'showUsage': True, 'showTools': True, 'showTodos': True, 'showSkills': True,
            'showAgents': True, 'showMcp': True}}
        (config / 'plugins' / 'claude-hud' / 'config.json').write_text(json.dumps(settings), encoding='utf-8')
        def run_git(*argv: str) -> None:
            subprocess.run([git, *argv], cwd=work, env=env, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=20, check=True)
        run_git('init', '-q', '-b', 'fixture-branch')
        run_git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                'commit', '-q', '--allow-empty', '-m', 'fixture')
        timestamp = datetime.now(timezone.utc).isoformat()
        def tool(number: int, name: str, arguments: dict) -> dict:
            return {'type': 'assistant', 'timestamp': timestamp, 'isSidechain': False,
                    'message': {'id': f'fixture-{number}', 'role': 'assistant', 'model': 'gpt-fixture',
                                'content': [{'type': 'tool_use', 'id': f'tool-{number}',
                                             'name': name, 'input': arguments}]}}
        rows = [{'type': 'user', 'timestamp': timestamp, 'isSidechain': False,
                 'message': {'role': 'user', 'content': 'synthetic fixture'}},
                tool(1, 'Read', {'file_path': str(work / 'fixture.txt')}),
                tool(2, 'TodoWrite', {'todos': [{'content': 'fixture-todo', 'status': 'in_progress',
                                                'activeForm': 'fixture-todo'}]}),
                tool(3, 'Skill', {'skill': 'fixture-skill'}),
                tool(4, 'Agent', {'subagent_type': 'Explore', 'description': 'fixture-agent', 'prompt': 'synthetic'}),
                tool(5, 'mcp__fixture__search', {'query': 'synthetic'})]
        transcript = root / 'fixture.jsonl'
        transcript.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        frame = {'session_id': 'synthetic', 'transcript_path': str(transcript), 'cwd': str(work),
                 'workspace': {'current_dir': str(work), 'project_dir': str(work)},
                 'model': {'id': 'gpt-fixture', 'display_name': 'GPT fixture'},
                 'context_window': {'current_usage': {'input_tokens': 5_000, 'cache_creation_input_tokens': 0,
                                                       'cache_read_input_tokens': 82_000}},
                 'prompt_cache': {'ttl': '5m', 'warm': True}}
        quota = {'seven_day': {'used_percentage': 30, 'resets_at': 1_893_456_000}}
        def render(payload: dict) -> str:
            corrected = statusline_adapter.corrected_payload(payload, {'gpt-fixture': 872_000}, quota)
            if 'prompt_cache' in corrected:
                raise RuntimeError('The status adapter forwarded an unsupported cache timer')
            result = subprocess.run([node, str(hud / 'hud.mjs')], input=json.dumps(corrected), cwd=work, env=env,
                                    capture_output=True, text=True, timeout=30, check=True)
            return ANSI.sub('', result.stdout)
        before = render(frame)
        with transcript.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps({'type': 'system', 'subtype': 'compact_boundary', 'timestamp': timestamp,
                                     'compactMetadata': {'postTokens': 51_271}}) + '\n')
        frame['context_window'] = {'current_usage': None}
        after = render(frame)
        checks = {'model': 'GPT fixture' in before, 'context': bool(re.search(r'10%\s*\(87k/872k\)', before)),
                  'weekly_usage': bool(re.search(r'(?:week|7d).{0,30}30\s*%', before, re.I)),
                  'branch': 'fixture-branch' in before, 'tool': 'Read' in before,
                  'todo': 'fixture-todo' in before, 'skill': 'fixture-skill' in before,
                  'agent': 'fixture-agent' in before, 'mcp': 'mcp__fixture__search' in before,
                  'compact_hint': bool(re.search(r'6%\s*\(51k/872k\)', after)),
                  'no_error': all('[claude-hud]' not in value and 'Type /hud' not in value for value in (before, after))}
        print(json.dumps({'synthetic_only': True, 'hud_version': '0.15.0', 'checks': checks}, sort_keys=True))
        if not all(checks.values()):
            raise RuntimeError('A synthetic HUD display check failed')


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        raise SystemExit('Synthetic HUD render failed; raw output withheld') from None
