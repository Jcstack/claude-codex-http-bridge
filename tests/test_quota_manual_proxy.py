"""Test an explicit loopback proxy using synthetic TLS and credentials only."""

import contextlib
import json
import os
from pathlib import Path
import shutil
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import accounts
import codex_usage
import quota_recovery as recovery


def generate_certificate(directory: Path, hostname: str) -> None:
    directory.mkdir()
    go = shutil.which("go")
    if go:
        env = {name: os.environ[name] for name in ("PATH", "HOME", "USERPROFILE", "LOCALAPPDATA", "SYSTEMROOT",
                                                  "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "TMPDIR")
               if name in os.environ}
        env.update(GOWORK="off", GO111MODULE="off", GOPROXY="off", GOSUMDB="off", GOTOOLCHAIN="local", GOENV="off")
        program = Path(__file__).with_name("generate_quota_tls_fixture.go")
        command = [go, "run", str(program), str(directory), hostname]
    else:
        executable = shutil.which("openssl")
        if not executable:
            if os.environ.get("BRIDGE_REQUIRE_QUOTA_TLS_FIXTURE") == "1":
                raise RuntimeError("The required synthetic TLS certificate generator is unavailable")
            raise unittest.SkipTest("Go or OpenSSL is required to generate a disposable TLS fixture")
        env = None
        command = [executable, "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes", "-days", "1",
                   "-keyout", str(directory / "key.pem"), "-out", str(directory / "certificate.pem"),
                   "-subj", "/CN=" + hostname, "-addext", "subjectAltName=DNS:" + hostname,
                   "-addext", "basicConstraints=critical,CA:TRUE"]
    subprocess.run(command, env=env, cwd=directory, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.PIPE, timeout=90, check=True)


def read_headers(stream) -> bytes:
    value = bytearray()
    while b"\r\n\r\n" not in value:
        part = stream.recv(4096)
        if not part:
            break
        value.extend(part)
        if len(value) > 16384:
            raise ValueError("Synthetic request headers exceeded their limit")
    return bytes(value)


class Proxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        try:
            initial = read_headers(self.request)
            lines = initial.split(b"\r\n")
            self.server.connect.append({
                "line": lines[0].decode("ascii", "replace"),
                "authorization": any(line.lower().startswith(b"authorization:") for line in lines[1:]),
                "private_before_tls": b"SYNTHETIC_ACCESS" in initial or b"SYNTHETIC_MANAGEMENT" in initial,
            })
            if self.server.mode == "reject":
                self.request.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
                return
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            with self.server.context.wrap_socket(self.request, server_side=True) as stream:
                request = read_headers(stream)
                lower = request.lower()
                self.server.encrypted.append({
                    "line": request.split(b"\r\n", 1)[0].decode("ascii", "replace"),
                    "host": b"\r\nhost: chatgpt.com\r\n" in lower,
                    "access": b"\r\nauthorization: bearer SYNTHETIC_ACCESS\r\n".lower() in lower,
                    "account": b"\r\nchatgpt-account-id: SYNTHETIC_ACCOUNT\r\n".lower() in lower,
                    "proxy_auth": b"\r\nproxy-authorization:" in lower,
                    "management": b"SYNTHETIC_MANAGEMENT" in request,
                })
                if self.server.mode == "redirect":
                    response = b"HTTP/1.1 302 Found\r\nLocation: https://redirect.invalid/\r\nContent-Length: 0\r\n\r\n"
                else:
                    body = self.server.body
                    response = (b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() +
                                b"\r\nConnection: close\r\n\r\n" + body)
                stream.sendall(response)
        except (OSError, ValueError):
            pass
        finally:
            self.server.finished.set()


class ManualQuotaProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="quota-tls-fixture-")
        root = Path(cls.temporary.name)
        try:
            generate_certificate(root / "correct", "chatgpt.com")
            generate_certificate(root / "wrong", "wrong.invalid")
            (root / "empty-certs").mkdir()
        except BaseException:
            cls.temporary.cleanup()
            raise
        cls.root = root

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def proxy(self, *, hostname="correct", mode="ok", body=None):
        server = Proxy(("127.0.0.1", 0), ProxyHandler)
        server.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.context.load_cert_chain(self.root / hostname / "certificate.pem", self.root / hostname / "key.pem")
        server.mode = mode
        server.body = body if body is not None else json.dumps({"rate_limit": {"allowed": True}}).encode()
        server.connect, server.encrypted = [], []
        server.finished = threading.Event()
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()

        def cleanup():
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.addCleanup(cleanup)
        return server

    @contextlib.contextmanager
    def environment(self, certificate: str, *, ambient_port=1, keylog=None):
        environment = {"SSL_CERT_FILE": str(self.root / certificate / "certificate.pem"),
                       "SSL_CERT_DIR": str(self.root / "empty-certs"),
                       "HTTP_PROXY": f"http://127.0.0.1:{ambient_port}",
                       "HTTPS_PROXY": f"http://127.0.0.1:{ambient_port}",
                       "NO_PROXY": "*", "no_proxy": "*"}
        if keylog is not None:
            environment["SSLKEYLOGFILE"] = str(keylog)
        lookup = socket.getaddrinfo

        def only_loopback(host, *args, **kwargs):
            if host != "127.0.0.1":
                raise AssertionError("Synthetic fixture tried to resolve an external host")
            return lookup(host, *args, **kwargs)

        with patch.dict(os.environ, environment), patch.object(socket, "getaddrinfo", side_effect=only_loopback):
            yield

    def test_explicit_proxy_tunnels_only_fixed_usage_over_verified_tls(self):
        proxy, ambient = self.proxy(), self.proxy(mode="reject")
        keylog = self.root / "must-not-create-keylog"
        with self.environment("correct", ambient_port=ambient.server_address[1], keylog=keylog):
            value = recovery._loopback_usage(proxy.server_address[1], "SYNTHETIC_ACCESS", "SYNTHETIC_ACCOUNT")
        self.assertEqual(value, {"rate_limit": {"allowed": True}})
        self.assertEqual(len(proxy.connect), 1)
        self.assertEqual(proxy.connect[0]["line"].split()[:2], ["CONNECT", "chatgpt.com:443"])
        self.assertFalse(proxy.connect[0]["authorization"] or proxy.connect[0]["private_before_tls"])
        self.assertEqual(proxy.encrypted, [{"line": "GET /backend-api/wham/usage HTTP/1.1", "host": True,
                                             "access": True, "account": True, "proxy_auth": False,
                                             "management": False}])
        self.assertEqual(ambient.connect, [])
        self.assertFalse(keylog.exists())

    def test_keylog_negative_control_sees_ambient_setting(self):
        if not hasattr(ssl.SSLContext, "keylog_filename"):
            self.skipTest("This interpreter does not support TLS key logging")
        keylog = self.root / "synthetic-control-keylog"
        env = {name: os.environ[name] for name in ("PATH", "HOME", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR")
               if name in os.environ}
        env["SSLKEYLOGFILE"] = str(keylog)
        child = subprocess.run([sys.executable, "-S", "-c",
                                'import ssl; print("enabled" if ssl.create_default_context().keylog_filename else "disabled")'],
                               cwd=self.root, env=env, capture_output=True, text=True, timeout=15, check=True)
        self.assertEqual(child.stdout.strip(), "enabled")
        self.assertTrue(keylog.is_file())

    def test_recovery_keeps_management_on_the_local_direct_path(self):
        body = json.dumps({"rate_limit": {"allowed": True, "limit_reached": False,
                         "secondary_window": {"used_percent": 0}}}).encode()
        proxy = self.proxy(body=body)
        record = {"account_id": "SYNTHETIC_ACCOUNT", "access_token": "SYNTHETIC_ACCESS",
                  "email": "fixture@example.invalid"}
        identity = accounts.fingerprint(record)
        selected = {"name": "fixture.json", "record": record, "fingerprint": identity}
        row = {"name": "fixture.json", "provider": "codex", "auth_index": "synthetic-index",
               "cooldowns": [{"scope": "credential", "reason": "credential_quota"}]}
        owner = type("Owner", (), {"state": {"pid": 10, "port": 12345}, "key": "SYNTHETIC_MANAGEMENT"})()
        local_requests = []

        def management(url, **options):
            self.assertTrue(url.startswith("http://127.0.0.1:12345/v8/management/"))
            self.assertEqual(options["headers"]["Authorization"], "Bearer SYNTHETIC_MANAGEMENT")
            local_requests.append(options.get("method", "GET"))
            return {"files": [row]} if url.endswith("credentials") else {"status": "ok"}

        with self.environment("correct"), patch.object(accounts, "switch_lock", return_value=contextlib.nullcontext()), \
             patch.object(accounts, "local_accounts", return_value=[selected]), \
             patch.object(accounts, "Management", return_value=owner), \
             patch.object(accounts, "request_json", side_effect=management), \
             patch.object(codex_usage, "active_bridge_auth", return_value=record):
            result = recovery.recover_once(self.root, usage_proxy_port=proxy.server_address[1])
        self.assertEqual(result, {"status": "recovered"})
        self.assertEqual(local_requests, ["GET", "GET", "POST"])
        self.assertEqual(len(proxy.encrypted), 1)
        self.assertFalse(proxy.connect[0]["private_before_tls"] or proxy.encrypted[0]["management"])

    def test_untrusted_or_wrong_hostname_never_receives_http_credentials(self):
        for presented, trusted in (("correct", "wrong"), ("wrong", "wrong")):
            with self.subTest(presented=presented, trusted=trusted):
                proxy = self.proxy(hostname=presented)
                with self.environment(trusted), self.assertRaisesRegex(RuntimeError, "Usage request failed"):
                    recovery._loopback_usage(proxy.server_address[1], "SYNTHETIC_ACCESS", "SYNTHETIC_ACCOUNT")
                self.assertTrue(proxy.finished.wait(5))
                self.assertEqual(len(proxy.connect), 1)
                self.assertFalse(proxy.connect[0]["private_before_tls"])
                self.assertEqual(proxy.encrypted, [])

    def test_proxy_rejection_redirect_and_invalid_data_fail_closed(self):
        for mode in ("reject", "redirect"):
            with self.subTest(mode=mode):
                proxy, ambient = self.proxy(mode=mode), self.proxy(mode="reject")
                with self.environment("correct", ambient_port=ambient.server_address[1]), \
                     self.assertRaisesRegex(RuntimeError, "Usage request failed"):
                    recovery._loopback_usage(proxy.server_address[1], "SYNTHETIC_ACCESS", "SYNTHETIC_ACCOUNT")
                self.assertTrue(proxy.finished.wait(5))
                self.assertEqual(ambient.connect, [])
                self.assertFalse(proxy.connect[0]["private_before_tls"])
                self.assertEqual(len(proxy.encrypted), 0 if mode == "reject" else 1)
        for value in (0, 65536, True, "8080"):
            with self.subTest(port=value), self.assertRaises(ValueError):
                recovery._loopback_usage(value, "SYNTHETIC_ACCESS", "SYNTHETIC_ACCOUNT")
        with self.assertRaises(ValueError):
            recovery._loopback_usage(8080, "SYNTHETIC_ACCESS\r\nHeader: injected", "SYNTHETIC_ACCOUNT")

    def test_bad_and_oversized_responses_fail_without_a_retry(self):
        for body in (b"not-json", b"[]", b'{"rate_limit":{},"rate_limit":{}}', b"x" * 2_000_001):
            with self.subTest(size=len(body)):
                proxy = self.proxy(body=body)
                with self.environment("correct"), self.assertRaisesRegex(RuntimeError, "Usage"):
                    recovery._loopback_usage(proxy.server_address[1], "SYNTHETIC_ACCESS", "SYNTHETIC_ACCOUNT")
                self.assertEqual(len(proxy.connect), 1)


if __name__ == "__main__":
    unittest.main()
