"""Opt-in Friends CONNECT transport: real loopback proxy and verified TLS origin.

TLS tests need the openssl executable only to create temporary test certificates. No keys are
committed and no external service, account, credential or data home is used.
"""
import base64
import http.client
import io
import os
import select
import shutil
import socket
import socketserver
import ssl
import subprocess
import tempfile
import threading
import time
import traceback
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest import mock

from support.numeric_http import ThreadingHTTPServer
from support.public_harness import C


class ConnectProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, origin_port, mode="relay", reason="refused"):
        self.origin_port, self.mode, self.reason = origin_port, mode, reason
        self.seen, self.stop = [], threading.Event()
        super().__init__(("127.0.0.1", 0), ConnectHandler)
        self.url = "http://127.0.0.1:%d" % self.server_address[1]
        threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.stop.set()
        self.shutdown()
        self.server_close()


class ConnectHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(3)
        line = self.rfile.readline(8192)
        headers = http.client.parse_headers(self.rfile)
        self.server.seen.append((line, headers))
        try:
            mode = self.server.mode
            if mode == "strict_authority" and headers.get("Host") != line.decode("ascii").split()[1]:
                self.wfile.write(b"HTTP/1.1 400 CONNECT Host mismatch\r\nContent-Length: 0\r\n\r\n")
                self.wfile.flush()
                return
            if mode == "refuse":
                self.wfile.write(("HTTP/1.0 407 %s\r\nContent-Length: 0\r\n\r\n" % self.server.reason).encode())
                self.wfile.flush()
                return
            if mode == "disconnect":
                return
            if mode == "stall_connect":
                # Trickled headers must not reset the total deadline.
                self.wfile.write(b"HTTP/1.0 200 OK\r\nX-Slow: ")
                self.wfile.flush()
                while not self.server.stop.wait(0.04):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                return
            if mode == "slow_connect_stall_tls":
                self.server.stop.wait(0.35)
            self.wfile.write(b"HTTP/1.0 200 Connection established\r\n\r\n")
            self.wfile.flush()
            if mode in ("stall_tls", "slow_connect_stall_tls"):
                self.server.stop.wait(3)
                return
            # Fixed local destination: never resolve a host from an incoming CONNECT request.
            with socket.create_connection(("127.0.0.1", self.server.origin_port), timeout=3) as upstream:
                while not self.server.stop.is_set():
                    ready, _, _ = select.select([self.connection, upstream], [], [], 0.1)
                    for source in ready:
                        data = source.recv(65536)
                        if not data:
                            return
                        (upstream if source is self.connection else self.connection).sendall(data)
        except OSError:
            pass                   # clients deliberately abort failed TLS and deadline tests


class ProxyConfigTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_no_selector_ignores_ambient_proxy_variables(self):
        os.environ.update({"HTTPS_PROXY": "http://ambient.invalid:91", "https_proxy": "http://other.invalid",
                           "HTTP_PROXY": "http://ambient.invalid", "ALL_PROXY": "socks5://ambient.invalid",
                           "NO_PROXY": "*"})
        self.assertIsNone(C._https_proxy())
        conn = C._friends_connection(urllib.parse.urlsplit("https://gateway.example"), 2)
        self.assertEqual(("gateway.example", 443), (conn.host, conn.port))
        self.assertFalse(conn._tunnel_host)
        conn.close()

    def test_valid_proxy_authorities_and_encoded_auth(self):
        for value, host, port in (("http://proxy.example", "proxy.example", 80),
                                  ("http://proxy.example:8080/", "proxy.example", 8080),
                                  ("http://[::1]:8080", "::1", 8080),
                                  ("http://[::ffff:127.0.0.1]:8080", "::ffff:127.0.0.1", 8080),
                                  ("http://proxy.example.:80", "proxy.example.", 80)):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"YOBIRU_HTTPS_PROXY_ENV": "APP_PROXY",
                                                                        "APP_PROXY": value}):
                self.assertEqual((host, port, {}), C._https_proxy())
        os.environ.update({"YOBIRU_HTTPS_PROXY_ENV": "APP_PROXY",
                           "APP_PROXY": "http://us%40er:p%3Ass%2F%25%2B@proxy.example:81"})
        host, port, headers = C._https_proxy()
        self.assertEqual(("proxy.example", 81), (host, port))
        self.assertEqual("Basic " + base64.b64encode(b"us@er:p:ss/%+").decode(), headers["Proxy-Authorization"])

    def test_bad_selector_missing_and_empty_value_fail_closed(self):
        for selector in ("", "9BAD", "bad-name", "APP_PROXY=secret", "APP_PROXY\n"):
            with self.subTest(selector=selector), mock.patch.dict(os.environ, {"YOBIRU_HTTPS_PROXY_ENV": selector}):
                with self.assertRaises(C.ApiError) as cm:
                    C._https_proxy()
                self.assertEqual("proxy_config", cm.exception.code)
        os.environ["YOBIRU_HTTPS_PROXY_ENV"] = "APP_PROXY"
        for value in (None, ""):
            if value is not None:
                os.environ["APP_PROXY"] = value
            with self.assertRaises(C.ApiError):
                C._https_proxy()

    def test_malformed_values_have_redacted_tracebacks(self):
        values = ("https://user:secret@proxy.example", "socks5://proxy.example", "http:///path",
                  "http://user:secret@", "http://proxy.example:0", "http://proxy.example:65536",
                  "http://proxy.example:", "http://proxy.example:bad", "http://proxy.example/path",
                  "http://proxy.example?", "http://proxy.example#", "http://proxy.example/?secret",
                  "http://proxy.example/#secret", "http://proxy.example\n", "http://proxy.\texample",
                  "http://proxy.example\x7f", "http://proxy.example\x85", "http://proxy.example\\evil",
                  "http://proxy%2eexample", "http://[::1]evil", "http://[::1", "http://a..example", "http://proxy.example..",
                  "http://user@proxy.example", "http://:secret@proxy.example", "http://u:secret@x@proxy.example",
                  "http://us%3Aer:secret@proxy.example", "http://u:secret%0a@proxy.example",
                  "http://u:secret%7f@proxy.example", "http://u:secret%C2%85@proxy.example",
                  "http://u:secret%xx@proxy.example", "http://u:secret%@proxy.example",
                  "http://u:secret%FF@proxy.example")
        os.environ["YOBIRU_HTTPS_PROXY_ENV"] = "APP_PROXY"
        for value in values:
            with self.subTest(value=value):
                os.environ["APP_PROXY"] = value
                try:
                    C._https_proxy()
                except C.ApiError as exc:
                    self.assertEqual("proxy_config", exc.code)
                    formatted = traceback.format_exc()
                    self.assertNotIn(value, formatted)
                    self.assertNotIn("secret", formatted)
                else:
                    self.fail("accepted malformed proxy URL")

    def test_ipv6_destination_refused_on_old_stdlib_before_connect(self):
        os.environ.update({"YOBIRU_HTTPS_PROXY_ENV": "APP_PROXY", "APP_PROXY": "http://proxy.example:8080"})
        target = urllib.parse.urlsplit("https://[::1]:443")
        # Force the old-stdlib branch on every test runtime as well as checking the real runtime.
        with mock.patch.object(C.http.client, "HTTPConnection", mock.Mock(spec=[])):
            with self.assertRaises(C.ApiError) as cm:
                C._friends_connection(target, 2)
            self.assertEqual("proxy_config", cm.exception.code)
        if hasattr(http.client.HTTPConnection, "_wrap_ipv6"):
            conn = C._friends_connection(target, 2)
            self.assertEqual(("::1", 443), (conn._tunnel_host, conn._tunnel_port))
            conn.close()



