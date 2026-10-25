"""Access-policy invariants through the pure evaluator and the actual HTTP request boundary.

Only loopback HTTP is used. Tailscale identity is supplied by a test stub; no real machine or runtime
configuration is read or changed.
"""
import datetime as dt
import http.client
import importlib.util
import ipaddress
import json
from pathlib import Path
import tempfile
import threading
import unittest
from support.numeric_http import ThreadingHTTPServer
from unittest.mock import patch

from support.pkg import REPO


spec = importlib.util.spec_from_file_location("tailmsg_policy_under_test", str(REPO / "tailmsg.py"))
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
core.log = lambda *args: None

PEER = {"node": "alpha.example.ts.net", "host": "workstation", "tags": ["tag:agents"],
        "user": "owner@example.invalid"}


class PolicyTests(unittest.TestCase):
    def test_new_and_legacy_config_defaults_are_distinct(self):
        for raw, mode, allowed in (({}, "tailnet", True), ({"allow": []}, "allowlist", False),
                                   ({"allow": ["alpha"]}, "allowlist", True),
                                   ({"access_policy": "tailnet", "allow": []}, "tailnet", True),
                                   ({"access_policy": "allowlist"}, "allowlist", False)):
            with self.subTest(raw=raw):
                cfg, _ = core.parse_config(raw)
                self.assertEqual(mode, cfg["access_policy"])
                self.assertEqual(allowed, core.access_decision(cfg, PEER)[0])
                self.assertEqual(allowed, core.access_decision(raw, PEER)[0])

    def test_rules_normalize_and_deny_has_precedence_for_every_rule_kind(self):
        for rule in ("ALPHA", "alpha.example.ts.net.", "WORKSTATION", "tag:agents", "user:OWNER@example.invalid"):
            with self.subTest(rule=rule):
                self.assertTrue(core.access_decision({"allow": [rule]}, PEER)[0])
                for mode in ("tailnet", "allowlist"):
                    cfg = {"access_policy": mode, "allow": [rule], "deny": [rule]}
                    self.assertEqual((False, "denied"), core.access_decision(cfg, PEER))
                    self.assertEqual((False, "denied"), core.access_decision(cfg, PEER, recipient_token=True))
                    self.assertEqual((False, "denied"), core.access_decision(cfg, PEER, self_discovery=True))
        self.assertEqual((True, "tailnet"), core.access_decision({"deny": ["other"]}, PEER))

    def test_malformed_policy_never_degrades_to_tailnet(self):
        bad = [None, [], {"access_policy": None}, {"access_policy": "public"}, {"access_policy": []}]
        for field in ("allow", "deny"):
            bad += [{field: value} for value in (None, "alpha", {}, [None], [1], [""], ["*"],
                                                 ["user:"], ["tag:"], ["alpha beta"], [" alpha"],
                                                 ["alpha\n"], ["https://example.invalid"])]
        for raw in bad:
            with self.subTest(raw=raw):
                with self.assertRaises(RuntimeError):
                    core.parse_access_policy(raw)
                self.assertEqual((False, "invalid_policy"), core.access_decision(raw, PEER))
                self.assertEqual((False, "invalid_policy"), core.access_decision(raw, PEER, recipient_token=True))

    def test_missing_or_malformed_identity_is_not_authenticated(self):
        bad = [None, {}, {"node": ""}, {"node": None}, {"node": " "}, {"node": "alpha\n"}, dict(PEER, tags="tag:agents"),
               dict(PEER, tags=[None]), dict(PEER, user=None), dict(PEER, ip="192.0.2.1"),
               dict(PEER, ip="192.168.1.1"), dict(PEER, ip="127.0.0.1"), dict(PEER, ip="invalid")]
        for peer in bad:
            with self.subTest(peer=peer):
                self.assertEqual((False, "unknown_peer"), core.access_decision({}, peer))

    def test_installed_bridge_loads_shared_policy_from_receiver_home(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            bridge_dir = home / "codex-local"
            bridge_dir.mkdir()
            (home / "tailmsg.py").write_bytes((REPO / "tailmsg.py").read_bytes())
            bridge_file = bridge_dir / "codex_bridge.py"
            bridge_file.write_bytes((REPO / "codex_bridge.py").read_bytes())
            spec = importlib.util.spec_from_file_location("installed_bridge_policy_test", str(bridge_file))
            bridge = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(bridge)
            self.assertEqual((True, "tailnet"), bridge._access_decision({}, PEER))
            self.assertEqual((False, "denied"), bridge._access_decision({"deny": ["alpha"]}, PEER))
            self.assertEqual((False, "not_allowed"), bridge._access_decision({"allow": []}, PEER))
            bridge._POLICY_CORE = None
            (home / "tailmsg.py").write_text("# older receiver without the policy evaluator\n")
            with self.assertRaisesRegex(bridge.Rejected, "invalid_access_policy"):
                bridge._access_decision({}, PEER)


class HttpPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.write_config({"allow": []})
        self.ctx = core.Ctx(self.home)
        self.peer = dict(PEER)
        self.ctx.whois = lambda ip: self.peer
        self.original_ranges = core.TAILNET_NETS
        core.TAILNET_NETS = core.TAILNET_NETS + [ipaddress.ip_network("127.0.0.0/8")]
        self.addCleanup(setattr, core, "TAILNET_NETS", self.original_ranges)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), core.make_handler(self.ctx, "bravo.example.ts.net"))
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.close)
        self.lookups = []
        self.ctx.native_name = lambda b: self.lookups.append(b)

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def write_config(self, config):
        core.atomic_write(self.home / "config.json", json.dumps(config).encode())

    def request(self, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        try:
            conn.request("GET" if body is None else "POST", path,
                         None if body is None else json.dumps(body), {"Content-Type": "application/json"})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def message(self, mid="message_0001"):
        return {"v": 1, "id": mid, "from": PEER["node"], "to": "bravo.example.ts.net",
                "ts": "2026-10-11T00:00:00Z", "wake": "none", "text": "policy fixture"}

    def staged_file(self, recipient=PEER["node"]):
        token = "a" * 32
        folder = self.ctx.outbox / token
        (folder / "files").mkdir(parents=True)
        (folder / "files" / "demo.txt").write_text("example bytes")
        (folder / "meta.json").write_text(json.dumps({
            "expires": core.iso(core.now_utc() + dt.timedelta(hours=1)), "to": recipient,
            "files": {"demo.txt": {"mime": "text/plain"}}}))
        return "/f/%s/demo.txt" % token

    def test_policy_changes_apply_without_restart_and_keep_other_runtime_settings(self):
        before = dict(self.ctx.cfg)
        self.assertEqual(403, self.request("/msg", self.message())[0])
        self.assertEqual(403, self.request("/info")[0])
        self.write_config({"access_policy": "tailnet", "allow": [], "wake_rate": 100})
        self.assertEqual(202, self.request("/msg", self.message())[0])
        self.assertEqual(200, self.request("/info")[0])
        self.write_config({"access_policy": "tailnet", "deny": ["tag:agents"]})
        self.assertEqual(403, self.request("/msg", self.message("message_0002"))[0])
        self.assertFalse((self.ctx.inbox / "message_0002.json").exists())
        self.assertEqual(403, self.request("/info")[0])
        self.assertEqual(before, self.ctx.cfg)

    def test_file_recipient_exception_remains_bound_to_identity_and_deny_wins(self):
        path = self.staged_file()
        self.assertEqual((200, b"example bytes"), self.request(path))  # historical token grant, empty allowlist
        self.peer = dict(PEER, node="other.example.ts.net")
        self.assertEqual(404, self.request(path)[0])
        self.peer = dict(PEER)
        self.write_config({"access_policy": "tailnet", "deny": ["user:owner@example.invalid"]})
        self.assertEqual(403, self.request(path)[0])
        self.write_config({"allow": ["alpha"], "deny": ["alpha"]})
        self.assertEqual(403, self.request(path)[0])

    def test_self_discovery_does_not_bypass_deny(self):
        self.peer = dict(PEER, node="bravo.example.ts.net", host="bravo")
        self.assertEqual(200, self.request("/info")[0])
        self.write_config({"allow": [], "deny": ["bravo"]})
        self.assertEqual(403, self.request("/info")[0])

    def test_malformed_or_missing_live_policy_fails_closed_on_all_data_routes(self):
        path = self.staged_file()
        for raw in ("{", "null", '{"access_policy":"tailnet","deny":null}', None):
            if raw is None:
                (self.home / "config.json").unlink()
            else:
                (self.home / "config.json").write_text(raw)
            for route, message in (("/info", None), (path, None), ("/msg", self.message())):
                with self.subTest(raw=raw, route=route):
                    self.assertEqual(403, self.request(route, message)[0])
        self.assertEqual([], list(self.ctx.inbox.iterdir()))

    def test_tailnet_mode_still_requires_source_address_and_verified_identity(self):
        self.write_config({"access_policy": "tailnet"})
        self.peer = None
        self.assertEqual(403, self.request("/info")[0])
        self.assertEqual(403, self.request("/msg", self.message())[0])
        self.peer = dict(PEER)
        core.TAILNET_NETS = self.original_ranges
        self.assertEqual(403, self.request("/info")[0])
        self.assertEqual(403, self.request("/msg", self.message())[0])


class WakePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.cfg = {"access_policy": "tailnet", "wake_attempts": 3, "wake_backoff_s": 0,
                    "wake_adapter": {"type": "exec", "argv": ["/unused/test-adapter"]}}
        self.write_config()
        self.ctx = core.Ctx(self.home)
        self.path = self.ctx.inbox / "message_wake.json"
        self.record = {"envelope": {"id": "message_wake", "from": PEER["node"], "text": "stored message"},
                       "received": {"peer": dict(PEER), "at": "2026-10-11T00:00:00Z",
                                    "wake": {"effective": "now"}}}
        self.path.write_text(json.dumps(self.record))

    def write_config(self):
        core.atomic_write(self.home / "config.json", json.dumps(self.cfg).encode())

    def test_deny_between_retries_stops_exec_and_webhook_before_next_invocation(self):
        for kind in ("exec", "webhook"):
            with self.subTest(adapter=kind):
                self.path.write_text(json.dumps(self.record))
                self.cfg.pop("deny", None)
                self.write_config()
                adapter = {"type": kind, "argv": ["/unused/test-adapter"], "url_env": "UNUSED_TEST_URL"}

                def first_attempt(*args):
                    self.cfg["deny"] = ["tag:agents"]
                    self.write_config()
                    raise core.AdapterError("temporary failure")

                with patch.object(self.ctx, "agent_adapter", return_value=adapter), \
                        patch.object(core, "call_" + kind, side_effect=first_attempt) as call:
                    self.assertFalse(core.deliver_wake(self.ctx, self.path))
                self.assertEqual(1, call.call_count)
                result = json.loads(self.path.read_text())
                self.assertEqual(self.record["envelope"], result["envelope"])
                self.assertEqual(self.record["received"], result["received"])
                self.assertEqual(1, result["wake_delivery"]["attempts"])
                self.assertEqual("access policy: denied", result["wake_delivery"]["error"])

    def test_manual_wake_cannot_bypass_revocation_or_invalid_receipt(self):
        for config in ({"deny": ["alpha"]}, {"access_policy": "allowlist", "allow": []}, {"deny": None}):
            with self.subTest(config=config):
                self.cfg.update(access_policy="tailnet", allow=[], deny=[])
                self.cfg.update(config)
                self.write_config()
                with patch.object(core, "call_exec") as call:
                    self.assertFalse(core.deliver_wake(self.ctx, self.path))
                    call.assert_not_called()
                self.assertEqual(0, json.loads(self.path.read_text())["wake_delivery"]["attempts"])
        self.cfg.update(access_policy="tailnet", allow=[], deny=[])
        self.write_config()
        for record in ({"envelope": self.record["envelope"]},
                       dict(self.record, envelope={"from": "different.example.ts.net"})):
            self.path.write_text(json.dumps(record))
            with patch.object(core, "call_exec") as call:
                self.assertFalse(core.deliver_wake(self.ctx, self.path))
                call.assert_not_called()

    def test_still_authorized_wake_uses_original_adapter_and_receipt(self):
        with patch.object(core, "call_exec", return_value="exit 0") as call:
            self.assertTrue(core.deliver_wake(self.ctx, self.path))
        call.assert_called_once_with(self.cfg["wake_adapter"], self.path, 120, None)
        result = json.loads(self.path.read_text())
        self.assertEqual(self.record["received"], result["received"])
        self.assertEqual(self.record["envelope"], result["envelope"])
        self.assertTrue(result["wake_delivery"]["ok"])
        self.assertEqual(1, result["wake_delivery"]["attempts"])


if __name__ == "__main__":
    unittest.main()
