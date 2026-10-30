"""Private Codex account staging and runtime selection; never modifies native Codex auth."""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import platform
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

import api_profile


CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"  # Public OAuth client, not a credential.
AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
REDIRECT_URI = "http://localhost:1455/auth/callback"
ALIAS = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z")


def private_directory(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError("Account directory must not be a symlink")
    path.mkdir(mode=0o700, exist_ok=True)
    if platform.system() == "Windows":
        import windows_acl
        windows_acl.lock([path])
    else:
        path.chmod(0o700)


def private_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Account data must be a private regular file")
    if platform.system() == "Windows":
        import windows_acl
        windows_acl.verify([path], protected=True)
    elif path.stat().st_mode & 0o077:
        raise RuntimeError("Account data must have private permissions")
    if path.stat().st_size > 1_000_000:
        raise RuntimeError("Account data is unexpectedly large")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("Invalid local account data")
    return value


def create_record(path: Path, record: dict) -> None:
    """Reserve a new staged alias exclusively, including simultaneous logins."""
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise RuntimeError("Account alias was already saved; its credentials were not replaced") from None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            if platform.system() == "Windows":
                import windows_acl
                windows_acl.lock([path])
            json.dump(record, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def stage_path(root: Path, alias: str) -> Path:
    if not ALIAS.fullmatch(alias):
        raise ValueError("Account alias must use 1–32 lowercase letters, digits, underscores or hyphens")
    directory = root / "accounts"
    private_directory(directory)
    return directory / (alias + ".json")


def account_subject(value: dict) -> str:
    """Workspace IDs can be shared by different people; distinguish the user too."""
    claims = {}
    token = value.get("id_token")
    if isinstance(token, str) and len(token) <= 100_000:
        try:
            parts = token.split(".")
            if len(parts) == 3:
                claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        except (ValueError, TypeError):
            pass
    if not isinstance(claims, dict):
        claims = {}
    auth = claims.get("https://api.openai.com/auth")
    auth = auth if isinstance(auth, dict) else {}
    for subject in (auth.get("chatgpt_user_id"), auth.get("user_id"), claims.get("sub")):
        if isinstance(subject, str) and subject:
            return "user:" + subject
    email = value.get("email") or claims.get("email")
    return "email:" + email.casefold() if isinstance(email, str) and email else ""


def identity_fingerprint(value: dict) -> str:
    account = value.get("account_id")
    if not isinstance(account, str) or not account or len(account) > 512:
        raise RuntimeError("Codex authorization did not provide an account identity")
    subject = account_subject(value)
    material = account + "\0" + subject if subject else account
    return hashlib.sha256(material.encode()).hexdigest()


def fingerprint(value: dict) -> str:
    return identity_fingerprint(value)[:16]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError("Account request redirect denied")


def request_json(url: str, *, method: str = "GET", headers: dict | None = None,
                 data: bytes | None = None, timeout: int = 30) -> dict:
    # No ambient proxy: OAuth tokens and the local management credential must not
    # be forwarded to a proxy selected by an unrelated workspace environment.
    if any("\r" in value or "\n" in value for value in (headers or {}).values()):
        raise RuntimeError("Invalid account request header")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = response.read(2_000_001)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Account request returned HTTP {error.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, ValueError):
        raise RuntimeError("Account request could not reach the approved endpoint") from None
    if len(payload) > 2_000_000:
        raise RuntimeError("Account response is unexpectedly large")
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeError):
        raise RuntimeError("Account endpoint did not return valid JSON") from None
    if not isinstance(value, dict):
        raise RuntimeError("Account endpoint did not return an object")
    return value


def token_record(tokens: dict, *, now: datetime | None = None) -> dict:
    for name in ("access_token", "refresh_token", "id_token"):
        value = tokens.get(name)
        if not isinstance(value, str) or not value or len(value) > 100_000:
            raise RuntimeError("Incomplete Codex authorization; existing accounts were not changed")
    try:
        parts = tokens["id_token"].split(".")
        if len(parts) != 3:
            raise ValueError()
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        identity = claims["https://api.openai.com/auth"]
        account_id = identity["chatgpt_account_id"]
        email = claims["email"]
    except (ValueError, KeyError, TypeError):
        raise RuntimeError("Codex authorization identity was missing") from None
    # These claims come only from the fixed TLS token exchange, not from an
    # uploaded/callback JWT. They are metadata, never an authorization decision.
    if not isinstance(email, str) or not email or not isinstance(account_id, str) or not account_id:
        raise RuntimeError("Codex authorization identity was incomplete")
    duration = tokens.get("expires_in")
    if type(duration) is not int or not 0 < duration <= 366 * 86400:
        raise RuntimeError("Codex authorization expiry was invalid")
    now = now or datetime.now(timezone.utc)
    record = {key: tokens[key] for key in ("access_token", "refresh_token", "id_token")}
    record.update(type="codex", account_id=account_id, email=email,
                  plan_type=identity.get("chatgpt_plan_type", "free"), disabled=True,
                  last_refresh=now.isoformat(), expired=(now + timedelta(seconds=duration)).isoformat())
    fingerprint(record)
    return record


def oauth_login(root: Path, alias: str, *, timeout: int = 600) -> Path:
    destination = stage_path(root, alias)
    if destination.exists():
        raise RuntimeError("Account alias already exists; choose a new alias")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Callback query contains an authorization code; never log it.

        def do_GET(self):
            self.connection.settimeout(3)
            try:
                parsed = urllib.parse.urlsplit(self.path)
            except ValueError:
                parsed = urllib.parse.urlsplit("/invalid")
            host = self.headers.get("Host", "")
            status, message = 400, "Authorization callback rejected."
            if (host not in ("localhost:1455", "127.0.0.1:1455") or len(self.path) > 16384
                    or parsed.scheme or parsed.netloc):
                pass
            elif parsed.path == "/success":
                status, message = 200, "Authorization received. You may close this tab."
            elif parsed.path == "/auth/callback" and not result:
                try:
                    query = urllib.parse.parse_qs(parsed.query, max_num_fields=12)
                except ValueError:
                    query = {}
                states, codes = query.get("state", []), query.get("code", [])
                if len(states) == 1 and hmac.compare_digest(states[0].encode(), state.encode()):
                    if len(codes) == 1 and 0 < len(codes[0]) <= 8192:
                        result["code"] = codes[0]
                        status, message = 200, "Authorization received. You may close this tab."
                    elif "error" in query:
                        result["error"] = "Authorization was declined; existing accounts were not changed"
                        status, message = 200, "Authorization was declined."
            payload = message.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    class Server(HTTPServer):
        allow_reuse_address = os.name != "nt"
        allow_reuse_port = False

        def server_bind(self):
            if os.name == "nt":
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()

        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(3)
            return connection, address

    try:
        server = Server(("127.0.0.1", 1455), Handler)
    except OSError:
        raise RuntimeError("Local authorization port 1455 is occupied; no existing listener was stopped") from None
    params = {"client_id": CLIENT_ID, "response_type": "code", "redirect_uri": REDIRECT_URI,
              "scope": "openid email profile offline_access", "state": state,
              "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "login",
              "id_token_add_organizations": "true", "codex_cli_simplified_flow": "true"}
    try:
        if not webbrowser.open(AUTHORIZE_URL + "?" + urllib.parse.urlencode(params), new=1):
            raise RuntimeError("Could not open the authorization page; existing accounts were not changed")
        print("Authorization page opened; select the other ChatGPT account. Existing sessions are unchanged.", flush=True)
        deadline = time.monotonic() + timeout
        server.timeout = 0.5
        while not result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if result.get("error"):
        raise RuntimeError(result["error"])
    if "code" not in result:
        raise RuntimeError("Authorization timed out; existing accounts were not changed")
    body = urllib.parse.urlencode({"grant_type": "authorization_code", "client_id": CLIENT_ID,
                                  "redirect_uri": REDIRECT_URI, "code": result["code"],
                                  "code_verifier": verifier}).encode()
    tokens = request_json(TOKEN_URL, method="POST", data=body,
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
    record = token_record(tokens)
    create_record(destination, record)
    print(f"Account '{alias}' authorized and staged privately; the running bridge has not switched.", flush=True)
    return destination


def initial_login(root: Path) -> None:
    """Use the same loopback-only flow for bootstrap, without replacing old auth."""
    alias = "primary" if not stage_path(root, "primary").exists() else "login-" + secrets.token_hex(4)
    destination = oauth_login(root, alias)
    with switch_lock(root):
        existing = local_accounts(root)
        if existing:
            print(f"Existing account selection retained. To switch, run: claude-codex account use {alias}")
            return
        import launcher
        runtime = root / "runtime"
        state = launcher.read_state(runtime) if runtime.is_dir() else None
        if state is not None and launcher.state_is_live(runtime, state):
            print(f"Running service retained. To select the staged account, run: claude-codex account use {alias}")
            return
        record = private_json(destination)
        name = "codex-bridge-" + fingerprint(record) + ".json"
        create_record(root / "auth" / name, {**record, "disabled": False})
        print("Initial bridge account selected. Native Codex credentials were not changed.")


@contextmanager
def isolated_account(root: Path, record: dict):
    """Run a disposable account validator; never edits the active auth directory."""
    import launcher
    directory = root / "runtime-accounts"
    private_directory(directory)
    windows = platform.system() == "Windows"
    proxy = root / "bin" / ("cli-proxy-api.exe" if windows else "cli-proxy-api")
    subprocess.run([sys.executable, str(root / "bridge_models.py"), "verify"],
                   check=True, capture_output=True, timeout=20)
    with tempfile.TemporaryDirectory(prefix="check-", dir=directory) as temporary:
        home = Path(temporary)
        private_directory(home)
        auth = home / "auth"
        private_directory(auth)
        credential = auth / "codex-validation.json"
        api_profile.write_private(credential, json.dumps({**record, "disabled": False}) + "\n")
        key = "sk-local-" + secrets.token_hex(32)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        config = ("config-version: 8\nserver:\n  host: \"127.0.0.1\"\n"
                  f"  port: {port}\n  max-request-body-bytes: 67108864\n"
                  "management:\n  allow-remote: false\n  secret-key: \"\"\n"
                  "  disable-control-panel: true\n  disable-auto-update-panel: true\n"
                  f"access:\n  api-keys:\n    - \"{key}\"\n"
                  f"oauth:\n  auth-dir: {json.dumps(str(auth))}\n"
                  "routing:\n  retry:\n    request-retry: 0\n"
                  "observability:\n  logs:\n    debug: false\n    logging-to-file: false\n"
                  "    request-log: false\n  usage:\n    usage-statistics-enabled: false\n")
        config_path, key_path = home / "config.yaml", home / "key"
        api_profile.write_private(config_path, config)
        api_profile.write_private(key_path, key + "\n")
        environment = os.environ.copy()
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            environment.pop(name, None)
        process = subprocess.Popen([str(proxy), "-config", str(config_path)], cwd=home, env=environment,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            launcher.wait_for_private_listener(process, port)
            environment.update(CODEX_BRIDGE_PORT=str(port), CODEX_BRIDGE_KEY_FILE=str(key_path))
            environment.pop("CODEX_BRIDGE_ROUTE", None)
            launcher.wait_for_catalog(process, environment, True)
            completed = subprocess.run([sys.executable, str(root / "bridge_models.py"), "snapshot"],
                                       env=environment, capture_output=True, text=True, check=True, timeout=15)
            models = json.loads(completed.stdout)
            yield {"url": f"http://127.0.0.1:{port}", "key": key, "models": models}
        finally:
            launcher.stop_process(process)
            # Preserve any token refresh from validation without ever importing it
            # into native Codex or enabling it in the active proxy.
            refreshed = private_json(credential)
            record.update(refreshed)
            record["disabled"] = True


def inference(probe: dict, model: str, inputs: list, *, include_reasoning: bool = False) -> dict:
    body = {"model": model, "instructions": "Follow the user's instruction briefly.",
            "input": inputs, "reasoning": {"effort": "low"}, "stream": False, "store": False}
    if include_reasoning:
        body["include"] = ["reasoning.encrypted_content"]
    return request_json(probe["url"] + "/v1/responses", method="POST",
                        headers={"Authorization": "Bearer " + probe["key"], "Content-Type": "application/json"},
                        data=json.dumps(body).encode(), timeout=120)


class Management:
    """Only the already-running, PID-verified loopback auth owner's management API."""

    def __init__(self, root: Path):
        import launcher
        self.root = root
        runtime = root / "runtime"
        state = launcher.read_state(runtime)
        if state is None or not launcher.state_is_live(runtime, state):
            raise RuntimeError("An existing healthy subscription bridge is required; nothing was restarted")
        self.state = state
        config = root / "config.yaml"
        if config.is_symlink() or not config.is_file():
            raise RuntimeError("Private local management configuration is missing")
        if platform.system() == "Windows":
            import windows_acl
            windows_acl.verify([config], protected=True)
        elif config.stat().st_mode & 0o077:
            raise RuntimeError("Local management configuration must have private permissions")
        text = config.read_text()
        match = re.search(r'(?m)^  secret-key: "([a-f0-9]{64})"$', text)
        if not match or "  allow-remote: false" not in text.splitlines():
            raise RuntimeError("No retained private loopback management credential; active service was not changed")
        self.key = match.group(1)
        self.base = f"http://127.0.0.1:{state['port']}/v0/management/"

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        resource = path.partition("?")[0]
        if (method, resource) not in {
                ("GET", "auth-files"), ("GET", "auth-files/models"),
                ("POST", "auth-files"), ("PATCH", "auth-files/status")}:
            raise RuntimeError("Unapproved account management operation")
        return request_json(self.base + path, method=method,
                            headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json"},
                            data=json.dumps(body).encode() if body is not None else None)

    def files(self) -> list[dict]:
        entries = self.call("GET", "auth-files").get("files")
        if not isinstance(entries, list):
            raise RuntimeError("Local account catalog is invalid")
        return [row for row in entries if isinstance(row, dict) and
                (row.get("provider") == "codex" or row.get("type") == "codex") and not row.get("runtime_only")]

    def set_disabled(self, name: str, disabled: bool) -> None:
        self.call("PATCH", "auth-files/status", {"name": name, "disabled": disabled})


def local_accounts(root: Path) -> list[dict]:
    directory = root / "auth"
    if directory.is_symlink() or not directory.is_dir():
        raise RuntimeError("Private active auth directory is missing")
    result = []
    for path in sorted(directory.glob("*.json")):
        record = private_json(path)
        if record.get("type") == "codex":
            result.append({"name": path.name, "record": record, "fingerprint": fingerprint(record)})
    return result


@contextmanager
def switch_lock(root: Path):
    directory = root / "accounts"
    private_directory(directory)
    path = directory / ".switch.lock"
    if path.is_symlink():
        raise RuntimeError("Account lock must not be a symlink")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "r+b", buffering=0) as handle:
        if platform.system() == "Windows":
            import windows_acl
            windows_acl.lock([path])
            import msvcrt
            # Lock before updating the byte: an update would race another owner.
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield directory
        finally:
            handle.seek(0)
            if platform.system() == "Windows":
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)


def select_record(root: Path, alias: str) -> dict:
    path = stage_path(root, alias)
    if path.exists():
        record = private_json(path)
        if record.get("type") != "codex":
            raise RuntimeError("Alias does not contain a staged Codex account")
        if (root / "auth").is_dir():
            matches = [row["record"] for row in local_accounts(root)
                       if row["fingerprint"] == fingerprint(record)]
            if len(matches) > 1:
                raise RuntimeError("Duplicate credentials for the same account need local review")
            if matches:
                return matches[0]
        return record
    if alias.startswith("account-"):
        matches = [row["record"] for row in local_accounts(root) if
                   "account-" + row["fingerprint"] == alias or
                   "account-" + hashlib.sha256(row["record"]["account_id"].encode()).hexdigest()[:16] == alias]
        if len(matches) == 1:
            return matches[0]
    raise RuntimeError("Unknown account alias; run claude-codex account list")


@contextmanager
def validation_route(root: Path, record: dict):
    active = [row for row in local_accounts(root) if not row["record"].get("disabled")]
    if len(active) != 1 or active[0]["fingerprint"] != fingerprint(record):
        with isolated_account(root, record) as probe:
            yield probe
        return
    # A selected account already has a refresh owner. Reuse its route rather
    # than starting a second process with the same rotating refresh token.
    import launcher
    management = Management(root)
    state = management.state
    runtime = root / "runtime"
    environment = launcher.service_environment(state, runtime)
    result = subprocess.run([sys.executable, str(root / "bridge_models.py"), "snapshot"],
                            env=environment, capture_output=True, text=True, check=True, timeout=15)
    key = launcher.state_paths(runtime, state)[2].read_text().strip()
    yield {"url": f"http://127.0.0.1:{state['port']}", "key": key, "models": json.loads(result.stdout)}
    after = [row for row in local_accounts(root) if not row["record"].get("disabled")]
    if len(after) != 1 or after[0]["fingerprint"] != fingerprint(record):
        raise RuntimeError("Account changed during validation; no selection was changed by this check")


def check_account(root: Path, alias: str) -> dict:
    import bridge_models
    record = select_record(root, alias)
    try:
        with validation_route(root, record) as probe:
            models = probe["models"]
            choices = bridge_models.aliases(models)
            model = choices.get("sol") or next(iter(models))
            response = inference(probe, model, [{"role": "user", "content": "Return ACCOUNT_READY only."}])
            if response.get("status") != "completed" or not response.get("output"):
                raise RuntimeError("New account inference did not complete; the active account was not changed")
            checked = {"fingerprint": fingerprint(record), "checked_at": time.time(), "model": model,
                       "models": sorted(models)}
    finally:
        path = stage_path(root, alias)
        if path.exists():
            api_profile.write_private(path, json.dumps(record) + "\n")
    api_profile.write_private(stage_path(root, alias).with_suffix(".checked"), json.dumps(checked) + "\n")
    return checked


def switch_account(root: Path, alias: str) -> None:
    with switch_lock(root) as directory:
        journal = directory / "switch.pending"
        if journal.exists():
            raise RuntimeError("An interrupted account change needs recovery; no additional change was made")
        management = Management(root)
        record = select_record(root, alias)
        identity = fingerprint(record)
        entries = local_accounts(root)
        existing = next((entry for entry in entries if entry["fingerprint"] == identity), None)
        before = {row["name"]: bool(row.get("disabled")) for row in management.files()}
        if existing and before.get(existing["name"]) is False and sum(not v for v in before.values()) == 1:
            print(f"Account '{alias}' is already selected; no sessions were restarted.")
            return
        checked_path = stage_path(root, alias).with_suffix(".checked")
        checked = private_json(checked_path) if checked_path.exists() else {}
        if (checked.get("fingerprint") != identity or type(checked.get("checked_at")) not in (int, float)
                or not 0 <= time.time() - checked["checked_at"] < 600):
            check_account(root, alias)
            record = select_record(root, alias)
        name = existing["name"] if existing else "codex-bridge-" + identity + ".json"
        if existing is None:
            management.call("POST", "auth-files?" + urllib.parse.urlencode({"name": name}),
                            {**record, "disabled": True})
            private_json(root / "auth" / name)
            before[name] = True
        api_profile.write_private(journal, json.dumps({"before": before, "target": name}) + "\n")
        try:
            # Enable and publish the validated target before disabling anything.
            # This avoids an interval with no available credential. Disable uses
            # Manager.Modify; DELETE/Remove would close admitted WS sessions.
            management.set_disabled(name, False)
            deadline = time.monotonic() + 15
            while True:
                models = management.call("GET", "auth-files/models?" + urllib.parse.urlencode({"name": name}))
                if models.get("models") or time.monotonic() >= deadline:
                    break
                time.sleep(0.2)
            if not models.get("models"):
                raise RuntimeError("New account did not publish any models")
            for previous, disabled in before.items():
                if previous != name and not disabled:
                    management.set_disabled(previous, True)
            after = {row["name"]: bool(row.get("disabled")) for row in management.files()}
            if {key for key, disabled in after.items() if not disabled} != {name}:
                raise RuntimeError("Active account selection could not be verified")
        except BaseException:
            restored = True
            # Restore enabled accounts first so rollback also avoids an empty pool.
            for previous, disabled in sorted(before.items(), key=lambda item: item[1]):
                try:
                    management.set_disabled(previous, disabled)
                except Exception:
                    restored = False
            if restored:
                journal.unlink()
                raise RuntimeError("Account switch failed; previous account selection was restored") from None
            raise RuntimeError("Account switch needs local recovery; private switch.pending was retained") from None
        journal.unlink()
        print(f"Account '{alias}' selected for subsequent requests. Existing streams and Claude sessions were not restarted.")


def recover_account(root: Path) -> None:
    with switch_lock(root) as directory:
        path = directory / "switch.pending"
        if not path.exists():
            print("No interrupted account change needs recovery.")
            return
        saved = private_json(path).get("before")
        if (not isinstance(saved, dict) or not saved or
                any(not isinstance(k, str) or type(v) is not bool for k, v in saved.items())):
            raise RuntimeError("Private account recovery record is invalid")
        management = Management(root)
        known = {row["name"] for row in management.files()}
        if not set(saved).issubset(known):
            raise RuntimeError("Account recovery catalog changed; private record retained for local review")
        for name, disabled in sorted(saved.items(), key=lambda item: item[1]):
            management.set_disabled(name, disabled)
        after = {row["name"]: bool(row.get("disabled")) for row in management.files()}
        if any(after.get(name) != disabled for name, disabled in saved.items()):
            raise RuntimeError("Account recovery could not be verified; private record retained")
        path.unlink()
        print("Previous account selection restored; no sessions were restarted.")


def main(arguments: list[str], *, root: Path) -> int:
    parser = argparse.ArgumentParser(prog="claude-codex account")
    commands = parser.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", help="Authorize another account without changing running sessions")
    login.add_argument("alias")
    commands.add_parser("list", help="Show private account aliases without email or credentials")
    commands.add_parser("recover", help="Restore account selection after an interrupted switch")
    for action in ("check", "use"):
        commands.add_parser(action).add_argument("alias")
    options = parser.parse_args(arguments)
    if options.command == "login":
        oauth_login(root, options.alias)
    elif options.command == "check":
        check_account(root, options.alias)
        print("Account inference verified; active sessions were not changed.")
    elif options.command == "use":
        switch_account(root, options.alias)
    elif options.command == "recover":
        recover_account(root)
    else:
        entries = local_accounts(root)
        aliases = {}
        for path in (root / "accounts").glob("*.json"):
            if ALIAS.fullmatch(path.stem):
                record = private_json(path)
                if record.get("type") == "codex":
                    aliases[fingerprint(record)] = path.stem
        installed = {row["fingerprint"] for row in entries}
        for row in entries:
            alias = aliases.get(row["fingerprint"], "account-" + row["fingerprint"])
            print(alias + (" [retained, disabled]" if row["record"].get("disabled") else " [selected]"))
        for identity, alias in aliases.items():
            if identity not in installed:
                print(alias + " [staged]")
    return 0
