"""Slow hosted endpoints cannot hold maintenance, other bots, or stale registrations hostage."""
import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from support.public_harness import C, FriendsGateway, wait_until
from test_friends_hosted_wake import LIMITS, Receiver, SECRET
from test_gateway_concurrency import alive, children

import bot_gateway as G


def hung_dns_delivery(home, limits, route, body, result):
    """Run the real transport with an OS resolver that never returns during this test."""
    def resolve(*args, **kwargs):
        time.sleep(60)
    with mock.patch.object(G.C.socket, "getaddrinfo", side_effect=resolve):
        G.hosted_wake_delivery(home, dict(limits, hosted_wake_private_hosts=0), route, body, result)


class HostedWakeWorkerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="yobiru hosted workers ")
        self.addCleanup(tmp.cleanup)
        self.gw = FriendsGateway(Path(tmp.name), limits=LIMITS).start()
        self.addCleanup(self.gw.stop)
        self.slow, self.fast = Receiver(), Receiver()
        self.addCleanup(self.slow.stop)
        self.addCleanup(self.fast.stop)
        self.a, self.ma = self.message("slowbot")
        self.b, self.mb = self.message("fastbot")
        self.gw.stop()
        self.store = G.Store(self.gw.home, G.load_config(self.gw.home))
        self.register(self.a, self.slow.url)
        self.register(self.b, self.fast.url)
        self.release = threading.Event()
        self.entered = threading.Event()
        self.finished = threading.Event()
        self.thread = None
        original = self.slow.server.RequestHandlerClass.do_POST
        entered, release = self.entered, self.release

        def blocked(handler):
            entered.set()
            release.wait(5)
            original(handler)
        self.slow.server.RequestHandlerClass.do_POST = blocked
        self.addCleanup(self.cleanup_delivery)

    def cleanup_delivery(self):
        self.release.set()
        if self.thread:
            self.thread.join(6)
        pool = getattr(self.store, "hosted_wake_workers", None)
        if pool:
            pool.close()

    def message(self, handle):
        bot = self.gw.bot(handle)
        self.gw.policy(bot, offered=["faq", "chat"], visitor=["faq", "chat"])
        _, token = self.gw.actor()
        cid = self.gw.conversation(bot, token)
        status, sent = self.gw.send(cid, token, kind="chat", text="synthetic worker test")
        self.assertEqual(202, status, sent)
        return bot, sent["message_id"]

    def register(self, bot, url):
        created = 1 if bot == self.a else 2
        self.gw.sql("INSERT OR REPLACE INTO hosted_wakes(bot_id,url_sealed,secret_sealed,url_host,header,scheme,"
                    "on_review,created_ts,updated_ts) VALUES(?,?,?,?,?,?,1,?,1)",
                    (bot, G.seal_route(self.gw.home, bot, url), G.seal_route(self.gw.home, bot, SECRET),
                     "127.0.0.1", "Authorization", "Bearer", created))

    def sweep_in_background(self, maintenance=False):
        def run():
            try:
                G.maintenance(self.store) if maintenance else G.hosted_wake_sweep(self.store, C.now_ts())
            finally:
                self.finished.set()
        self.thread = threading.Thread(target=run)
        self.thread.start()
        self.assertTrue(self.entered.wait(3), "slow endpoint was not called")

    def pump(self):
        G.hosted_wake_sweep(self.store, C.now_ts())

    def test_slow_webhook_does_not_delay_another_bot_or_maintenance(self):
        self.gw.sql("INSERT INTO counters(key,window,count,expires_ts) VALUES('expired',1,1,1)")
        self.sweep_in_background(maintenance=True)
        self.assertTrue(self.finished.wait(0.5), "network delivery held up maintenance")
        self.assertEqual([], self.gw.sql("SELECT * FROM counters WHERE key='expired'"))
        self.assertEqual(1, len(self.fast.wait(1, seconds=1)), "unrelated bot waited for the slow endpoint")
        self.assertFalse(self.release.is_set())

    def test_deleted_route_is_not_used_for_work_waiting_for_a_slot(self):
        with mock.patch.object(G, "HOSTED_WAKE_WORKERS", 1, create=True):
            self.sweep_in_background()
            self.gw.sql("DELETE FROM hosted_wakes WHERE bot_id=?", (self.b,))
            self.release.set()
            self.thread.join(4)
            for _ in range(10):
                self.pump()
                time.sleep(0.05)
        self.assertEqual([], self.fast.requests, "deleted registration received a new wake")
        row = self.gw.sql("SELECT wake_state FROM messages WHERE id=?", (self.mb,))[0]
        self.assertEqual("none", row["wake_state"])

    def test_replaced_route_is_read_when_a_worker_becomes_available(self):
        with mock.patch.object(G, "HOSTED_WAKE_WORKERS", 1, create=True):
            self.sweep_in_background()
            self.register(self.b, self.slow.url)
            self.release.set()
            self.thread.join(4)
            wait_until(lambda: (self.pump(), len(self.slow.requests) == 2)[1], timeout=5)
        self.assertEqual([], self.fast.requests, "superseded registration received a new wake")
        self.assertEqual(2, len(self.slow.requests))

    def test_hung_dns_is_bounded_and_releases_a_slot_without_replay(self):
        self.store.limits["hosted_wake_timeout_seconds"] = 1
        with mock.patch.object(G, "HOSTED_WAKE_WORKERS", 1):
            with mock.patch.object(G, "hosted_wake_delivery", hung_dns_delivery):
                self.pump()
            pool = self.store.hosted_wake_workers
            self.assertEqual([self.a], list(pool.active))
            process = pool.active[self.a][0]
            for _ in range(3):
                self.pump()
                self.assertEqual(1, len(pool.active))
            # The child has its own deadline even if its maintenance parent stops collecting.
            process.join(3)
            self.assertFalse(process.is_alive(), "resolver outlived the delivery deadline")
            self.assertTrue(wait_until(lambda: (self.pump(), len(self.fast.requests) == 1)[1], timeout=4))
            for _ in range(3):
                self.pump()
        row = self.gw.sql("SELECT wake_state, wake_seq FROM messages WHERE id=?", (self.ma,))[0]
        self.assertEqual(("uncertain", 1), (row["wake_state"], row["wake_seq"]))
        self.assertEqual([], self.slow.requests)

    def test_registration_is_rechecked_inside_the_intent_transaction(self):
        original = self.store.tx
        changed = []

        def transaction(write=True):
            if write and not changed:
                self.gw.sql("DELETE FROM hosted_wakes WHERE bot_id=?", (self.a,))
                changed.append(True)
            return original(write)
        with mock.patch.object(self.store, "tx", side_effect=transaction):
            self.assertIsNone(G.hosted_wake_intent(self.store, G.Api(self.store), self.a, C.now_ts()))
        self.assertTrue(changed)
        self.assertEqual("none", self.gw.sql("SELECT wake_state FROM messages WHERE id=?", (self.ma,))[0]["wake_state"])

    def test_old_delivery_cannot_overwrite_a_replacement_registration(self):
        self.sweep_in_background()
        self.register(self.a, self.fast.url)
        self.release.set()
        self.thread.join(3)

        def collected():
            self.pump()
            return self.gw.sql("SELECT wake_state FROM messages WHERE id=?", (self.ma,))[0]["wake_state"] == "accepted"
        self.assertTrue(wait_until(collected, timeout=4))
        current = self.gw.sql("SELECT last_outcome FROM hosted_wakes WHERE bot_id=?", (self.a,))[0]
        self.assertIsNone(current["last_outcome"])


