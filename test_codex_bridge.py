"""Offline queue tests; no actual app, session, network, or Tailmsg replies."""
import contextlib
import datetime
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import codex_bridge
from codex_bridge import Bridge, Rejected, load_config
from codex_queue_client import CodexAppError
import tailmsg as receiver_core


class DeliveryError(Exception):
    def __init__(self, unknown):
        self.delivery_unknown = unknown
        super().__init__("private content must never be persisted")


class FakeClient:
    def __init__(self):
        self.calls = []
        self.send_error = None
        self.send_result = {"status": "queued"}
        self.read_result = {"turns": []}
        self.closes = 0

    def close(self):
        self.closes += 1

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "send_message_to_thread":
            if self.send_error is not None:
                raise self.send_error
            return self.send_result
        if name == "read_thread":
            return self.read_result
        raise AssertionError(name)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.tailmsg = self.root / "tailmsg"
        self.inbox = self.tailmsg / "inbox"
        self.inbox.mkdir(parents=True)
        self.local = {"allow": ["grok-bot"], "default_agent": "codex"}
        self.write_local()
        self.now = 1700000000.0
        self.client = FakeClient()
        self.config = {
            "bridge_home": str(self.root / "state"), "tailmsg_home": str(self.tailmsg),
            "dispatcher_thread_id": "dispatcher-fixed", "dispatcher_host_id": "local",
            "retry_seconds": 10, "batch_delay_seconds": 30, "poll_seconds": 1,
        }
        self.bridge = self.open_bridge()
        self.addCleanup(lambda: self.bridge.close())

    def open_bridge(self):
        return Bridge(self.config, client_factory=lambda _: self.client, clock=lambda: self.now)

    def write_local(self):
        (self.tailmsg / "config.json").write_text(json.dumps(self.local), encoding="utf-8")

    def message(self, mid="message_0001", wake="now", **overrides):
        date = datetime.datetime.fromtimestamp(self.now, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        record = {
            "envelope": {"v": 1, "id": mid, "from": "grok-bot.tailnet.ts.net", "agent": "codex",
                         "text": "PRIVATE body $(malicious) `command`", "wake": wake},
            "received": {"at": date, "peer": {"node": "grok-bot.tailnet.ts.net",
                         "host": "grok-bot", "tags": [], "user": "test@example.invalid"},
                         "agent": "codex", "agent_status": "known",
                         "wake": {"requested": wake, "effective": wake}},
        }
        record.update(overrides)
        path = self.inbox / (mid + ".json")
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def rewrite(self, path, update):
        record = json.loads(path.read_text(encoding="utf-8"))
        update(record)
        path.write_text(json.dumps(record), encoding="utf-8")

    def state(self):
        return self.bridge.status()[0]["state"]

    def request_fixture(self, state="received", overdue=False):
        path = self.message()
        self.rewrite(path, lambda r: r["envelope"].update(
            kind="request", request={"timeout_seconds": 900, "reply_port": 7766}))
        ledger = Mock()
        ledger.request_snapshot.return_value = {"state": state, "overdue": overdue}
        self.addCleanup(patch.stopall)
        patch("codex_bridge._POLICY_CORE", ledger).start()
        patch("codex_bridge._access_decision", return_value=(True, "tailnet")).start()
        self.set_reply_consent(False)
        return path, ledger

    def set_reply_consent(self, value):
        self.bridge.config["allow_replies"] = value
        Path(self.bridge.config_path).write_text(json.dumps({"allow_replies": value}))

    def pull_request_fixture(self, named=True):
        """A receiver-written record plus real request ledger and local uploaded file; no network."""
        self.now = time.time()
        path = self.message()
        stable = receiver_core.stable_peer_id("nSharedDevice")
        content = b"harmless uploaded input\n"
        name = "PRIVATE-input.txt"
        record = json.loads(path.read_text())
        record["envelope"].update({
            "from": stable, "to": "receiver.example.ts.net", "ts": receiver_core.iso(receiver_core.now_utc()),
            "kind": "request", "request": {"timeout_seconds": 900, "reply_port": 7766, "transport": "pull",
                "uploads": [{"name": name, "token": "a" * 32, "sha256": hashlib.sha256(content).hexdigest(),
                             "size": len(content), "mime": "text/plain"}]}})
        record["received"].update(node="receiver.example.ts.net")
        record["received"]["peer"].update({"node": "grok-bot.tailnet.ts.net" if named else stable,
            "dns_name": "grok-bot.tailnet.ts.net" if named else "", "stable_id": "nSharedDevice"})
        path.write_text(json.dumps(record))
        directory = self.inbox / record["envelope"]["id"] / "files"
        directory.mkdir(parents=True)
        (directory / name).write_bytes(content)
        self.local = {"access_policy": "tailnet", "default_agent": "codex"}
        self.write_local()
        receiver_core.accept_request(SimpleNamespace(home=self.tailmsg), record)
        policy_patch = patch("codex_bridge._POLICY_CORE", receiver_core)
        policy_patch.start()
        self.addCleanup(policy_patch.stop)
        self.set_reply_consent(True)
        return path, directory

    def test_pull_request_with_named_peer_reaches_fixed_session_and_verifies_local_input(self):
        path, directory = self.pull_request_fixture()
        self.assertEqual("queued", self.bridge.ingest(path)["status"])
        self.assertEqual(1, self.bridge.dispatch_due()["enqueued"])
        prompt = self.client.calls[0][1]["prompt"]
        self.assertEqual("dispatcher-fixed", self.client.calls[0][1]["threadId"])
        self.assertIn("request.uploads, not fetch", prompt)
        self.assertIn(json.dumps(str(directory)), prompt)
        self.assertIn("makes no network connection", prompt)
        self.assertIn("Do not connect back", prompt)
        self.assertIn("original requester to poll", prompt)
        self.assertNotIn("PRIVATE", prompt)  # neither the message text nor sender-supplied file names
        context = receiver_core.Ctx(str(self.tailmsg))
        with patch.object(context, "resolve", side_effect=AssertionError("no reverse resolution")), \
                patch.object(context, "whois", side_effect=AssertionError("no network identity query")), \
                patch.object(receiver_core.http.client, "HTTPConnection", side_effect=AssertionError("no reverse HTTP")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            receiver_core.cmd_fetch(context, SimpleNamespace(id="message_0001"))
        self.assertIn("sha256 verified", output.getvalue())

    def test_pull_request_with_empty_display_name_is_accepted(self):
        path, _ = self.pull_request_fixture(named=False)
        self.assertEqual("queued", self.bridge.ingest(path)["status"])
        self.assertEqual(1, self.bridge.dispatch_due()["enqueued"])

    def test_pull_sender_stable_identity_mismatch_and_invalid_from_are_rejected(self):
        path, _ = self.pull_request_fixture()
        self.rewrite(path, lambda record: record["received"]["peer"].update(stable_id="nOtherDevice"))
        with self.assertRaisesRegex(Rejected, "sender_mismatch"):
            self.bridge.ingest(path)
        self.rewrite(path, lambda record: record["envelope"].update({"from": {"node": "nSharedDevice"}}))
        with self.assertRaisesRegex(Rejected, "sender_mismatch"):
            self.bridge.ingest(path)
        self.assertEqual([], self.client.calls)

    def test_pull_access_is_rechecked_by_stable_identity_before_dispatch(self):
        path, _ = self.pull_request_fixture()
        self.bridge.ingest(path)
        self.local["deny"] = [receiver_core.stable_peer_id("nSharedDevice")]
        self.write_local()
        self.assertEqual(1, self.bridge.dispatch_due()["skipped"])
        self.assertEqual([], self.client.calls)

    def test_pull_request_without_reply_consent_records_blocked_without_reverse_outbox(self):
        path, _ = self.pull_request_fixture()
        self.set_reply_consent(False)
        self.assertEqual("request_replies_disabled", self.bridge.ingest(path)["reason"])
        self.assertEqual("blocked", receiver_core.request_snapshot(self.tailmsg, "message_0001")["state"])
        with receiver_core.request_db(SimpleNamespace(home=self.tailmsg)) as database:
            self.assertEqual(0, database.execute("SELECT COUNT(*) FROM response_outbox").fetchone()[0])
        self.assertEqual([], self.client.calls)

    def test_tracked_request_without_reply_consent_is_blocked_without_model(self):
        path, ledger = self.request_fixture()
        self.assertEqual("request_replies_disabled", self.bridge.ingest(path)["reason"])
        ledger.request_record_response.assert_called_once_with(
            self.tailmsg, "message_0001", "blocked", "Automatic agent replies are disabled on this receiver.")
        self.assertEqual([], self.bridge.status())
        self.assertEqual(0, self.bridge.dispatch_due()["enqueued"])
        self.assertEqual([], self.client.calls)

    def test_tracked_request_requires_final_result_in_session_prompt(self):
        path, ledger = self.request_fixture()
        self.set_reply_consent(True)
        self.assertEqual("queued", self.bridge.ingest(path)["status"])
        self.assertEqual(1, self.bridge.dispatch_due()["enqueued"])
        prompt = self.client.calls[0][1]["prompt"]
        self.assertIn("tracked Yobiru request", prompt)
        self.assertIn("request status ID --direction incoming --json", prompt)
        self.assertIn("verified final result", prompt)
        self.assertIn("A handoff is not a final result", prompt)
        self.assertNotIn("PRIVATE", prompt)
        ledger.request_record_response.assert_not_called()

    def test_request_permission_revoked_between_ingest_and_dispatch(self):
        path, ledger = self.request_fixture()
        self.set_reply_consent(True)
        self.bridge.ingest(path)
        Path(self.bridge.config_path).write_text(json.dumps({"allow_replies": False}))
        self.assertEqual(1, self.bridge.dispatch_due()["skipped"])
        self.assertEqual([], self.client.calls)
        ledger.request_record_response.assert_called_once()

    def test_missing_request_permission_config_never_reuses_old_consent(self):
        path, ledger = self.request_fixture()
        self.set_reply_consent(True)
        self.bridge.ingest(path)
        Path(self.bridge.config_path).unlink()
        self.assertEqual(1, self.bridge.dispatch_due()["skipped"])
        self.assertEqual([], self.client.calls)
        ledger.request_record_response.assert_not_called()

    def test_finished_and_expired_requests_never_start_a_new_turn(self):
        path, ledger = self.request_fixture()
        self.set_reply_consent(True)
        for state in ("completed", "failed", "blocked", "rejected", "timed_out"):
            ledger.request_snapshot.return_value = {"state": state, "overdue": False}
            self.assertEqual("skipped", self.bridge.ingest(path)["status"])
        ledger.request_snapshot.return_value = {"state": "received", "overdue": True}
        self.assertEqual("request_expired", self.bridge.ingest(path)["reason"])
        ledger.request_record_response.assert_not_called()
        self.assertEqual([], self.bridge.status())

    def test_request_expiring_before_the_official_queue_send_is_not_sent(self):
        # Pre-queue expiry: ingested locally, expired before any `codex queue` call. Expiry after the
        # official queue accepted the notification is DeliveryEvidenceTests' post-acceptance case.
        path, ledger = self.request_fixture()
        self.set_reply_consent(True)
        self.bridge.ingest(path)
        ledger.request_snapshot.return_value = {"state": "timed_out", "overdue": True}
        self.assertEqual(1, self.bridge.dispatch_due()["skipped"])
        self.assertEqual([], self.client.calls)

    def test_notification_prompt_does_not_request_a_reply(self):
        path = self.message()
        self.rewrite(path, lambda r: r["envelope"].update(kind="notice"))
        self.bridge.ingest(path)
        self.bridge.dispatch_due()
        self.assertIn("Do not send an automatic reply", self.client.calls[0][1]["prompt"])

    def test_deduplication_and_metadata_only_notification(self):
        path = self.message()
        before = path.read_bytes()
        self.assertEqual("queued", self.bridge.ingest(path)["status"])
        self.assertEqual("duplicate", self.bridge.ingest(path)["status"])
        self.assertEqual(1, self.bridge.dispatch_due()["enqueued"])
        self.bridge.scan()
        self.bridge.dispatch_due()
        self.assertEqual(1, len(self.client.calls))
        name, args = self.client.calls[0]
        self.assertEqual("send_message_to_thread", name)
        self.assertEqual("dispatcher-fixed", args["threadId"])
        self.assertIn("[TAILMSG:message_0001]", args["prompt"])
        self.assertIn(str(path), args["prompt"])
        self.assertNotIn("PRIVATE", args["prompt"])
        self.assertNotIn("PRIVATE", json.dumps(self.bridge.status()))
        self.assertEqual(before, path.read_bytes())  # Does not write ack/wake metadata.
        self.assertEqual("enqueued", self.state())
        self.assertEqual(1, self.client.closes)

    def test_rejects_unknown_peer_and_sender_mismatch(self):
        path = self.message()
        self.local["allow"] = ["other-node"]
        self.write_local()
        with self.assertRaisesRegex(Rejected, "peer_not_allowed"):
            self.bridge.ingest(path)
        self.local["allow"] = ["grok-bot"]
        self.write_local()
        self.rewrite(path, lambda r: r["envelope"].update({"from": "other-node"}))
        with self.assertRaisesRegex(Rejected, "sender_mismatch"):
            self.bridge.ingest(path)
        self.assertEqual([], self.bridge.status())

    def test_rejects_agent_conflicts_and_missing_receipt(self):
        path = self.message()
        self.rewrite(path, lambda r: r["envelope"].update({"agent": "grok"}))
        with self.assertRaisesRegex(Rejected, "wrong_agent"):
            self.bridge.ingest(path)
        path = self.message()
        self.rewrite(path, lambda r: r["received"].update({"agent_status": "unknown"}))
        with self.assertRaisesRegex(Rejected, "wrong_agent"):
            self.bridge.ingest(path)
        path = self.message(received=None)
        with self.assertRaisesRegex(Rejected, "missing_verified_receipt"):
            self.bridge.ingest(path)

    def test_rejects_wrong_paths_symlinks_and_filename_id_mismatch(self):
        path = self.message()
        outside = self.root / path.name
        outside.write_bytes(path.read_bytes())
        with self.assertRaisesRegex(Rejected, "invalid_inbox_path"):
            self.bridge.ingest(outside)
        link = self.inbox / "message_link.json"
        link.symlink_to(outside)
        with self.assertRaisesRegex(Rejected, "invalid_inbox_path"):
            self.bridge.ingest(link)
        self.rewrite(path, lambda r: r["envelope"].update({"id": "different_0001"}))
        with self.assertRaisesRegex(Rejected, "invalid_message_id"):
            self.bridge.ingest(path)

    def test_none_and_acked_are_never_automatically_dispatched(self):
        self.message(wake="none")
        self.message("message_0002", acked_at="2020-01-01T00:00:00Z")
        result = self.bridge.scan()
        self.assertEqual(2, result["skipped"])
        self.bridge.dispatch_due()
        self.assertEqual([], self.client.calls)
        self.assertEqual([], self.bridge.status())

    def test_batch_delayed_and_now_immediate(self):
        self.bridge.ingest(self.message(wake="batch"))
        self.bridge.ingest(self.message("message_0002", wake="now"))
        self.assertEqual(1, self.bridge.dispatch_due()["enqueued"])
        self.now += 29
        self.assertEqual(0, self.bridge.dispatch_due()["enqueued"])
        self.now += 1
        self.assertEqual(1, self.bridge.dispatch_due()["enqueued"])
        self.assertEqual(2, len(self.client.calls))

    def test_historical_messages_dispatch_by_receipt_time_not_filename(self):
        scanned_at = self.now
        self.now -= 120
        first = self.message("zzzz_first_0001")
        self.now += 60
        second = self.message("aaaa_second_0002")
        self.now = scanned_at
        self.assertEqual(2, self.bridge.scan()["queued"])
        self.assertEqual(2, self.bridge.dispatch_due()["enqueued"])
        prompts = [args["prompt"] for name, args in self.client.calls]
        self.assertIn("[TAILMSG:zzzz_first_0001]", prompts[0])
        self.assertIn("[TAILMSG:aaaa_second_0002]", prompts[1])
        rows = {row["id"]: row for row in self.bridge.status()}
        self.assertEqual(scanned_at, rows["zzzz_first_0001"]["created_at"])
        self.assertEqual(scanned_at - 120, rows["zzzz_first_0001"]["received_at"])
        self.assertEqual(scanned_at - 60, rows["aaaa_second_0002"]["received_at"])
        self.assertTrue(first.exists() and second.exists())

    def test_now_priority_precedes_older_due_batch(self):
        scanned_at = self.now
        self.now -= 120
        self.message("aaaa_batch_0001", wake="batch")
        self.now = scanned_at
        self.message("zzzz_now_0002", wake="now")
        self.bridge.scan()
        self.assertEqual(2, self.bridge.dispatch_due()["enqueued"])
        self.assertIn("[TAILMSG:zzzz_now_0002]", self.client.calls[0][1]["prompt"])

    def test_existing_schema_migrates_verified_receipt_without_fabricating_missing_time(self):
        original_now = self.now
        self.now -= 60
        path = self.message()
        self.now = original_now
        self.bridge.close()
        db_path = Path(self.config["bridge_home"]) / "bridge.sqlite3"
        db_path.unlink()
        with contextlib.closing(sqlite3.connect(str(db_path))) as legacy:
            legacy.execute("""CREATE TABLE messages (
                id TEXT PRIMARY KEY, path TEXT NOT NULL, wake TEXT NOT NULL,
                state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                error TEXT, thread_id TEXT NOT NULL, host_id TEXT NOT NULL)""")
            for mid, source in (("message_0001", path), ("missing_0001", self.inbox / "missing_0001.json")):
                legacy.execute("INSERT INTO messages VALUES (?,?,?,'pending',0,?,?,?,NULL,?,?)",
                    (mid, str(source), "now", self.now, self.now - 10, self.now - 5,
                     self.config["dispatcher_thread_id"], "local"))
            legacy.commit()
        self.bridge = self.open_bridge()
        rows = {row["id"]: row for row in self.bridge.status()}
        self.assertEqual(self.now - 60, rows["message_0001"]["received_at"])
        self.assertIsNone(rows["missing_0001"]["received_at"])
        self.assertEqual(self.now - 10, rows["message_0001"]["created_at"])
        self.assertEqual(self.now - 5, rows["message_0001"]["updated_at"])
        self.bridge.close()
        self.bridge = self.open_bridge()  # Migration is safe to run again.
        dispatched = self.bridge.dispatch_due()
        self.assertEqual(1, dispatched["enqueued"])
        self.assertEqual(1, dispatched["skipped"])
        self.assertEqual(1, len(self.client.calls))

    def test_known_not_sent_failure_retries_after_backoff(self):
        self.bridge.ingest(self.message())
        self.client.send_error = DeliveryError(False)
        self.assertEqual(1, self.bridge.dispatch_due()["retry"])
        self.assertEqual("retry", self.state())
        self.assertNotIn("private content", json.dumps(self.bridge.status()))
        self.now += 9
        self.bridge.dispatch_due()
        self.assertEqual(1, len(self.client.calls))
        self.client.send_error = None
        self.now += 1
        self.assertEqual(1, self.bridge.dispatch_due()["enqueued"])
        self.assertEqual(2, self.bridge.status()[0]["attempts"])

    def test_explicit_tool_rejection_retries(self):
        self.bridge.ingest(self.message())
        self.client.send_result = {"ok": False, "error": "private rejection details"}
        self.assertEqual(1, self.bridge.dispatch_due()["retry"])
        self.assertNotIn("private rejection", json.dumps(self.bridge.status()))

    def test_unknown_send_only_reconciles_and_never_blindly_resends(self):
        self.bridge.ingest(self.message())
        self.client.send_error = DeliveryError(True)
        self.assertEqual(1, self.bridge.dispatch_due()["uncertain"])
        self.client.send_error = None
        for _ in range(3):
            self.now += 10
            self.assertEqual(1, self.bridge.dispatch_due()["uncertain"])
        self.assertEqual(1, sum(name == "send_message_to_thread" for name, _ in self.client.calls))
        self.client.read_result = {"turns": [{"input": "[TAILMSG:message_0001]"}]}
        self.now += 10
        self.assertEqual(1, self.bridge.dispatch_due()["delivered"])
        self.assertEqual("delivered", self.state())

    def test_unclassified_failure_is_conservatively_uncertain(self):
        self.bridge.ingest(self.message())
        self.client.send_error = TimeoutError("private timeout details")
        self.assertEqual(1, self.bridge.dispatch_due()["uncertain"])

    def test_restart_preserves_deduplication_and_recovers_inflight_sends(self):
        path = self.message()
        self.bridge.ingest(path)
        self.bridge.db.execute("UPDATE messages SET state='sending',attempts=1")
        self.bridge.db.commit()
        self.bridge.close()
        self.bridge = self.open_bridge()
        self.assertEqual("duplicate", self.bridge.ingest(path)["status"])
        self.assertEqual(1, self.bridge.dispatch_due()["uncertain"])
        self.assertEqual(["read_thread"], [name for name, _ in self.client.calls])
        self.bridge.close()
        self.bridge = self.open_bridge()
        self.assertEqual("uncertain", self.state())

    def test_revoked_allowlist_is_checked_before_dispatch(self):
        self.bridge.ingest(self.message())
        self.local["allow"] = []
        self.write_local()
        self.assertEqual(1, self.bridge.dispatch_due()["skipped"])
        self.assertEqual([], self.client.calls)

    def test_tailnet_policy_accepts_new_peer_without_allow_rule(self):
        self.local = {"access_policy": "tailnet", "default_agent": "codex", "allow": []}
        self.write_local()
        self.assertEqual("queued", self.bridge.ingest(self.message())["status"])
        self.assertEqual(1, self.bridge.dispatch_due()["enqueued"])

    def test_deny_policy_is_rechecked_before_dispatch(self):
        for rule in ("grok-bot", "grok-bot.tailnet.ts.net", "user:test@example.invalid"):
            with self.subTest(rule=rule):
                self.local = {"access_policy": "tailnet", "default_agent": "codex"}
                self.write_local()
                mid = "message_deny_%d" % len(self.bridge.status())
                self.bridge.ingest(self.message(mid))
                self.local["deny"] = [rule]
                self.write_local()
                self.assertEqual(1, self.bridge.dispatch_due()["skipped"])
                self.assertEqual([], self.client.calls)

    def test_invalid_policy_and_unknown_identity_fail_closed_in_tailnet_mode(self):
        path = self.message()
        self.local = {"access_policy": "tailnet", "deny": None}
        self.write_local()
        with self.assertRaisesRegex(Rejected, "invalid_access_policy"):
            self.bridge.ingest(path)
        self.local = {"access_policy": "tailnet"}
        self.write_local()
        self.rewrite(path, lambda r: r["received"]["peer"].update({"tags": "malformed"}))
        with self.assertRaisesRegex(Rejected, "missing_verified_peer"):
            self.bridge.ingest(path)

    def test_single_dispatch_process_lock(self):
        import fcntl
        self.bridge.ingest(self.message())
        with (self.bridge.home / "dispatch.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(0, self.bridge.dispatch_due()["enqueued"])
        self.assertEqual(1, self.bridge.dispatch_due()["enqueued"])

    def test_config_rejects_invalid_timing(self):
        path = self.root / "bridge-config.json"
        for value in (True, -1, "fast", float("inf")):
            config = dict(self.config, poll_seconds=value)
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(path)

    def test_custom_config_path_and_home_are_explicit_trusted_notification_metadata(self):
        path = self.root / "separate-settings" / "custom-bridge.json"
        path.parent.mkdir()
        values = dict(self.config, bridge_config_path="/untrusted/config-override.json")
        path.write_text(json.dumps(values), encoding="utf-8")
        loaded = load_config(path)
        self.assertEqual(str(path), loaded["bridge_config_path"])
        self.bridge.close()
        self.bridge = Bridge(loaded, client_factory=lambda _: self.client, clock=lambda: self.now)
        message_path = self.message()
        self.bridge.ingest(message_path)
        self.bridge.dispatch_due()
        prompt = self.client.calls[0][1]["prompt"]
        self.assertIn("Tailmsg home: " + json.dumps(str(self.tailmsg)), prompt)
        self.assertIn("Bridge state directory: " + json.dumps(self.config["bridge_home"]), prompt)
        self.assertIn("Bridge configuration path: " + json.dumps(str(path)), prompt)
        self.assertNotIn("/untrusted/", prompt)
        self.assertNotIn("~/.tailmsg", prompt)
        self.assertNotIn("PRIVATE", prompt)

    def test_schema_initialization_failure_closes_database(self):
        self.bridge.close()
        db_path = Path(self.config["bridge_home"]) / "bridge.sqlite3"
        db_path.unlink()
        connection = sqlite3.connect(str(db_path))
        connection.execute("CREATE TABLE messages (id TEXT PRIMARY KEY)")
        connection.commit()
        with patch("codex_bridge.sqlite3.connect", return_value=connection):
            with self.assertRaises(sqlite3.OperationalError):
                self.open_bridge()
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_daemon_and_wake_can_migrate_same_legacy_schema_concurrently(self):
        path = self.message()
        self.bridge.close()
        db_path = Path(self.config["bridge_home"]) / "bridge.sqlite3"
        db_path.unlink()
        with contextlib.closing(sqlite3.connect(str(db_path))) as legacy:
            legacy.execute("""CREATE TABLE messages (
                id TEXT PRIMARY KEY, path TEXT NOT NULL, wake TEXT NOT NULL,
                state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                error TEXT, thread_id TEXT NOT NULL, host_id TEXT NOT NULL)""")
        barrier = threading.Barrier(2)
        def migrate_and_ingest(_index):
            barrier.wait(timeout=5)
            bridge = self.open_bridge()
            try:
                status = bridge.ingest(path)["status"]
                return status, bridge.status()[0]["received_at"]
            finally:
                bridge.close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(migrate_and_ingest, range(2)))
        self.assertEqual(["duplicate", "queued"], sorted(status for status, _ in results))
        self.assertEqual([self.now, self.now], [received for _, received in results])
        self.bridge = self.open_bridge()
        self.assertEqual(1, len(self.bridge.status()))

class DeliveryEvidenceTests(BridgeTests):
    """Queue acceptance, a seen notification, start and final results stay separate evidence. Real request
    ledgers in temporary homes, a fake queue client and clock; no Codex runtime, socket or network."""

    def setUp(self):
        super().setUp()
        self.now = float(int(time.time()))      # the request ledger uses real time
        self.core = codex_bridge._core()

    def sends(self):
        return sum(name == "send_message_to_thread" for name, _ in self.client.calls)

    def receipt(self, queue_id="q_0123456789abcdef"):
        self.client.send_result = {"ok": True, "status": "enqueued", "transport": "codex_queue", "queueId": queue_id}

    def tracked(self, mid="request_0001", wake="now"):
        """A tracked request as the receiver stores it: inbox record plus its ledger row and receipt."""
        self.set_reply_consent(True)
        path = self.message(mid, wake=wake)
        self.rewrite(path, lambda r: r["envelope"].update(
            kind="request", to="dispatcher.tailnet.ts.net", ts=self.core.iso(self.core.now_utc()),
            request={"timeout_seconds": 900, "reply_port": 7766}))
        self.core.accept_request(self.core.Ctx(self.tailmsg), json.loads(path.read_text()))
        return path

    def ledger(self, sql, *args):
        with contextlib.closing(sqlite3.connect(str(self.tailmsg / "requests" / "ledger.sqlite3"))) as db:
            rows = db.execute(sql, args).fetchall()
            db.commit()
            return rows

    def diagnose(self, **overrides):
        return codex_bridge.diagnostics(dict(self.bridge.config, **overrides), now=self.now)

    def phases(self, **overrides):
        return self.diagnose(**overrides)["execution"]["phases"]

    def enqueue(self, *paths):
        self.receipt()
        for path in paths:
            self.bridge.ingest(path)
        self.assertEqual(len(paths), self.bridge.dispatch_due()["enqueued"])

    def test_accepted_receipt_is_kept_once_and_never_resent(self):
        self.enqueue(self.message())
        row = self.bridge.status()[0]
        self.assertEqual(("enqueued", "q_0123456789abcdef", "codex_queue", self.now, 1),
                         (row["state"], row["queue_id"], row["transport"], row["enqueued_at"], row["attempts"]))
        accepted = self.now
        for _ in range(3):                                   # a day of scans, dispatch passes and a restart
            self.now += 8 * 3600
            self.bridge.scan()
            self.bridge.dispatch_due()
            self.bridge.observe()
        self.bridge.close()
        self.bridge = self.open_bridge()
        self.bridge.scan()
        self.bridge.dispatch_due()
        row = self.bridge.status()[0]
        self.assertEqual((1, 1, accepted, "enqueued"), (self.sends(), row["attempts"], row["enqueued_at"], row["state"]))
        self.assertNotIn("read_thread", [name for name, _ in self.client.calls])   # no history polling
        diagnosis = self.diagnose()
        oldest = diagnosis["execution"]["oldest_without_start"]
        self.assertEqual(("message_0001", "awaiting_handling_too_long", 24 * 3600, "enqueued_at"),
                         (oldest["id"], oldest["phase"], oldest["age_seconds"], oldest["age_basis"]))
        self.assertTrue(diagnosis["execution_unknown"])
        self.assertIn("accepted_without_start", diagnosis["reasons"])
        self.receipt("not a queue id!")
        self.bridge.ingest(self.message("message_0002"))
        self.bridge.dispatch_due()
        self.assertIsNone({r["id"]: r for r in self.bridge.status()}["message_0002"]["queue_id"])

    def test_request_expiring_after_official_acceptance_is_flagged_not_resent(self):
        path = self.tracked()
        self.enqueue(path)
        outbox = self.ledger("SELECT COUNT(*) FROM response_outbox")[0][0]
        record = path.read_bytes()
        self.ledger("UPDATE requests SET deadline='2000-01-01T00:00:00Z'")
        self.now += 60
        self.bridge.scan()
        self.bridge.dispatch_due()
        self.bridge.observe()
        diagnosis = self.diagnose()
        self.assertEqual({"past_deadline_without_start": 1}, diagnosis["execution"]["phases"])
        self.assertIn("deadline_passed_without_final", diagnosis["reasons"])
        self.assertTrue(diagnosis["execution_unknown"])
        self.assertEqual(1, self.sends())                                         # never sent again
        self.assertEqual(record, path.read_bytes())                               # never acknowledged
        self.assertEqual(outbox, self.ledger("SELECT COUNT(*) FROM response_outbox")[0][0])   # no result invented

    def test_running_request_is_never_queued_as_fresh_work(self):
        path, ledger = self.request_fixture(state="running")
        self.set_reply_consent(True)
        self.assertEqual("request_running", self.bridge.ingest(path)["reason"])
        self.assertEqual([], self.bridge.status())
        ledger.request_snapshot.return_value = {"state": "received", "overdue": False}
        self.assertEqual("queued", self.bridge.ingest(path)["status"])
        ledger.request_snapshot.return_value = {"state": "running", "last_state": "running", "overdue": False}
        self.assertEqual(1, self.bridge.dispatch_due()["skipped"])
        self.assertEqual(("skipped", "request_running"), (self.state(), self.bridge.status()[0]["error"]))
        self.assertEqual([], self.client.calls)

    def test_marker_in_history_means_notification_seen_not_started(self):
        paths = [self.tracked("request_0001"), self.message("message_0002"), self.message("message_0003")]
        for path in paths:
            self.bridge.ingest(path)
        self.client.send_error = DeliveryError(True)
        self.assertEqual(3, self.bridge.dispatch_due()["uncertain"])
        self.client.send_error = None
        self.client.read_result = {"tailmsgMarkers": ["[TAILMSG:request_0001]", "[TAILMSG:message_0002]"]}
        self.now += 10
        counts = self.bridge.dispatch_due()
        self.assertEqual((2, 1), (counts["delivered"], counts["uncertain"]))
        self.assertEqual(1, [name for name, _ in self.client.calls].count("read_thread"))   # one read per pass
        self.assertEqual(3, self.sends())
        self.assertEqual(self.now, {r["id"]: r for r in self.bridge.status()}["request_0001"]["seen_at"])
        self.assertEqual({"awaiting_start": 1, "awaiting_handling": 1, "delivery_unconfirmed": 1}, self.phases())
        for result in (RuntimeError("history read failed"), {"isError": True}, {"tailmsgMarkers": []}):
            self.client.read_result = result if isinstance(result, dict) else None
            with patch.object(self.client, "call_tool", side_effect=result) if not isinstance(result, dict) else \
                    contextlib.suppress():
                self.now += 10
                self.bridge.dispatch_due()
            self.assertEqual("uncertain", {r["id"]: r for r in self.bridge.status()}["message_0003"]["state"])
        self.assertEqual(3, self.sends())

    def test_durable_lifecycle_outranks_an_old_transport_state(self):
        path = self.tracked()
        self.bridge.ingest(path)
        self.client.send_error = DeliveryError(True)
        self.bridge.dispatch_due()
        self.core.respond_request(self.core.Ctx(self.tailmsg), "request_0001", "completed", "Done")
        diagnosis = self.diagnose()
        self.assertEqual({"final": 1}, diagnosis["execution"]["phases"])
        self.assertIsNone(diagnosis["execution"]["oldest_without_start"])
        self.assertEqual({"confirmed": 0, "pending": 1, "unknown": 0}, diagnosis["execution"]["final_response_delivery"])
        self.assertNotIn("delivery_unconfirmed", diagnosis["reasons"])
        self.assertFalse(diagnosis["execution_unknown"])

    def test_observe_closes_only_on_positive_evidence(self):
        acked, gone = self.message("message_0001"), self.message("message_0002")
        unresolved, timed_out, final = self.tracked("request_0001"), self.tracked("request_0002"), self.tracked("request_0003")
        self.enqueue(acked, gone, unresolved, timed_out, final)
        self.rewrite(acked, lambda r: r.update(acked_at="2026-01-01T00:00:00Z"))
        gone.unlink()
        ctx = self.core.Ctx(self.tailmsg)
        self.ledger("UPDATE requests SET deadline='2000-01-01T00:00:00Z' WHERE id='request_0002'")
        self.core.expire_requests(ctx)
        self.rewrite(timed_out, lambda r: r.update(acked_at="2026-01-01T00:00:00Z"))   # ack after a timeout
        result = self.core.respond_request(ctx, "request_0003", "completed", "Done")
        ledger = self.tailmsg / "requests"
        moved = self.root / "ledger moved away"
        os.rename(str(ledger), str(moved))                       # ledger and inbox file both missing for a while
        unresolved_record = unresolved.read_bytes()
        unresolved.unlink()
        self.bridge.observe()
        self.assertEqual({"message_0001": "acked", "message_0002": "record_missing"},
                         {r["id"]: r["closed_reason"] for r in self.bridge.status() if r["closed_at"]})
        os.rename(str(moved), str(ledger))                       # the receiver's ledger and inbox come back
        unresolved.write_bytes(unresolved_record)
        self.ledger("UPDATE response_outbox SET delivered_at='2026-01-01T00:00:00Z' WHERE id=?", result["id"])
        self.bridge.observe()
        closed = {r["id"]: r["closed_reason"] for r in self.bridge.status() if r["closed_at"]}
        self.assertEqual({"message_0001": "acked", "message_0002": "record_missing", "request_0003": "request_final"},
                         closed)
        self.assertEqual({"awaiting_start": 1, "past_deadline_without_start": 1}, self.phases())
        self.assertEqual(5, self.sends())

    def test_unknown_provenance_never_closes_or_borrows_evidence(self):
        legacy, foreign = self.message("message_0001"), self.tracked("request_0001")
        self.enqueue(legacy, foreign)
        self.core.respond_request(self.core.Ctx(self.tailmsg), "request_0001", "completed", "Done")
        self.bridge.db.execute("UPDATE messages SET tracked=NULL WHERE id='message_0001'")   # an older row
        self.bridge.db.execute("UPDATE messages SET path=? WHERE id='request_0001'",
                               (str(self.root / "another home" / "inbox" / "request_0001.json"),))
        self.bridge.db.commit()
        legacy.unlink()
        self.bridge.observe()
        self.assertEqual([], [r["id"] for r in self.bridge.status() if r["closed_at"]])
        diagnosis = self.diagnose()
        self.assertEqual({"record_unavailable": 1, "provenance_mismatch": 1}, diagnosis["execution"]["phases"])
        self.assertEqual(("partial", "unknown"), (diagnosis["observation_quality"],
                                                  self.diagnose(codex_remote=None)["state"]
                                                  if not diagnosis["reasons"] else "unknown"))
        self.bridge.db.execute("UPDATE messages SET thread_id='another-thread', path=? WHERE id='request_0001'",
                               (str(self.inbox / "request_0001.json"),))
        self.bridge.db.commit()
        self.assertEqual(1, self.phases()["provenance_mismatch"])

    def test_invalid_ledger_times_are_unknown_not_a_start(self):
        self.enqueue(self.tracked("request_0001"), self.tracked("request_0002"))
        self.ledger("UPDATE requests SET started_at='garbage', progress_at='13:00' WHERE id='request_0001'")
        self.ledger("UPDATE requests SET state='running', started_at='garbage', progress_at='x' WHERE id='request_0002'")
        self.assertEqual({"awaiting_start": 1, "running": 1}, self.phases())

    def test_stale_progress_is_reported_without_changing_the_lifecycle(self):
        self.enqueue(self.tracked())
        ctx = self.core.Ctx(self.tailmsg)
        self.core.respond_request(ctx, "request_0001", "running", "Started")
        self.assertEqual({"running": 1}, self.phases())
        self.now = float(int(time.time())) + 2 * 3600   # progress was stamped in real time, maybe a second after setUp
        diagnosis = self.diagnose()
        self.assertEqual({"running_without_recent_progress": 1}, diagnosis["execution"]["phases"])
        self.assertGreaterEqual(diagnosis["execution"]["stalled_running"]["progress_age_seconds"], 7200)
        self.assertEqual("running", self.core.request_snapshot(self.tailmsg, "request_0001")["state"])

    def test_status_and_diagnose_are_read_only(self):
        config = dict(self.config, bridge_home=str(self.root / "never created"))
        path = self.root / "read-only.json"
        path.write_text(json.dumps(config))

        def run(command):
            output = io.StringIO()
            with patch("codex_bridge.Bridge", side_effect=AssertionError("constructor")), \
                    patch("subprocess.Popen", side_effect=AssertionError("process")), \
                    patch("codex_bridge.DaemonHeartbeat", side_effect=AssertionError("heartbeat")), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(0, codex_bridge.main(["--config", str(path), command]))
            return json.loads(output.getvalue())
        self.assertEqual([], run("status"))
        diagnosis = run("diagnose")
        self.assertEqual(("unavailable", {"available": False, "reason": "queue_absent"}),
                         (diagnosis["observation_quality"], diagnosis["queue"]))
        self.assertIn("daemon_not_confirmed", diagnosis["reasons"])
        self.assertFalse((self.root / "never created").exists())
        self.bridge.ingest(self.message())
        self.bridge.close()
        legacy = Path(self.config["bridge_home"]) / "bridge.sqlite3"
        with contextlib.closing(sqlite3.connect(str(legacy))) as db:     # back to an old schema
            db.execute("CREATE TABLE old AS SELECT id,path,wake,state,attempts,next_attempt,created_at,updated_at,"
                       "error,thread_id,host_id FROM messages")
            db.execute("DROP TABLE messages")
            db.execute("ALTER TABLE old RENAME TO messages")
            db.commit()
        config["bridge_home"] = self.config["bridge_home"]
        path.write_text(json.dumps(config))
        before = (legacy.read_bytes(), os.stat(str(legacy)).st_mtime_ns)
        self.assertEqual(["message_0001"], [row["id"] for row in run("status")])
        legacy_view = run("diagnose")
        self.assertEqual((1, False), (legacy_view["dispatch"]["waiting"], legacy_view["queue"]["receipt_columns"]))
        self.assertEqual(before, (legacy.read_bytes(), os.stat(str(legacy)).st_mtime_ns))
        self.bridge = self.open_bridge()

    def test_daemon_phases_are_isolated_and_reported_by_its_heartbeat(self):
        heartbeat = codex_bridge.DaemonHeartbeat(self.bridge.home)
        self.bridge.ingest(self.message())
        with patch.object(self.bridge, "scan", side_effect=OSError("private detail")), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            outcomes, failure = codex_bridge.daemon_cycle(self.bridge, heartbeat)
        self.assertEqual({"scan": "failed", "dispatch": "ok", "observe": "ok"}, outcomes)
        self.assertEqual(1, self.sends())                                 # dispatch still ran
        self.assertIn("codex_bridge: scan_failed OSError", stderr.getvalue())
        state = self.bridge.home / "daemon.json"
        self.assertEqual(0o600, os.stat(str(state)).st_mode & 0o777)
        self.assertNotIn("private detail", state.read_text())
        self.assertEqual({"phase": "scan", "error": "OSError"},
                         {k: json.loads(state.read_text())["failure"][k] for k in ("phase", "error")})
        diagnosis = self.diagnose()
        self.assertEqual("reporting", diagnosis["daemon"]["state"])
        self.assertIn("daemon_cycle_failed", diagnosis["reasons"])
        codex_bridge.daemon_cycle(self.bridge, heartbeat)
        doc = json.loads(state.read_text())
        self.assertIsNone(doc["failure"])
        self.assertTrue(doc["last_full_success_at"])

    def test_unknown_outcome_with_rate_limit_text_stays_uncertain_without_details(self):
        self.bridge.ingest(self.message())
        self.client.send_error = CodexAppError("HTTP 429 Too Many Requests: private body", True, "queue_failed")
        self.assertEqual(1, self.bridge.dispatch_due()["uncertain"])
        self.client.send_error = None
        self.now += 60
        self.bridge.dispatch_due()
        self.assertEqual(1, self.sends())
        self.assertEqual(("uncertain", "delivery_unconfirmed"), (self.state(), self.bridge.status()[0]["error"]))
        self.assertNotIn("429", json.dumps(self.bridge.status()))
        self.assertIn("delivery_unconfirmed", self.diagnose()["reasons"])

    def test_unavailable_desktop_is_known_unsent_and_its_gaps_are_reported(self):
        self.bridge.ingest(self.message())
        self.client.send_error = CodexAppError("Codex desktop is not available", False, "app_unavailable")
        self.assertEqual(1, self.bridge.dispatch_due()["retry"])
        diagnosis = self.diagnose()
        self.assertIn("dispatch_blocked", diagnosis["reasons"])
        self.assertEqual({"app_unavailable": 1}, diagnosis["queue"]["retry_reasons"])
        caps = diagnosis["capabilities"]
        self.assertEqual(("codex_desktop", "not_available", "not_observed", False, False),
                         (caps["transport"], caps["resident"], caps["runtime_events"],
                          caps["retract_after_enqueue"], caps["automatic_resend"]))
        self.client.send_error = None
        self.receipt()
        self.now += 3600
        self.assertEqual(1, self.bridge.dispatch_due()["enqueued"])
        self.assertEqual("enqueued", self.state())

    def test_resident_connection_is_never_start_evidence(self):
        self.enqueue(self.message())
        (self.bridge.home / "resident.json").write_text(json.dumps(
            {"state": "connected", "thread_id": "dispatcher-fixed", "updated_at": time.time(), "reason": None}))
        self.now += 3600
        diagnosis = self.diagnose(codex_remote="unix:///nonexistent/app.sock")
        self.assertEqual("connected", diagnosis["resident"]["state"])
        self.assertNotIn("resident_not_connected", diagnosis["reasons"])
        self.assertIn("accepted_without_start", diagnosis["reasons"])
        self.assertEqual("daemon_keeps_thread_loaded", diagnosis["capabilities"]["resident"])

    def test_one_dispatcher_owns_a_send_even_when_two_run_together(self):
        self.bridge.ingest(self.message())
        started, release, results = threading.Event(), threading.Event(), []
        client = FakeClient()

        def call_tool(name, arguments):
            client.calls.append((name, arguments))
            started.set()
            release.wait(5)
            return {"ok": True}
        client.call_tool = call_tool

        def first():
            bridge = Bridge(self.config, client_factory=lambda _: client, clock=lambda: self.now)
            try:
                results.append(bridge.dispatch_due())
            finally:
                bridge.close()
        thread = threading.Thread(target=first)
        thread.start()
        self.assertTrue(started.wait(5))
        second = Bridge(self.config, client_factory=lambda _: client, clock=lambda: self.now)
        try:
            busy = second.dispatch_due()
        finally:
            second.close()
        release.set()
        thread.join(5)
        self.assertEqual((1, 1, 1), (busy.get("busy"), results[0]["enqueued"], len(client.calls)))

    def test_legacy_rows_keep_their_states_without_invented_evidence(self):
        for mid in ("message_0001", "message_0002", "message_0003", "message_0004", "message_0005"):
            self.message(mid)
        self.bridge.close()
        db_path = Path(self.config["bridge_home"]) / "bridge.sqlite3"
        db_path.unlink()
        with contextlib.closing(sqlite3.connect(str(db_path))) as legacy:
            legacy.execute("""CREATE TABLE messages (
                id TEXT PRIMARY KEY, path TEXT NOT NULL, wake TEXT NOT NULL,
                state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL, received_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                error TEXT, thread_id TEXT NOT NULL, host_id TEXT NOT NULL)""")
            for mid, state in (("message_0001", "enqueued"), ("message_0002", "delivered"),
                               ("message_0003", "uncertain"), ("message_0004", "sending"),
                               ("message_0005", "skipped")):
                legacy.execute("INSERT INTO messages VALUES (?,?,'now',?,1,?,?,?,?,NULL,'dispatcher-fixed','local')",
                               (mid, str(self.inbox / (mid + ".json")), state, self.now, self.now, self.now, self.now))
            legacy.commit()
        self.bridge = self.open_bridge()
        rows = {r["id"]: r for r in self.bridge.status()}
        self.assertEqual({None}, {(r["queue_id"] or r["enqueued_at"] or r["seen_at"] or r["closed_at"]) for r in rows.values()})
        self.assertEqual({0}, {r["tracked"] for r in rows.values()})          # from the stored records only
        counts = self.bridge.dispatch_due()
        states = {r["id"]: r["state"] for r in self.bridge.status()}
        self.assertEqual({"message_0001": "enqueued", "message_0002": "delivered", "message_0003": "uncertain",
                          "message_0004": "uncertain", "message_0005": "skipped"}, states)
        self.assertEqual((0, 2), (self.sends(), counts["uncertain"]))          # reconciled only, never resent
        self.assertEqual("received_at", self.diagnose()["execution"]["oldest_without_start"]["age_basis"])


HANDLER = r"""#!%(python)s
# Neutral test handler: records its argv and whether stdin was empty, then acts per its mode file.
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "handler-calls.jsonl"), "a") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.read()}) + "\n")
mode = open(os.path.join(here, "handler-mode")).read().strip()
if mode == "spawn":
    import subprocess
    child = subprocess.Popen(["sleep", "60"])
    with open(os.path.join(here, "handler-pids.json"), "w") as out:
        json.dump([os.getpid(), child.pid], out)
if mode in ("sleep", "spawn"):
    time.sleep(30)
sys.exit(3 if mode == "fail" else 0)
"""


class KindRoutingTests(BridgeTests):
    """excluded_kinds / excluded_kind_adapter: generic, neutral kinds and a local fake handler."""

    def setUp(self):
        super().setUp()
        self.tools = self.root / "handler tools"
        self.tools.mkdir()
        self.handler = self.tools / "handler"
        self.handler.write_text(HANDLER % {"python": sys.executable})
        self.handler.chmod(0o755)
        self.handler_mode("ok")
        self.config.update(excluded_kinds=["routed-kind"],
                           excluded_kind_adapter=[str(self.handler), "--fixed", "local arg"],
                           excluded_kind_timeout_seconds=5)
        self.bridge.close()
        self.bridge = self.open_bridge()

    def handler_mode(self, value):
        (self.tools / "handler-mode").write_text(value)

    def handler_calls(self):
        log = self.tools / "handler-calls.jsonl"
        return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []

    def routed(self, mid="routed_0001", wake="now", **overrides):
        path = self.message(mid, wake=wake, **overrides)
        self.rewrite(path, lambda r: r["envelope"].update({"kind": "routed-kind"}))
        return path

    def test_trusted_route_runs_handler_with_only_the_canonical_path(self):
        path = self.routed()
        result, code = self.bridge.wake(self.inbox / ".." / "inbox" / path.name)
        self.assertEqual((0, "forwarded"), (code, result["status"]))
        self.assertEqual([{"argv": ["--fixed", "local arg", str(path)], "stdin": ""}], self.handler_calls())
        self.assertNotIn("PRIVATE", json.dumps(self.handler_calls()))
        self.assertEqual([], self.bridge.status())
        self.assertEqual([], self.client.calls)

    def test_ordinary_kinds_still_queue_through_wake(self):
        result, code = self.bridge.wake(self.message())
        self.assertEqual((0, "queued"), (code, result["status"]))
        self.assertEqual([], self.handler_calls())

    def test_none_batch_and_acked_never_run_the_handler(self):
        for mid, wake, extra, reason in (("routed_none", "none", {}, "wake_none"),
                                         ("routed_batch", "batch", {}, "wake_batch"),
                                         ("routed_acked", "now", {"acked_at": "2020-01-01T00:00:00Z"}, "acked")):
            with self.subTest(reason=reason):
                result, code = self.bridge.wake(self.routed(mid, wake=wake, **extra))
                self.assertEqual((0, "skipped", reason), (code, result["status"], result["reason"]))
        self.assertEqual([], self.handler_calls())
        self.assertEqual([], self.bridge.status())

    def test_unverified_records_are_rejected_before_routing(self):
        path = self.routed()
        outside = self.root / path.name
        outside.write_bytes(path.read_bytes())
        link = self.inbox / "routed_link1.json"
        link.symlink_to(outside)
        for candidate in (outside, link):
            with self.assertRaisesRegex(Rejected, "invalid_inbox_path"):
                self.bridge.wake(candidate)
        self.rewrite(path, lambda r: r["envelope"].update({"from": "other-node"}))
        with self.assertRaisesRegex(Rejected, "sender_mismatch"):
            self.bridge.wake(path)
        path = self.routed("routed_0002")
        self.rewrite(path, lambda r: r["envelope"].update({"agent": "grok"}))
        with self.assertRaisesRegex(Rejected, "wrong_agent"):
            self.bridge.wake(path)
        path = self.routed("routed_0003")
        self.local["allow"] = ["other-node"]
        self.write_local()
        with self.assertRaisesRegex(Rejected, "peer_not_allowed"):
            self.bridge.wake(path)
        self.local["allow"] = ["grok-bot"]
        self.write_local()
        self.rewrite(path, lambda r: r["envelope"].update({"kind": ["routed-kind"]}))
        with self.assertRaisesRegex(Rejected, "invalid_kind"):
            self.bridge.wake(path)
        self.assertEqual([], self.handler_calls())

    def test_scan_and_dispatch_never_run_the_handler_or_queue_excluded_kinds(self):
        self.routed()
        self.message("message_0002")
        self.assertEqual({"queued": 1, "duplicate": 0, "skipped": 1, "rejected": 0}, self.bridge.scan())
        self.assertEqual(1, self.bridge.dispatch_due()["enqueued"])
        self.assertEqual(1, len(self.client.calls))
        self.assertNotIn("routed_0001", json.dumps(self.client.calls))
        self.assertEqual([], self.handler_calls())

    def test_rows_queued_before_routing_are_revalidated_at_dispatch(self):
        self.bridge.close()
        plain = {k: v for k, v in self.config.items() if not k.startswith("excluded_kind")}
        self.bridge = Bridge(plain, client_factory=lambda _: self.client, clock=lambda: self.now)
        for mid in ("routed_0001", "routed_0002"):
            self.bridge.ingest(self.routed(mid))
        self.bridge.db.execute("UPDATE messages SET state='uncertain' WHERE id='routed_0002'")
        self.bridge.db.commit()
        self.bridge.close()
        self.bridge = self.open_bridge()
        self.assertEqual(2, self.bridge.dispatch_due()["skipped"])
        self.assertEqual({"skipped"}, {row["state"] for row in self.bridge.status()})
        self.assertEqual({"excluded_kind"}, {row["error"] for row in self.bridge.status()})
        self.assertEqual([], self.client.calls)
        self.assertEqual([], self.handler_calls())

    def test_started_handlers_report_outcomes_without_inviting_a_replay(self):
        self.handler_mode("fail")
        result, code = self.bridge.wake(self.routed("routed_fail"))
        self.assertEqual((0, "forward_failed", 3), (code, result["status"], result["exit_code"]))
        self.handler_mode("sleep")
        self.bridge.handler_timeout = 1
        started = time.monotonic()
        result, code = self.bridge.wake(self.routed("routed_slow"))
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual((0, "forward_timeout"), (code, result["status"]))
        self.assertEqual(2, len(self.handler_calls()))       # each ran once; nothing is replayed here
        self.assertEqual([], self.bridge.status())

    def test_handlers_that_cannot_start_fail_without_running(self):
        self.bridge.excluded_kind_adapter = [str(self.tools / "missing-handler")]
        result, code = self.bridge.wake(self.routed("routed_gone"))
        self.assertEqual((127, "forward_not_started", "handler_unavailable"), (code, result["status"], result["reason"]))
        self.bridge.excluded_kind_adapter = None
        result, code = self.bridge.wake(self.routed("routed_none_cfg"))
        self.assertEqual((0, "skipped", "excluded_kind_no_handler"), (code, result["status"], result["reason"]))
        self.assertEqual([], self.handler_calls())

    def test_handler_budget_ends_before_the_receiver_timeout(self):
        self.bridge.handler_timeout = 60
        for hook, budget in ((None, 60), (0, 60), (30, 20), (11.5, 1.5), (10.5, None), (5, None),
                             (-1, None), (True, None), ("120", None), (float("inf"), None)):
            with self.subTest(hook=hook):
                self.local["hook_timeout_s"] = hook
                if hook is None:
                    del self.local["hook_timeout_s"]
                self.write_local()
                self.assertEqual(budget, self.bridge.handler_budget())
        self.local["hook_timeout_s"] = 5
        self.write_local()
        before = self.local
        result, code = self.bridge.wake(self.routed())
        self.assertEqual((1, "forward_not_started", "no_handler_time_budget"), (code, result["status"], result["reason"]))
        self.assertEqual(before, json.loads((self.tailmsg / "config.json").read_text()))   # config never changed
        self.assertEqual([], self.handler_calls())

    def test_receiver_timeout_never_leaves_a_handler_behind(self):
        self.local["hook_timeout_s"] = 11.5          # 1.5 s handler budget inside an 11.5 s receiver call
        self.write_local()
        self.handler_mode("spawn")
        path = self.root / "routing.json"
        path.write_text(json.dumps(self.config), encoding="utf-8")
        script = Path(__file__).resolve().with_name("codex_bridge.py")
        run = subprocess.run([sys.executable, str(script), "--config", str(path), "wake", str(self.routed())],
                             capture_output=True, text=True, timeout=11.5)   # what the receiver allows
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertEqual("forward_timeout", json.loads(run.stdout)["status"])
        self.assertIn("codex_bridge: forward_timeout", run.stderr)
        pids = json.loads((self.tools / "handler-pids.json").read_text())
        deadline = time.monotonic() + 5
        alive = pids
        while alive and time.monotonic() < deadline:
            alive = [pid for pid in pids if _alive(pid)]
            time.sleep(0.05)
        self.assertEqual([], alive)

    def test_malformed_routing_config_fails_closed_before_side_effects(self):
        path = self.root / "routing.json"
        state = self.root / "never created"
        for key, value in (("excluded_kinds", "routed-kind"), ("excluded_kinds", [""]), ("excluded_kinds", ["x" * 33]),
                           ("excluded_kinds", ["bad\nkind"]), ("excluded_kinds", [7]),
                           ("excluded_kind_adapter", "relative-handler"), ("excluded_kind_adapter", []),
                           ("excluded_kind_adapter", ["relative-handler"]), ("excluded_kind_adapter", ["/bin/x", ""]),
                           ("excluded_kind_timeout_seconds", 0), ("excluded_kind_timeout_seconds", 301),
                           ("excluded_kind_timeout_seconds", True), ("excluded_kind_timeout_seconds", "60")):
            with self.subTest(key=key, value=value):
                config = dict(self.config, bridge_home=str(state), **{key: value})
                path.write_text(json.dumps(config), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_config(path)
                with self.assertRaises(ValueError):
                    Bridge(config, client_factory=lambda _: self.client)
                self.assertFalse(state.exists())

    def test_command_line_wake_reports_and_exits_with_the_outcome(self):
        path = self.root / "routing.json"
        path.write_text(json.dumps(self.config), encoding="utf-8")
        script = Path(__file__).resolve().with_name("codex_bridge.py")
        self.handler_mode("fail")
        run = subprocess.run([sys.executable, str(script), "--config", str(path), "wake", str(self.routed())],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(0, run.returncode, run.stderr)             # started: the receiver must not replay it
        self.assertEqual("forward_failed", json.loads(run.stdout)["status"])
        self.assertIn("codex_bridge: forward_failed", run.stderr)
        run = subprocess.run([sys.executable, str(script), "--config", str(path), "status"],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual([], json.loads(run.stdout))
        self.assertEqual(1, len(self.handler_calls()))

    def test_tracked_requests_routed_to_a_handler_are_never_restarted(self):
        self.bridge.excluded_kinds = frozenset(["routed-kind", "request"])
        path = self.routed()
        self.rewrite(path, lambda r: r["envelope"].update(kind="request", request={"timeout_seconds": 900,
                                                                                   "reply_port": 7766}))
        ledger = Mock()
        self.addCleanup(patch.stopall)
        patch("codex_bridge._POLICY_CORE", ledger).start()
        patch("codex_bridge._access_decision", return_value=(True, "tailnet")).start()
        for snapshot, reason in (({"state": "running", "overdue": False}, "request_running"),
                                 ({"state": "completed", "overdue": False}, "request_completed"),
                                 ({"state": "received", "overdue": True}, "request_expired")):
            ledger.request_snapshot.return_value = snapshot
            result, code = self.bridge.wake(path)
            self.assertEqual((0, "skipped", reason), (code, result["status"], result["reason"]))
        self.assertEqual([], self.handler_calls())
        ledger.request_snapshot.return_value = {"state": "received", "overdue": False}
        self.assertEqual("forwarded", self.bridge.wake(path)[0]["status"])
        ledger.request_record_response.assert_not_called()


def _alive(pid):
    """True while pid exists and is not a zombie (a reaped or exited child counts as gone)."""
    try:
        with open("/proc/%d/stat" % pid) as stat_file:
            return stat_file.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


if __name__ == "__main__":
    unittest.main()
