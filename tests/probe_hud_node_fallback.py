"""Check a reviewed HUD's Node resolver using synthetic paths only."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


PROBE = r"""
import { pathToFileURL } from 'node:url';
import path from 'node:path';
const {resolvePosixNode} = await import(pathToFileURL(path.join(process.argv[1], 'hud.mjs')));
const absolute = path.join(path.parse(process.execPath).root, 'fixture-bin');
let seen = [];
const chosen = resolvePosixNode({PATH: ['relative-bin', absolute].join(path.delimiter)}, (candidate) => {
  seen.push(candidate);
  return candidate === path.join(absolute, 'node') ? '/synthetic-node' : null;
});
const absoluteFirst = chosen === '/synthetic-node' && seen.length === 1 && seen[0] === path.join(absolute, 'node');
seen = [];
const fallback = resolvePosixNode({PATH: 'relative-bin'}, (candidate) => {
  seen.push(candidate);
  return candidate === path.join('/opt/homebrew/bin', 'node') ? '/synthetic-fallback' : null;
});
const usesFallback = fallback === '/synthetic-fallback' && seen.length === 1;
const absent = resolvePosixNode({PATH: 'relative-bin'}, () => null) === null;
const ok = absoluteFirst && usesFallback && absent;
console.log(JSON.stringify({absoluteFirst, usesFallback, absent, syntheticOnly: true}));
if (!ok) process.exitCode = 1;
"""

RELAY_PROBE = r"""
import { pathToFileURL } from 'node:url';
import path from 'node:path';
Object.defineProperty(process.versions, 'bun', {value: 'fixture'});
const {main} = await import(pathToFileURL(path.join(process.argv[1], 'hud.mjs')));
await main({argv: ['fixture', 'fixture']});
"""

ARGV0_CAPTURE = r"""
if (process.env.BRIDGE_HUD_ARGV0_FIXTURE === '1' && !process.versions.bun) {
  process.stdout.write(JSON.stringify({argv0: process.argv0}));
  process.exit(0);
}
"""


def check_posix_relay_argv0(hud: Path, node: str, root: Path, env: dict[str, str]) -> None:
    """Exercise the Bun-to-Node relay with a resolved executable, never real HUD input."""
    overlay = root / 'hud-overlay'
    overlay.mkdir()
    (overlay / 'hud.mjs').write_text(
        ARGV0_CAPTURE + (hud / 'hud.mjs').read_text(encoding='utf-8'), encoding='utf-8'
    )
    shutil.copyfile(hud / 'collector.mjs', overlay / 'collector.mjs')
    bin_dir = root / 'fixture-bin'
    bin_dir.mkdir()
    (bin_dir / 'node').symlink_to(Path(node).resolve())
    relay_env = dict(env, PATH=str(bin_dir), BRIDGE_HUD_ARGV0_FIXTURE='1')
    result = subprocess.run([node, '--input-type=module', '-e', RELAY_PROBE, str(overlay)],
                            cwd=root, env=relay_env, capture_output=True, text=True,
                            encoding='utf-8', timeout=15, check=True)
    if json.loads(result.stdout) != {'argv0': 'node'}:
        raise RuntimeError('The Node relay did not preserve the shim process name')


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
        raise RuntimeError('Node is required')
    with tempfile.TemporaryDirectory(prefix='bridge-hud-node-', dir=os.environ.get('RUNNER_TEMP')) as temporary:
        root = Path(temporary).resolve()
        env = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'WINDIR', 'PATHEXT') if key in os.environ}
        env.update(HOME=str(root), USERPROFILE=str(root), TMPDIR=str(root), TMP=str(root), TEMP=str(root))
        result = subprocess.run([node, '--input-type=module', '-e', PROBE, str(hud)], cwd=root, env=env,
                                capture_output=True, text=True, encoding='utf-8', timeout=15, check=True)
        output = json.loads(result.stdout)
        if output != {'absoluteFirst': True, 'usesFallback': True, 'absent': True, 'syntheticOnly': True}:
            raise RuntimeError('Unexpected resolver result')
        if os.name != 'nt':
            check_posix_relay_argv0(hud, node, root, env)
        print(json.dumps({'synthetic_only': True, 'absolute_paths_only': True,
                          'system_fallback_selected': True, 'missing_node_is_reported': True,
                          'posix_shim_argv0': 'verified' if os.name != 'nt' else 'not_applicable'}))


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        raise SystemExit('Synthetic HUD Node resolution failed; raw output withheld') from None
