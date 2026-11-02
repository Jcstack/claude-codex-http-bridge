"""Run the pinned HUD's security fixture with a temporary home and no credentials.

The explicitly selected HUD JavaScript runs as the current user: temporary HOME
and environment filtering are not an OS sandbox. Use only reviewed source.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


CONFIG_FAILURE = r"""
import { createRequire, syncBuiltinESMExports } from 'node:module';
import { pathToFileURL } from 'node:url';
import path from 'node:path';
const require = createRequire(import.meta.url);
const child = require('node:child_process');
const calls = [];
child.execFile = (_file, args, _options, callback) => {
  calls.push(args);
  if (args.includes('config')) {
    const error = new Error('synthetic enumeration failure'); error.code = 128;
    queueMicrotask(() => callback(error, '', ''));
  } else {
    queueMicrotask(() => callback(null, 'synthetic status', ''));
  }
  return {};
};
syncBuiltinESMExports();
// Exercise the same Node callback failure path on every test host. The separate
// upstream fixture invokes real Git on the native host.
Object.defineProperty(process, 'platform', {value: 'linux'});
const {createProbes} = await import(pathToFileURL(path.join(process.argv[1], 'collector.mjs')));
const replies = await createProbes({cwd: process.argv[2], argv: []})(['git.status', 'git.numstat']);
const blocked = calls.length === 1 && calls[0].includes('config') &&
  replies['git.status']?.error === 'failed' && replies['git.numstat']?.error === 'failed';
console.log(JSON.stringify({failed_config_blocks_git: blocked, mock_only: true}));
if (!blocked) process.exitCode = 1;
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hud-dir', type=Path, required=True)
    args = parser.parse_args()
    hud = args.hud_dir.resolve()
    package = json.loads((hud / 'package.json').read_text(encoding='utf-8'))
    if package.get('name') != 'claude-hud' or package.get('version') != '0.15.0':
        raise RuntimeError('The fixture expects the pinned HUD 0.15.0 source')
    node = shutil.which('node')
    if not node:
        raise RuntimeError('Node 22.18 or newer is required')
    with tempfile.TemporaryDirectory(prefix='bridge-hud-security-', dir=os.environ.get('RUNNER_TEMP')) as temporary:
        home = Path(temporary).resolve()
        env = {name: os.environ[name] for name in ('PATH', 'SYSTEMROOT', 'WINDIR', 'PATHEXT', 'COMSPEC')
               if name in os.environ}
        env.update(HOME=str(home), USERPROFILE=str(home), TMPDIR=str(home), TMP=str(home), TEMP=str(home),
                   LANG='en_US.UTF-8', TZ='UTC', GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0')
        config = home / 'empty-gitconfig'
        config.write_text('', encoding='utf-8')
        env['GIT_CONFIG_GLOBAL'] = str(config)
        owner = subprocess.run([node, str(hud / 'verify' / 'owner.check.mjs')], cwd=home, env=env,
                               capture_output=True, text=True, timeout=120)
        failure = subprocess.run([node, '--input-type=module', '-e', CONFIG_FAILURE, str(hud), str(home)],
                                 cwd=home, env=env, capture_output=True, text=True, timeout=20)
        print(json.dumps({'synthetic_only': True, 'hud_version': '0.15.0',
                          'native_owner_security_fixture_passed': owner.returncode == 0,
                          'config_failure_blocks_git_mock_passed': failure.returncode == 0,
                          'owner_failure_count': sum(line.startswith('FAIL ') and not re.match(r'FAIL \d+/\d+', line)
                                                     for line in owner.stdout.splitlines())},
                         sort_keys=True))
        if owner.returncode or failure.returncode:
            raise RuntimeError('HUD safety fixture failed; raw output withheld')


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        raise SystemExit('HUD security observation failed; raw output withheld') from None
