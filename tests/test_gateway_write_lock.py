"""A worker's poll of its jobs never waits for the database's write lock.

The poll reads message bodies, from the blob store when they are large. Run under the write lock, one slow read
there held up every other write on the gateway until it gave up with 503 storage_busy. Here another connection
holds the write lock for the whole poll, the way such a read would.
"""
import hashlib
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from support.public_harness import C, FriendsGateway

import bot_gateway as G


class WorkerPollWriteLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="yobiru write lock test ")
        cls.gw = FriendsGateway(Path(cls._tmp.name)).start()

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls._tmp.cleanup()

    def setUp(self):
        self.bot = self.gw.bot("w" + hashlib.sha256(self.id().encode()).hexdigest()[:12])
        self.gw.policy(self.bot, offered=["faq", "chat"], visitor=["faq", "chat"])
        actor, token = self.gw.actor()
        cid = self.gw.conversation(self.bot, token)
        self.assertEqual(200, self.gw.contact(self.bot, actor, "trust")[0])
        self.mid = self.gw.chat(cid, token, "synthetic job while the lock is held")
        self.wtoken, self.wid = self.gw.native(self.bot)

    def last_seen(self):
        return self.gw.sql("SELECT last_seen_ts FROM workers WHERE id=?", (self.wid,))[0]["last_seen_ts"]

    def test_job_poll_is_answered_while_another_writer_holds_the_lock(self):
        holder = sqlite3.connect(str(self.gw.home / "gateway.sqlite3"), timeout=0, isolation_level=None)
        try:
            holder.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            status, obj = self.gw.call("GET", "/v1/worker/jobs", None, self.wtoken)
            elapsed = time.monotonic() - started
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        self.assertEqual(200, status, obj)
        self.assertIn(self.mid, [j["job_id"] for j in obj["jobs"]])
        # Only recording the worker's last use waits for the lock, and only SEEN_WAIT_MS, never the 15 s busy timeout.
        self.assertLess(elapsed, G.SEEN_WAIT_MS / 1000.0 + 5, elapsed)
        self.assertIsNone(self.last_seen())

    def test_job_poll_still_records_the_workers_last_use(self):
        before = int(C.now_ts())
        self.gw.ok("GET", "/v1/worker/jobs", None, self.wtoken)
        seen = self.last_seen()
        self.assertIsNotNone(seen)
        self.assertGreaterEqual(seen, before)
        self.assertLessEqual(seen, int(C.now_ts()))
        self.gw.ok("GET", "/v1/worker", None, self.wtoken)
        self.assertGreaterEqual(self.last_seen(), seen)


if __name__ == "__main__":
    unittest.main()
