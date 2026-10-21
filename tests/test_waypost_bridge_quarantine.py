"""The Waypost bridge quarantines what it refuses instead of halting.

A small loopback stub plays the Waypost API (describe, messages, inbox feed, receipts) and a recording script
plays the Yobiru launcher, so these run without a Waypost binary. test_waypost_bridge_e2e.py repeats the
incoming case against a real Waypost when WAYPOST_TEST_BINARY is set. Synthetic data on 127.0.0.1 only.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BRIDGE = REPO / "tools" / "waypost_bridge.py"
MAPPED, UNRELATED, UNREADABLE = "1111111111111111", "2222222222222222", "3333333333333333"


class FakeWaypost(object):
    """describe, message send, inbox feed, message read and receipts; records every write."""

    def __init__(self, feed=(), messages=None):
        self.posted, self.receipts = [], []
        self.feed, self.messages = list(feed), dict(messages or {})
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, data):
                body = json.dumps({"ok": True, "data": data}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/v0/describe":
                    return self.reply({"space_id": "space_test", "actor_id": "actor_bridge",
                                       "consumer_id": "consumer_bridge",
                                       "scopes": ["message.send", "message.read", "receipt.write"],
                                       "limits": {"max_page_size": 20, "max_json_bytes": 16384},
                                       "allowed_recipient_ids": ["actor_primary"]})
                return self.reply(outer.messages[self.path.rsplit("/", 1)[1]])

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/v0/messages":
                    outer.posted.append(body)
                    return self.reply({"submission_state": "committed", "message_id": "message_%d" % len(outer.posted)})
                if self.path == "/v0/receipts":
                    outer.receipts.append(body)
                    return self.reply({"submission_state": "committed", "consumer_id": "consumer_bridge",
                                       "items": body["items"]})
                items = [] if body.get("cursor") else outer.feed      # /v0/inbox/read: one page, then nothing
                return self.reply({"items": items, "has_more": False, "next_cursor": "cursor_after"})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.server.server_port

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@unittest.skipUnless(os.name == "posix", "the bridge requires POSIX")
class QuarantineTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="yobiru waypost quarantine ")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        (self.home / "inbox").mkdir(parents=True, mode=0o700)
        self.home.chmod(0o700)
        self.token = self.root / "token"
        self.token.write_text("synthetic-token")
        self.token.chmod(0o600)
        self.routes = self.root / "routes.json"
        self.routes.write_text(json.dumps({"incoming": {"alpha.example.ts.net/grok": "actor_primary"},
                                           "outgoing": {"actor_primary": "alpha/grok"}}))
        self.calls = self.root / "launcher-calls.txt"
        self.launcher = self.root / "launcher.sh"            # plays `yobiru send`; records every invocation
        self.launcher.write_text("#!/bin/sh\necho \"$*\" >> %s\nexit 1\n" % self.calls)
        self.journal = self.home / "waypost-bridge.sqlite3"

    def message(self, mid, node, text="synthetic report"):
        env = {"id": mid, "from": node, "from_agent": "grok", "text": text}
        (self.home / "inbox" / (mid + ".json")).write_text(json.dumps({"envelope": env, "received": {"peer": {"node": node}}}))

    def waypost(self, **kw):
        fake = FakeWaypost(**kw)
        self.addCleanup(fake.close)
        return fake

    def args(self, fake, *flags):
        return [sys.executable, str(BRIDGE), "--home", str(self.home), "--yobiru", str(self.launcher),
                "--waypost-url", fake.url, "--token-file", str(self.token), "--map", str(self.routes)] + list(flags)

    def run_bridge(self, fake, *flags):
        return subprocess.run(self.args(fake, "--once", *flags) if not set(flags) & {
            "--list-refused", "--dismiss-refused", "--retry-refused"} else self.args(fake, *flags),
            capture_output=True, text=True, timeout=30)

    def reports(self, result, key="refused"):
        return [(row[key], row.get("item_id")) for row in map(json.loads, result.stderr.splitlines()) if key in row]

    def rows(self, table, key):
        db = sqlite3.connect(str(self.journal))
        try:
            return sorted(db.execute("SELECT %s,state,error FROM %s" % (key, table)).fetchall())
        finally:
            db.close()

    def test_unrelated_and_unreadable_records_are_quarantined_and_never_stop_the_bridge(self):
        fake = self.waypost()
        self.message(MAPPED, "alpha.example.ts.net")
        self.message(UNRELATED, "unrelated.example.ts.net", "UNRELATED-PRIVATE report")
        (self.home / "inbox" / (UNREADABLE + ".json")).write_text("{not json")
        first = self.run_bridge(fake)
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual({"incoming_sent": 1, "outgoing_sent": 0, "refused": 2}, json.loads(first.stdout))
        self.assertEqual([("INVALID_INBOX_RECORD", UNREADABLE), ("UNKNOWN_INCOMING_ROUTE", UNRELATED)],
                         sorted(self.reports(first)))
        self.assertEqual([], self.reports(first, "error"))
        self.assertEqual(1, len(fake.posted))
        self.assertNotIn("UNRELATED-PRIVATE", json.dumps(fake.posted))
        self.assertEqual([(MAPPED, "sent", None), (UNRELATED, "refused", "UNKNOWN_INCOMING_ROUTE"),
                          (UNREADABLE, "refused", "INVALID_INBOX_RECORD")], self.rows("incoming", "envelope_id"))
        again = self.run_bridge(fake)
        self.assertEqual((0, [], 1), (again.returncode, self.reports(again), len(fake.posted)), "reported once only")
        (self.home / "inbox" / (UNRELATED + ".json")).unlink()
        self.assertEqual(0, self.run_bridge(fake).returncode, "removing a quarantined record changes nothing")
        # The continuous bridge keeps polling instead of exiting after the first pass.
        command = self.args(fake, "--interval", "0.1")
        running = subprocess.Popen([command[0], "-u"] + command[1:], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        try:
            time.sleep(1.5)
            self.assertIsNone(running.poll(), "the continuous bridge stopped")
        finally:
            running.terminate()
            out, _ = running.communicate(timeout=10)
        self.assertGreater(len(out.splitlines()), 1)
        self.assertFalse(self.calls.exists(), "nothing was sent to Yobiru")

    def test_quarantined_items_are_resolved_only_explicitly(self):
        fake = self.waypost()
        self.message(UNRELATED, "unrelated.example.ts.net")
        self.assertEqual(0, self.run_bridge(fake).returncode)
        listed = self.run_bridge(fake, "--list-refused")
        self.assertEqual([{"direction": "incoming", "item_id": UNRELATED, "state": "refused",
                           "error": "UNKNOWN_INCOMING_ROUTE"}], [json.loads(x) for x in listed.stdout.splitlines()])
        retried = self.run_bridge(fake, "--retry-refused", UNRELATED)
        self.assertEqual(0, retried.returncode, retried.stderr)
        self.assertEqual([], self.rows("incoming", "envelope_id"), "retry only forgets the refusal")
        again = self.run_bridge(fake)
        self.assertEqual([("UNKNOWN_INCOMING_ROUTE", UNRELATED)], self.reports(again), "planned again, refused again")
        self.assertEqual(0, self.run_bridge(fake, "--dismiss-refused", UNRELATED).returncode)
        self.assertEqual([(UNRELATED, "dismissed", "UNKNOWN_INCOMING_ROUTE")], self.rows("incoming", "envelope_id"))
        later = self.run_bridge(fake)
        self.assertEqual((0, [], []), (later.returncode, self.reports(later), fake.posted))
        unknown = self.run_bridge(fake, "--dismiss-refused", "9999999999999999")
        self.assertEqual(2, unknown.returncode)
        self.assertIn("ONLY_ONE_REFUSED_ITEM_CAN_BE_RESOLVED", unknown.stderr)

    def test_refused_waypost_items_are_never_sent_or_acknowledged(self):
        feed, messages = [], {}
        for n, (producer, body, attachments) in enumerate([("actor_other", {"text": "unmapped producer"}, []),
                                                            ("actor_primary", {"text": "with a file"}, ["blob_1"]),
                                                            ("actor_primary", {"structured": True}, [])]):
            item, message = "item_%d" % n, "msg_%d" % n
            feed.append({"item_type": "message", "item_id": item, "resource_id": message, "producer_id": producer,
                         "space_id": "space_test"})
            messages[message] = {"message_id": message, "producer_id": producer, "space_id": "space_test",
                                 "body": body, "attachment_ids": attachments}
        fake = self.waypost(feed=feed, messages=messages)
        first = self.run_bridge(fake)
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual([("UNKNOWN_OUTGOING_ROUTE", "item_0"), ("ATTACHMENTS_UNSUPPORTED", "item_1"),
                          ("INVALID_WAYPOST_MESSAGE", "item_2")], self.reports(first))
        self.assertEqual(([], False), (fake.receipts, self.calls.exists()), "no ACK and no Yobiru send")
        self.assertEqual(["refused"] * 3, [state for _, state, _ in self.rows("outgoing", "item_id")])
        self.assertEqual(0, self.run_bridge(fake).returncode)
        self.assertEqual([], fake.receipts)
        retry = self.run_bridge(fake, "--retry-refused", "item_0")
        self.assertEqual(2, retry.returncode)
        self.assertIn("ONLY_INCOMING_REFUSALS_CAN_BE_RETRIED", retry.stderr)

    def test_refusals_kept_pending_by_an_earlier_build_are_quarantined_on_open(self):
        fake = self.waypost()
        self.message(UNRELATED, "unrelated.example.ts.net")
        self.assertEqual(0, self.run_bridge(fake).returncode)
        db = sqlite3.connect(str(self.journal))
        with db:                                          # the earlier build kept refusals as failing pending rows
            db.execute("UPDATE incoming SET state='pending' WHERE envelope_id=?", (UNRELATED,))
        db.close()
        result = self.run_bridge(fake)
        self.assertEqual((0, [], []), (result.returncode, self.reports(result), self.reports(result, "error")))
        self.assertEqual([(UNRELATED, "refused", "UNKNOWN_INCOMING_ROUTE")], self.rows("incoming", "envelope_id"))
        self.assertEqual([], fake.posted)

    def test_dry_run_plans_refusals_without_failing(self):
        fake = self.waypost()
        self.message(MAPPED, "alpha.example.ts.net")
        self.message(UNRELATED, "unrelated.example.ts.net")
        dry = subprocess.run(self.args(fake, "--dry-run"), capture_output=True, text=True, timeout=30)
        self.assertEqual(0, dry.returncode, dry.stderr)
        self.assertEqual({"dry_run": True, "incoming_planned": 1, "outgoing_planned": 0, "refused_planned": 1,
                          "sent": False}, json.loads(dry.stdout))
        self.assertEqual([("UNKNOWN_INCOMING_ROUTE", UNRELATED)], self.reports(dry))
        self.assertFalse(self.journal.exists())
        self.assertEqual([], fake.posted)


if __name__ == "__main__":
    unittest.main()
