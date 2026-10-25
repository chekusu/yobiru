"""Failure-path diagnostics using isolated data homes, fake Tailscale and loopback HTTP only."""
import argparse
import contextlib
import importlib.util
import io
import json
import re
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler
from support.numeric_http import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from support.pkg import REPO, Package


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


core = load("transport_core", REPO / "tailmsg.py")
Y = load("transport_wrapper", REPO / "plugins/yobiru/scripts/yobiru.py")
PEER = "bravo.example.ts.net"
MID = "0123456789abcdef"


class SlowPeer:
    """Receive a request, but withhold its response until the client has timed out."""
    def __init__(self):
        self.requests = []
        self.release = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                owner.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                owner.release.wait(5)

            def do_GET(self):
                owner.requests.append(self.path)
                owner.release.wait(5)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


class TransportTests(unittest.TestCase):
    def slow_peer(self):
        server = SlowPeer()
        self.addCleanup(server.close)
        return server

    def test_timeout_after_receiver_stored_message_is_unknown_not_retried(self):
        server = self.slow_peer()
        with self.assertRaises(core.TransportError) as raised:
            core.http_json("127.0.0.1", server.port, "POST", "/msg",
                           {"id": MID, "text": "中文 🙂"}, host=PEER, timeout=0.1)
        detail = str(raised.exception)
        self.assertIn(PEER, detail)
        self.assertIn("127.0.0.1:%d" % server.port, detail)
        self.assertIn("timed out", detail)
        self.assertIn("Delivery is unknown", detail)
        self.assertIn(MID, detail)
        self.assertIn("tailscale ping --c 3 --timeout 3s " + PEER, detail)
        self.assertIn("new send creates a new ID", detail)
        self.assertEqual([{"id": MID, "text": "中文 🙂"}], server.requests)

    def test_health_timeout_names_endpoint_without_delivery_claim(self):
        server = self.slow_peer()
        result = Y.probe_health("127.0.0.1", server.port, timeout=0.1, peer=PEER)
        self.assertFalse(result["reachable"])
        self.assertEqual("TimeoutError", result["error"])
        self.assertIn("GET /health to " + PEER, result["detail"])
        self.assertIn("127.0.0.1:%d" % server.port, result["detail"])
        self.assertNotIn("Delivery", result["detail"])
        self.assertEqual(["/health"], server.requests)

    def test_health_timeout_name_is_stable_for_legacy_socket_timeout(self):
        # Before Python 3.10, socket.timeout was a separate OSError subclass named "timeout".
        legacy_timeout = type("timeout", (OSError,), {})
        for timeout_type in (socket.timeout, legacy_timeout):
            for stage in ("request", "getresponse", "read"):
                with self.subTest(timeout_type=timeout_type.__name__, stage=stage):
                    conn = mock.Mock()
                    target = conn.getresponse.return_value if stage == "read" else conn
                    getattr(target, stage).side_effect = timeout_type("private-detail\n\x1b[31m")
                    with mock.patch.object(Y.socket, "timeout", timeout_type), \
                            mock.patch.object(Y.http.client, "HTTPConnection", return_value=conn):
                        result = Y.probe_health("127.0.0.1", 9999, peer=PEER)
                    self.assertFalse(result["reachable"])
                    self.assertEqual("TimeoutError", result["error"])
                    self.assertIn("GET /health to " + PEER, result["detail"])
                    self.assertNotIn("Delivery", result["detail"])
                    self.assertNotIn("private-detail", result["error"])
                    self.assertNotIn("\n", result["detail"])
                    self.assertNotIn("\x1b", result["detail"])
                    conn.close.assert_called_once()

    def test_health_non_timeout_error_names_are_preserved(self):
        for error_type in (ConnectionRefusedError, Y.http.client.BadStatusLine, ValueError):
            with self.subTest(error_type=error_type.__name__):
                conn = mock.Mock()
                conn.getresponse.side_effect = error_type("private-detail\n\x1b[31m")
                with mock.patch.object(Y.http.client, "HTTPConnection", return_value=conn):
                    result = Y.probe_health("127.0.0.1", 9999, peer=PEER)
                self.assertFalse(result["reachable"])
                self.assertEqual(error_type.__name__, result["error"])
                self.assertNotIn("private-detail", result["error"])
                if issubclass(error_type, Y.http.client.HTTPException):
                    self.assertNotIn("private-detail", result["detail"])
                self.assertNotIn("\n", result["detail"])
                self.assertNotIn("\x1b", result["detail"])
                conn.close.assert_called_once()

    def test_discovery_timeout_keeps_machine_readable_error_and_adds_context(self):
        server = self.slow_peer()
        result = Y.probe_info("127.0.0.1", server.port, timeout=0.1, expect_node=PEER)
        self.assertEqual(("unreachable", "timeout"), (result["kind"], result["error"]))
        self.assertIn("GET /info to " + PEER, result["detail"])
        self.assertIn("tailscale ping", result["detail"])

    def test_dispatcher_error_names_are_stable_across_python_versions(self):
        legacy_timeout = type("timeout", (OSError,), {})
        for timeout_type in (socket.timeout, legacy_timeout):
            for error_type, expected in ((timeout_type, "TimeoutError"),
                                         (ConnectionRefusedError, "ConnectionRefusedError"),
                                         (ValueError, "ValueError")):
                with self.subTest(timeout_type=timeout_type.__name__, error_type=error_type.__name__):
                    bridge = mock.Mock()
                    bridge.diagnostics.side_effect = error_type("private-detail\n\x1b[31m")
                    with mock.patch.object(Y.socket, "timeout", timeout_type), \
                            mock.patch.object(Y, "bridge_module", return_value=bridge):
                        result = Y.dispatcher_diagnostics(Path("unused"), cfg={})
                    self.assertEqual({"state": "unknown", "error": expected}, result)

    def test_notice_error_names_preserve_delivery_outcomes_across_python_versions(self):
        legacy_timeout = type("timeout", (OSError,), {})
        for timeout_type in (socket.timeout, legacy_timeout):
            for error_type, expected in ((timeout_type, "TimeoutError"),
                                         (ConnectionRefusedError, "ConnectionRefusedError")):
                for stage in ("connect", "request", "getresponse", "read"):
                    with self.subTest(timeout_type=timeout_type.__name__,
                                      error_type=error_type.__name__, stage=stage):
                        conn = mock.Mock()
                        target = conn.getresponse.return_value if stage == "read" else conn
                        getattr(target, stage).side_effect = error_type("private-detail\n\x1b[31m")
                        with mock.patch.object(Y.socket, "timeout", timeout_type), \
                                mock.patch.object(Y.http.client, "HTTPConnection", return_value=conn):
                            result = Y.post_notice("127.0.0.1", 9999, PEER, {"id": MID})
                        if stage == "connect":
                            self.assertEqual(("not_sent", "unreachable (%s)" % expected, None), result)
                            conn.request.assert_not_called()
                        else:
                            self.assertEqual(("uncertain", "no complete answer after sending (%s)" % expected,
                                              None), result)
                            conn.request.assert_called_once()
                        conn.connect.assert_called_once()
                        conn.close.assert_called_once()

    def test_http_rejection_json_and_non_json_bodies_are_unchanged(self):
        for raw, expected in ((b'{"error":"policy denied"}', {"error": "policy denied"}),
                              ("日本語 🙂".encode(), {"raw": "日本語 🙂"})):
            with self.subTest(raw=raw):
                conn = mock.Mock()
                conn.getresponse.return_value.status = 403
                conn.getresponse.return_value.read.return_value = raw
                with mock.patch.object(core.http.client, "HTTPConnection", return_value=conn):
                    self.assertEqual((403, expected), core.http_json("127.0.0.1", 9999, "POST", "/msg",
                                                                  {"id": MID}, host=PEER))
                conn.request.assert_called_once()
                conn.close.assert_called_once()

    def test_connection_closes_on_header_and_body_errors_and_ipv6_is_unambiguous(self):
        for stage, error in (("request", ConnectionRefusedError("refused")),
                             ("getresponse", ConnectionResetError("reset")),
                             ("read", core.http.client.IncompleteRead(b"part", 3))):
            with self.subTest(stage=stage):
                conn = mock.Mock()
                target = conn.getresponse.return_value if stage == "read" else conn
                getattr(target, stage).side_effect = error
                with mock.patch.object(core.http.client, "HTTPConnection", return_value=conn):
                    with self.assertRaises(core.TransportError) as raised:
                        core.http_json("::1", 9999, "GET", "/health", host=PEER)
                self.assertIn("[::1]:9999", str(raised.exception))
                self.assertIn(type(error).__name__, str(raised.exception))
                conn.close.assert_called_once()

    def test_exception_details_do_not_reflect_remote_content_or_terminal_controls(self):
        error = core.http.client.BadStatusLine("http://peer/f/private-grant/file\n\x1b[31m" + "x" * 1000)
        detail = core.transport_diagnostic(PEER, "127.0.0.1", 9999, "file fetch", error)
        self.assertIn("BadStatusLine", detail)
        self.assertNotIn("private-grant", detail)
        self.assertNotIn("\n", detail)
        self.assertNotIn("\x1b", detail)
        detail = core.transport_diagnostic(PEER, "127.0.0.1", 9999, "GET /health", OSError("\n\x1b" + "x" * 1000))
        self.assertNotIn("\n", detail)
        self.assertNotIn("\x1b", detail)
        self.assertNotIn("x" * 201, detail)

    def test_fetch_transport_errors_exclude_grant_token_and_local_errors_stay_local(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(core, "log"):
            ctx = core.Ctx(tmp)
            entry = {"name": "report.txt", "url": "http://%s/f/private-grant/report.txt" % PEER,
                     "sha256": "0" * 64, "size": 1}
            record = {"envelope": {"id": MID, "fetch": [entry]}, "received": {"peer": {"node": PEER}}}
            for kind in ("network", "disk", "unwritable"):
                with self.subTest(kind=kind):
                    if kind == "unwritable":        # a real local failure: the partial download path is a folder
                        (ctx.inbox / MID / "files" / "report.txt.part").mkdir(parents=True)
                    conn = mock.Mock()
                    conn.getresponse.return_value.status = 200
                    conn.getresponse.return_value.read.return_value = b"x"
                    if kind == "network":
                        conn.getresponse.side_effect = socket.timeout("timed out")
                    # The download file is created 0600 through private_open; a read-only disk fails there.
                    disk = mock.patch.object(core, "private_open", side_effect=PermissionError("disk read-only")) \
                        if kind == "disk" else contextlib.nullcontext()
                    output = io.StringIO()
                    with mock.patch.object(core, "load_msg", return_value=(None, record)), \
                            mock.patch.object(ctx, "resolve", return_value=(PEER, "127.0.0.1")), \
                            mock.patch.object(core, "is_tailnet_ip", return_value=True), \
                            mock.patch.object(core.http.client, "HTTPConnection", return_value=conn), \
                            contextlib.redirect_stdout(output), disk:
                        with self.assertRaises(SystemExit) as raised:
                            core.cmd_fetch(ctx, argparse.Namespace(id=MID))
                    self.assertEqual(2, raised.exception.code)
                    detail = output.getvalue()
                    self.assertNotIn("private-grant", detail)
                    if kind == "network":
                        self.assertIn("file fetch to " + PEER, detail)
                        self.assertIn("127.0.0.1:80", detail)
                    else:
                        self.assertIn("disk read-only" if kind == "disk" else "Is a directory", detail)
                        self.assertNotIn("Tailscale", detail)
                    conn.close.assert_called_once()


class PackagedDiagnosticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = Package()

    @classmethod
    def tearDownClass(cls):
        cls.pkg.cleanup()

    def test_real_connection_refusal_in_packaged_send_and_doctor(self):
        pkg = self.pkg
        user = pkg.new_home("refusal")
        home = user / ".tailmsg"
        home.mkdir()
        # Hold a bound but non-listening port: it cannot be stolen by another listener.
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
            (home / "config.json").write_text(json.dumps({"port": port, "bind": "127.0.0.1"}))
            calls = pkg.root / "calls.jsonl"
            state = pkg.fake_state("refusal", calls=str(calls), status={"BackendState": "Running",
                "Self": {"DNSName": "alpha.example.ts.net.", "TailscaleIPs": ["127.0.0.1"]},
                "Peer": {"p": {"DNSName": PEER + ".", "HostName": "bravo",
                               "TailscaleIPs": ["127.0.0.1"], "Online": True}}})
            opts = {"tailscale": True, "extra": {"FAKE_TS_STATE": str(state)}}
            result = pkg.run(["send", "bravo/codex", "hello 🙂"], user, **opts)
            self.assertEqual(1, result.returncode)
            self.assertEqual("", result.stdout)
            self.assertIn("POST /msg to " + PEER, result.stderr)
            self.assertIn("127.0.0.1:%d" % port, result.stderr)
            # Linux refuses a connection to a bound but non-listening port; a host whose firewall silently drops
            # it (some macOS setups) times out instead. Either way the message was not delivered.
            refused = "ConnectionRefusedError" in result.stderr
            if sys.platform.startswith("linux"):
                self.assertTrue(refused, result.stderr)
            else:
                self.assertTrue(refused or re.search(r"\b(?:TimeoutError|timeout)\b", result.stderr), result.stderr)
            self.assertIn("Delivery is unknown for message", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            _, data = pkg.run_json(["doctor", "--json"], user, **opts)
            receiver = next(c for c in data["checks"] if c["check"] == "receiver")
            self.assertIn("GET /health to alpha.example.ts.net", receiver["detail"])
            self.assertIn("127.0.0.1:%d" % port, receiver["detail"])
            self.assertIn("tailscale ping", receiver["detail"])
            self.assertFalse(data["changed"])
            queries = [json.loads(line) for line in calls.read_text().splitlines()]
            self.assertTrue(all(query in (["status", "--json"], ["debug", "prefs"]) for query in queries), queries)


if __name__ == "__main__":
    unittest.main()
