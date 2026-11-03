"""Observe the account helper's network boundary with owned local servers."""

import json
import os
import socket
import socketserver
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
import urllib.request

import accounts


class FixtureServer(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer normally performs a reverse lookup during construction.
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.received.append((self.path, self.headers.get("Authorization") == "Bearer synthetic"))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", self.server.redirect_to)
            self.end_headers()
            return
        body = json.dumps({"server": self.server.label}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class AccountTransportTests(unittest.TestCase):
    def server(self, label):
        server = FixtureServer(("127.0.0.1", 0), Handler)
        server.received, server.label, server.redirect_to = [], label, ""
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()

        def cleanup():
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.addCleanup(cleanup)
        return server

    def test_manual_account_request_is_direct_even_when_a_proxy_is_set(self):
        target, proxy, redirected = self.server("target"), self.server("proxy"), self.server("redirected")
        host = "quota-fixture.invalid"
        target_url = f"http://{host}:{target.server_port}"
        target.redirect_to = f"http://{host}:{redirected.server_port}/destination"
        proxy_url = f"http://127.0.0.1:{proxy.server_port}"
        environment = {"http_proxy": proxy_url, "HTTP_PROXY": proxy_url, "https_proxy": proxy_url,
                       "HTTPS_PROXY": proxy_url, "no_proxy": "", "NO_PROXY": ""}
        lookup = socket.getaddrinfo

        def local_lookup(name, *args, **kwargs):
            return lookup("127.0.0.1" if name == host else name, *args, **kwargs)

        def unavailable_lookup(name, *args, **kwargs):
            if name == host:
                raise socket.gaierror("synthetic unavailable direct route")
            return lookup(name, *args, **kwargs)

        headers = {"Authorization": "Bearer synthetic"}
        with patch.dict(os.environ, environment, clear=True), patch.object(socket, "getaddrinfo", side_effect=local_lookup):
            # Control: the same process and URL use the proxy with the default opener.
            request = urllib.request.Request(target_url + "/control", headers=headers)
            with urllib.request.build_opener().open(request, timeout=3) as response:
                self.assertEqual(json.load(response), {"server": "proxy"})
            self.assertEqual(proxy.received, [(target_url + "/control", True)])
            self.assertEqual(target.received, [])

            self.assertEqual(accounts.request_json(target_url + "/usage", headers=headers, timeout=3),
                             {"server": "target"})
            self.assertEqual(target.received, [("/usage", True)])
            self.assertEqual(len(proxy.received), 1)
            with self.assertRaisesRegex(RuntimeError, "redirect denied"):
                accounts.request_json(target_url + "/redirect", headers=headers, timeout=3)
            self.assertEqual(redirected.received, [])
            self.assertEqual(len(proxy.received), 1)

        # A failed direct lookup does not silently retry through the configured proxy.
        with patch.dict(os.environ, environment, clear=True), patch.object(socket, "getaddrinfo", side_effect=unavailable_lookup):
            with self.assertRaisesRegex(RuntimeError, "could not reach the approved endpoint"):
                accounts.request_json(target_url + "/usage", headers=headers, timeout=3)
        self.assertEqual(len(proxy.received), 1)
        self.assertEqual(redirected.received, [])


if __name__ == "__main__":
    unittest.main()
