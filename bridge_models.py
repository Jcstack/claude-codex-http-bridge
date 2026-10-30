#!/usr/bin/env python3
"""Discover Codex chat models from the authenticated local HTTP bridge."""

import json
import hashlib
import hmac
import os
from pathlib import Path
import platform
import re
import secrets
import socket
import sys
import tempfile
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parent
REQUIRED_EFFORTS = {"low", "medium", "high", "xhigh", "max"}


def verify_install() -> None:
    windows = platform.system() == "Windows"
    proxy = ROOT / "bin" / ("cli-proxy-api.exe" if windows else "cli-proxy-api")
    checksum_file = ROOT / "proxy.sha256"
    config = ROOT / "config.yaml"
    auth = ROOT / "auth"
    for path, expected_mode in ((ROOT, 0o700), (auth, 0o700), (config, 0o600), (checksum_file, 0o600)):
        if path.is_symlink() or (not windows and path.stat().st_mode & 0o777 != expected_mode):
            raise RuntimeError(f"Unsafe permissions or symlink: {path}")
    credentials = list(auth.glob("*.json"))
    for credential in credentials:
        if credential.is_symlink() or not credential.is_file() or (
            not windows and credential.stat().st_mode & 0o777 != 0o600
        ):
            raise RuntimeError("OAuth credential file must be a private regular file")
    if not proxy.is_file() or proxy.is_symlink() or not os.access(proxy, os.X_OK):
        raise RuntimeError("Reviewed proxy binary is missing or not executable")
    if windows:
        import windows_acl
        windows_acl.verify([ROOT, auth, config, checksum_file, proxy], protected=True)
        windows_acl.verify(credentials)
    expected = checksum_file.read_text().strip()
    if not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise RuntimeError("Invalid proxy checksum file")
    digest = hashlib.sha256()
    with proxy.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    if not hmac.compare_digest(digest.hexdigest(), expected):
        raise RuntimeError("Proxy binary does not match the installed verified release")
    matches = re.findall(r'(?m)^    - "sk-local-([a-f0-9]{64})"$', config.read_text())
    if len(matches) != 1 or matches[0] == "0" * 64:
        raise RuntimeError("Local template key is missing or insecure")


def make_session() -> None:
    """Create a private, single-session proxy config without printing its key."""
    runtime = ROOT / "runtime"
    runtime.mkdir(mode=0o700, exist_ok=True)
    runtime.chmod(0o700)
    if platform.system() == "Windows":
        import windows_acl
        windows_acl.lock([runtime])
    directory = Path(tempfile.mkdtemp(prefix="session-", dir=runtime))
    try:
        if platform.system() == "Windows":
            windows_acl.lock([directory])
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        key = "sk-local-" + secrets.token_hex(32)
        source = (ROOT / "config.yaml").read_text()
        required_settings = (
            '  host: "127.0.0.1"',
            '  max-request-body-bytes: 67108864',
            '  allow-remote: false',
            '  disable-control-panel: true',
            '  disable-auto-update-panel: true',
            '    request-log: false',
            '    usage-statistics-enabled: false',
        )
        if any(setting not in source.splitlines() for setting in required_settings):
            raise RuntimeError("Bridge config template lost a privacy or security setting")
        # Older private installations predate these settings. Apply them only
        # when they have no routing section, so explicit local policy wins.
        if not re.search(r"(?m)^routing:\s*$", source):
            source += ("\nrouting:\n  retry:\n    request-retry: 2\n"
                       "  cooldown:\n    transient-error-cooldown-seconds: -1\n")
        if platform.system() != "Windows" and (ROOT / "auth").stat().st_mode & 0o777 != 0o700:
            raise RuntimeError("Bridge auth directory must have 0700 permissions")
        config, port_count = re.subn(
            r"(?m)^(  port: )\d+$",
            lambda match: match.group(1) + str(port),
            source,
        )
        config, key_count = re.subn(
            r'(?m)^(    - )"sk-local-[a-f0-9]+"$',
            lambda match: match.group(1) + '"' + key + '"',
            config,
        )
        if port_count != 1 or key_count != 1:
            raise RuntimeError("Bridge config template has an unexpected port or key layout")
        config_path = directory / "config.yaml"
        key_path = directory / "key"
        for path, content in ((config_path, config), (key_path, key + "\n")):
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as handle:
                handle.write(content)
        if platform.system() == "Windows":
            windows_acl.lock([config_path, key_path])
        print(port, directory, config_path, key_path, directory / "proxy.log", sep="\t")
    except BaseException:
        for path in directory.iterdir():
            path.unlink()
        directory.rmdir()
        raise


