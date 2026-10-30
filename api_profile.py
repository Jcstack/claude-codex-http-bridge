#!/usr/bin/env python3
"""Private OpenAI API-key profile for the Claude Code model bridge."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import getpass
import json
import os
from pathlib import Path
import platform
import re
import secrets
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request


API_MODELS_URL = "https://api.openai.com/v1/models"
DOCS_CATALOG_URL = "https://developers.openai.com/api/docs/models.md"
DOCS_MODEL_BASE = "https://developers.openai.com/api/docs/models/"
MODEL_ID = re.compile(r"gpt-[0-9]+(?:\.[0-9]+)?-[a-z0-9-]+\Z")
REQUIRED_EFFORTS = {"low", "medium", "high", "xhigh", "max"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise RuntimeError("Official model metadata request redirected unexpectedly")


def fetch_text(url: str, key: str | None = None) -> str:
    if url != API_MODELS_URL and url != DOCS_CATALOG_URL and not (
        url.startswith(DOCS_MODEL_BASE) and url.endswith(".md") and
        MODEL_ID.fullmatch(url[len(DOCS_MODEL_BASE):-3])
    ):
        raise RuntimeError("Model metadata URL is not an approved OpenAI endpoint")
    headers = {"User-Agent": "claude-codex-http-bridge"}
    if key is not None:
        if url != API_MODELS_URL:
            raise RuntimeError("API key must only be sent to the official model-list endpoint")
        headers["Authorization"] = "Bearer " + key
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(NoRedirect())
    for attempt in range(3):
        try:
            with opener.open(request, timeout=12) as response:
                data = response.read(2_000_001)
                if len(data) > 2_000_000:
                    raise RuntimeError("Official model metadata response is unexpectedly large")
                return data.decode("utf-8")
        except urllib.error.HTTPError as error:
            if attempt < 2 and error.code in (429, 500, 502, 503, 504):
                time.sleep(.25 * (2 ** attempt))
                continue
            raise RuntimeError(f"Official model metadata returned HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt < 2:
                time.sleep(.25 * (2 ** attempt))
                continue
            raise RuntimeError(f"Could not fetch official model metadata: {error}") from None
        except UnicodeError as error:
            raise RuntimeError(f"Official model metadata was not UTF-8: {error}") from None
    raise RuntimeError("Official model metadata request failed")


def key_path(root: Path) -> Path:
    return root / "openai-api-key"


def validate_key(key: str) -> str:
    key = key.strip()
    if not 20 <= len(key) <= 512 or any(not 33 <= ord(char) <= 126 for char in key):
        raise RuntimeError("Expected a single printable OpenAI project API key")
    return key


def read_key(root: Path) -> str:
    path = key_path(root)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("No private OpenAI project key; run claude-codex api key set")
    if platform.system() == "Windows":
        import windows_acl
        windows_acl.verify([path], protected=True)
    elif path.stat().st_mode & 0o777 != 0o600:
        raise RuntimeError("OpenAI project key file must have 0600 permissions")
    return validate_key(path.read_text().strip())


def write_private(path: Path, value: str) -> None:
    if path.is_symlink():
        raise RuntimeError("Refusing symlinked private file")
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        if platform.system() == "Windows":
            import windows_acl
            windows_acl.lock([temporary])
        os.replace(temporary, path)
        if platform.system() == "Windows":
            windows_acl.lock([path])
    finally:
        temporary.unlink(missing_ok=True)


def set_key(root: Path, key: str) -> None:
    if (root / "runtime-api" / "service.json").exists():
        raise RuntimeError("Stop the API bridge after background tasks before changing its key")
    write_private(key_path(root), validate_key(key) + "\n")


def key_command(root: Path, action: str) -> int:
    if action == "status":
        try:
            read_key(root)
        except RuntimeError:
            print("OpenAI project key is not configured or is unsafe")
            return 1
        print("OpenAI project key is stored locally with private permissions")
        return 0
    if action != "set":
        raise ValueError("Usage: claude-codex api key set | status")
    if not os.isatty(0):
        raise RuntimeError("Run api key set in an interactive terminal; the key is not accepted as an argument")
    set_key(root, getpass.getpass("OpenAI project API key (hidden): "))
    print("OpenAI project key stored locally; run claude-codex api models")
    return 0


def parse_model_markdown(model_id: str, markdown: str) -> dict | None:
    if not MODEL_ID.fullmatch(model_id):
        return None
    declared_id = re.search(r"(?m)^Model ID: `([^`]+)`\s*$", markdown)
    if declared_id is None or declared_id.group(1) != model_id:
        return None
    context = re.search(r"(?m)^- ([\d,]+) context window\s*$", markdown)
    efforts = (re.search(r"`reasoning\.effort` supports ([\s\S]{0,200}?)\.", markdown) or
               re.search(r"(?im)^Reasoning\.effort supports:\s*([^\n]+)$", markdown))
    modalities = re.search(r"(?m)^- Input modalities: ([^\n]+)$", markdown)
    tools = re.search(r"(?ms)^## Supported tools\n([\s\S]*?)(?=^## |\Z)", markdown)
    if not (context and efforts and modalities and tools):
        return None
    levels = set(re.findall(r"\b(none|low|medium|high|xhigh|max)\b", efforts.group(1)))
    if not REQUIRED_EFFORTS.issubset(levels) or "text" not in modalities.group(1).split(", "):
        return None
    if not re.search(r"(?m)^- web_search\s*$", tools.group(1)):
        return None
    window = int(context.group(1).replace(",", ""))
    if not 200_000 <= window <= 10_000_000:
        return None
    return {
        "context_window": window,
        "supported_reasoning_levels": [{"effort": level} for level in
                                       ("none", "low", "medium", "high", "xhigh", "max") if level in levels],
        "supports_search_tool": True,
        "input_modalities": [part.strip() for part in modalities.group(1).split(",")],
        "source": DOCS_MODEL_BASE + model_id + ".md",
    }


def discover_models(key: str) -> dict[str, dict]:
    try:
        listing = json.loads(fetch_text(API_MODELS_URL, key))
    except ValueError:
        raise RuntimeError("OpenAI model list was not valid JSON") from None
    if not isinstance(listing, dict) or not isinstance(listing.get("data"), list):
        raise RuntimeError("OpenAI model list has an unexpected shape")
    available = {row["id"] for row in listing["data"]
                 if isinstance(row, dict) and isinstance(row.get("id"), str) and
                 MODEL_ID.fullmatch(row["id"])}
    catalog = fetch_text(DOCS_CATALOG_URL)
    documented = set(re.findall(r"\(/api/docs/models/(gpt-[a-z0-9.-]+)\.md\)", catalog))
    candidates = sorted(available & documented)
    if not candidates:
        raise RuntimeError("No API-accessible GPT model appears in the official model catalog")
    models = {}
    unavailable = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fetch_text, DOCS_MODEL_BASE + model_id + ".md"): model_id
                   for model_id in candidates}
        for future in as_completed(futures):
            model_id = futures[future]
            try:
                parsed = parse_model_markdown(model_id, future.result())
            except RuntimeError:
                unavailable.append(model_id)
                continue
            if parsed is not None:
                models[model_id] = parsed
    if not models:
        raise RuntimeError("No API model has verified context, reasoning effort, text input, and web search metadata")
    print(f"OpenAI API discovery: {len(available)} GPT IDs, {len(candidates)} documented, "
          f"{len(models)} eligible", file=sys.stderr)
    if unavailable:
        print("OpenAI API model pages unavailable: " + ", ".join(sorted(unavailable)), file=sys.stderr)
    return dict(sorted(models.items()))


def make_session(root: Path, runtime: Path) -> tuple[int, Path, Path, Path, Path]:
    key = read_key(root)
    models = discover_models(key)
    auth = root / "api-auth"
    if auth.is_symlink():
        raise RuntimeError("API auth directory must not be a symlink")
    auth.mkdir(mode=0o700, exist_ok=True)
    if platform.system() == "Windows":
        import windows_acl
        windows_acl.lock([auth])
    else:
        auth.chmod(0o700)
    directory = Path(tempfile.mkdtemp(prefix="session-", dir=runtime))
    if platform.system() == "Windows":
        windows_acl.lock([directory])
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    local_key = "sk-local-" + secrets.token_hex(32)
    model_config = "\n".join(
        f'        - name: {json.dumps(model_id)}\n'
        f'          max-context-length: {spec["context_window"]}\n'
        f'          thinking:\n'
        f'            levels: {json.dumps([entry["effort"] for entry in spec["supported_reasoning_levels"]])}'
        for model_id, spec in models.items()
    )
    config = f'''config-version: 8
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
    - {json.dumps(local_key)}
oauth:
  auth-dir: {json.dumps(str(auth))}
api-keys:
  codex:
    - name: direct-openai-api
      base-url: "https://api.openai.com/v1"
      models:
{model_config}
      keys:
        - api-key: {json.dumps(key)}
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
    config_path, key_file = directory / "config.yaml", directory / "key"
    write_private(runtime / "catalog.json", json.dumps(models, separators=(",", ":")) + "\n")
    write_private(key_file, local_key + "\n")
    write_private(config_path, config)
    return port, directory, config_path, key_file, directory / "proxy.log"
