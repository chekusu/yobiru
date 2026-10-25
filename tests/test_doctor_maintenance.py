"""Doctor and status keep transport reachability, maintenance readiness, request backlog and dispatcher evidence
apart, and never turn missing or malformed evidence into a healthy result. Temporary homes and a loopback HTTP
stub only; the plugin CLI and its bundled core are loaded in-process (run `tools/build_plugin.py sync` first)."""
import contextlib
import datetime as dt
import importlib.util
import json
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler
from support.numeric_http import ThreadingHTTPServer
from pathlib import Path

from support.pkg import REPO

spec = importlib.util.spec_from_file_location("yobiru_doctor_under_test", REPO / "plugins" / "yobiru" / "scripts" / "yobiru.py")
Y = importlib.util.module_from_spec(spec)
spec.loader.exec_module(Y)
core = Y.core()
core.log = lambda *a: None
A, B = "alpha.example.ts.net", "bravo.example.ts.net"


def checks(f):
    out = {}

    def add(status, name, detail, fix=None):
        out[name] = {"status": status, "detail": detail, "fix": fix}
    Y.maintenance_check(f, add)
    Y.backlog_check(f, add)
    Y.dispatcher_check(f, add)
    return out


def receiver(maintenance=None, ok=True):
    r = {"url": "http://100.64.0.2:7766/health", "reachable": True, "http_status": 200, "ok": ok, "v": 1, "node": B}
    if maintenance is not None:
        r["maintenance"] = maintenance
    return r


class MaintenanceCheckTests(unittest.TestCase):
    def test_degraded_maintenance_is_an_error_even_though_the_receiver_answers(self):
        health = {"supported": True, "status": "degraded", "reasons": ["worker_dead"], "instance": "i1",
                  "components": {"requests": "dead", "responses": "ok", "attachments": "ok"}}
        f = {"receiver": receiver(health, ok=False), "home": "/h"}
        found = checks(f)["maintenance"]
        self.assertEqual("error", found["status"])
        self.assertIn("requests dead", found["detail"])
        self.assertIn("replacement", found["fix"])

    def test_stalled_is_a_warning_and_starting_is_information(self):
        for status, reasons, expected in (("degraded", ["no_recent_full_cycle"], "warn"), ("starting", [], "info"),
                                          ("healthy", [], "ok")):
            health = {"supported": True, "status": status, "reasons": reasons, "components": {"responses": "x"}}
            self.assertEqual(expected, checks({"receiver": receiver(health), "home": "/h"})["maintenance"]["status"])

    def test_an_older_runtime_is_not_treated_as_healthy(self):
        found = checks({"receiver": receiver(), "home": "/h"})["maintenance"]
        self.assertEqual("warn", found["status"])
        self.assertIn("install-runtime", found["fix"])


class HeartbeatRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def write(self, doc):
        (self.home / Y.MAINTENANCE_FILE).write_text(doc if isinstance(doc, str) else json.dumps(doc))

    def doc(self, instance):
        return {"schema": 1, "instance": instance, "written_at": core.iso(core.now_utc()), "status": "healthy",
                "workers": {"requests": {"status": "ok", "last_full_success_age_seconds": 1}}}

    def test_only_the_live_instance_makes_the_record_current(self):
        self.write(self.doc("old-process"))
        record = Y.maintenance_record(self.home, live_instance="new-process")
        self.assertEqual((True, False), (record["present"], record["current"]))
        health = {"supported": True, "status": "healthy", "reasons": [], "components": {"requests": "ok"},
                  "instance": "new-process"}
        found = checks({"receiver": receiver(health), "maintenance_record": record, "home": "/h"})["maintenance"]
        self.assertIn("another receiver process", found["detail"])
        self.assertTrue(Y.maintenance_record(self.home, live_instance="old-process")["current"])

    def test_malformed_or_missing_records_are_reported_as_such(self):
        self.assertEqual({"present": False}, Y.maintenance_record(self.home))
        for raw in ("{not json", json.dumps({"schema": 2}), json.dumps([1])):
            self.write(raw)
            record = Y.maintenance_record(self.home, live_instance="x")
            self.assertTrue(record.get("error"), raw)
            found = checks({"receiver": receiver({"supported": True, "status": "healthy", "reasons": [],
                                                  "components": {}}), "maintenance_record": record,
                            "home": "/h"})["maintenance"]
            self.assertIn("unreadable", found["detail"])
            self.assertNotIn("another receiver process", found["detail"])
            unreachable = checks({"receiver": {"reachable": False}, "maintenance_record": record, "home": "/h"})
            self.assertEqual("warn", unreachable["maintenance"]["status"])


class BacklogCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "b"
        self.ctx = core.Ctx(self.home)

    def response(self, mid, age, attempts, due=True):
        env = {"v": 1, "id": mid, "from": A, "to": B, "ts": core.iso(core.now_utc()), "kind": "request",
               "text": "x", "request": {"timeout_seconds": 900, "reply_port": 7766}}
        core.accept_request(self.ctx, {"envelope": env, "received": {"at": env["ts"], "peer": {"node": A},
                                                                     "agent": None, "agent_status": "default"}})
        stamp = core.iso(core.now_utc() - dt.timedelta(seconds=age))
        with contextlib.closing(sqlite3.connect(str(self.home / "requests" / "ledger.sqlite3"))) as db:
            for rid, envelope in db.execute("SELECT id, envelope FROM response_outbox WHERE request_id=?", (mid,)).fetchall():
                value = json.loads(envelope)
                value["ts"] = stamp
                db.execute("UPDATE response_outbox SET envelope=?, attempts=?, next_attempt=? WHERE id=?",
                           (json.dumps(value), attempts, 0 if due else 4102444800, rid))
            db.commit()

    def found(self):
        return checks({"request_backlog": core.request_backlog(self.home), "home": str(self.home)})["requests"]

    def test_only_due_never_attempted_responses_suggest_delivery_is_not_running(self):
        # Rows are created in time order, as the receiver writes them.
        self.response("1111222233334444", age=3600, attempts=5)              # old, already retried (peer offline)
        self.response("3333444455556666", age=3500, attempts=0, due=False)   # old, not due yet
        self.response("2222333344445555", age=10, attempts=0)                # fresh, not attempted yet
        found = self.found()
        self.assertEqual("info", found["status"])
        self.assertIn("3 waiting for delivery", found["detail"])
        self.assertIsNone(found["fix"])

    def test_old_due_responses_never_attempted_are_a_warning(self):
        self.response("4444555566667777", age=3600, attempts=0)              # old, due, never attempted
        self.response("2222333344445555", age=10, attempts=0)
        found = self.found()
        self.assertEqual("warn", found["status"])
        self.assertIn("without any delivery attempt", found["fix"])

    def test_ages_come_from_each_responses_valid_timestamp_not_insertion_order(self):
        self.response("2222333344445555", age=10, attempts=0)          # written first, fresh stamp
        self.response("4444555566667777", age=3600, attempts=0)        # written later, after a clock step back
        backlog = core.request_backlog(self.home)
        self.assertGreaterEqual(backlog["responses"]["oldest_unattempted_due_age_seconds"], 3600)
        self.assertGreaterEqual(backlog["responses"]["oldest_pending_age_seconds"], 3600)
        self.assertEqual("warn", self.found()["status"])

    def test_a_malformed_first_row_never_hides_an_old_due_response(self):
        self.response("1111222233334444", age=10, attempts=0)
        self.response("4444555566667777", age=3600, attempts=0)
        with contextlib.closing(sqlite3.connect(str(self.home / "requests" / "ledger.sqlite3"))) as db:
            db.execute("UPDATE response_outbox SET envelope='{\"ts\": \"garbage\"}' "
                       "WHERE request_id='1111222233334444'")
            db.commit()
        backlog = core.request_backlog(self.home)["responses"]
        self.assertEqual(1, backlog["unreadable_timestamps"])
        self.assertGreaterEqual(backlog["oldest_unattempted_due_age_seconds"], 3600)
        found = self.found()
        self.assertEqual("warn", found["status"])
        self.assertIn("unreadable timestamp", found["detail"])
        backlog = core.request_backlog(self.home)
        backlog["responses"]["age_scan_truncated"] = True
        partial = checks({"request_backlog": backlog, "home": str(self.home)})["requests"]
        self.assertIn("(partial)", partial["detail"])

    def test_an_unreadable_ledger_is_unknown_not_empty(self):
        (self.home / "requests").mkdir()
        (self.home / "requests" / "ledger.sqlite3").write_bytes(b"not a database" * 50)
        found = self.found()
        self.assertEqual("warn", found["status"])
        self.assertIn("backlog unknown", found["detail"])
        self.assertNotIn("nothing was changed", found["fix"])


class ProbeTests(unittest.TestCase):
    def test_health_probe_keeps_the_maintenance_summary(self):
        body = {"ok": False, "v": 1, "node": B, "receiver_alive": True, "status": "degraded",
                "maintenance": {"supported": True, "status": "degraded", "components": {"requests": "dead"},
                                "reasons": ["worker_dead"], "instance": "i1"}}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        result = Y.probe_health("127.0.0.1", server.server_address[1])
        self.assertEqual((True, False, "degraded"), (result["reachable"], result["ok"], result["status"]))
        self.assertEqual(body["maintenance"], result["maintenance"])
        found = checks({"receiver": result, "home": "/h"})["maintenance"]
        self.assertEqual("error", found["status"])


if __name__ == "__main__":
    unittest.main()
