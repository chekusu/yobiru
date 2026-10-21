"""Receiver identity failures with isolated homes, simulated CLI failures and socket-pair HTTP.

The slow CLI test executes a local Python stub; no test contacts a real Tailscale daemon.
"""
import http.client
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from support.pkg import REPO


spec = importlib.util.spec_from_file_location("tailmsg_identity_under_test", str(REPO / "tailmsg.py"))
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
core.log = lambda *args: None

IP = "100.64.0.42"
PEER = "alpha.example.ts.net"
ME = "bravo.example.ts.net"
WHOIS = {"Node": {"Name": PEER + ".", "Hostinfo": {"Hostname": "alpha"}}}
UNAVAILABLE = {"ok": False, "error": "identity lookup unavailable", "code": "identity_unavailable"}


class IdentityLookupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        (self.home / "config.json").write_text(json.dumps({"access_policy": "tailnet"}))
        self.ctx = core.Ctx(self.home)

    def cli_result(self, stdout="", stderr="", returncode=0):
        return subprocess.CompletedProcess(["tailscale"], returncode, stdout, stderr)

    def request(self, path="/info", body=None, ip=IP):
        """Run the actual HTTP handler with a synthetic peer address and a five-second client."""
        server, client = socket.socketpair()
        client.settimeout(5)
        errors = []

        def serve():
            try:
                core.make_handler(self.ctx, ME)(server, (ip, 40000), None)
            except Exception as exc:
                errors.append(exc)
            finally:
                server.close()

        raw = b"" if body is None else json.dumps(body).encode()
        method = "GET" if body is None else "POST"
        headers = ("%s %s HTTP/1.1\r\nHost: receiver\r\nConnection: close\r\n"
                   "Content-Type: application/json\r\nContent-Length: %d\r\n\r\n" %
                   (method, path, len(raw))).encode()
        worker = threading.Thread(target=serve, daemon=True)
        response = None
        worker.start()
        try:
            client.sendall(headers + raw)
            response = http.client.HTTPResponse(client)
            response.begin()
            result = response.status, dict(response.getheaders()), json.loads(response.read())
        finally:
            if response is not None:
                response.close()
            client.close()
            worker.join(16)
        self.assertFalse(worker.is_alive(), "request handler did not terminate")
        if errors:
            raise errors[0]
        return result

    def assert_unavailable(self, response):
        status, headers, body = response
        self.assertEqual(503, status)
        self.assertEqual(UNAVAILABLE, body)
        self.assertEqual("1", headers.get("Retry-After"))

    def test_timeout_is_retryable_and_next_request_recovers(self):
        failure = subprocess.TimeoutExpired(["tailscale", "whois"], 3)
        with mock.patch.object(core.subprocess, "run", side_effect=[failure, self.cli_result(json.dumps(WHOIS))]) as run:
            self.assert_unavailable(self.request())
            self.assertNotIn(IP, self.ctx._whois_cache)
            self.assertEqual(200, self.request()[0])
        self.assertEqual(2, run.call_count)
        self.assertTrue(all(call.kwargs["timeout"] == 3 for call in run.call_args_list))

    def test_daemon_unreachable_is_retryable(self):
        for error in ("failed to connect to local tailscaled",
                      "Failed to connect to local Tailscale daemon for /localapi/v0/whois: connection refused"):
            with self.subTest(error=error), mock.patch.object(
                    core.subprocess, "run", return_value=self.cli_result(stderr=error, returncode=1)):
                self.assert_unavailable(self.request())
                self.assertNotIn(IP, self.ctx._whois_cache)

    def test_expired_cache_cannot_authorize_when_lookup_fails(self):
        cached = {"node": PEER, "ip": IP}
        self.ctx._whois_cache[IP] = (time.time() - 1, cached)
        with mock.patch.object(core.subprocess, "run", side_effect=subprocess.TimeoutExpired("tailscale", 3)):
            self.assert_unavailable(self.request())
        self.assertLess(self.ctx._whois_cache[IP][0], time.time())

    def test_fresh_cache_avoids_lookup(self):
        self.ctx._whois_cache[IP] = (time.time() + 60, {"node": PEER, "ip": IP})
        with mock.patch.object(core.subprocess, "run", side_effect=AssertionError("unexpected lookup")) as run:
            self.assertEqual(200, self.request()[0])
        run.assert_not_called()

    def test_unknown_identity_and_ordinary_cli_errors_remain_forbidden(self):
        cases = [self.cli_result(json.dumps(value)) for value in ({}, {"Node": None}, {"Node": {}})]
        cases += [self.cli_result(stderr=error, returncode=1) for error in (
            "peer not found", "no matching peer", "permission denied", "access denied", "operation not permitted",
            "Failed to connect to local Tailscale daemon: permission denied",
            "Failed to connect to local Tailscale daemon: Access is denied.", "unknown CLI failure")]
        cases += [FileNotFoundError("missing test executable"), PermissionError("test executable permission denied")]
        for outcome in cases:
            self.ctx._whois_cache.clear()
            behavior = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
            with self.subTest(outcome=outcome), mock.patch.object(core.subprocess, "run", **behavior):
                status, headers, body = self.request()
                self.assertEqual(403, status)
                self.assertEqual({"ok": False, "error": "unknown tailnet peer"}, body)
                self.assertNotIn("Retry-After", headers)

    def test_policy_denial_remains_forbidden(self):
        (self.home / "config.json").write_text(json.dumps({"access_policy": "tailnet", "deny": ["alpha"]}))
        with mock.patch.object(core.subprocess, "run", return_value=self.cli_result(json.dumps(WHOIS))):
            status, headers, body = self.request()
        self.assertEqual(403, status)
        self.assertEqual("peer denied by access policy", body["error"])
        self.assertNotIn("Retry-After", headers)

    def test_unavailable_identity_blocks_all_authenticated_routes_before_side_effects(self):
        message = {"v": 1, "id": "message_0001", "from": PEER, "to": ME,
                   "ts": "2026-10-17T00:00:00Z", "text": "isolated fixture", "wake": "none"}
        routes = [("/info", None), ("/msg", message), ("/f/" + "a" * 32 + "/fixture.txt", None),
                  ("/requests/message_0001", None)]
        with mock.patch.object(core.subprocess, "run", side_effect=subprocess.TimeoutExpired("tailscale", 3)), \
                mock.patch.object(self.ctx, "native_name") as native_name, \
                mock.patch.object(core, "info_payload") as info, \
                mock.patch.object(core, "incoming_request_status") as request_status, \
                mock.patch.object(core, "run_hook") as wake:
            for path, body in routes:
                with self.subTest(path=path):
                    self.assert_unavailable(self.request(path, body))
        native_name.assert_not_called()
        info.assert_not_called()
        request_status.assert_not_called()
        wake.assert_not_called()
        self.assertEqual([], list(self.ctx.inbox.iterdir()))
        self.assertEqual([], list(self.ctx.outbox.iterdir()))

    def test_non_tailnet_source_is_rejected_before_lookup(self):
        with mock.patch.object(core.subprocess, "run", side_effect=AssertionError("unexpected lookup")) as run:
            status, headers, body = self.request(ip="192.0.2.42")
        self.assertEqual(403, status)
        self.assertEqual("not a tailnet address", body["error"])
        self.assertNotIn("Retry-After", headers)
        run.assert_not_called()

    def test_typed_cli_timeout_preserves_general_cli_timeout(self):
        with mock.patch.object(core.subprocess, "run", side_effect=subprocess.TimeoutExpired("tailscale", 15)) as run:
            with self.assertRaises(core.TailscaleUnavailable):
                self.ctx.ts("status", "--json")
        self.assertEqual(15, run.call_args.kwargs["timeout"])

    def test_real_stub_timeout_returns_status_before_client_deadline_and_recovers(self):
        stub = self.home / "slow_tailscale.py"
        stub.write_text("import json, time\nfrom pathlib import Path\n"
                        "if not Path(__file__).with_suffix('.ready').exists():\n    time.sleep(20)\n"
                        "print(json.dumps(" + repr(WHOIS) + "))\n")
        self.ctx.cfg["tailscale_cmd"] = [sys.executable, str(stub)]
        start = time.monotonic()
        self.assert_unavailable(self.request())
        self.assertLess(time.monotonic() - start, 5)
        self.assertNotIn(IP, self.ctx._whois_cache)
        stub.with_suffix(".ready").touch()
        self.assertEqual(200, self.request()[0])


if __name__ == "__main__":
    unittest.main()
