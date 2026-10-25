"""Receiver maintenance supervision, bounded response delivery and read-only lifecycle diagnostics.

Isolated temporary homes, fake clocks, mocked transports and loopback sockets only: no Tailscale, peer,
service, production home or network. A real outage or native agent execution is not simulated here.
"""
import argparse
import contextlib
import datetime as dt
import errno
import http.client
import importlib.util
import io
import ipaddress
import json
import os
import socket
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from support.numeric_http import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from support.pkg import REPO

spec = importlib.util.spec_from_file_location("maintenance_core", REPO / "tailmsg.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
core.TAILNET_NETS.append(ipaddress.ip_network("127.0.0.0/8"))
core.log = lambda *a: None

A, B, C = "alpha.example.ts.net", "bravo.example.ts.net", "charlie.example.ts.net"
SECRET = "SECRET-BODY-7f3a"


def eventually(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class Clock(object):
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now


class Alive(object):
    """A stand-in for a running worker thread when a cycle is driven directly."""

    def is_alive(self):
        return True


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.a = core.Ctx(self.root / "a")        # sends requests and receives their responses
        self.b = core.Ctx(self.root / "b")        # receives requests, keeps the ledger and its response outbox

    def request(self, mid, text="Please inspect", ts=None, timeout=900, peer=A, port=8123):
        env = {"v": 1, "id": mid, "from": peer, "to": B, "ts": ts or core.iso(core.now_utc()), "kind": "request",
               "text": text, "agent": "worker", "request": {"timeout_seconds": timeout, "reply_port": port}}
        rec = {"envelope": env, "received": {"at": core.iso(core.now_utc()), "peer": {"node": peer},
                                             "agent": "worker", "agent_status": "known"}}
        core.accept_request(self.b, rec)
        return env

    def ledger(self, ctx=None):
        db = sqlite3.connect(str((ctx or self.b).home / "requests" / "ledger.sqlite3"))
        db.row_factory = sqlite3.Row
        return contextlib.closing(db)

    def row(self, mid):
        with self.ledger() as db:
            return dict(db.execute("SELECT * FROM requests WHERE direction='incoming' AND id=?", (mid,)).fetchone())


# ---------------------------------------------------------------- supervision
class SupervisorTests(Base):
    def workers(self, **phases):
        return {name: (((name, fn),), 0.01, 60) for name, fn in phases.items()}

    def started(self, m):
        """Stop and join this supervisor's threads before the temporary home is removed."""
        def stop():
            m.stop.set()
            for worker in m.workers.values():
                if hasattr(worker.thread, "join") and worker.thread.is_alive():
                    worker.thread.join(5)
        self.addCleanup(stop)
        return m

    def test_dead_worker_gets_exactly_one_replacement_and_stays_reported_until_a_full_cycle(self):
        calls = []

        def phase(ctx):
            calls.append(1)
            if len(calls) == 1:
                raise SystemExit("the worker thread ends here")
        m = self.started(core.Maintenance(self.b, workers=self.workers(requests=phase)))
        m.start()
        worker = m.workers["requests"]
        first = worker.thread
        self.assertTrue(eventually(lambda: not first.is_alive()))
        snapshot = m.snapshot()
        self.assertEqual(("degraded", "dead"), (snapshot["status"], snapshot["components"]["requests"]))
        self.assertIn("worker_dead", snapshot["reasons"])
        m.supervise()
        second = worker.thread
        self.assertIsNot(first, second)
        for _ in range(20):                    # a live worker is never replaced again
            m.supervise()
        self.assertIs(second, worker.thread)
        self.assertTrue(eventually(lambda: m.snapshot()["status"] == "healthy"))
        detail = m.snapshot(detail=True)["workers"]["requests"]
        self.assertEqual((1, 2), (detail["restarts"], detail["generation"]))
        self.assertEqual("SystemExit", detail["last_exit"]["error"])
        self.assertIsNone(detail["failure"])
        m.stop.set()

    def test_slow_live_worker_is_reported_stalled_but_never_cloned(self):
        release, entered, clock = threading.Event(), [], Clock()

        def slow(ctx):
            entered.append(1)
            release.wait(10)
        m = self.started(core.Maintenance(self.b, workers={"responses": ((("flush", slow),), 0.01, 1800)},
                                          clock=clock))
        self.addCleanup(release.set)
        m.start()
        self.assertTrue(eventually(lambda: entered))
        clock.now += 660                       # a long but legitimate delivery pass
        self.assertEqual("starting", m.snapshot()["components"]["responses"])
        clock.now += 1200                      # far beyond the diagnostic threshold
        snapshot = m.snapshot(detail=True)
        self.assertEqual("stalled", snapshot["workers"]["responses"]["status"])
        self.assertEqual("flush", snapshot["workers"]["responses"]["phase"])
        self.assertGreaterEqual(snapshot["workers"]["responses"]["in_cycle_seconds"], 1860)
        thread = m.workers["responses"].thread
        for _ in range(50):
            m.supervise()
        self.assertIs(thread, m.workers["responses"].thread)
        self.assertEqual(1, len(entered))
        self.assertEqual(1, m.workers["responses"].generation)
        release.set()
        self.assertTrue(eventually(lambda: m.workers["responses"].success is not None))
        m.stop.set()

    def test_worker_that_cannot_start_leaves_the_listener_degraded_and_is_retried(self):
        m = self.started(core.Maintenance(self.b, workers=self.workers(requests=lambda ctx: None)))
        with mock.patch.object(core.threading.Thread, "start", side_effect=RuntimeError("can't start new thread")):
            m.start()                          # recorded, not raised
        snapshot = m.snapshot(detail=True)
        self.assertEqual("degraded", snapshot["status"])
        self.assertEqual({"phase": "start", "error": "RuntimeError"},
                         {k: snapshot["workers"]["requests"]["failure"][k] for k in ("phase", "error")})
        m.supervise()
        self.assertTrue(eventually(lambda: m.snapshot()["status"] == "healthy"))
        m.stop.set()

    def test_a_recovery_failure_never_stops_expiry_or_delivery(self):
        old = core.iso(core.now_utc() - dt.timedelta(seconds=30))
        self.request("1111222233334444", ts=old, timeout=10)
        self.b.resolve = lambda peer: (peer, "100.64.0.1")
        self.b.whois = lambda ip: {"node": A}
        posts = []

        def post(ip, port, method, path, env=None, **kw):
            posts.append(env["id"])
            return 202, {"ok": True, "id": env["id"]}
        m = core.Maintenance(self.b)
        for worker in m.workers.values():
            worker.thread, worker.started = Alive(), m.clock()
        with mock.patch.object(core, "recover_request_inbox", side_effect=OSError(errno.ENOSPC, "disk full")), \
                mock.patch.object(core, "http_json", side_effect=post):
            m._cycle(m.workers["requests"])
            m._cycle(m.workers["responses"])
        requests = m.snapshot(detail=True)["workers"]["requests"]
        self.assertEqual({"recover": "failed", "expire": "ok"}, requests["last_outcomes"])
        self.assertEqual(("recover", "OSError"), (requests["failure"]["phase"], requests["failure"]["error"]))
        self.assertIsNone(requests["last_full_success_at"])
        self.assertEqual("timed_out", self.row("1111222233334444")["state"])    # expiry still persisted
        self.assertEqual(2, len(posts))                                          # receipt and timeout delivered
        self.assertIn("cycle_failed", m.snapshot()["reasons"])
        m._cycle(m.workers["requests"])                                          # disk recovered
        self.assertIsNone(m.workers["requests"].failure)
        self.assertEqual(("ok", []), (m.snapshot()["components"]["requests"], m.snapshot()["reasons"]))

    def test_heartbeat_write_failure_degrades_health_until_a_later_write_succeeds(self):
        m = core.Maintenance(self.b, workers=self.workers(requests=lambda ctx: None), heartbeat_every=0)
        worker = m.workers["requests"]
        worker.thread, worker.started = Alive(), m.clock()
        m._cycle(worker)
        path = self.b.home / core.MAINTENANCE_FILE
        before = path.read_bytes()
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        with mock.patch.object(core.os, "replace", side_effect=OSError(errno.EIO, "I/O error")):
            m._cycle(worker)
        snapshot = m.snapshot()
        self.assertEqual("degraded", snapshot["status"])
        self.assertIn("heartbeat_write_failed", snapshot["reasons"])
        self.assertEqual(before, path.read_bytes())                    # the previous durable record is kept
        self.assertEqual([], list(self.b.home.glob(".maintenance.json.*")))   # no temporary file left
        m._cycle(worker)
        self.assertEqual("healthy", m.snapshot()["status"])
        doc = json.loads(path.read_text())
        self.assertEqual(3, doc["workers"]["requests"]["cycles"])
        self.assertEqual(1, doc["heartbeat"]["failures"])

    def test_directory_sync_tolerates_only_unsupported_filesystems(self):
        real = os.fsync

        def fsync_failing(code):
            def fsync(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError(code, os.strerror(code))
                return real(fd)
            return fsync
        path = self.root / "doc.json"
        with mock.patch.object(core.os, "fsync", side_effect=fsync_failing(errno.EINVAL)):
            core.write_private_json(path, {"x": 1})
        self.assertEqual({"x": 1}, json.loads(path.read_text()))
        with mock.patch.object(core.os, "fsync", side_effect=fsync_failing(errno.EIO)):
            with self.assertRaises(OSError):
                core.write_private_json(path, {"x": 2})

    def test_heartbeat_and_health_hold_metadata_only(self):
        self.request("5555666677778888", text=SECRET)
        (self.b.outbox / ("t" * 40) / "files").mkdir(parents=True)
        (self.b.outbox / ("t" * 40) / "files" / "secret-name.txt").write_text(SECRET)
        m = core.Maintenance(self.b, heartbeat_every=0)
        for worker in m.workers.values():
            worker.thread, worker.started = Alive(), m.clock()
            m._cycle(worker)
        text = (self.b.home / core.MAINTENANCE_FILE).read_text()
        self.b.maintenance = m
        health = json.dumps(core.health_payload(self.b, B))
        for marker in (SECRET, "secret-name", A, "5555666677778888", "t" * 40, str(self.b.home)):
            self.assertNotIn(marker, text)
            self.assertNotIn(marker, health)
        self.assertEqual({"ok", "v", "node", "receiver_alive", "status", "maintenance"},
                         set(json.loads(health)))
        self.assertEqual({"supported", "status", "components", "reasons", "instance"},
                         set(json.loads(health)["maintenance"]))

    def test_a_new_process_never_inherits_an_earlier_heartbeat(self):
        first = core.Maintenance(self.b, workers=self.workers(requests=lambda ctx: None), heartbeat_every=0)
        first.workers["requests"].thread, first.workers["requests"].started = Alive(), first.clock()
        first._cycle(first.workers["requests"])
        second = core.Maintenance(self.b, workers=self.workers(requests=lambda ctx: None))
        self.assertNotEqual(first.instance, second.instance)
        self.assertEqual("dead", second.snapshot()["components"]["requests"])   # nothing started yet
        self.assertEqual(first.instance, json.loads((self.b.home / core.MAINTENANCE_FILE).read_text())["instance"])


class HealthEndpointTests(Base):
    def serve(self, ctx):
        server = ThreadingHTTPServer(("127.0.0.1", 0), core.make_handler(ctx, B))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        c.request("GET", "/health")
        r = c.getresponse()
        body = json.loads(r.read())
        c.close()
        return r.status, body

    def test_reachable_but_degraded_receiver_is_not_reported_healthy(self):
        m = core.Maintenance(self.b, workers={"requests": ((("x", lambda ctx: None),), 1, 60)})
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        m.workers["requests"].thread, m.workers["requests"].started = dead, m.clock()
        self.b.maintenance = m
        status, body = self.serve(self.b)
        self.assertEqual(200, status)                       # transport reachability stays compatible
        self.assertEqual((False, True, "degraded"), (body["ok"], body["receiver_alive"], body["status"]))
        self.assertEqual({"requests": "dead"}, body["maintenance"]["components"])

    def test_receiver_without_supervision_reports_unknown(self):
        status, body = self.serve(self.b)
        self.assertEqual((200, "unknown", {"supported": False}), (status, body["status"], body["maintenance"]))


# ---------------------------------------------------------------- response delivery
class FlushTests(Base):
    def setUp(self):
        super().setUp()
        self.b.resolve = lambda peer: (peer, "100.64.0.1")
        self.b.whois = lambda ip: {"node": A}

    def outbox(self):
        with self.ledger() as db:
            return [dict(r) for r in db.execute("SELECT id,peer,attempts,delivered_at FROM response_outbox ORDER BY rowid")]

    def test_concurrent_flush_is_busy_and_sends_nothing(self):
        self.request("1111111111111111")
        holder = core.FileLock(self.b.home / "requests" / "flush.lock")
        self.assertTrue(holder.acquire())
        try:
            with mock.patch.object(core, "http_json") as post:
                self.assertEqual(core.BUSY, core.flush_responses(self.b))
                self.assertEqual("busy", core.request_maintenance(self.b)["flush"])
                post.assert_not_called()
        finally:
            holder.release()
        self.assertEqual(0, self.outbox()[0]["attempts"])

    def test_lock_errors_other_than_contention_are_failures_not_busy(self):
        import fcntl
        self.request("1111111111111111")
        with mock.patch.object(fcntl, "flock", side_effect=OSError(errno.EIO, "I/O error")):
            with self.assertRaises(OSError):
                core.flush_responses(self.b)
            self.assertEqual("failed:OSError", core.request_maintenance(self.b)["flush"])
        with mock.patch.object(fcntl, "flock", side_effect=BlockingIOError(errno.EWOULDBLOCK, "busy")):
            self.assertEqual(core.BUSY, core.flush_responses(self.b))

    def test_offline_backlog_never_starves_a_healthy_peer(self):
        for index in range(100):
            self.request("a%015d" % index, peer=A)
        self.request("c%015d" % 0, peer=C)
        self.b.whois = lambda ip: {"node": {"100.64.0.1": A, "100.64.0.3": C}[ip]}
        self.b.resolve = lambda peer: (peer, {A: "100.64.0.1", C: "100.64.0.3"}[peer])
        attempts = []

        def post(ip, port, method, path, env=None, **kw):
            attempts.append(env["to"])
            if env["to"] == A:
                raise core.TransportError("offline")
            return 202, {"ok": True, "id": env["id"]}
        with mock.patch.object(core, "http_json", side_effect=post):
            counts = core.flush_responses(self.b)
        self.assertEqual(1, attempts.count(A))                  # one failed attempt per destination per pass
        self.assertEqual(1, attempts.count(C))
        healthy = [r for r in self.outbox() if r["peer"] == C]
        self.assertTrue(healthy[0]["delivered_at"])
        self.assertEqual((101, 1, 1, 99), (counts["due"], counts["delivered"], counts["failed"], counts["deferred"]))

    def test_each_pass_is_bounded(self):
        for index in range(30):
            self.request("b%015d" % index)
        with mock.patch.object(core, "http_json", side_effect=lambda *a, **kw: (202, {"ok": True, "id": a[4]["id"]})):
            self.assertEqual(20, core.flush_responses(self.b)["delivered"])
        clock = Clock()

        def slow(*a, **kw):
            clock.now += 200                   # one exchange used up the whole pass budget
            return 202, {"ok": True, "id": a[4]["id"]}
        with mock.patch.object(core, "http_json", side_effect=slow):
            counts = core.flush_responses(self.b, clock=clock)
        self.assertEqual((1, 9), (counts["delivered"], counts["deferred"]))

    def test_ledger_failure_after_a_possible_post_keeps_the_same_response_pending(self):
        core.save_outgoing_request(self.a, dict(self.request("2222333344445555"), to=B), B, 9123)
        real_db, real_deliver, fail = core.request_db, core.deliver_response, []

        def flaky_db(ctx):
            if fail:
                fail.pop()
                raise sqlite3.OperationalError("disk I/O error")
            return real_db(ctx)

        def deliver(ctx, row, resolved):
            result = real_deliver(ctx, row, resolved)
            fail.append(1)                      # the POST may have reached the peer; the bookkeeping fails
            return result
        sent = []

        def post(ip, port, method, path, env=None, **kw):
            sent.append(env["id"])
            fresh = core.accept_response(self.a, env, B)
            return (202 if fresh else 200), {"ok": True, "id": env["id"], "duplicate": not fresh}
        with mock.patch.object(core, "http_json", side_effect=post), \
                mock.patch.object(core, "request_db", side_effect=flaky_db), \
                mock.patch.object(core, "deliver_response", side_effect=deliver):
            with self.assertRaises(sqlite3.OperationalError):
                core.flush_responses(self.b)
        self.assertEqual([(0, None)], [(r["attempts"], r["delivered_at"]) for r in self.outbox()])
        with mock.patch.object(core, "http_json", side_effect=post):
            core.flush_responses(self.b)
        self.assertEqual(sent[0], sent[1])                        # the same immutable response ID
        self.assertTrue(self.outbox()[0]["delivered_at"])
        with self.ledger(self.a) as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM response_events").fetchone()[0])


class PeerServer(object):
    """A loopback HTTP peer that writes a scripted raw response."""

    def __init__(self, script):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.script = script
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        try:
            conn, _ = self.listener.accept()
        except OSError:
            return
        with conn:
            conn.recv(65536)
            try:
                self.script(conn)
            except OSError:
                pass

    def close(self):
        self.listener.close()


class BoundedExchangeTests(unittest.TestCase):
    def call(self, script, deadline=0.3, limit=4096):
        server = PeerServer(script)
        self.addCleanup(server.close)
        started = time.monotonic()
        try:
            return core.http_json("127.0.0.1", server.port, "POST", "/msg", {"id": "0011223344556677"},
                                  timeout=1, deadline=deadline, limit=limit)
        finally:
            self.elapsed = time.monotonic() - started

    def test_a_valid_prefix_then_a_stall_fails_closed_at_the_deadline(self):
        def script(conn):
            body = b'{"ok": true, "id": "0011223344556677"}'
            conn.sendall(b"HTTP/1.1 202 Accepted\r\nContent-Type: application/json\r\nContent-Length: 10000\r\n\r\n" + body)
            time.sleep(3)
        with self.assertRaises(core.TransportError):
            self.call(script)
        self.assertLess(self.elapsed, 2)

    def test_trickling_peer_cannot_extend_the_exchange(self):
        def script(conn):
            conn.sendall(b"HTTP/1.1 202 Accepted\r\nContent-Length: 1000\r\n\r\n")
            for _ in range(1000):
                conn.sendall(b" ")
                time.sleep(0.04)
        with self.assertRaises(core.TransportError):
            self.call(script, deadline=0.4)
        self.assertLess(self.elapsed, 2)

    def test_oversized_or_short_bodies_are_never_a_confirmation(self):
        valid = b'{"ok": true, "id": "0011223344556677"}'
        oversized = valid + b" " * 100 + b"garbage"
        for header, body in ((b"Content-Length: %d" % len(oversized), oversized),     # beyond the limit
                             (b"Connection: close", oversized),                          # no length, too long
                             (b"Content-Length: 500", valid)):                           # shorter than declared
            with self.subTest(header=header):
                def script(conn, header=header, body=body):
                    conn.sendall(b"HTTP/1.1 202 Accepted\r\n" + header + b"\r\n\r\n" + body)
                    conn.shutdown(socket.SHUT_WR)
                with self.assertRaises(core.TransportError):
                    self.call(script, deadline=2, limit=len(valid) + 50)

        def complete(conn):
            conn.sendall(b"HTTP/1.1 202 Accepted\r\nContent-Length: %d\r\n\r\n" % len(valid) + valid)
        self.assertEqual((202, {"ok": True, "id": "0011223344556677"}), self.call(complete, deadline=2))


class AttachmentCleanupTests(Base):
    def grant(self, name, expires=None, meta=True, raw=None):
        d = self.b.outbox / name
        (d / "files").mkdir(parents=True)
        if raw is not None:
            (d / "meta.json").write_text(raw)
        elif meta:
            (d / "meta.json").write_text(json.dumps({"expires": expires}))
        return d

    def test_failures_are_counted_and_the_pass_is_never_reported_as_success(self):
        past = core.iso(core.now_utc() - dt.timedelta(hours=3))
        future = core.iso(core.now_utc() + dt.timedelta(hours=3))
        expired, stuck = self.grant("e" * 40, past), self.grant("s" * 40, past)
        active, partial = self.grant("a" * 40, future), self.grant("p" * 40, meta=False)
        broken = self.grant("m" * 40, raw="{not json")
        (self.b.outbox / "stray-file").write_text("x")
        real = core.shutil.rmtree

        def rmtree(path, *a, **kw):
            if Path(path) == stuck:
                raise PermissionError(errno.EACCES, "denied")
            return real(path, *a, **kw)
        with mock.patch.object(core.shutil, "rmtree", side_effect=rmtree):
            outcome, counts, error = core.run_phase(core.sweep_outbox, self.b)
        self.assertEqual(("failed", "attachment_cleanup_failed"), (outcome, error))
        self.assertEqual({"scanned": 6, "active": 1, "deleted": 1, "incomplete": 1, "failed": 2, "ignored": 1}, counts)
        self.assertFalse(expired.exists())
        self.assertTrue(stuck.exists() and active.exists() and partial.exists() and broken.exists())

    def test_a_missing_outbox_fails_the_cycle_without_ending_the_worker(self):
        os.rmdir(str(self.b.outbox))
        self.assertEqual(("failed", {}, "FileNotFoundError"), core.run_phase(core.sweep_outbox, self.b))


# ---------------------------------------------------------------- ledger lifecycle evidence
class LifecycleTests(Base):
    def test_times_are_written_with_their_responses_and_survive_restart(self):
        self.request("3333444455556666")
        self.assertIsNotNone(self.row("3333444455556666")["received_at"])
        first = core.respond_request(self.b, "3333444455556666", "running", "Started")
        second = core.respond_request(self.b, "3333444455556666", "running", "Still working")
        with self.assertRaises(ValueError):
            core.respond_request(self.b, "3333444455556666", "completed", "x" * (256 * 1024))
        final = core.respond_request(self.b, "3333444455556666", "completed", "Done")
        restarted = core.Ctx(self.b.home)
        row = self.row("3333444455556666")
        self.assertEqual((first["ts"], second["ts"], final["ts"], final["id"]),
                         (row["started_at"], row["progress_at"], row["final_at"], row["final_response_id"]))
        view = core.request_snapshot(restarted.home, "3333444455556666")
        self.assertTrue(view["start_confirmed"])
        with self.ledger() as db:
            self.assertEqual(4, db.execute("SELECT COUNT(*) FROM response_outbox").fetchone()[0])

    def test_effective_timeout_keeps_start_evidence_and_late_final_is_still_accepted(self):
        old = core.iso(core.now_utc() - dt.timedelta(seconds=30))
        self.request("4444555566667777", ts=old, timeout=60)
        started = core.respond_request(self.b, "4444555566667777", "running", "Started")
        with self.ledger() as db:
            db.execute("UPDATE requests SET deadline=? WHERE id=?", (old, "4444555566667777"))
            db.commit()
        view = core.request_snapshot(self.b.home, "4444555566667777")
        self.assertEqual(("timed_out", "running", False, True, started["ts"]),
                         (view["state"], view["last_state"], view["timeout_persisted"], view["start_confirmed"],
                          view["started_at"]))
        core.expire_requests(self.b)
        with self.assertRaises(ValueError):
            core.respond_request(self.b, "4444555566667777", "running", "Again")
        core.respond_request(self.b, "4444555566667777", "completed", "Finished late")
        row = self.row("4444555566667777")
        self.assertEqual((started["ts"], "completed"), (row["started_at"], row["state"]))
        self.assertTrue(row["timed_out_at"])

    def legacy_ledger(self):
        """A ledger as written before the lifecycle columns, with a wall clock that stepped backwards."""
        path = self.b.home / "requests"
        path.mkdir(mode=0o700)
        db = sqlite3.connect(str(path / "ledger.sqlite3"))
        db.executescript("""
            CREATE TABLE requests (direction TEXT NOT NULL, id TEXT NOT NULL, peer TEXT NOT NULL, port INTEGER NOT NULL,
                envelope TEXT NOT NULL, record TEXT, deadline TEXT NOT NULL, state TEXT NOT NULL,
                seq INTEGER NOT NULL DEFAULT 0, latest TEXT, submission TEXT, timed_out_at TEXT, PRIMARY KEY (direction,id));
            CREATE TABLE response_outbox (id TEXT PRIMARY KEY, request_id TEXT NOT NULL, peer TEXT NOT NULL,
                port INTEGER NOT NULL, envelope TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, delivered_at TEXT, last_error TEXT);
            CREATE TABLE response_events (id TEXT PRIMARY KEY, request_id TEXT NOT NULL, envelope TEXT NOT NULL,
                received_at TEXT NOT NULL);""")
        responses = [("r1", "received", 1, "2026-10-20T11:30:00Z"), ("r2", "running", 2, "2026-10-20T12:00:00Z"),
                     ("r3", "running", 3, "2026-10-20T11:00:00Z"), ("r4", "completed", 4, "2026-10-20T11:10:00Z")]
        for rid, state, seq, ts in responses:
            env = {"id": rid + "0000000000000"[:13], "ts": ts, "response": {"state": state, "seq": seq}}
            db.execute("INSERT INTO response_outbox(id,request_id,peer,port,envelope) VALUES (?,?,?,?,?)",
                       (env["id"], "6666777788889999", A, 8123, json.dumps(env)))
        record = {"received": {"at": "2026-10-20T11:29:59Z", "peer": {"node": A}}}
        db.execute("INSERT INTO requests VALUES ('incoming','6666777788889999',?,8123,'{}',?,?,'completed',4,?,NULL,NULL)",
                   (A, json.dumps(record), "2026-10-21T00:00:00Z", json.dumps(env)))
        db.commit()
        db.close()

    def test_migration_backfills_in_durable_order_even_after_a_clock_step_back(self):
        self.legacy_ledger()
        with contextlib.ExitStack() as stack:          # two writers opening the old ledger together
            barrier = threading.Barrier(2)
            errors = []

            def open_ledger():
                try:
                    barrier.wait(5)
                    with core.request_db(self.b):
                        pass
                except Exception as exc:       # pragma: no cover - reported below
                    errors.append(exc)
            threads = [threading.Thread(target=open_ledger) for _ in range(2)]
            [t.start() for t in threads]
            [t.join(10) for t in threads]
            self.assertEqual([], errors)
        row = self.row("6666777788889999")
        self.assertEqual(("2026-10-20T11:29:59Z", "2026-10-20T12:00:00Z", "2026-10-20T11:00:00Z"),
                         (row["received_at"], row["started_at"], row["progress_at"]))
        self.assertEqual(("2026-10-20T11:10:00Z", "r40000000000000"), (row["final_at"], row["final_response_id"]))


class MetadataTests(Base):
    def test_projection_is_text_free_and_joins_the_exact_final_response(self):
        self.request("7777888899990000", text=SECRET)
        self.request("8888999900001111")
        core.respond_request(self.b, "7777888899990000", "completed", SECRET)
        final = core.request_snapshot(self.b.home, "7777888899990000")["response"]
        with self.ledger() as db:              # the earlier receipt was delivered, the final was not
            db.execute("UPDATE response_outbox SET delivered_at='2026-10-21T00:00:00Z' WHERE id!=?", (final["id"],))
            # an inconsistent row: another request claims this final response as its own
            db.execute("UPDATE requests SET state='completed', final_response_id=? WHERE id='8888999900001111'",
                       (final["id"],))
            db.commit()
        meta, reason = core.request_metadata(self.b.home, ["7777888899990000", "8888999900001111", "../etc"])
        self.assertIsNone(reason)
        self.assertEqual(("pending", final["id"]), (meta["7777888899990000"]["final_response_delivery"],
                                                     meta["7777888899990000"]["final_response_id"]))
        self.assertEqual("unknown", meta["8888999900001111"]["final_response_delivery"])
        self.assertNotIn(SECRET, json.dumps(meta))
        self.assertNotIn(A, json.dumps(meta))
        with self.ledger() as db:
            db.execute("UPDATE response_outbox SET delivered_at='2026-10-21T00:00:01Z' WHERE id=?", (final["id"],))
            db.commit()
        meta, _ = core.request_metadata(self.b.home, ["7777888899990000"])
        self.assertEqual("confirmed", meta["7777888899990000"]["final_response_delivery"])
        backlog = core.request_backlog(self.b.home)
        self.assertEqual((0, 1), (backlog["incoming"]["final_response_pending"],
                                  backlog["incoming"]["final_response_unknown"]))

    def test_older_writers_and_invalid_times_never_produce_false_evidence(self):
        self.request("9999000011112222")
        self.request("0000111122223333")
        core.respond_request(self.b, "9999000011112222", "completed", "Done")
        with self.ledger() as db:              # an older runtime wrote these rows without lifecycle columns
            db.execute("UPDATE requests SET final_response_id=NULL, final_at=NULL, started_at='garbage', "
                       "progress_at='13:00' WHERE id IN ('9999000011112222','0000111122223333')")
            db.commit()
        meta, _ = core.request_metadata(self.b.home, ["9999000011112222", "0000111122223333"])
        done, waiting = meta["9999000011112222"], meta["0000111122223333"]
        self.assertTrue(done["final_response_id"])                     # exact id from the final envelope
        self.assertEqual("pending", done["final_response_delivery"])
        self.assertEqual((None, None, False), (waiting["started_at"], waiting["progress_at"], waiting["start_confirmed"]))

    def test_reads_see_committed_wal_writes_and_change_no_rows(self):
        self.request("1212121212121212")
        writer = sqlite3.connect(str(self.b.home / "requests" / "ledger.sqlite3"))
        self.addCleanup(writer.close)
        writer.execute("UPDATE requests SET state='running', started_at=? WHERE id='1212121212121212'",
                       (core.iso(core.now_utc()),))
        writer.commit()                                     # committed, not checkpointed
        before = self.dump()
        meta, _ = core.request_metadata(self.b.home, ["1212121212121212"])
        backlog = core.request_backlog(self.b.home)
        self.assertTrue(meta["1212121212121212"]["start_confirmed"])
        self.assertEqual(1, backlog["incoming"]["running"])
        self.assertEqual(before, self.dump())

    def dump(self):
        with self.ledger() as db:
            return [tuple(r) for table in ("requests", "response_outbox", "response_events")
                    for r in db.execute("SELECT * FROM %s ORDER BY rowid" % table)]

    def test_unreadable_ledgers_are_unknown_never_an_empty_backlog(self):
        self.assertEqual({"available": False, "reason": "ledger_absent"}, core.request_backlog(self.b.home))
        self.assertFalse((self.b.home / "requests").exists())          # reading created nothing
        (self.b.home / "requests").mkdir()
        (self.b.home / "requests" / "ledger.sqlite3").write_bytes(b"not a database" * 100)
        backlog = core.request_backlog(self.b.home)
        self.assertFalse(backlog["available"])
        self.assertIn(backlog["reason"], ("ledger_corrupt", "ledger_unreadable"))
        self.assertEqual(({}, backlog["reason"]), core.request_metadata(self.b.home, ["1212121212121212"]))
        if os.geteuid() != 0:
            (self.b.home / "requests" / "ledger.sqlite3").unlink()
            self.request("1313131313131313")
            os.chmod(str(self.b.home / "requests"), 0)
            self.addCleanup(os.chmod, str(self.b.home / "requests"), 0o700)
            self.assertFalse(core.request_backlog(self.b.home)["available"])


class ManualMaintainTests(Base):
    def run_maintain(self):
        output = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(output):
            try:
                core.cmd_request(self.b, argparse.Namespace(request_cmd="maintain"))
            except SystemExit as exc:
                code = exc.code
        return code, json.loads(output.getvalue())

    def test_every_phase_is_attempted_and_failures_are_reported(self):
        self.b.resolve = lambda peer: (peer, "100.64.0.1")
        self.b.whois = lambda ip: {"node": A}
        self.request("1414141414141414")
        with mock.patch.object(core, "recover_request_inbox", side_effect=OSError("full")), \
                mock.patch.object(core, "http_json", return_value=(202, {"ok": False})) as post:
            code, out = self.run_maintain()
        self.assertEqual(1, code)
        self.assertEqual({"recover": "failed:OSError", "expire": "ok", "flush": "ok"}, out["phases"])
        self.assertEqual((False, False), (out["ok"], out["complete"]))
        self.assertEqual(1, post.call_count)
        holder = core.FileLock(self.b.home / "requests" / "flush.lock")
        self.assertTrue(holder.acquire())
        self.addCleanup(holder.release)
        code, out = self.run_maintain()
        self.assertEqual((0, True, False, "busy"), (code, out["ok"], out["complete"], out["phases"]["flush"]))


if __name__ == "__main__":
    unittest.main()
