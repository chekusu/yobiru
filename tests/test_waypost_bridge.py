"""Unit tests for the Waypost bridge reference: pure mapping only.

No Tailscale, no receiver, no Waypost service needed. Live flows stay in
selftest.sh and the documented manual acceptance.
"""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "waypost_bridge", REPO / "tools" / "waypost_bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class EnvelopeIdTests(unittest.TestCase):
    def test_bounds_match_spec(self):
        self.assertTrue(bridge.valid_envelope_id("9f2c4e1ab07d33c5"))
        self.assertTrue(bridge.valid_envelope_id("a" * 8))
        self.assertTrue(bridge.valid_envelope_id("A-_0" * 16))
        for bad in ("short", "a" * 65, "has space", "uniçode", "", None, 42):
            self.assertFalse(bridge.valid_envelope_id(bad))

    def test_stable_request_id_is_fixed_shape_and_rejects_garbage(self):
        first = bridge.stable_request_id("9f2c4e1ab07d33c5")
        self.assertRegex(first, r"\Ayb-[0-9a-f]{32}\Z")
        self.assertEqual(first, bridge.stable_request_id("9f2c4e1ab07d33c5"))
        self.assertNotEqual(first, bridge.stable_request_id("1a2b3c4d5e6f7a8b"))
        with self.assertRaises(ValueError):
            bridge.stable_request_id("nope")

    def test_live_keys_do_not_collapse_distinct_spaces_or_authenticated_sources(self):
        mid = "9f2c4e1ab07d33c5"
        first = bridge.stable_request_id(mid, 'space_one', 'alpha.example.ts.net', 'grok')
        self.assertEqual(first, bridge.stable_request_id(mid, 'space_one', 'alpha.example.ts.net', 'grok'))
        for space, peer, agent in [('space_two', 'alpha.example.ts.net', 'grok'),
                                   ('space_one', 'bravo.example.ts.net', 'grok'),
                                   ('space_one', 'alpha.example.ts.net', 'other')]:
            self.assertNotEqual(first, bridge.stable_request_id(mid, space, peer, agent))


class AgentNameTests(unittest.TestCase):
    def test_unicode_names_normalize_but_case_matters(self):
        self.assertEqual(bridge.normalize_agent("grok"), "grok")
        self.assertEqual(bridge.normalize_agent("私信联系人"), "私信联系人")
        self.assertNotEqual(bridge.normalize_agent("Codex"), bridge.normalize_agent("codex"))

    def test_illegal_names_rejected_before_any_identity_is_minted(self):
        for bad in ("", " a", "a ", "a/b", "a\nb", "a\u0085b", "a\u2028b", "a\u2029b", "x" * 65, None):
            with self.assertRaises(ValueError):
                bridge.normalize_agent(bad)

    def test_actor_display_names_follow_host_slash_agent(self):
        self.assertEqual(
            bridge.waypost_actor_for_sender("dev-server.example.ts.net", "codex"),
            "dev-server/codex")


class CorrelationTests(unittest.TestCase):
    def test_round_trip_inside_json_body_only(self):
        payload = {"type": "status", "ok": True}
        carried = bridge.inject_correlation(dict(payload), "9f2c4e1ab07d33c5")
        self.assertEqual(payload, {"type": "status", "ok": True})
        request_id, in_reply_to = bridge.extract_correlation(carried)
        self.assertEqual(request_id, "9f2c4e1ab07d33c5")
        self.assertIsNone(in_reply_to)

    def test_reply_chain_survives(self):
        carried = bridge.inject_correlation({}, "9f2c4e1ab07d33c5", in_reply_to="1a2b3c4d5e6f7a8b")
        self.assertEqual(bridge.extract_correlation(carried),
                         ("9f2c4e1ab07d33c5", "1a2b3c4d5e6f7a8b"))

    def test_garbage_yields_no_correlation(self):
        self.assertEqual(bridge.extract_correlation({"waypost_request_id": "nope"}), (None, None))
        self.assertEqual(bridge.extract_correlation(None), (None, None))
        with self.assertRaises(ValueError):
            bridge.inject_correlation({}, "nope")


class ReceiptMapTests(unittest.TestCase):
    def test_progress_is_received_never_processed(self):
        self.assertEqual(bridge.map_receipt_state("received"), "received")
        self.assertEqual(bridge.map_receipt_state("running"), "received")

    def test_final_outcomes_are_processed(self):
        for state in ("completed", "failed", "rejected", "blocked"):
            self.assertEqual(bridge.map_receipt_state(state), "processed")

    def test_timeout_maps_to_nothing(self):
        # timed_out means unknown: reconcile first, never auto-process.
        self.assertIsNone(bridge.map_receipt_state("timed_out"))
        self.assertIsNone(bridge.map_receipt_state("late"))
        self.assertIsNone(bridge.map_receipt_state("bogus"))


@unittest.skipUnless(os.name == 'posix', 'The optional bridge currently requires POSIX private-file checks.')
class TokenFileTests(unittest.TestCase):
    def test_symlink_token_files_are_refused(self):
        with tempfile.TemporaryDirectory() as home:
            target = Path(home) / 'token'
            target.write_text('synthetic-secret')
            target.chmod(0o600)
            link = Path(home) / 'linked-token'
            link.symlink_to(target)
            with self.assertRaises((OSError, ValueError)):
                bridge.read_token(link)

    def test_wide_token_files_refused(self):
        with tempfile.TemporaryDirectory() as home:
            loose = Path(home) / "token"
            loose.write_text("secret", encoding="utf-8")
            loose.chmod(0o644)
            with self.assertRaises(ValueError):
                bridge.read_token(str(loose))
            tight = Path(home) / "tight"
            tight.write_text("secret", encoding="utf-8")
            tight.chmod(0o600)
            self.assertEqual(bridge.read_token(str(tight)), "secret")


if __name__ == "__main__":
    unittest.main()