@unittest.skipUnless(sys.platform.startswith("linux"), "reads child process trees from /proc")
class HostedWakeShutdownTests(unittest.TestCase):
    def test_sigterm_to_gateway_pid_reaps_inflight_workers(self):
        for processes in (1, 2):
            with self.subTest(processes=processes), tempfile.TemporaryDirectory(prefix="yobiru wake shutdown ") as tmp:
                gw = FriendsGateway(Path(tmp), limits=dict(LIMITS, hosted_wake_timeout_seconds=30))
                cfg_file = gw.home / "gateway.json"
                cfg = json.loads(cfg_file.read_text())
                cfg["processes"] = processes
                cfg_file.write_text(json.dumps(cfg))
                receiver = Receiver()
                entered, release = threading.Event(), threading.Event()

                def blocked(handler):
                    entered.set()
                    release.wait(5)
                receiver.server.RequestHandlerClass.do_POST = blocked
                try:
                    gw.start()
                    bot = gw.bot("shutdownbot")
                    gw.policy(bot, offered=["faq", "chat"], visitor=["faq", "chat"])
                    _, token = gw.actor()
                    cid = gw.conversation(bot, token)
                    self.assertEqual(202, gw.send(cid, token, kind="chat", text="synthetic shutdown")[0])
                    gw.ok("PUT", "/v1/owner/bots/%s/wake" % bot,
                          {"url": receiver.url, "secret": SECRET}, gw.owner_token)
                    self.assertTrue(entered.wait(5), "delivery did not start")
                    descendants = []
                    frontier = children(gw.service.proc.pid)
                    while frontier:
                        descendants.extend(frontier)
                        frontier = [child for parent in frontier for child in children(parent)]
                    self.assertTrue(descendants)
                    os.kill(gw.service.proc.pid, signal.SIGTERM)
                    self.assertEqual(0, gw.service.proc.wait(timeout=5))
                    self.assertTrue(wait_until(lambda: not any(alive(pid) for pid in descendants), timeout=3),
                                    "gateway left an active wake process behind")
                finally:
                    release.set()
                    gw.stop()
                    receiver.stop()


if __name__ == "__main__":
    unittest.main()
