"""Reads without the write lock, and the gateway in several processes.

A read transaction refuses writes, so a request that turns out to need one runs again under the write lock instead
of writing outside it. The connector poll, which every online agent repeats every few seconds, never needs one. With
`processes` above 1, request processes share one listening socket, one more process runs the maintenance sweeps, and
the first process replaces a child that exits and stops them all on SIGTERM.
"""
import asyncio
import importlib.util
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from support.public_harness import FriendsGateway, wait_until

import bot_common as C
import bot_gateway as G

ROOT = Path(__file__).resolve().parent.parent
LINUX = sys.platform.startswith("linux")


def counter(db, key="synthetic", expires=1):
    db.execute("INSERT INTO counters(key, window, count, expires_ts) VALUES(?, 1, 1, ?)", (key, expires))


class ReadTransactionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="yobiru read tx ")
        self.addCleanup(tmp.cleanup)
        self.gw = FriendsGateway(Path(tmp.name))
        self.store = G.Store(self.gw.home, G.load_config(self.gw.home))

    def count(self, key="synthetic"):
        return self.gw.sql("SELECT COUNT(*) AS n FROM counters WHERE key=?", (key,))[0]["n"]

    def test_a_read_transaction_refuses_writes(self):
        with self.assertRaises(sqlite3.OperationalError) as caught:
            with self.store.tx(write=False) as db:
                counter(db)
        self.assertTrue(G.needs_write(caught.exception))
        self.assertEqual(0, self.count())
        with self.store.tx() as db:                    # the pooled connection writes again in a write transaction
            counter(db)
        self.assertEqual(1, self.count())

    def test_a_read_that_needs_a_write_runs_again_under_the_write_lock(self):
        modes = []

        def handler(db):
            modes.append(db.execute("PRAGMA query_only").fetchone()[0])
            counter(db)
            return "done"
        self.assertEqual("done", self.store.attempt(handler))
        self.assertEqual([1, 0], modes)
        self.assertEqual(1, self.count())

    def test_a_plain_read_or_a_refusal_runs_once(self):
        calls = []

        def read(db):
            calls.append("read")
            return db.execute("SELECT COUNT(*) FROM counters").fetchone()[0]

        def refuse(db):
            calls.append("refuse")
            raise G.Fail(404, "not_found")
        self.assertEqual(0, self.store.attempt(read))
        with self.assertRaises(G.Fail):
            self.store.attempt(refuse)
        with self.assertRaises(G.Fail):
            self.store.attempt(refuse, write=True)
        self.assertEqual(["read", "refuse", "refuse"], calls)

    def test_connections_are_reused_and_a_failed_transaction_leaves_nothing(self):
        with self.store.tx() as first:
            pass
        with self.assertRaises(G.Fail):
            with self.store.tx() as db:
                self.assertIs(first, db)
                counter(db)
                raise G.Fail(409, "conflict")
        with self.store.tx(write=False) as db:
            self.assertIs(first, db)                    # rolled back and pooled, so it could begin again
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM counters").fetchone()[0])
        self.assertLessEqual(len(self.store._idle), G.IDLE_CONNECTIONS)

    def test_maintenance_truncates_a_long_write_ahead_log(self):
        wal = Path(str(self.store.path) + "-wal")
        with self.store.tx() as db:
            counter(db)
        self.assertGreater(wal.stat().st_size, 0)
        with mock.patch.object(G, "WAL_RESET_BYTES", 0):
            G.reset_wal(self.store)
        self.assertEqual(0, wal.stat().st_size)
        self.assertEqual(1, self.count())

    def external(self):
        """A store with `external_checkpoints`: Litestream checkpoints, and keeps a read transaction open for it."""
        config = self.gw.home / "gateway.json"
        cfg = json.loads(config.read_text())
        cfg["external_checkpoints"] = True
        config.write_text(json.dumps(cfg))
        return G.Store(self.gw.home, G.load_config(self.gw.home))

    def test_the_setting_takes_true_or_false(self):
        config = self.gw.home / "gateway.json"
        cfg = json.loads(config.read_text())
        self.assertFalse(G.load_config(self.gw.home)["external_checkpoints"])
        cfg["external_checkpoints"] = "yes"
        config.write_text(json.dumps(cfg))
        with self.assertRaises(ValueError):
            G.load_config(self.gw.home)

    def test_with_external_checkpoints_the_log_is_left_to_litestream(self):
        store = self.external()
        wal = Path(str(store.path) + "-wal")
        with store.tx() as db:
            self.assertEqual(0, db.execute("PRAGMA wal_autocheckpoint").fetchone()[0])
            counter(db)
        size = wal.stat().st_size
        self.assertGreater(size, 0)
        with mock.patch.object(G, "WAL_RESET_BYTES", 0):
            G.reset_wal(store)
        self.assertEqual(size, wal.stat().st_size)

    def test_past_the_backstop_the_log_is_truncated_without_waiting_for_its_reader(self):
        store = self.external()
        wal = Path(str(store.path) + "-wal")
        with store.tx() as db:
            counter(db)
        reader = sqlite3.connect(str(store.path), isolation_level=None)
        self.addCleanup(reader.close)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM counters").fetchone()
        with store.tx() as db:
            counter(db, "second")
        with mock.patch.object(G, "WAL_EXTERNAL_LIMIT_BYTES", 0), mock.patch.object(G, "log") as logged:
            started = time.monotonic()
            G.reset_wal(store)
            self.assertLess(time.monotonic() - started, 2)          # not the 15 s busy timeout
            self.assertGreater(wal.stat().st_size, 0)                # the reader still needs the log
            self.assertIn("is Litestream running?", logged.call_args[0][0])
            with store.tx() as db:
                self.assertEqual(15000, db.execute("PRAGMA busy_timeout").fetchone()[0])
                counter(db, "third")
            reader.execute("COMMIT")
            G.reset_wal(store)
            self.assertEqual(0, wal.stat().st_size)
            self.assertEqual(1, logged.call_count)                  # once an hour
        self.assertEqual(1, self.count("third"))


class ConnectorPollTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="yobiru connector poll ")
        self.addCleanup(tmp.cleanup)
        self.gw = FriendsGateway(Path(tmp.name)).start()
        self.addCleanup(self.gw.stop)

    def test_the_connector_poll_only_reads_and_leaves_out_work_past_its_deadline(self):
        gw = self.gw
        alpha, beta = gw.bot("alpha"), gw.bot("beta")
        sa, sb = gw.source_for(alpha), gw.source_for(beta)
        gw.follow(sa, beta)
        gw.follow(sb, alpha)
        _, wid = gw.native(beta)
        ctok = gw.ok("POST", "/v1/owner/bots/%s/workers/%s/connector" % (beta, wid), {}, gw.owner_token)["token"]
        # A direct message has no deadline of its own; this one is given one, as a sender may.
        status, sent = gw.send(gw.conversation(beta, sa), sa, kind="chat", text="hello", deadline_seconds=600)
        self.assertEqual(202, status, sent)
        mid = sent["message_id"]
        self.assertEqual([mid], [j["job_id"] for j in gw.ok("GET", "/v1/connector/jobs", None, ctok)["jobs"]])

        # Queued work of other bots, as on a busy gateway: the poll must look at this bot's work only.
        db = gw.db()
        try:
            db.execute("BEGIN")
            db.executemany("INSERT INTO messages(id, conversation_id, bot_id, actor_id, author, op_key, request_hash, "
                           "kind, content, state, route, created_ts, deadline_ts) VALUES(?, ?, 'other', 'actor', "
                           "'caller', ?, 'hash', 'chat', '{}', 'queued', 'agent', 1, ?)",
                           (("other-%d" % i, "other-%d" % i, "other-%d" % i, int(time.time()) + 3600)
                            for i in range(3000)))
            db.execute("COMMIT")
        finally:
            db.close()
        store = G.Store(gw.home, G.load_config(gw.home))
        api = G.Api(store)
        deadline = gw.sql("SELECT deadline_ts FROM messages WHERE id=?", (mid,))[0]["deadline_ts"]
        for now, expected in ((C.now_ts(), [mid]), (deadline + 1, [])):
            with self.subTest(now=now), store.tx(write=False) as db:     # a write here would raise
                r = G.Request()
                r.params, r.body, r.now = {}, {}, now
                r.subject = api.authenticate(db, ("connector",), ctok, now)
                ticks = []
                db.set_progress_handler(lambda: ticks.append(None), 100)
                try:
                    status, obj = api.connector_jobs(db, r)
                finally:
                    db.set_progress_handler(None, 0)
                self.assertEqual((200, expected), (status, [j["job_id"] for j in obj["jobs"]]))
                # VM work, not time: this bot's own work takes a few hundred instructions, a walk through the
                # other bot's 3,000 queued messages about 15,000
                self.assertLess(len(ticks), 50, "the connector poll examined other bots' queued work")
        self.assertEqual("queued", gw.sql("SELECT state FROM messages WHERE id=?", (mid,))[0]["state"])


