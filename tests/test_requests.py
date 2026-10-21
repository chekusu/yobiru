"""Durable request state machine, ownership and crash/retry regressions; isolated homes only."""
import argparse
import contextlib
import datetime as dt
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support.pkg import REPO

spec = importlib.util.spec_from_file_location("request_core", REPO / "tailmsg.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)

A, B, C = "alpha.example.ts.net", "bravo.example.ts.net", "charlie.example.ts.net"
MID = "0011223344556677"


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.a = core.Ctx(Path(self.temp.name) / "a")
        self.b = core.Ctx(Path(self.temp.name) / "b")
        self.env = {"v": 1, "id": MID, "from": A, "to": B, "ts": core.iso(core.now_utc()),
                    "kind": "request", "text": "Please inspect", "agent": "worker", "from_agent": "codex",
                    "request": {"timeout_seconds": 900, "reply_port": 8123}}
        self.rec = {"envelope": self.env, "received": {"at": self.env["ts"], "peer": {"node": A},
                                                     "agent": "worker", "agent_status": "known"}}

    def send(self):
        core.save_outgoing_request(self.a, self.env, B, 9123)
        core.accept_request(self.b, self.rec)

    def state(self, ctx, direction="incoming"):
        return core.request_snapshot(ctx.home, MID, direction)

    def test_received_and_final_are_durable_and_ack_guarded(self):
        self.send()
        core.recover_request_inbox(self.b)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            core.cmd_ack(self.b, argparse.Namespace(id=[MID]))
        receipt = self.state(self.b)["response"]
        self.assertEqual(receipt["response"]["state"], "received")
        self.assertEqual(receipt["wake"], "none")
        core.accept_response(self.a, receipt, B)
        self.assertFalse(self.state(self.a, "outgoing")["outcome_known"])
        final = core.respond_request(self.b, MID, "completed", "Inspected successfully")
        core.accept_response(self.a, final, B)
        with contextlib.redirect_stdout(io.StringIO()):
            core.cmd_ack(self.b, argparse.Namespace(id=[MID]))
        self.assertEqual(self.state(self.a, "outgoing")["state"], "completed")
        self.assertEqual(core.request_record_response(self.b.home, MID, "completed", "Inspected successfully"), final)
        with self.assertRaises(ValueError):
            core.respond_request(self.b, MID, "failed", "Contradiction")

    def test_response_matching_rejects_forged_peer_agent_and_unknown_request(self):
        self.send()
        response = self.state(self.b)["response"]
        for changes, peer in (({}, C), ({"from_agent": "intruder"}, B),
                              ({"agent": "intruder"}, B), ({"reply_to": "9988776655443322"}, B)):
            invalid = dict(response, **changes)
            with self.assertRaises(ValueError):
                core.accept_response(self.a, invalid, peer)
        self.assertEqual(self.state(self.a, "outgoing")["state"], "pending")

    def test_reordered_duplicate_responses_never_downgrade_final(self):
        self.send()
        receipt = self.state(self.b)["response"]
        running = core.respond_request(self.b, MID, "running", "Working")
        final = core.respond_request(self.b, MID, "completed", "Done")
        self.assertTrue(core.accept_response(self.a, final, B))
        self.assertTrue(core.accept_response(self.a, running, B))
        self.assertTrue(core.accept_response(self.a, receipt, B))
        self.assertFalse(core.accept_response(self.a, final, B))
        self.assertEqual(self.state(self.a, "outgoing")["state"], "completed")
        with self.assertRaises(ValueError):
            core.accept_response(self.a, dict(final, text="modified"), B)

    def test_duplicate_request_authentication_and_semantic_conflicts(self):
        self.send()
        self.assertFalse(core.accept_request(self.b, self.rec))
        forged = dict(self.rec, received={"peer": {"node": C}})
        with self.assertRaises(ValueError):
            core.accept_request(self.b, forged)
        with self.assertRaises(ValueError):
            core.accept_request(self.b, dict(self.rec, envelope=dict(self.env, text="Different operation")))
        with core.request_db(self.b) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM response_outbox").fetchone()[0], 1)

    def test_status_only_original_sender_and_unknown_agent_rejected(self):
        self.rec["received"]["agent_status"] = "unknown"
        self.send()
        self.assertIsNone(core.incoming_request_status(self.b, MID, C))
        self.assertEqual(core.incoming_request_status(self.b, MID, A)["state"], "rejected")

    def test_timeout_remains_unknown_and_accepts_late_actual_result(self):
        self.env["ts"] = core.iso(core.now_utc() - dt.timedelta(seconds=20))
        self.env["request"]["timeout_seconds"] = 10
        self.send()
        core.expire_requests(self.b)
        timeout = self.state(self.b)["response"]
        self.assertEqual(timeout["response"]["state"], "timed_out")
        core.accept_response(self.a, timeout, B)
        self.assertFalse(self.state(self.a, "outgoing")["outcome_known"])
        self.assertTrue(self.state(self.a, "outgoing")["overdue"])
        with self.assertRaises(ValueError):
            core.respond_request(self.b, MID, "running", "Restart operation")
        final = core.respond_request(self.b, MID, "completed", "Original operation completed late")
        core.accept_response(self.a, final, B)
        self.assertEqual(self.state(self.a, "outgoing")["state"], "completed")
        self.assertTrue(self.state(self.a, "outgoing")["late"])
        self.assertIsNotNone(self.state(self.a, "outgoing")["timed_out_at"])

    def test_restart_reconstructs_inbox_and_retries_identical_offline_response(self):
        self.send()
        restarted = core.Ctx(self.b.home)
        core.recover_request_inbox(restarted)
        self.assertEqual(json.loads((restarted.inbox / (MID + ".json")).read_text())["envelope"], self.env)
        restarted.resolve = lambda host: (host, "100.64.0.2")
        restarted.whois = lambda ip: {"node": A}
        ids = []
        def unavailable(ip, port, method, path, env, **kwargs):
            ids.append(env["id"])
            self.assertEqual(port, 8123)
            raise core.TransportError("offline")
        with mock.patch.object(core, "http_json", side_effect=unavailable):
            core.flush_responses(restarted)
        with core.request_db(restarted) as db:
            self.assertIsNone(db.execute("SELECT delivered_at FROM response_outbox").fetchone()[0])
            db.execute("UPDATE response_outbox SET next_attempt=0")
        def success(ip, port, method, path, env, **kwargs):
            ids.append(env["id"])
            return 202, {"id": env["id"], "ok": True}
        with mock.patch.object(core, "http_json", side_effect=success):
            core.flush_responses(restarted)
        self.assertEqual(len(ids), 2)
        self.assertEqual(ids[0], ids[1])
        with core.request_db(restarted) as db:
            self.assertIsNotNone(db.execute("SELECT delivered_at FROM response_outbox").fetchone()[0])

    def reply_with(self, files, text="Finished", state="completed", ctx=None):
        a = argparse.Namespace(file=[str(f) for f in files], ttl_hours=None, text=text)
        return core.respond_with_files(ctx or self.b, self.rec, a, state)

    def staged(self):
        return sorted(p.name for p in self.b.outbox.iterdir())

    def ledger(self):
        with core.request_db(self.b) as db:
            row = db.execute("SELECT seq, latest FROM requests WHERE direction='incoming' AND id=?", (MID,)).fetchone()
            outbox = db.execute("SELECT id FROM response_outbox WHERE request_id=?", (MID,)).fetchall()
        return row["seq"], json.loads(row["latest"]), [r["id"] for r in outbox]

    def artifact(self, data=b"same bytes\n" * 100, name="result.txt"):
        path = Path(self.temp.name) / "files" / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
        return path

    def test_an_identical_final_reply_with_files_returns_the_stored_response(self):
        self.send()
        result = self.artifact()
        first = self.reply_with([result])
        copies, before = self.staged(), self.ledger()
        self.assertEqual(1, len(copies))
        again = self.reply_with([result])            # re-staged under a new token, then found identical
        self.assertEqual(first, again)                # the stored id, seq and original fetch URLs
        self.assertEqual((copies, before), (self.staged(), self.ledger()))
        changed = [lambda: self.reply_with([self.artifact(b"same bytez\n" * 100)]),      # same size, other hash
                   lambda: self.reply_with([self.artifact(b"other size")]),
                   lambda: self.reply_with([self.artifact(name="renamed.txt")]),
                   lambda: self.reply_with([self.artifact()], text="Finished!"),
                   lambda: self.reply_with([self.artifact()], state="failed"),
                   lambda: self.reply_with([])]
        for attempt in changed:
            with self.assertRaisesRegex(ValueError, "durable final result"):
                attempt()
            self.artifact()                          # restore the original content for the next case
        self.assertEqual((copies, before), (self.staged(), self.ledger()), "no refused retry leaves a copy")

    def test_cleanup_follows_the_ledger_not_the_exception(self):
        self.send()
        result = self.artifact()
        real = core.respond_request
        with mock.patch.object(core, "respond_request", side_effect=OSError("before the commit")):
            with self.assertRaises(OSError):
                self.reply_with([result])
        self.assertEqual([], self.staged(), "nothing refers to a copy whose response never committed")

        def commit_then_fail(*args, **kwargs):
            real(*args, **kwargs)
            raise OSError("after the durable commit")
        with mock.patch.object(core, "respond_request", side_effect=commit_then_fail):
            with self.assertRaises(OSError):
                self.reply_with([result])
        copies = self.staged()
        self.assertEqual(1, len(copies), "the committed response refers to its copy: it must stay")
        seq, latest, _ = self.ledger()
        self.assertIn("/f/%s/" % copies[0], latest["fetch"][0]["url"])
        self.assertEqual(result.read_bytes(), (self.b.outbox / copies[0] / "files" / result.name).read_bytes())
        self.assertEqual(latest, self.reply_with([result]))
        self.assertEqual(copies, self.staged())

    def test_a_copy_is_kept_when_the_ledger_cannot_be_read(self):
        self.send()
        with mock.patch.object(core, "respond_request", side_effect=OSError("failed")), \
                mock.patch.object(core, "staged_result_referenced", side_effect=core.sqlite3.OperationalError("locked")), \
                contextlib.redirect_stderr(io.StringIO()) as log:
            with self.assertRaises(OSError):
                self.reply_with([self.artifact()])
        self.assertEqual(1, len(self.staged()))
        self.assertIn("kept staged result files", log.getvalue())

    def test_an_uncertain_ledger_keeps_the_copy_and_creates_nothing(self):
        import sqlite3

        def remove_ledger(path):
            for part in path.parent.glob(path.name + "*"):
                part.rename(part.with_name(part.name + ".moved"))

        def drop_row(path):
            with contextlib.closing(sqlite3.connect(str(path))) as db, db:
                db.execute("DELETE FROM requests WHERE direction='incoming' AND id=?", (MID,))

        def corrupt_latest(path):
            with contextlib.closing(sqlite3.connect(str(path))) as db, db:
                db.execute("UPDATE requests SET latest='{not json' WHERE direction='incoming' AND id=?", (MID,))
        for name, change in (("missing ledger", remove_ledger), ("missing request row", drop_row),
                             ("malformed response", corrupt_latest)):
            with self.subTest(name):
                home = Path(self.temp.name) / name.replace(" ", "-")
                self.a, self.b = core.Ctx(home / "a"), core.Ctx(home / "b")
                self.send()
                ledger = self.b.home / "requests" / "ledger.sqlite3"

                def fail_after_change(*args, **kwargs):
                    change(ledger)
                    raise OSError("failed while the ledger changed")
                with mock.patch.object(core, "respond_request", side_effect=fail_after_change), \
                        contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(OSError):
                        self.reply_with([self.artifact()])
                self.assertEqual(1, len(self.staged()), "uncertainty is not proof that the copy is unused")
                if name == "missing ledger":
                    self.assertFalse(ledger.exists(), "cleanup must not create an empty ledger")

    def test_failed_staging_leaves_no_partial_copy(self):
        self.send()
        first, missing = self.artifact(), Path(self.temp.name) / "files" / "missing.txt"
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.reply_with([first, missing])                    # the second file is missing
        real_open, calls = core.private_open, []

        def second_copy_fails(path, *args, **kwargs):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("disk full")
            return real_open(path, *args, **kwargs)
        with mock.patch.object(core, "private_open", side_effect=second_copy_fails):
            with self.assertRaises(OSError):
                self.reply_with([first, self.artifact(name="second.txt")])
        with mock.patch.object(core, "atomic_write", side_effect=OSError("meta.json not written")):
            with self.assertRaises(OSError):
                self.reply_with([first])
        self.assertEqual([], self.staged())
        self.assertEqual("received", self.state(self.b)["state"])

    def test_concurrent_identical_replies_store_one_response_and_one_copy(self):
        import threading
        self.send()
        result, results, errors = self.artifact(), [], []
        barrier = threading.Barrier(4)

        def reply():
            try:
                ctx = core.Ctx(self.b.home)                     # as separate processes would
                barrier.wait(10)
                results.append(self.reply_with([result], ctx=ctx))
            except Exception as exc:                            # pragma: no cover - reported below
                errors.append(exc)
        workers = [threading.Thread(target=reply) for _ in range(4)]
        with contextlib.redirect_stderr(io.StringIO()):
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(30)
        self.assertEqual([], errors)
        self.assertEqual(4, len(results))
        self.assertTrue(all(r == results[0] for r in results), "BEGIN IMMEDIATE serializes the final result")
        self.assertEqual(1, len(self.staged()))
        seq, latest, outbox = self.ledger()
        self.assertEqual((results[0], 1), (latest, len([x for x in outbox if x == latest["id"]])))

    def test_read_only_snapshot_does_not_create_state(self):
        path = Path(self.temp.name) / "absent"
        self.assertIsNone(core.request_snapshot(path, MID))
        self.assertFalse(path.exists())

    def test_validation_rejects_malformed_extensions(self):
        self.assertIsNone(core.validate_env(self.env))
        for request in ({"timeout_seconds": True, "reply_port": 7766},
                        {"timeout_seconds": 0, "reply_port": 7766},
                        {"timeout_seconds": 900, "reply_port": 65536},
                        {"timeout_seconds": 900, "reply_port": 7766, "extra": True}):
            self.assertIsNotNone(core.validate_env(dict(self.env, request=request)))
        self.send()
        response = self.state(self.b)["response"]
        for changes in ({"wake": "now"}, {"response": {"state": "completed", "seq": True}},
                        {"kind": "text"}):
            self.assertIsNotNone(core.validate_env(dict(response, **changes)))

    def test_send_negotiation_refuses_legacy_without_creating_or_posting_request(self):
        self.a.self_node = lambda: {"node": A}
        self.a.resolve = lambda peer: (B, "100.64.0.2")
        args = argparse.Namespace(peer=B, kind="request", text="work", reply_to=None, wake="now",
                                  file=[], ttl_hours=None, timeout_seconds=900, legacy_request=False)
        with mock.patch.object(core, "http_json", return_value=(200, {"version": "0.4.4"})) as transport:
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                core.cmd_send(self.a, args)
            self.assertEqual(transport.call_count, 1)
            self.assertEqual(transport.call_args.args[2], "GET")
        self.assertFalse((self.a.home / "requests").exists())
        args.legacy_request = True
        with mock.patch.object(core, "http_json", return_value=(202, {"ok": True})) as transport:
            with contextlib.redirect_stdout(io.StringIO()):
                core.cmd_send(self.a, args)
            self.assertEqual(transport.call_args.args[2], "POST")
            self.assertNotIn("request", transport.call_args.args[4])
        self.assertFalse((self.a.home / "requests").exists())

    def test_invalid_capabilities_never_send_and_wrong_http_receipt_is_unknown(self):
        self.a.self_node = lambda: {"node": A}
        self.a.resolve = lambda peer: (B, "100.64.0.2")
        args = argparse.Namespace(peer=B, kind="request", text="work", reply_to=None, wake="now",
                                  file=[], ttl_hours=None, timeout_seconds=900, legacy_request=False)
        for info in ({"node": C, "capabilities": [core.REQUEST_CAPABILITY]},
                     {"node": B, "capabilities": core.REQUEST_CAPABILITY},
                     {"node": B, "capabilities": None}):
            with mock.patch.object(core, "http_json", return_value=(200, info)) as transport:
                with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                    core.cmd_send(self.a, args)
                self.assertEqual(transport.call_count, 1)
        with mock.patch.object(core, "http_json", side_effect=[
                (200, {"node": B, "capabilities": [core.REQUEST_CAPABILITY]}),
                (202, {"ok": True, "id": "wrong_id"})]):
            output = io.StringIO()
            with self.assertRaises(SystemExit), contextlib.redirect_stdout(output):
                core.cmd_send(self.a, args)
            result = json.loads(output.getvalue())
            self.assertEqual(result["submission"], "unknown")

    def test_status_list_are_read_only_even_when_deadline_passed(self):
        self.send()
        with core.request_db(self.b) as db:
            db.execute("UPDATE requests SET deadline='2000-01-01T00:00:00Z'")
        with mock.patch.object(core, "request_db", side_effect=AssertionError("query attempted write")):
            args = argparse.Namespace(request_cmd="status", id=MID, direction="incoming", refresh=False, json=True)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                core.cmd_request(self.b, args)
            result = json.loads(output.getvalue())
            self.assertEqual(result["state"], "timed_out")
            self.assertEqual(result["last_state"], "received")
            self.assertFalse(result["outcome_known"])
        core.recover_request_inbox(self.b)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            core.cmd_ack(self.b, argparse.Namespace(id=[MID]))
        missing = Path(self.temp.name) / "never-created"
        with mock.patch.object(core.sys, "argv", ["tailmsg", "--home", str(missing), "request", "list", "--json"]):
            with contextlib.redirect_stdout(io.StringIO()):
                core.main()
        self.assertFalse(missing.exists())

    def test_oversized_or_invalid_result_does_not_commit_terminal_state(self):
        self.send()
        for text in ("x" * (256 * 1024), None):
            with self.assertRaises(ValueError):
                core.respond_request(self.b, MID, "completed", text)
            self.assertEqual(self.state(self.b)["state"], "received")
        response = self.state(self.b)["response"]
        self.assertIsNotNone(core.validate_env(dict(response, response={"state": "received", "seq": 2 ** 70})))
        invalid = dict(self.env, ts="9999-12-31T23:59:59Z")
        self.assertIsNotNone(core.validate_env(invalid))

    def test_malformed_refresh_preserves_local_state_without_traceback(self):
        self.send()
        self.a.resolve = lambda peer: (B, "100.64.0.2")
        self.a.whois = lambda ip: {"node": B}
        args = argparse.Namespace(request_cmd="status", id=MID, direction="outgoing", refresh=True, json=True)
        for response in ({"request": None}, {"request": []}, {"request": {"response": 1}},
                         {"request": {"response": {"reply_to": MID}}}):
            with mock.patch.object(core, "http_json", return_value=(200, response)):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    core.cmd_request(self.a, args)
                self.assertEqual(json.loads(output.getvalue())["refresh"], "unavailable")
                self.assertEqual(self.state(self.a, "outgoing")["state"], "pending")
        self.a.whois = lambda ip: {"node": C}
        with mock.patch.object(core, "http_json") as transport, contextlib.redirect_stdout(io.StringIO()):
            core.cmd_request(self.a, args)
            transport.assert_not_called()

    def test_unauthenticated_confirmation_does_not_remove_response_outbox(self):
        self.send()
        self.b.resolve = lambda peer: (A, "100.64.0.1")
        self.b.whois = lambda ip: {"node": A}
        with mock.patch.object(core, "http_json", return_value=(202, {"ok": False, "id": self.state(self.b)["response"]["id"]})):
            core.flush_responses(self.b)
        with core.request_db(self.b) as db:
            self.assertIsNone(db.execute("SELECT delivered_at FROM response_outbox").fetchone()[0])

    def test_protocol_response_cannot_be_acked_materialized_or_woken(self):
        self.send()
        response = self.state(self.b)["response"]
        core.accept_response(self.a, response, B)
        response_id = response["id"]
        path = self.a.inbox / (response_id + ".json")
        self.assertFalse(path.exists())
        for handler, args in ((core.cmd_ack, argparse.Namespace(id=[response_id])),
                              (core.cmd_wake, argparse.Namespace(id=response_id))):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                handler(self.a, args)
            self.assertFalse(path.exists())
        # Defense in depth: even an old/manual inbox file containing a protocol response cannot
        # become executable through the generic wake adapter path.
        _, rec = core.load_msg(self.a, response_id)
        path.write_text(json.dumps(rec))
        self.a.agents = {"codex": {"wake_adapter": {"type": "exec", "argv": ["fake-adapter"]}}}
        with mock.patch.object(core, "call_exec") as adapter:
            self.assertFalse(core.deliver_wake(self.a, path))
            adapter.assert_not_called()

    def test_wake_checks_deadline_again_before_each_adapter_retry(self):
        self.send()
        core.recover_request_inbox(self.b)
        self.b.agents = {"worker": {"wake_adapter": {"type": "exec", "argv": ["fake-adapter"]}}}
        self.b.cfg["wake_attempts"] = 3
        def deadline_passed(seconds):
            with core.request_db(self.b) as db:
                db.execute("UPDATE requests SET deadline='2000-01-01T00:00:00Z'")
        with mock.patch.object(core, "call_exec", side_effect=core.AdapterError("temporary failure")) as adapter:
            with mock.patch.object(core.time, "sleep", side_effect=deadline_passed):
                self.assertFalse(core.deliver_wake(self.b, self.b.inbox / (MID + ".json")))
            self.assertEqual(adapter.call_count, 1)
        core.respond_request(self.b, MID, "completed", "Already completed")
        with mock.patch.object(core, "call_exec") as adapter:
            self.assertFalse(core.deliver_wake(self.b, self.b.inbox / (MID + ".json")))
            adapter.assert_not_called()

    def test_send_persists_before_post_and_never_replays_unknown_operation(self):
        self.a.self_node = lambda: {"node": A}
        self.a.resolve = lambda peer: (B, "100.64.0.2")
        args = argparse.Namespace(peer=B, kind="request", text="work", reply_to=None, wake="now",
                                  file=[], ttl_hours=None, timeout_seconds=900, legacy_request=False)
        mid = []
        def post(ip, port, method, path, env=None, **kwargs):
            if method == "GET":
                return 200, {"node": B, "capabilities": [core.REQUEST_CAPABILITY]}
            mid.append(env["id"])
            self.assertEqual(core.request_snapshot(self.a.home, env["id"], "outgoing")["submission"], "prepared")
            raise core.TransportError("response lost")
        with mock.patch.object(core, "http_json", side_effect=post):
            with self.assertRaises(core.TransportError):
                core.cmd_send(self.a, args)
        self.assertEqual(len(mid), 1)
        self.assertEqual(core.request_snapshot(self.a.home, mid[0], "outgoing")["submission"], "unknown")
        with mock.patch.object(core, "http_json") as transport:
            core.request_maintenance(self.a)
            transport.assert_not_called()


if __name__ == "__main__":
    unittest.main()
