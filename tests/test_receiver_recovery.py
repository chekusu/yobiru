"""Recovery regressions: isolated homes, synthetic LocalAPI, socket-pair HTTP, no real tailnet."""
import argparse
import contextlib
import hashlib
import http.client
import importlib.util
import io
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from support.numeric_http import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from support.pkg import REPO

spec = importlib.util.spec_from_file_location("recovery_core", REPO / "tailmsg.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
core.log = lambda *args: None
spec = importlib.util.spec_from_file_location("recovery_cli", REPO / "plugins/yobiru/scripts/yobiru.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
A, B, IP = "alpha.example.ts.net", "bravo.example.ts.net", "100.64.0.42"
INITIAL = {"node": B, "stable_id": "bravo-stable", "ips": ["100.64.0.43"]}


def status(**changes):
    own = {"DNSName": B + ".", "ID": "bravo-stable", "TailscaleIPs": ["100.64.0.43"]}
    own.update(changes)
    return {"BackendState": "Running", "Self": own}


class ReceiverRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.ctx = core.Ctx(self.home)
        self.current = status()
        self.ctx.ts = lambda *args, **kw: json.dumps(self.current)
        self.ctx.whois = self.ctx.whois_uncached = lambda ip: {"node": A, "ip": ip, "stable_id": "alpha-stable"}
        self.ctx.receiver_identity = core.ReceiverIdentity(self.ctx, INITIAL)

    def request(self, path, value=None, method=None):
        raw = b"" if value is None else value if isinstance(value, bytes) else json.dumps(value).encode()
        method = method or ("GET" if value is None else "POST")
        server, client = socket.socketpair()
        client.settimeout(5)
        errors = []
        def run():
            try:
                core.make_handler(self.ctx, B)(server, (IP, 40000), None)
            except Exception as exc:
                errors.append(exc)
            finally:
                server.close()
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            client.sendall(("%s %s HTTP/1.1\r\nHost: receiver\r\nConnection: close\r\n"
                            "Content-Type: application/json\r\nContent-Length: %d\r\n\r\n" %
                            (method, path, len(raw))).encode() + raw)
            reply = http.client.HTTPResponse(client)
            reply.begin()
            result = reply.status, json.loads(reply.read())
            reply.close()
        finally:
            client.close()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        if errors:
            raise errors[0]
        return result

    def message(self):
        return {"v": 1, "id": "0011223344556677", "from": A, "to": B,
                "ts": core.iso(core.now_utc()), "kind": "text", "text": "fixture", "wake": "none"}

    def test_healthy_identity_preserves_info_and_health(self):
        self.assertEqual(B, self.request("/info")[1]["node"])
        body = self.request("/health")[1]
        self.assertEqual({"status": "ok", "restart_required": False}, body["receiver_identity"])
        self.assertNotIn("bravo-stable", json.dumps(body))

    def test_rename_relogin_or_address_change_latches_without_rebinding_saved_work(self):
        saved = self.home / "inbox" / "existing.json"
        saved.write_text(json.dumps({"envelope": self.message(), "received": {"node": B}}))
        before = saved.read_bytes()
        for change in ({"DNSName": "renamed.example.ts.net."}, {"ID": "replacement-stable"},
                       {"TailscaleIPs": ["100.64.0.99"]}):
            with self.subTest(change=change):
                self.ctx.receiver_identity = core.ReceiverIdentity(self.ctx, INITIAL)
                self.current = status(**change)
                with mock.patch.object(self.ctx, "whois", wraps=self.ctx.whois) as whois:
                    for path, body in (("/info", None), ("/requests/0011223344556677", None),
                                       ("/f/" + "a" * 32 + "/saved.txt", None), ("/msg", self.message()),
                                       ("/requests/prepare", self.message())):
                        code, answer = self.request(path, body)
                        self.assertEqual((503, "receiver_identity_changed"), (code, answer["code"]))
                    self.assertEqual(1, whois.call_count)
                self.current = status()  # returning to the old name cannot unlock a confirmed transition
                self.assertEqual("receiver_identity_changed", self.ctx.receiver_identity.check())
                health = self.request("/health")[1]
                self.assertEqual((False, "degraded", True),
                                 (health["ok"], health["status"], health["receiver_identity"]["restart_required"]))
                self.assertEqual(before, saved.read_bytes())
                self.assertFalse((self.home / "requests").exists())
                self.assertEqual([], list(self.ctx.outbox.iterdir()))

    def test_missing_or_unavailable_local_identity_can_recover_without_rebinding(self):
        for current in ({}, {"BackendState": "NeedsLogin"}, status(ID=None), status(TailscaleIPs=[])):
            self.current = current
            code, body = self.request("/info")
            self.assertEqual((503, "receiver_identity_unavailable"), (code, body["code"]))
            self.current = status()
            self.assertEqual(200, self.request("/info")[0])
        self.assertEqual(core.ReceiverIdentity.key(INITIAL), self.ctx.receiver_identity.initial)

    def test_changed_identity_is_not_rechecked_or_unlatched_by_failed_lookup(self):
        self.current = status(ID="new-stable")
        self.assertEqual("receiver_identity_changed", self.ctx.receiver_identity.check())
        with mock.patch.object(self.ctx, "ts", side_effect=AssertionError("must stay latched")):
            self.assertEqual("receiver_identity_changed", self.ctx.receiver_identity.check())

    def test_localapi_deadline_and_lock_wait_are_bounded(self):
        stub = self.home / "slow.py"
        stub.write_text("import time\ntime.sleep(30)\n")
        self.ctx.cfg["tailscale_cmd"] = [sys.executable, str(stub)]
        del self.ctx.ts
        start = time.monotonic()
        self.assertEqual("receiver_identity_unavailable", self.ctx.receiver_identity.check())
        self.assertLess(time.monotonic() - start, 1.8)
        self.ctx.receiver_identity.lock.acquire()
        try:
            start = time.monotonic()
            self.assertEqual("receiver_identity_unavailable", self.ctx.receiver_identity.check())
            self.assertLess(time.monotonic() - start, 1.8)
        finally:
            self.ctx.receiver_identity.lock.release()

    def test_concurrent_checks_share_only_an_inflight_observation(self):
        calls, results = [], []
        gate, entered = threading.Event(), threading.Event()
        lock, arrived, count = self.ctx.receiver_identity.lock, threading.Condition(), [0]
        class ObservedLock:
            def acquire(self, **kwargs):
                with arrived:
                    count[0] += 1  # guard.check has already captured the generation
                    arrived.notify_all()
                return lock.acquire(**kwargs)
            def release(self):
                lock.release()
        self.ctx.receiver_identity.lock = ObservedLock()
        def lookup(*args, **kwargs):
            calls.append(kwargs["timeout"])
            entered.set()
            gate.wait(5)
            return json.dumps(self.current)
        self.ctx.ts = lookup
        first = threading.Thread(target=lambda: results.append(self.ctx.receiver_identity.check()))
        first.start()
        self.assertTrue(entered.wait(1))
        workers = [threading.Thread(target=lambda: results.append(self.ctx.receiver_identity.check())) for _ in range(31)]
        start = time.monotonic()
        for worker in workers:
            worker.start()
        try:
            with arrived:
                self.assertTrue(arrived.wait_for(lambda: count[0] == 32, timeout=1))
        finally:
            gate.set()
        for worker in [first] + workers:
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertEqual([None] * 32, results)
        self.assertEqual([1], calls)
        self.assertLess(time.monotonic() - start, 1)
        self.current = status(ID="replacement-stable")
        self.assertEqual("receiver_identity_changed", self.ctx.receiver_identity.check())
        self.assertEqual([1, 1], calls)

    def test_slow_but_valid_peer_identity_completes_preflight_and_tracked_send(self):
        def peer(ip):
            time.sleep(2.1)  # allowed by the receiver's existing three-second whois budget
            return {"node": A, "ip": ip}
        self.ctx.whois = peer
        with mock.patch.object(core, "is_tailnet_ip", return_value=True):
            server = ThreadingHTTPServer(("127.0.0.1", 0), core.make_handler(self.ctx, B))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            sender = core.Ctx(self.home / "sender")
            sender.self_node = lambda: {"node": A}
            sender.resolve = lambda peer: (B, "127.0.0.1")
            args = argparse.Namespace(peer=B + ":" + str(server.server_port), kind="request", text="slow fixture",
                                      reply_to=None, wake="none", file=[], ttl_hours=None,
                                      timeout_seconds=900, legacy_request=False)
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    core.cmd_send(sender, args)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)
        self.assertEqual(1, len(list(self.ctx.inbox.glob("*.json"))))

    def test_transient_identity_failure_leaves_same_message_retriable(self):
        path = self.ctx.inbox / "0011223344556677.json"
        record = {"envelope": self.message(), "received": {"peer": {"node": A}}}
        path.write_text(json.dumps(record))
        self.ctx.cfg["wake_adapter"] = {"type": "exec", "argv": ["fixture"]}
        self.current = {"BackendState": "NeedsLogin"}
        with mock.patch.object(core, "call_exec", return_value="exit 0") as wake:
            self.assertFalse(core.deliver_wake(self.ctx, path))
            wake.assert_not_called()
            self.current = status()
            self.assertTrue(core.deliver_wake(self.ctx, path))
            wake.assert_called_once()
        actual = json.loads(path.read_text())
        self.assertEqual(record["envelope"], actual["envelope"])
        self.assertEqual((True, 1), (actual["wake_delivery"]["ok"], actual["wake_delivery"]["attempts"]))
        self.assertNotIn("acked_at", actual)

    def test_change_after_peer_auth_blocks_message_before_storage(self):
        def peer(ip):
            self.current = status(ID="replacement-stable")
            return {"node": A, "ip": ip}
        self.ctx.whois = peer
        code, body = self.request("/msg", self.message())
        self.assertEqual((503, "receiver_identity_changed"), (code, body["code"]))
        self.assertEqual([], list(self.ctx.inbox.iterdir()))

    def test_change_during_upload_discards_temporary_bytes(self):
        payload, token, mid = b"safe fixture", "a" * 32, "0011223344556677"
        item = {"size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        def manifest(*args):
            self.current = status(ID="replacement-stable")  # after the initial identity check
            return item
        with mock.patch.object(core, "pull_upload_manifest", side_effect=manifest):
            code, body = self.request("/requests/%s/uploads/%s" % (mid, token), payload, "PUT")
        self.assertEqual((503, "receiver_identity_changed"), (code, body["code"]))
        self.assertEqual([], list((self.home / "requests/uploads" / mid).iterdir()))

    def test_change_during_peer_lookup_never_returns_stale_info(self):
        def peer(ip):
            self.current = status(DNSName="renamed.example.ts.net.")
            return {"node": A, "ip": ip}
        self.ctx.whois = peer
        with mock.patch.object(core, "info_payload") as metadata:
            code, body = self.request("/info")
        self.assertEqual((503, "receiver_identity_changed"), (code, body["code"]))
        metadata.assert_not_called()

    def test_change_during_response_destination_lookup_never_posts(self):
        for phase in ("resolve", "whois"):
            with self.subTest(phase=phase):
                self.current = status()
                self.ctx.receiver_identity = core.ReceiverIdentity(self.ctx, INITIAL)
                def resolve(peer):
                    if phase == "resolve":
                        self.current = status(ID="replacement-stable")
                    return A, IP
                def whois(ip):
                    if phase == "whois":
                        self.current = status(ID="replacement-stable")
                    return {"node": A, "ip": ip}
                self.ctx.resolve, self.ctx.whois = resolve, whois
                row = {"peer": A, "port": 7766, "envelope": "{}", "id": "0011223344556677"}
                with mock.patch.object(core, "http_json") as send:
                    self.assertEqual("receiver_identity_changed", core.deliver_response(self.ctx, row, {}))
                send.assert_not_called()

    def test_changed_identity_blocks_maintenance_response_and_adapter_effects(self):
        self.current = status(ID="replacement-stable")
        action = mock.Mock()
        self.assertEqual(("failed", {}, "receiver_identity_changed"), core.run_phase(action, self.ctx))
        action.assert_not_called()
        with mock.patch.object(core, "http_json") as send:
            self.assertEqual("receiver_identity_changed", core.deliver_response(self.ctx, {}, {}))
        send.assert_not_called()
        path = self.ctx.inbox / "0011223344556677.json"
        path.write_text(json.dumps({"envelope": self.message(), "received": {"peer": {"node": A}}}))
        self.ctx.cfg["wake_adapter"] = {"type": "exec", "argv": ["fixture"]}
        with mock.patch.object(core, "call_exec") as wake:
            self.assertFalse(core.deliver_wake(self.ctx, path))
        wake.assert_not_called()
        self.assertEqual(0, json.loads(path.read_text())["wake_delivery"]["attempts"])


class OfflineDiscoveryTests(unittest.TestCase):
    def test_offline_flag_still_probes_and_keeps_reachability_separate(self):
        node = {"node": B, "host": "bravo", "ip": IP, "self": False, "online": False}
        facts = {"cfg": None, "tailscale": {"node": A}}
        for result, expected in (({"kind": "info", "http_status": 200, "error": None,
                                  "info": {"version": "0.7.14", "agents": [], "warnings": []}}, "ok"),
                                 ({"kind": "unreachable", "http_status": None, "error": "timeout"}, "unreachable")):
            with mock.patch.object(cli, "probe_info", return_value=result) as probe:
                rows = cli.discovery_rows(None, facts, [node], None, 7766, 0.2, False)
            probe.assert_called_once_with(IP, 7766, 0.2, expect_node=B, host=B)
            self.assertFalse(rows[0]["tailscale_online"])
            self.assertEqual(expected, rows[0]["receiver"])
            self.assertEqual("allowed" if expected == "ok" else "unknown", rows[0]["send_permission"])


class PreflightReasonTests(unittest.TestCase):
    def test_each_rejection_has_correct_reason_and_no_request_or_attachment_state(self):
        cases = [(403, {}, "access denied"), (500, {}, "HTTP 500"), (404, {}, "HTTP 404"),
                 (200, [], "malformed"), (200, {"raw": "untrusted body"}, "malformed"),
                 (200, {"node": A, "capabilities": [core.REQUEST_CAPABILITY]}, "identity mismatch"),
                 (200, {"node": B, "capabilities": "wrong"}, "malformed"),
                 (200, {"node": B, "capabilities": []}, "does not advertise request-lifecycle-v1"),
                 (503, {"code": "receiver_identity_changed"}, "receiver_identity_changed"),
                 (503, {"code": "identity_unavailable"}, "identity_unavailable"),
                 (None, core.TransportError("fixture timeout"), "transport failure")]
        for code, result, reason in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as tmp:
                ctx = core.Ctx(tmp)
                ctx.self_node = lambda: {"node": A}
                ctx.resolve = lambda peer: (B, IP)
                args = argparse.Namespace(peer=B, kind="request", text="fixture", reply_to=None, wake="none",
                                          file=["must-not-be-read"], ttl_hours=None, timeout_seconds=900,
                                          legacy_request=False)
                options = {"side_effect": result} if isinstance(result, Exception) else {"return_value": (code, result)}
                error = io.StringIO()
                with mock.patch.object(core, "http_json", **options) as http, \
                        mock.patch.object(core.secrets, "token_hex") as create_id, \
                        self.assertRaises(SystemExit), contextlib.redirect_stderr(error):
                    core.cmd_send(ctx, args)
                self.assertIn(reason, error.getvalue())
                self.assertNotIn("untrusted body", error.getvalue())
                create_id.assert_not_called()
                self.assertEqual(1, http.call_count)
                self.assertEqual("GET", http.call_args.args[2])
                self.assertEqual((5, 5, 64 * 1024), tuple(http.call_args.kwargs[k] for k in ("timeout", "deadline", "limit")))
                self.assertFalse((Path(tmp) / "requests").exists())
                self.assertEqual([], list(ctx.outbox.iterdir()))


if __name__ == "__main__":
    unittest.main()