@unittest.skipUnless(shutil.which("openssl"), "TLS loopback tests need the openssl executable")
class ProxyLoopbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="yobiru proxy TLS ")
        cls.addClassCleanup(cls.tmp.cleanup)
        root = Path(cls.tmp.name)
        cls.cert, key = root / "cert.pem", root / "key.pem"
        config = root / "cert.cnf"
        config.write_text("[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=ext\n"
                          "[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost,DNS:xn--bcher-kva.example,IP:::1\n"
                          "basicConstraints=critical,CA:TRUE\n"
                          "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign\n"
                          "extendedKeyUsage=serverAuth\n")
        subprocess.run([shutil.which("openssl"), "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                        "-config", str(config), "-keyout", str(key), "-out", str(cls.cert)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=20)
        # Some Apple LibreSSL builds ignore SSL_CERT_FILE for default trust loading. Do not
        # replace the production context or disable verification to make those tests pass.
        with mock.patch.dict(os.environ, {"SSL_CERT_FILE": str(cls.cert)}):
            trusted = ssl.create_default_context().get_ca_certs(binary_form=True)
        if ssl.PEM_cert_to_DER_cert(cls.cert.read_text()) not in trusted:
            raise unittest.SkipTest("runtime does not load SSL_CERT_FILE: " + ssl.OPENSSL_VERSION)
        cls.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.tls.load_cert_chain(str(cls.cert), str(key))

    def setUp(self):
        env = mock.patch.dict(os.environ, {"YOBIRU_HTTPS_PROXY_ENV": "APP_PROXY",
                                          "SSL_CERT_FILE": str(self.cert)}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.seen, self.sni = [], []
        outer = self

        class OriginHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_request(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.seen.append((self.command, self.path, self.headers, body))
                status = 307 if self.path.endswith("redirect") else 200
                raw = b'{"ok":true}' if self.path.endswith("json") else b"raw body"
                self.send_response(status)
                self.send_header("Content-Type", "application/json" if self.path.endswith("json") else "image/png")
                self.send_header("Content-Length", str(len(raw)))
                if status == 307:
                    self.send_header("Location", "https://redirect.invalid/never")
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_request

        self.origin = ThreadingHTTPServer(("127.0.0.1", 0), OriginHandler)
        self.tls.set_servername_callback(lambda sock, name, ctx: self.sni.append(name))
        self.origin.socket = self.tls.wrap_socket(self.origin.socket, server_side=True)
        threading.Thread(target=self.origin.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.close_origin)
        self.url = "https://localhost:%d/base" % self.origin.server_port
        self.proxy = self.new_proxy()
        os.environ["APP_PROXY"] = self.proxy.url.replace("http://", "http://us%40er:p%3Ass@")
        self.token = C.new_token("owner")

    def close_origin(self):
        self.origin.shutdown()
        self.origin.server_close()

    def new_proxy(self, mode="relay", reason="refused"):
        proxy = ConnectProxy(self.origin.server_port, mode, reason)
        self.addCleanup(proxy.close)
        return proxy

    def helpers(self, path="/json", timeout=2):
        return (lambda: C.api_call(self.url, "GET", path, token=self.token, timeout=timeout),
                lambda: C.post_raw(self.url, path, b"payload", {"Authorization": "Bearer " + self.token},
                                   timeout=timeout),
                lambda: C.get_raw(self.url, path, 128, timeout=timeout))

    def test_verified_get_post_and_raw_helpers_isolate_proxy_and_gateway_auth(self):
        self.assertEqual((200, {"ok": True}), self.helpers()[0]())
        self.assertEqual((200, {"ok": True}), C.api_call(self.url, "POST", "/json", {"msg": "hello"}, self.token))
        self.assertEqual((200, "application/json", b'{"ok":true}'), self.helpers()[1]())
        self.assertEqual((200, "image/png", b"raw body"), C.get_raw(self.url, "/avatar", 128))
        self.assertEqual(["localhost"] * 4, self.sni)
        self.assertEqual(["/base/json"] * 3 + ["/base/avatar"], [r[1] for r in self.seen])
        self.assertEqual(b'{"msg":"hello"}', self.seen[1][3])
        self.assertEqual(b"payload", self.seen[2][3])
        for line, headers in self.proxy.seen:
            self.assertEqual("CONNECT localhost:%d" % self.origin.server_port, line.decode().rsplit(" ", 1)[0])
            self.assertEqual("Basic dXNAZXI6cDpzcw==", headers.get("Proxy-Authorization"))
            self.assertIsNone(headers.get("Authorization"))
            self.assertNotIn(self.token, str(headers))
        for _, _, headers, _ in self.seen:
            self.assertIsNone(headers.get("Proxy-Authorization"))
        self.assertEqual(["Bearer " + self.token] * 3 + [None],
                         [r[2].get("Authorization") for r in self.seen])

    def test_idna_destination_keeps_verified_hostname_and_sni(self):
        self.url = self.url.replace("localhost", "b\u00fccher.example")
        self.assertEqual((200, {"ok": True}), self.helpers()[0]())
        self.assertEqual(["xn--bcher-kva.example"], self.sni)
        self.assertTrue(self.proxy.seen[0][0].startswith(b"CONNECT xn--bcher-kva.example:"))

    @unittest.skipUnless(hasattr(http.client.HTTPConnection, "_wrap_ipv6"),
                         "runtime explicitly refuses IPv6 CONNECT destinations")
    def test_ipv6_connect_host_matches_authority_and_keeps_verified_tls(self):
        proxy = self.new_proxy("strict_authority")
        os.environ["APP_PROXY"] = proxy.url.replace("http://", "http://us%40er:p%3Ass@")
        for port in (None, self.origin.server_port):
            with self.subTest(port=port):
                self.url = "https://[::1]" + (":%d" % port if port else "") + "/base"
                self.assertEqual((200, {"ok": True}), self.helpers()[0]())
                self.assertEqual((200, "application/json", b'{"ok":true}'), self.helpers()[1]())
                self.assertEqual((200, "image/png", b"raw body"), C.get_raw(self.url, "/avatar", 128))
                authority = "[::1]:%d" % (port or 443)
                for line, headers in proxy.seen[-3:]:
                    self.assertEqual("CONNECT " + authority, line.decode().rsplit(" ", 1)[0])
                    self.assertEqual([authority], headers.get_all("Host"))
                    self.assertEqual("Basic dXNAZXI6cDpzcw==", headers.get("Proxy-Authorization"))
                    self.assertIsNone(headers.get("Authorization"))
        self.assertEqual(6, len(self.seen))
        for _, _, headers, _ in self.seen:
            self.assertIsNone(headers.get("Proxy-Authorization"))

    def test_direct_default_and_plain_loopback_ignore_proxy_configuration(self):
        del os.environ["YOBIRU_HTTPS_PROXY_ENV"]
        os.environ.update({"HTTPS_PROXY": self.proxy.url, "https_proxy": self.proxy.url,
                           "HTTP_PROXY": self.proxy.url, "ALL_PROXY": self.proxy.url})
        self.assertEqual((200, {"ok": True}), self.helpers()[0]())
        self.assertEqual([], self.proxy.seen)
        # Even an invalid selector cannot change local HTTP MCP routing.
        os.environ["YOBIRU_HTTPS_PROXY_ENV"] = "invalid selector"
        conn = C._friends_connection(urllib.parse.urlsplit("http://127.0.0.1:1234"), 2)
        self.assertIs(type(conn), http.client.HTTPConnection)
        self.assertEqual(("127.0.0.1", 1234), (conn.host, conn.port))
        conn.close()

    def test_no_proxy_bypass_and_no_direct_fallback_on_refusal_or_disconnect(self):
        for mode in ("refuse", "disconnect"):
            proxy = self.new_proxy(mode)
            os.environ.update({"APP_PROXY": proxy.url, "NO_PROXY": "*", "no_proxy": "localhost"})
            for call in self.helpers():
                with self.assertRaises(C.ApiError) as cm:
                    call()
                self.assertEqual("unreachable", cm.exception.code)
            self.assertEqual(3, len(proxy.seen))
        self.assertEqual([], self.seen)

    def test_proxy_error_and_debug_output_never_echo_secrets(self):
        secret = "synthetic-proxy-secret"
        proxy = self.new_proxy("refuse", "user:" + secret)
        os.environ["APP_PROXY"] = proxy.url.replace("http://", "http://user:" + secret + "@")
        out = io.StringIO()
        with mock.patch.object(http.client.HTTPConnection, "debuglevel", 1), redirect_stdout(out), redirect_stderr(out):
            for call in self.helpers():
                try:
                    call()
                except C.ApiError:
                    out.write(traceback.format_exc())
                else:
                    self.fail("proxy refusal accepted")
        text = out.getvalue()
        self.assertNotIn(secret, text)
        self.assertNotIn(os.environ["APP_PROXY"], text)
        self.assertNotIn(self.token, text)
        self.assertNotIn(base64.b64encode(("user:" + secret).encode()).decode(), text)
        # Debug output must also stay disabled on a successful tunnel and gateway request.
        os.environ["APP_PROXY"] = self.proxy.url
        out = io.StringIO()
        with mock.patch.object(http.client.HTTPConnection, "debuglevel", 1), redirect_stdout(out), redirect_stderr(out):
            self.helpers()[0]()
        self.assertEqual("", out.getvalue())

    def test_untrusted_ca_and_hostname_mismatch_fail_before_bearer_request(self):
        for untrusted in (True, False):
            with self.subTest(untrusted=untrusted):
                if untrusted:
                    os.environ["SSL_CERT_FILE"] = str(self.cert.parent / "absent.pem")
                else:
                    os.environ["SSL_CERT_FILE"] = str(self.cert)
                    self.url = self.url.replace("localhost", "127.0.0.1")
                with self.assertRaises(C.ApiError) as cm:
                    self.helpers()[0]()
                self.assertIn("SSLCertVerificationError", cm.exception.detail)
        self.assertEqual([], self.seen)
        self.assertEqual(2, len(self.proxy.seen))

    def test_redirects_and_response_bounds_are_preserved(self):
        for call in self.helpers("/redirect"):
            with self.assertRaises(C.ApiError) as cm:
                call()
            self.assertEqual("redirect_refused", cm.exception.code)
        self.assertEqual(3, len(self.proxy.seen))
        self.assertEqual(["/base/redirect"] * 3, [r[1] for r in self.seen])
        with self.assertRaises(C.ApiError) as cm:
            C.get_raw(self.url, "/avatar", 2)
        self.assertEqual("response_too_large", cm.exception.code)
        with mock.patch.object(C, "MAX_RESPONSE_BYTES", 2):
            for call in self.helpers()[:2]:
                with self.assertRaises(C.ApiError) as cm:
                    call()
                self.assertEqual("response_too_large", cm.exception.code)

    def test_deadline_covers_connect_tls_and_their_combined_time(self):
        for mode in ("stall_connect", "stall_tls", "slow_connect_stall_tls"):
            proxy = self.new_proxy(mode)
            os.environ["APP_PROXY"] = proxy.url
            started = time.monotonic()
            with self.subTest(mode=mode), self.assertRaises(C.ApiError) as cm:
                C.api_call(self.url, "GET", "/json", timeout=0.6)
            self.assertEqual("unreachable", cm.exception.code)
            self.assertLess(time.monotonic() - started, 0.83, mode)
        self.assertEqual([], self.seen)


if __name__ == "__main__":
    unittest.main()