def children(pid):
    out = []
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                stat = Path("/proc/%s/stat" % entry).read_text()
            except OSError:
                continue
            if int(stat.rsplit(")", 1)[1].split()[1]) == pid:
                out.append(int(entry))
    return sorted(out)


def alive(pid):
    try:
        state = Path("/proc/%d/stat" % pid).read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state != "Z"


@unittest.skipUnless(LINUX, "reads process trees from /proc")
class MultiProcessTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="yobiru processes ")
        self.addCleanup(tmp.cleanup)
        self.gw = FriendsGateway(Path(tmp.name))
        cfg = json.loads((self.gw.home / "gateway.json").read_text())
        cfg["processes"] = 3
        (self.gw.home / "gateway.json").write_text(json.dumps(cfg))
        self.gw.start()
        self.addCleanup(self.gw.kill)
        self.parent = self.gw.service.proc.pid
        self.assertTrue(wait_until(lambda: len(children(self.parent)) == 4))

    def test_request_processes_share_the_port_and_one_more_runs_maintenance(self):
        results, lock = [], threading.Lock()

        def poll():
            for _ in range(10):
                status, _ = self.gw.call("GET", "/v1/health")
                with lock:
                    results.append(status)
        threads = [threading.Thread(target=poll) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual([200] * 120, results)
        db = self.gw.db()
        try:
            counter(db, "stale", expires=int(time.time()) - 5)
        finally:
            db.close()
        self.assertTrue(wait_until(lambda: not self.gw.sql("SELECT 1 FROM counters WHERE key='stale'")))

    def test_a_child_that_exits_is_replaced(self):
        before = children(self.parent)
        os.kill(before[-1], signal.SIGKILL)
        replaced = lambda: len(children(self.parent)) == 4 and before[-1] not in children(self.parent)  # noqa: E731
        self.assertTrue(wait_until(replaced))
        self.assertEqual(200, self.gw.call("GET", "/v1/health")[0])

    def test_sigterm_to_the_first_process_stops_them_all(self):
        kids = children(self.parent)
        os.kill(self.parent, signal.SIGTERM)
        self.assertEqual(0, self.gw.service.proc.wait(timeout=15))
        self.assertTrue(wait_until(lambda: not any(alive(pid) for pid in kids)))


class ProcessesSettingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="yobiru processes setting ")
        self.addCleanup(tmp.cleanup)
        self.gw = FriendsGateway(Path(tmp.name))
        self.path = self.gw.home / "gateway.json"
        self.cfg = json.loads(self.path.read_text())

    def test_processes_is_checked(self):
        self.assertEqual(1, G.load_config(self.gw.home)["processes"])
        for value in (0, 65, "2", True, 1.5):
            with self.subTest(value=value):
                self.path.write_text(json.dumps(dict(self.cfg, processes=value)))
                with self.assertRaises(ValueError):
                    G.load_config(self.gw.home)
        self.path.write_text(json.dumps(dict(self.cfg, processes=8)))
        self.assertEqual(8, G.load_config(self.gw.home)["processes"])
        r = subprocess.run(self.gw.prefix + ["--gateway-home", str(self.gw.home), "serve", "--processes", "0"],
                           capture_output=True, text=True, env=self.gw.env, timeout=60)
        self.assertEqual(2, r.returncode, r.stderr)


