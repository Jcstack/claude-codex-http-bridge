"""Fixture-only, in-memory source loading for a synthetic quota watcher.

The caller supplies the reviewed manifest. This is not a production launcher,
an OS sandbox, a credential boundary, or an authorization to signal a process.
Only private marked temporary roots without an auth directory are accepted.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import quota_monitor_handoff
import quota_snapshot_fixture
import runtime_bundle


FRAME_LIMIT = 24 * 1024 * 1024


class MemoryFixtureUnavailable(RuntimeError):
    """A synthetic in-memory watcher cannot be prepared safely."""


# Constant bootstrap, passed as an interpreter argument by the fixture parent.
# -I -S excludes the working directory, PYTHONPATH, and site initialization.
# It never imports a project module before validating the complete frame.
BOOTSTRAP = r'''
import base64,hashlib,hmac,importlib.abc,importlib.util,json,os,sys
from pathlib import Path

names=("quota_recovery.py","accounts.py","api_profile.py","codex_usage.py",
       "launcher.py","model_helper.py","runtime_bundle.py","process_identity.py")
def pairs(values):
    result={}
    for key,value in values:
        if key in result: raise ValueError("duplicate field")
        result[key]=value
    return result
with os.fdopen(int(sys.argv[1]),"rb") as reader:
    frame=reader.read(24*1024*1024+1)
if len(frame)>24*1024*1024 or not hmac.compare_digest(hashlib.sha256(frame).hexdigest(),sys.argv[2]):
    raise SystemExit("fixture frame rejected")
record=json.loads(frame,object_pairs_hook=pairs)
if not isinstance(record,dict) or set(record)!={"version","manifest","files"} or record["version"]!=1:
    raise SystemExit("fixture frame rejected")
manifest,files=record["manifest"],record["files"]
if not isinstance(manifest,dict) or not isinstance(files,dict) or set(manifest)!=set(names) or set(files)!=set(names):
    raise SystemExit("fixture frame rejected")
modules={}
for name in names:
    if not isinstance(files[name],str) or not isinstance(manifest[name],str): raise SystemExit("fixture frame rejected")
    data=base64.b64decode(files[name],validate=True)
    if len(data)>2*1024*1024 or not hmac.compare_digest(hashlib.sha256(data).hexdigest(),manifest[name]):
        raise SystemExit("fixture frame rejected")
    modules[name[:-3]]=data
class MemoryLoader(importlib.abc.MetaPathFinder,importlib.abc.Loader):
    def find_spec(self,fullname,path=None,target=None):
        if fullname not in modules: return None
        return importlib.util.spec_from_loader(fullname,self,origin="fixture-memory")
    def create_module(self,spec): return None
    def exec_module(self,module):
        module.__file__="<fixture-memory:"+module.__name__+">"
        exec(compile(modules[module.__name__],module.__file__,"exec"),module.__dict__)
loader=MemoryLoader()
sys.meta_path.insert(0,loader)
root=Path(sys.argv[3])
if (root/"auth").exists() or (root/"auth").is_symlink(): raise SystemExit("fixture root rejected")
import quota_recovery
for name in ("quota_recovery","accounts","codex_usage","launcher","runtime_bundle"):
    if getattr(sys.modules.get(name),"__loader__",None) is not loader: raise SystemExit("fixture import rejected")
for name in modules:
    if name in sys.modules and getattr(sys.modules[name],"__loader__",None) is not loader:
        raise SystemExit("fixture import rejected")
raise SystemExit(quota_recovery.watch_wait(root))
'''


def prepare_fixture(root: Path, source: Path, expected: dict) -> tuple[bytes, str]:
    """Read verified fixture sources into a bounded frame, without publishing files."""
    if os.name != "posix":
        raise MemoryFixtureUnavailable("Memory fixture requires POSIX inherited descriptors")
    try:
        root, source = Path(root).absolute(), Path(source).absolute()
        quota_monitor_handoff._fixture_root(root)
        quota_snapshot_fixture._manifest(expected)
        contents = {}
        for name in quota_snapshot_fixture.FILES:
            data = quota_snapshot_fixture._read(source / name)
            if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected[name]):
                raise MemoryFixtureUnavailable("Fixture source differs from the reviewed manifest")
            contents[name] = base64.b64encode(data).decode("ascii")
        frame = runtime_bundle.canonical({"version": 1, "manifest": expected, "files": contents})
        if len(frame) > FRAME_LIMIT:
            raise MemoryFixtureUnavailable("Fixture frame exceeds its limit")
        return frame, hashlib.sha256(frame).hexdigest()
    except MemoryFixtureUnavailable:
        raise
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
        raise MemoryFixtureUnavailable("Memory fixture cannot be prepared") from None


def spawn_fixture(root: Path, frame: bytes, expected_digest: str) -> subprocess.Popen:
    """Start only an owned temporary child, without sending any signal."""
    if os.name != "posix":
        raise MemoryFixtureUnavailable("Memory fixture requires POSIX inherited descriptors")
    try:
        root = Path(root).absolute()
        quota_monitor_handoff._fixture_root(root)
        if not isinstance(frame, bytes) or len(frame) > FRAME_LIMIT:
            raise MemoryFixtureUnavailable("Fixture frame is invalid")
        if not isinstance(expected_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_digest):
            raise MemoryFixtureUnavailable("Fixture digest is invalid")
        environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(root),
                       "TMPDIR": str(root), "LANG": "C", "CODEX_HOME": str(root / "codex-fixture"),
                       "XDG_CONFIG_HOME": str(root / "xdg-fixture"), "CLAUDE_CODEX_BRIDGE_HOME": str(root)}
        with tempfile.TemporaryFile(dir=root) as handle:
            handle.write(frame)
            handle.flush()
            handle.seek(0)
            return subprocess.Popen([sys.executable, "-I", "-S", "-B", "-c", BOOTSTRAP,
                                     str(handle.fileno()), expected_digest, str(root)],
                                    cwd=root, env=environment, pass_fds=(handle.fileno(),),
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, start_new_session=True, bufsize=0)
    except MemoryFixtureUnavailable:
        raise
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        raise MemoryFixtureUnavailable("Memory fixture cannot be started") from None
