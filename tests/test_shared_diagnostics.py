"""Peer doctor checks use loopback HTTP fixtures; no Tailscale, real peers or live config is touched."""
import contextlib
import importlib.util
import io
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from support.numeric_http import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from support.pkg import REPO


spec = importlib.util.spec_from_file_location("yobiru_shared_diagnostics", REPO / "plugins/yobiru/scripts/yobiru.py")
Y = importlib.util.module_from_spec(spec)
spec.loader.exec_module(Y)


class SharedPeerDoctorTests(unittest.TestCase):
    def setUp(self):
        self.routes = {
            "/health": (200, {"ok": True, "v": 1}),
            "/info": (200, {"ok": True, "v": 1, "product": "yobiru", "version": "0.5.1",
                             "node": "receiver.example.ts.net", "agents": [],
                             "capabilities": ["request-lifecycle-v1", "request-pull-v1"]}),
        }
        self.requests = []
        self.delay = 0
        case = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                case.requests.append((self.command, self.path))
                time.sleep(case.delay)
                status, payload = case.routes.get(self.path, (404, {}))
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        original = Y.http_request

        def loopback(ip, port, *args, **kwargs):
            self.assertEqual("100.101.102.103", ip)
            return original("127.0.0.1", self.server.server_port, *args, **kwargs)

        self.redirect = mock.patch.object(Y, "http_request", side_effect=loopback)
        self.redirect.start()
        self.addCleanup(self.redirect.stop)

    def checks(self, timeout=0.5):
        return {c["check"]: c for c in Y.peer_doctor_checks({}, "100.101.102.103", 7766, timeout)}

    def test_peer_checks_get_health_info_and_capability_without_sending(self):
        checks = self.checks()
        for name in ("peer-target", "peer-transport", "peer-health", "peer-identity", "peer-pull"):
            self.assertEqual("ok", checks[name]["status"], checks[name])
        self.assertEqual([("GET", "/health"), ("GET", "/info")], self.requests)
        self.assertIn("do not prove", checks["peer-verification"]["detail"])
        template = checks["peer-network-policy"]["fix"]
        self.assertIn('"ip":["tcp:7766"]', template)
        self.assertIn("<CALLER_TAILSCALE_IP>", template)
        self.assertNotIn("100.101.102.103", template)

    def test_health_success_does_not_hide_identity_refusal(self):
        self.routes["/info"] = (403, {"error": "unknown Tailscale peer"})
        checks = self.checks()
        self.assertEqual("ok", checks["peer-health"]["status"])
        self.assertEqual("error", checks["peer-identity"]["status"])
        self.assertIn("identity or access policy", checks["peer-identity"]["detail"])
        self.assertNotIn("peer-pull", checks)

    def test_timeout_is_bounded_and_does_not_claim_acl_failure(self):
        self.delay = 0.25
        start = time.monotonic()
        checks = self.checks(timeout=0.05)
        self.assertLess(time.monotonic() - start, 0.2)
        self.assertEqual("error", checks["peer-transport"]["status"])
        self.assertIn("timeout", checks["peer-transport"]["detail"])
        self.assertIn("cannot identify", checks["peer-network-policy"]["detail"])
        self.assertNotIn("peer-identity", checks)

    def test_no_pull_capability_is_reported_without_fallback(self):
        self.routes["/info"][1]["capabilities"] = ["request-lifecycle-v1"]
        checks = self.checks()
        self.assertEqual("ok", checks["peer-identity"]["status"])
        self.assertEqual("warn", checks["peer-pull"]["status"])
        self.assertIn("not advertised", checks["peer-pull"]["detail"])
        self.assertEqual([("GET", "/health"), ("GET", "/info")], self.requests)

    def test_unrelated_http_service_is_not_accepted_as_yobiru(self):
        self.routes["/health"] = (200, {"status": "healthy"})
        self.routes["/info"] = (200, {"ok": True, "product": "something-else"})
        checks = self.checks()
        self.assertEqual("ok", checks["peer-transport"]["status"])
        self.assertEqual("warn", checks["peer-health"]["status"])
        self.assertEqual("error", checks["peer-identity"]["status"])

    def test_explicit_ip_works_when_shared_peer_has_no_discoverable_name(self):
        with mock.patch.object(Y, "tailnet_nodes", side_effect=AssertionError("must not require DNSName")):
            self.assertEqual("ok", self.checks()["peer-pull"]["status"])

    def test_nontailscale_address_does_not_probe(self):
        checks = Y.peer_doctor_checks({}, "192.0.2.20", 7766)
        self.assertEqual("error", checks[0]["status"])
        self.assertEqual([], self.requests)

    def test_capabilities_are_bounded_typed_data(self):
        payload = self.routes["/info"][1]
        payload["capabilities"] = [False, {"request-pull-v1": True}, "request-pull-v1\n", "request-pull-v1"]
        info, error = Y.validate_info(json.dumps(payload).encode())
        self.assertIsNone(error)
        self.assertEqual(["request-pull-v1"], info["capabilities"])

    def test_no_probe_skips_peer_http_and_keeps_home_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "absent home"
            output = io.StringIO()
            with mock.patch.object(Y, "gather", return_value={}) as gather, \
                    mock.patch.object(Y, "doctor_checks", return_value=[]), contextlib.redirect_stdout(output):
                self.assertEqual(0, Y.cmd_doctor(home, "test", ["--peer", "100.101.102.103", "--json", "--no-probe"]))
            gather.assert_called_once_with(home, "test", probe=False)
            self.assertFalse(home.exists())
            self.assertFalse(json.loads(output.getvalue())["changed"])
            self.assertEqual([], self.requests)


if __name__ == "__main__":
    unittest.main()