class BenchmarkToolTests(unittest.TestCase):
    def held_result(self, response, *, send_status=202, early=False, poll_seconds=0):
        spec = importlib.util.spec_from_file_location("bench_gateway_under_test", ROOT / "tools" / "bench_gateway.py")
        bench = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bench)
        clock = [100.0]
        real_sleep = asyncio.sleep

        async def run():
            delivered = asyncio.Event()

            async def http(port, method, path, token=None, body=None, timeout=60):
                if path == "/v1/connector/jobs":
                    return 200, {"ok": True, "cursor": "a" * 32, "jobs": [], "checks": [], "replies": []}
                if path.startswith("/v1/connector/jobs?"):
                    if not early:
                        await delivered.wait()
                    clock[0] += poll_seconds
                    if isinstance(response, Exception):
                        raise response
                    return response
                if path == "/v1/actors":
                    return 201, {"token": "synthetic-visitor"}
                if path == "/v1/bots/bot_probe/conversations":
                    return 201, {"conversation_id": "conv_probe"}
                self.assertEqual("/v1/conversations/conv_probe/messages", path)
                delivered.set()
                await real_sleep(0)  # The held response can arrive before the sender receives its receipt.
                clock[0] += 0.1
                return send_status, {"ok": send_status == 202, "message_id": "msg_probe"}

            async def sleep(delay):
                clock[0] += delay
                await real_sleep(0)

            with mock.patch.object(bench, "http", side_effect=http), \
                    mock.patch.object(bench, "gateway_usage", return_value=(0, 0)), \
                    mock.patch.object(bench.time, "monotonic", side_effect=lambda: clock[0]), \
                    mock.patch.object(bench.asyncio, "sleep", side_effect=sleep):
                return await bench.hold(0, 0, [{"bot_id": "bot_probe", "connector": "synthetic-connector"}], 1,
                                        wait=6)
        return asyncio.run(run())

    def test_failed_held_polls_are_not_successful_wakes(self):
        for response, status in (((503, {"ok": False, "error": "busy"}), "503"),
                                 (OSError("synthetic connection failure"), "-1")):
            with self.subTest(status=status):
                result = self.held_result(response, early=True)
                self.assertEqual({status: 1}, result["status"])
                self.assertEqual(0, result["woken"])
                self.assertIsNone(result["wake_p50_ms"])
                self.assertIsNone(result["wake_max_ms"])
                self.assertEqual(0, self.held_result(response, poll_seconds=6)["held_full_wait"])

    def test_malformed_or_unchanged_listings_are_not_successful_wakes(self):
        listing = {"ok": True, "cursor": "b" * 32, "jobs": [{"job_id": "msg_probe"}],
                   "checks": [], "replies": []}
        for obj in ({}, [], dict(listing, cursor=None), dict(listing, cursor="bad"),
                    dict(listing, jobs=None), dict(listing, jobs=[None]), dict(listing, ok=False),
                    dict(listing, cursor="a" * 32)):
            with self.subTest(obj=obj):
                result = self.held_result((200, obj))
                self.assertEqual(0, result["woken"])
                self.assertIsNone(result["wake_max_ms"])
                unchanged = isinstance(obj, dict) and obj.get("cursor") == "a" * 32
                self.assertEqual(0 if unchanged else 1, result["invalid_listings"])
                self.assertEqual(1 if unchanged else 0,
                                 self.held_result((200, obj), poll_seconds=6)["held_full_wait"])

    def test_a_wake_requires_the_successfully_sent_probe_job(self):
        listing = {"ok": True, "cursor": "b" * 32, "jobs": [{"job_id": "msg_probe"}],
                   "checks": [], "replies": []}
        for response, status in (((200, dict(listing, jobs=[{"job_id": "msg_other"}])), 202),
                                 ((200, listing), 503)):
            with self.subTest(response=response, send_status=status):
                result = self.held_result(response, send_status=status)
                self.assertEqual(0, result["woken"])
                self.assertEqual({str(status): 1}, result["probe_status"])
        result = self.held_result((200, listing))
        self.assertEqual(1, result["woken"])
        self.assertGreaterEqual(result["wake_p50_ms"], 0)
        self.assertGreaterEqual(result["wake_max_ms"], 0)

    @unittest.skipUnless(hasattr(os, "fork"), "the multi-process gateway needs fork")
    def test_the_benchmark_runs_against_a_multi_process_gateway(self):
        with tempfile.TemporaryDirectory(prefix="yobiru bench ") as tmp:
            r = subprocess.run([sys.executable, str(ROOT / "tools" / "bench_gateway.py"), "--bots", "4",
                                "--messages", "300", "--chats", "2", "--processes", "2", "--clients", "1",
                                "--concurrency", "4", "--seconds", "1", "--keep", tmp, "--quiet"],
                               capture_output=True, text=True, timeout=300)
        self.assertEqual(0, r.returncode, r.stderr[-2000:])
        result = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertGreater(result["ok_per_second"], 0)
        self.assertEqual(["200"], list(result["status"]))
        self.assertEqual(300, result["messages"])

    @unittest.skipUnless(hasattr(os, "fork"), "the multi-process gateway needs fork")
    def test_the_benchmark_holds_waiting_connectors(self):
        with tempfile.TemporaryDirectory(prefix="yobiru bench hold ") as tmp:
            r = subprocess.run([sys.executable, str(ROOT / "tools" / "bench_gateway.py"), "--bots", "6",
                                "--messages", "60", "--chats", "2", "--processes", "2", "--hold", "6", "--probes",
                                "2", "--wait-seconds", "6", "--keep", tmp, "--quiet"],
                               capture_output=True, text=True, timeout=300)
        self.assertEqual(0, r.returncode, r.stderr[-2000:])
        result = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual((6, 2, 2, 4), (result["held"], result["probes"], result["woken"], result["held_full_wait"]))
        self.assertLess(result["wake_max_ms"], 3000)


if __name__ == "__main__":
    unittest.main()
