"""Tailnet Lock support is read-only: doctor and `lock status` report the lock, this device's signature and hidden
peers; `lock guide` prints the owner's own commands. Nothing here signs, initializes or changes the lock.
The plugin CLI is loaded in-process (run `tools/build_plugin.py sync` first)."""
import importlib.util
import json
import unittest

from support.pkg import REPO

spec = importlib.util.spec_from_file_location("yobiru_lock_under_test", REPO / "plugins" / "yobiru" / "scripts" / "yobiru.py")
Y = importlib.util.module_from_spec(spec)
spec.loader.exec_module(Y)

PUB_A, PUB_B, PUB_C = ("tlpub:" + c * 64 for c in "abc")
NODE = "nodekey:" + "d" * 64
PEER_NODE = "nodekey:" + "e" * 64


def lock_json(**kw):
    d = {"Enabled": True, "PublicKey": PUB_A, "NodeKey": NODE, "NodeKeySigned": True,
         "TrustedKeys": [{"Key": PUB_B, "Votes": 1}, {"Key": PUB_C, "Votes": 1}], "FilteredPeers": []}
    d.update(kw)
    return json.dumps(d)


def run_checks(lock, adapters=None):
    out = {}

    def add(status, name, detail, fix=None):
        out[name] = {"status": status, "detail": detail, "fix": fix}
    Y.lock_check({"tailscale": {"lock": lock}, "adapters": adapters or []}, add)
    return out


AGENT = [{"agent": "codex", "adapter": "command: /usr/bin/true", "checks": []}]


class ParseTests(unittest.TestCase):
    def test_parses_public_facts_only(self):
        lock = Y.parse_tailnet_lock(lock_json(FilteredPeers=[{"Name": "Bot.Example.ts.net.", "NodeKey": PEER_NODE}]))
        self.assertTrue(lock["enabled"] and lock["signed"])
        self.assertEqual([PUB_B, PUB_C], lock["trusted_keys"])
        self.assertFalse(lock["is_signer"])
        self.assertEqual([{"name": "bot.example.ts.net", "node_key": PEER_NODE}], lock["filtered_peers"])

    def test_disabled_lock_without_optional_fields(self):
        lock = Y.parse_tailnet_lock(json.dumps({"Enabled": False}))
        self.assertFalse(lock["enabled"])
        self.assertEqual([], lock["trusted_keys"])

    def test_malformed_output_is_reported_not_raised(self):
        self.assertRaises(ValueError, Y.parse_tailnet_lock, "[]")


class DoctorTests(unittest.TestCase):
    def test_off_is_information_with_the_guide(self):
        c = run_checks(Y.parse_tailnet_lock(json.dumps({"Enabled": False})))["tailnet-lock"]
        self.assertEqual("info", c["status"])
        self.assertIn("lock guide", c["fix"])

    def test_unsigned_device_is_an_error_that_names_the_sign_command(self):
        c = run_checks(Y.parse_tailnet_lock(lock_json(NodeKeySigned=False)))["tailnet-lock"]
        self.assertEqual("error", c["status"])
        self.assertIn("looks offline", c["detail"])
        self.assertIn("tailscale lock sign " + NODE, c["fix"])

    def test_signed_with_two_keys_is_ok_and_one_key_warns(self):
        self.assertEqual("ok", run_checks(Y.parse_tailnet_lock(lock_json()))["tailnet-lock"]["status"])
        one = Y.parse_tailnet_lock(lock_json(TrustedKeys=[{"Key": PUB_B}]))
        self.assertEqual("warn", run_checks(one)["tailnet-lock"]["status"])

    def test_hidden_peers_are_named_as_unsigned_not_offline(self):
        lock = Y.parse_tailnet_lock(lock_json(FilteredPeers=[{"Name": "grok.example.ts.net.", "NodeKey": PEER_NODE}]))
        c = run_checks(lock)["lock-unsigned-peers"]
        self.assertEqual("warn", c["status"])
        self.assertIn("grok.example.ts.net", c["detail"])
        self.assertIn("not offline", c["detail"])

    def test_signing_device_that_wakes_agents_is_warned(self):
        lock = Y.parse_tailnet_lock(lock_json(TrustedKeys=[{"Key": PUB_A}, {"Key": PUB_B}]))
        self.assertTrue(lock["is_signer"])
        self.assertNotIn("lock-signer", run_checks(lock))
        c = run_checks(lock, AGENT)["lock-signer"]
        self.assertEqual("warn", c["status"])
        self.assertIn("tailscale lock remove " + PUB_A, c["fix"])

    def test_unreadable_state_is_information(self):
        self.assertEqual("info", run_checks({"error": "unknown flag --json"})["tailnet-lock"]["status"])
        self.assertEqual({}, run_checks(None))


class GuideTests(unittest.TestCase):
    def test_off_prints_init_with_the_given_signers_and_never_claims_to_run_it(self):
        lock = Y.parse_tailnet_lock(json.dumps({"Enabled": False, "PublicKey": PUB_A}))
        text = "\n".join(Y.lock_guide(lock, [PUB_B, PUB_C, PUB_B], 7766, agent_machine=True))
        self.assertIn("tailscale lock init --gen-disablements 3 --confirm %s %s" % (PUB_B, PUB_C), text)
        self.assertIn("this machine wakes agents", text)
        self.assertIn("disablement secrets", text)
        self.assertIn('"autogroup:member:7766"', text)

    def test_on_explains_signing_and_pre_signed_auth_keys(self):
        text = "\n".join(Y.lock_guide(Y.parse_tailnet_lock(lock_json()), [], 7766, agent_machine=False))
        self.assertIn("tailscale lock sign nodekey:", text)
        self.assertIn("tailscale lock sign tskey-auth-", text)
        self.assertNotIn("lock init", text)


if __name__ == "__main__":
    unittest.main()
