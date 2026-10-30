"""Pack and verify source-locked runtime binaries; never handle credentials."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform

import install


SOURCE = Path(__file__).resolve().parent
UPSTREAM = "9717e325ba6ab2c7483ea93ccb5f2047eb2e7dd5"
PATCHES = ("12d97cf-inline-tools-backport.patch", "12d97cf-deferred-tools.patch", "web-search-replay.patch", "loopback-tls.patch")


def canonical(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def target() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    if system not in ("darwin", "linux", "windows") or arch not in ("arm64", "x86_64"):
        raise RuntimeError("Unsupported runtime platform")
    return system + "-" + arch


def source_lock() -> dict:
    saved = SOURCE / "runtime-source-lock.json"
    if saved.exists():
        return json.loads(saved.read_text())
    files = [SOURCE / "upstream" / "patches" / name for name in PATCHES]
    files += [SOURCE / "gateway" / name for name in ("go.mod", "main.go", "tls.go")]
    return {"upstream": UPSTREAM, "files": {str(path.relative_to(SOURCE)).replace(os.sep, "/"): install.file_hash(path) for path in files}}


def private_dir(path: Path) -> None:
    install.private_dir(path)
    if os.name == "nt":
        import windows_acl
        windows_acl.lock([path])


def safe_file(path: Path, *, private: bool = True, limit: int = 128 * 1024 * 1024) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise RuntimeError("Runtime file is missing, symlinked or too large")
    if private and os.name != "nt" and path.stat().st_mode & 0o077:
        raise RuntimeError("Runtime file permissions are too broad")
    if private and os.name == "nt":
        import windows_acl
        windows_acl.verify([path])


def verify(bundle: Path) -> tuple[dict, str]:
    if bundle.is_symlink() or not bundle.is_dir():
        raise RuntimeError("Runtime bundle must be a regular directory")
    manifest = bundle / "bundle.json"
    safe_file(manifest, private=False, limit=65536)
    value = json.loads(manifest.read_text())
    if (not isinstance(value, dict) or set(value) != {"format", "target", "source", "binaries"}
            or value["format"] != 1 or value["target"] != target() or value["source"] != source_lock()):
        raise RuntimeError("Runtime platform or source lock does not match this launcher")
    suffix = ".exe" if os.name == "nt" else ""
    expected = {"proxy": "cli-proxy-api" + suffix, "ingress": "bridge-ingress" + suffix}
    binaries = value["binaries"]
    if not isinstance(binaries, dict) or set(binaries) != set(expected):
        raise RuntimeError("Runtime binary list is invalid")
    for kind, filename in expected.items():
        row = binaries[kind]
        if not isinstance(row, dict) or set(row) != {"file", "sha256"} or row["file"] != filename:
            raise RuntimeError("Unexpected runtime binary path")
        safe_file(bundle / filename, private=False)
        if install.file_hash(bundle / filename) != row["sha256"]:
            raise RuntimeError("Runtime binary checksum mismatch")
    identity = hashlib.sha256(canonical(value)).hexdigest()
    return value, identity


def pack(proxy: Path, ingress: Path, output: Path) -> None:
    if output.exists() and any(output.iterdir()):
        raise RuntimeError("Bundle output must be empty")
    private_dir(output)
    suffix = ".exe" if os.name == "nt" else ""
    binaries = {}
    for kind, source, name in (("proxy", proxy, "cli-proxy-api"), ("ingress", ingress, "bridge-ingress")):
        safe_file(source, private=False)
        name += suffix
        install.write_file(output / name, source.read_bytes(), 0o700)
        binaries[kind] = {"file": name, "sha256": install.file_hash(output / name)}
    install.write_file(output / "bundle.json", canonical({"format": 1, "target": target(),
                       "source": source_lock(), "binaries": binaries}), 0o600)
    if os.name == "nt":
        import windows_acl
        windows_acl.lock(list(output.iterdir()))
    verify(output)


def activate(root: Path, source: Path) -> str:
    value, identity = verify(source)
    directory = root / "runtimes"
    private_dir(directory)
    destination = directory / identity
    private_dir(destination)
    for kind in ("proxy", "ingress"):
        name = value["binaries"][kind]["file"]
        path = destination / name
        if not path.exists():
            install.write_file(path, (source / name).read_bytes(), 0o700)
        safe_file(path)
    manifest = destination / "bundle.json"
    if not manifest.exists():
        install.write_file(manifest, canonical(value), 0o600)
    if os.name == "nt":
        import windows_acl
        windows_acl.lock(list(destination.iterdir()))
    if verify(destination)[1] != identity:
        raise RuntimeError("Installed runtime bundle changed unexpectedly")
    install.write_file(root / "runtime-selected.json", canonical({"bundle": identity}), 0o600)
    if os.name == "nt":
        windows_acl.lock([root / "runtime-selected.json"])
    return identity


def selected(root: Path) -> tuple[Path, dict, str] | None:
    path = root / "runtime-selected.json"
    if not path.exists(): return None
    safe_file(path, limit=65536)
    value = json.loads(path.read_text())
    identity = value.get("bundle") if isinstance(value, dict) else None
    if not isinstance(identity, str) or len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
        raise RuntimeError("Invalid selected runtime identity")
    if (root / "runtimes").is_symlink(): raise RuntimeError("Runtime directory must not be symlinked")
    directory = root / "runtimes" / identity
    manifest, actual = verify(directory)
    if actual != identity: raise RuntimeError("Selected runtime identity mismatch")
    return directory, manifest, identity


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy", required=True, type=Path)
    parser.add_argument("--ingress", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    pack(args.proxy.resolve(), args.ingress.resolve(), args.output.absolute())
    print("Packed source-locked runtime; no credential files were read")


if __name__ == "__main__":
    main()
