"""Keep a TLS loopback endpoint stable while reviewed translators are replaced.

The legacy authenticated service stays alive. New translators receive only a
separate local guard key; only the ingress can read the legacy localhost key.
No OAuth file or OpenAI project key is copied or changed.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import signal
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

import api_profile
import launcher
import runtime_bundle


GENERATION = re.compile(r"g-[a-f0-9]{24}\Z")


def directory(root: Path) -> Path:
    path = root / "runtime-managed"
    runtime_bundle.private_dir(path)
    return path


def read(path: Path) -> dict:
    runtime_bundle.safe_file(path, limit=262144)
    value = json.loads(path.read_text())
    if not isinstance(value, dict): raise RuntimeError("Invalid private runtime metadata")
    return value


def write(path: Path, value: dict) -> None:
    api_profile.write_private(path, runtime_bundle.canonical(value).decode())


def private_key(path: Path) -> str:
    runtime_bundle.safe_file(path, limit=4096)
    key = path.read_text().strip()
    if not 32 <= len(key) <= 512 or any(not 33 <= ord(c) <= 126 for c in key):
        raise RuntimeError("Invalid private localhost key")
    return key


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def state(root: Path) -> dict | None:
    path = root / "runtime-managed" / "service.json"
    if not path.exists(): return None
    value = read(path)
    if set(value) != {"pid", "port", "ingress_bundle", "current", "generations"}:
        raise RuntimeError("Invalid managed runtime state")
    if any(type(value.get(k)) is not int or value[k] < 1 for k in ("pid", "port")) or value["port"] > 65535:
        raise RuntimeError("Invalid managed listener identity")
    if not re.fullmatch(r"[a-f0-9]{64}", str(value["ingress_bundle"])):
        raise RuntimeError("Invalid managed ingress identity")
    if not isinstance(value["generations"], dict) or value["current"] not in value["generations"]:
        raise RuntimeError("Invalid managed generation list")
    for generation, row in value["generations"].items():
        if not GENERATION.fullmatch(generation) or not isinstance(row, dict) or set(row) != {"pid", "port", "fingerprint"}:
            raise RuntimeError("Invalid managed generation")
        if (type(row["pid"]) is not int or row["pid"] < 1 or type(row["port"]) is not int or not 1 <= row["port"] <= 65535
                or not re.fullmatch(r"[a-f0-9]{64}", str(row["fingerprint"]))):
            raise RuntimeError("Invalid managed backend identity")
    return value


def endpoint_status(root: Path, value: dict) -> dict:
    if launcher.listener_pids(value["port"]) != {value["pid"]}:
        raise RuntimeError("Managed ingress is unavailable; no key was sent")
    runtime = root / "runtime-managed"
    return get_json(runtime, value["port"], "/bridge/status", private_key(runtime / "front-key"))


def get_json(runtime: Path, port: int, path: str, key: str) -> dict:
    context = ssl.create_default_context(cafile=str(runtime / "ca.pem"))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=context), api_profile.NoRedirect())
    request = urllib.request.Request(f"https://127.0.0.1:{port}" + path, headers={"Authorization": "Bearer " + key})
    try:
        with opener.open(request, timeout=10) as response:
            data = response.read(4 * 1024 * 1024 + 1)
        if len(data) > 4 * 1024 * 1024: raise RuntimeError("Managed catalog exceeds metadata limit")
        value = json.loads(data)
        if not isinstance(value, dict): raise RuntimeError("Managed endpoint returned invalid metadata")
        return value
    except (urllib.error.URLError, ValueError, TimeoutError):
        raise RuntimeError("Managed endpoint readiness check failed; raw transport details withheld") from None


def start(binary: Path, arguments: list[str], log: Path, *, ca: Path | None = None, authority: str | None = None) -> subprocess.Popen:
    runtime_bundle.safe_file(binary)
    environment = os.environ.copy()
    if ca is not None:
        if authority is None: raise RuntimeError("Private loopback TLS authority is required")
        environment["CLAUDE_CODEX_LOOPBACK_CA"] = str(ca)
        environment["CLAUDE_CODEX_LOOPBACK_AUTHORITY"] = authority
    environment["NO_PROXY"] = "127.0.0.1,localhost"
    environment["no_proxy"] = "127.0.0.1,localhost"
    detached = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
                if os.name == "nt" else {"start_new_session": True})
    if log.is_symlink(): raise RuntimeError("Runtime log must not be a symlink")
    descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    if os.name == "nt":
        import windows_acl
        windows_acl.lock([log])
    with os.fdopen(descriptor, "ab") as handle:
        return subprocess.Popen([str(binary), *arguments], stdin=subprocess.DEVNULL, stdout=handle,
                                stderr=subprocess.STDOUT, env=environment, cwd=log.parent, **detached)


def origin_manifest(root: Path, runtime: Path, base_runtime: Path, base: dict) -> None:
    if not launcher.state_is_live(base_runtime, base):
        raise RuntimeError("Existing Codex service is unavailable; no key was copied")
    key_path = launcher.state_paths(base_runtime, base)[2]
    key = private_key(key_path)
    # A fresh immutable identity also avoids reusing a gateway generation when
    # the original listener or its local key changes.
    identity = hashlib.sha256(f'{base["pid"]}:{base["port"]}:{key}'.encode()).hexdigest()[:24]
    key_copy = runtime / ("origin-" + identity + "-key")
    if not key_copy.exists(): api_profile.write_private(key_copy, key + "\n")
    write(runtime / "origin.json", {"generation": "origin-" + identity, "pid": base["pid"],
                                   "port": base["port"], "key_file": str(key_copy)})


def backend_config(runtime: Path, port: int, front_port: int, models: dict, key: str) -> str:
    model_rows = []
    for model, spec in sorted(models.items()):
        if not re.fullmatch(r"gpt-[a-z0-9.-]+", model): raise RuntimeError("Invalid catalog model ID")
        levels = [item["effort"] for item in spec["supported_reasoning_levels"]]
        model_rows.append(f'        - name: {json.dumps(model)}\n          max-context-length: {int(spec["context_window"])}\n'
                          f'          thinking:\n            levels: {json.dumps(levels)}')
    return f'''config-version: 8
server:
  host: "127.0.0.1"
  port: {port}
  max-request-body-bytes: 67108864
management:
  allow-remote: false
  secret-key: "{secrets.token_hex(32)}"
  disable-control-panel: true
  disable-auto-update-panel: true
access:
  api-keys:
    - {json.dumps(key)}
oauth:
  auth-dir: {json.dumps(str(runtime / "empty-auth"))}
routing:
  retry:
    request-retry: 0
  cooldown:
    transient-error-cooldown-seconds: -1
api-keys:
  codex:
    - name: guarded-local-codex
      base-url: "https://127.0.0.1:{front_port}/bridge/origin/v1"
      models:
{chr(10).join(model_rows)}
      keys:
        - api-key: {json.dumps(private_key(runtime / "origin-guard-key"))}
observability:
  logs:
    debug: false
    logging-to-file: false
    request-log: false
  usage:
    usage-statistics-enabled: false
multimedia:
  disable-image-generation: "passthrough"
'''


def owned_stop(row: dict) -> None:
    owners = launcher.listener_pids(row["port"])
    if not owners: return
    if owners != {row["pid"]}: raise RuntimeError("Backend port owner changed; no process was stopped")
    os.kill(row["pid"], signal.SIGTERM)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if launcher.listener_pids(row["port"]) != {row["pid"]}: return
        time.sleep(.05)
    raise RuntimeError("Owned backend did not stop after draining")


def drain(root: Path, value: dict) -> None:
    active = endpoint_status(root, value).get("active", {})
    if not isinstance(active, dict): raise RuntimeError("Invalid ingress active counts")
    for generation, row in list(value["generations"].items()):
        if generation != value["current"] and active.get(generation) == 0:
            owned_stop(row)
            # Keep the private generation files for incident inspection. They
            # contain no OAuth material and are excluded from the repository.
            del value["generations"][generation]
    write(root / "runtime-managed" / "service.json", value)


def ensure(root: Path, base_runtime: Path, base: dict, models: dict, *, force: bool = False) -> dict:
    with launcher.service_lock():
        chosen = runtime_bundle.selected(root)
        if chosen is None: raise RuntimeError("No reviewed runtime bundle is installed")
        bundle, manifest, identity = chosen
        runtime = directory(root)
        runtime_bundle.private_dir(runtime / "empty-auth")
        origin_manifest(root, runtime, base_runtime, base)
        value = state(root)
        original_state = json.loads(json.dumps(value)) if value is not None else None
        ingress = None
        backend = None
        previous_manifest = (runtime / "backend.json").read_bytes() if (runtime / "backend.json").exists() else None
        try:
            ingress_binary = bundle / manifest["binaries"]["ingress"]["file"]
            initialized = subprocess.run([str(ingress_binary), "--root", str(runtime), "--init-tls"],
                                        capture_output=True, timeout=20, cwd=runtime)
            if initialized.returncode: raise RuntimeError("Could not initialize private loopback TLS identity")
            if value is not None:
                endpoint_status(root, value)
            if value is None:
                front_port = free_port()
                for name in ("front-key", "origin-guard-key"):
                    if not (runtime / name).exists(): api_profile.write_private(runtime / name, secrets.token_hex(32) + "\n")
                ingress = start(ingress_binary, ["--root", str(runtime), "--listen", f"127.0.0.1:{front_port}",
                    "--key-file", str(runtime / "front-key"), "--backend-file", str(runtime / "backend.json"),
                    "--origin-key-file", str(runtime / "origin-guard-key"), "--origin-backend-file", str(runtime / "origin.json"), "--tls"],
                    runtime / "ingress.log")
                launcher.wait_for_private_listener(ingress, front_port)
                value = {"pid": ingress.pid, "port": front_port, "ingress_bundle": identity, "current": "", "generations": {}}
                endpoint_status(root, value)
            fingerprint = hashlib.sha256(runtime_bundle.canonical({"bundle": identity, "models": models})).hexdigest()
            old = value["generations"].get(value["current"])
            if not force and old and old["fingerprint"] == fingerprint and launcher.listener_pids(old["port"]) == {old["pid"]}:
                drain(root, value)
                return value
            generation = "g-" + secrets.token_hex(12)
            generation_dir = runtime / generation
            runtime_bundle.private_dir(generation_dir)
            key, port = secrets.token_hex(32), free_port()
            api_profile.write_private(generation_dir / "key", key + "\n")
            api_profile.write_private(generation_dir / "config.yaml", backend_config(runtime, port, value["port"], models, key))
            proxy = bundle / manifest["binaries"]["proxy"]["file"]
            backend = start(proxy, ["--local-model", "-config", str(generation_dir / "config.yaml")],
                            generation_dir / "proxy.log", ca=runtime / "ca.pem", authority=f'127.0.0.1:{value["port"]}')
            launcher.wait_for_private_listener(backend, port)
            # Preflight only localhost metadata, never a paid model request.
            deadline = time.monotonic() + 20
            while True:
                request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/models", headers={"Authorization": "Bearer " + key})
                if launcher.listener_pids(port) != {backend.pid}: raise RuntimeError("Replacement listener owner changed")
                try:
                    with urllib.request.build_opener(urllib.request.ProxyHandler({}), api_profile.NoRedirect()).open(request, timeout=2) as response:
                        ids = {item["id"] for item in json.load(response).get("data", [])}
                    if set(models).issubset(ids): break
                except (urllib.error.URLError, ValueError, TimeoutError): pass
                if backend.poll() is not None or time.monotonic() >= deadline: raise RuntimeError("Replacement catalog did not become ready")
                time.sleep(.1)
            value["generations"][generation] = {"pid": backend.pid, "port": port, "fingerprint": fingerprint}
            value["current"] = generation
            write(runtime / "service.json", value)
            # Commit routing last. All readiness checks precede admission, so
            # startup failure cannot kill an admitted replacement request.
            write(runtime / "backend.json", {"generation": generation, "port": port, "pid": backend.pid,
                                            "key_file": str(generation_dir / "key")})
        except BaseException:
            if previous_manifest is not None:
                api_profile.write_private(runtime / "backend.json", previous_manifest.decode())
            else:
                (runtime / "backend.json").unlink(missing_ok=True)
            if original_state is None:
                (runtime / "service.json").unlink(missing_ok=True)
            else:
                write(runtime / "service.json", original_state)
            launcher.stop_process(backend)
            launcher.stop_process(ingress)
            raise
        try:
            drain(root, value)
        except (OSError, RuntimeError):
            print("Managed endpoint is ready; old generation retirement is deferred", file=sys.stderr)
        return value


def stop(root: Path) -> None:
    with launcher.service_lock():
        value = state(root)
        if value is None: return
        active = endpoint_status(root, value).get("active", {})
        if any(type(n) is not int or n != 0 for n in active.values()):
            raise RuntimeError("Managed requests are active; finish tasks before stopping")
        for row in value["generations"].values(): owned_stop(row)
        owned_stop(value)
        (root / "runtime-managed" / "service.json").unlink()


def client_environment(root: Path, value: dict, environment: dict, directory: Path) -> dict:
    runtime = root / "runtime-managed"
    result = dict(environment)
    result["ANTHROPIC_BASE_URL"] = f'https://127.0.0.1:{value["port"]}'
    result["ANTHROPIC_AUTH_TOKEN"] = private_key(runtime / "front-key")
    ca = (runtime / "ca.pem").read_bytes()
    original = result.get("NODE_EXTRA_CA_CERTS")
    if original:
        path = Path(original)
        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            raise RuntimeError("Existing extra CA certificate file is unavailable or too large")
        ca += b"\n" + path.read_bytes()
    trusted = directory / "trusted-ca.pem"
    api_profile.write_private(trusted, ca.decode("ascii"))
    result["NODE_EXTRA_CA_CERTS"] = str(trusted)
    result["NODE_TLS_REJECT_UNAUTHORIZED"] = "1"
    for name in ("NO_PROXY", "no_proxy"):
        result[name] = result.get(name, "").rstrip(", ") + ",127.0.0.1,localhost"
    return result


def client_settings(base: Path, environment: dict, directory: Path) -> Path:
    value = read(base)
    settings_env = value.get("env", {})
    if not isinstance(settings_env, dict): raise RuntimeError("Invalid per-session environment settings")
    value["env"] = {**settings_env, **{name: environment[name] for name in environment if
        name.startswith("ANTHROPIC_DEFAULT_") or name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
        "NODE_EXTRA_CA_CERTS", "NODE_TLS_REJECT_UNAUTHORIZED", "NO_PROXY", "no_proxy", "ENABLE_TOOL_SEARCH",
        "CLAUDE_CODE_ATTRIBUTION_HEADER", "CLAUDE_CODE_GATEWAY_HINT_HEADERS", "CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING")}}
    path = directory / "managed-settings.json"
    write(path, value)
    return path