def get_json(path: str, key: str) -> dict:
    port = os.environ.get("CODEX_BRIDGE_PORT", "")
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise RuntimeError("Missing private bridge port")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}" + path,
        headers={"Authorization": "Bearer " + key},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=8) as response:
            result = json.load(response)
            if not isinstance(result, dict):
                raise RuntimeError("Local model catalog has an unexpected shape")
            return result
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Local model catalog returned HTTP {error.code}") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise RuntimeError(f"Could not read the local model catalog: {error}") from None


def discover() -> tuple[dict[str, dict], list[str]]:
    key_path = os.environ.get("CODEX_BRIDGE_KEY_FILE", "")
    if not key_path:
        raise RuntimeError("Missing private bridge key file")
    key = Path(key_path).read_text().strip()
    public = get_json("/v1/models", key)
    rich = get_json("/v1/models?client_version=cpa", key)
    if not isinstance(public.get("data"), list) or not isinstance(rich.get("models"), list):
        raise RuntimeError("Local model catalog is missing the expected model lists")
    public_ids = {
        row["id"]
        for row in public["data"]
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    rows = {
        row["slug"]: row
        for row in rich["models"]
        if isinstance(row, dict)
        and isinstance(row.get("slug"), str)
        and row["slug"] in public_ids
    }
    if os.environ.get("CODEX_BRIDGE_ROUTE") == "api":
        metadata_path = Path(os.environ.get("CODEX_BRIDGE_API_METADATA_FILE", ""))
        if not metadata_path.is_file() or metadata_path.is_symlink():
            raise RuntimeError("Private OpenAI API model metadata is missing")
        if platform.system() == "Windows":
            import windows_acl
            windows_acl.verify([metadata_path], protected=True)
        elif metadata_path.stat().st_mode & 0o777 != 0o600:
            raise RuntimeError("OpenAI API model metadata must have 0600 permissions")
        try:
            verified = json.loads(metadata_path.read_text())
        except ValueError:
            raise RuntimeError("OpenAI API model metadata is not valid JSON") from None
        if not isinstance(verified, dict):
            raise RuntimeError("OpenAI API model metadata has an unexpected shape")
        rows = {
            slug: {**rows.get(slug, {}), "slug": slug, **spec}
            for slug, spec in verified.items()
            if slug in public_ids and isinstance(spec, dict)
        }
    chat = {
        slug: row
        for slug, row in rows.items()
        if isinstance(slug, str)
        and slug.startswith("gpt-")
        and not slug.startswith("gpt-image-")
        and isinstance(row.get("input_modalities"), list)
        and "text" in row["input_modalities"]
    }
    images = sorted(
        slug for slug in rows if isinstance(slug, str) and slug.startswith("gpt-image-")
    )
    return chat, images


def compact_window(row: dict) -> int | None:
    context = row.get("context_window")
    if type(context) is not int or context < 200_000:
        return None
    # Leave room for output and differences between Claude and Codex token counts.
    computed = min(int(context * 0.8), context - 70_000)
    native = row.get("auto_compact_token_limit")
    if isinstance(native, int) and native > 0:
        computed = min(computed, native)
    window = computed // 10_000 * 10_000
    return window if window >= 100_000 else None


def eligible(row: dict) -> bool:
    levels = row.get("supported_reasoning_levels")
    if not isinstance(levels, list):
        return False
    efforts = {
        entry.get("effort")
        for entry in levels
        if isinstance(entry, dict)
    }
    return (
        compact_window(row) is not None
        and REQUIRED_EFFORTS.issubset(efforts)
        and row.get("supports_search_tool") is True
    )


def version_key(slug: str) -> tuple[int, int]:
    match = re.fullmatch(r"gpt-(\d+)(?:\.(\d+))?-[a-z0-9-]+", slug)
    return (int(match.group(1)), int(match.group(2) or 0)) if match else (-1, -1)


def display_key(slug: str) -> tuple[int, int, int, str]:
    major, minor = version_key(slug)
    family = slug.rsplit("-", 1)[-1]
    return (-major, -minor, {"sol": 0, "astra": 1, "terra": 2, "luna": 3}.get(family, 9), slug)


def aliases(chat: dict[str, dict]) -> dict[str, str]:
    found = {}
    for family in ("sol", "astra", "terra", "luna"):
        matching = [
            slug for slug, row in chat.items()
            if slug.endswith("-" + family) and eligible(row)
        ]
        if matching:
            found[family] = max(matching, key=version_key)
    return found


def behaves_as(slug: str) -> str:
    if slug.endswith("-astra"):
        return "claude-opus-5-5"
    if slug.endswith("-luna"):
        return "claude-haiku-5-5"
    return "claude-sonnet-5-5"


def runtime_settings(chat: dict[str, dict]) -> Path:
    ready = [slug for slug, row in chat.items() if eligible(row)]
    if not ready or any(not re.fullmatch(r"gpt-[a-z0-9.-]+", slug) for slug in ready):
        raise RuntimeError("No safe Codex model IDs in catalog")
    directory = ROOT / "runtime-models"
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    if platform.system() == "Windows":
        import windows_acl
        windows_acl.lock([directory])
    options = []
    model_settings = {}
    api_route = os.environ.get("CODEX_BRIDGE_ROUTE") == "api"
    for slug in sorted(ready, key=display_key):
        row = chat[slug]
        label = "GPT-" + slug.removeprefix("gpt-").replace("-", " ").title()
        window = compact_window(row)
        options.append({
            "model": slug,
            "label": label + (" (OpenAI API)" if api_route else " (Codex)"),
            "description": f"{'OpenAI API' if api_route else 'Codex route'} {row['context_window'] // 1000}k; auto-compact {window // 1000}k",
            "behavesAs": behaves_as(slug),
        })
        model_settings[slug] = {"autoCompactWindow": window}
    value = {
        "modelPicker": {
            "options": options,
            "replaceBuiltInOptions": True,
        },
        "modelSettings": model_settings,
    }
    if os.environ.get("CLAUDE_CODEX_PDF_GUARD") != "0":
        guard = ROOT / "webfetch_pdf_guard.py"
        value["hooks"] = {"PostToolUse": [{
            "matcher": "WebFetch",
            "hooks": [{"type": "command", "command": sys.executable,
                       "args": [str(guard)], "timeout": 5}],
        }]}
    content = json.dumps(value, indent=2) + "\n"
    path = directory / ("catalog-" + hashlib.sha256(content.encode()).hexdigest() + ".json")
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.read_text() != content)):
        raise RuntimeError("Private model settings file changed unexpectedly")
    if not path.exists():
        temporary = directory / ("catalog-" + secrets.token_hex(8) + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(content)
        os.replace(temporary, path)
    path.chmod(0o600)
    if platform.system() == "Windows":
        windows_acl.lock([path])
    return path


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("verify", "session", "catalog", "list", "prepare"):
        print("Usage: bridge_models.py verify | session | catalog | list | prepare MODEL", file=sys.stderr)
        return 2
    if sys.argv[1] == "verify":
        if len(sys.argv) != 2:
            return 2
        verify_install()
        return 0
    if sys.argv[1] == "session":
        if len(sys.argv) != 2:
            return 2
        make_session()
        return 0
    chat, images = discover()
    if sys.argv[1] == "catalog":
        if len(sys.argv) != 2:
            return 2
        print(json.dumps({"chat": len(chat), "eligible": sum(eligible(row) for row in chat.values())}))
        return 0
    choices = aliases(chat)
    if sys.argv[1] == "list":
        print(("OpenAI API" if os.environ.get("CODEX_BRIDGE_ROUTE") == "api" else "Codex") +
              " chat models listed by local HTTP /v1/models:")
        for slug in sorted(chat, key=display_key):
            row = chat[slug]
            window = compact_window(row)
            state = "catalog-ok" if eligible(row) else "metadata-incomplete"
            alias = next((name for name, target in choices.items() if target == slug), "")
            print(
                f"  {slug:<18} context={row.get('context_window', '?'):<7} "
                f"compact={window or '?':<7} {state} {alias}"
            )
        if images:
            print("Image-only API models (not Claude Code base models): " + ", ".join(images))
        print("Catalog listing does not verify inference access to every model; selection checks access on first request.")
        print("Claude Code effort: low, medium, high, xhigh, max. Codex Ultra is a separate client mode.")
        return 0
    if len(sys.argv) != 3:
        print("Specify one model ID or sol/astra/terra/luna.", file=sys.stderr)
        return 2
    selected = choices.get(sys.argv[2], sys.argv[2])
    row = chat.get(selected)
    if row is None:
        raise RuntimeError(f"Model {selected!r} is not in the live local catalog; run claude-codex models")
    if not eligible(row):
        raise RuntimeError(f"Model {selected!r} lacks verified context, low-max effort, or web search metadata")
    windows = {slug: item["context_window"] for slug, item in chat.items() if eligible(item)}
    print(selected, row["context_window"], compact_window(row), runtime_settings(chat),
          json.dumps(windows, separators=(",", ":")), sep="\t")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError) as error:
        print(f"Codex model discovery: {error}", file=sys.stderr)
        raise SystemExit(1) from None
