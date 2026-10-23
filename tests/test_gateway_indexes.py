"""Gateway indexes must keep reply release local to one conversation as stored history grows."""
import json
import tempfile
import unittest
from pathlib import Path

from support.public_harness import FriendsGateway

import bot_gateway as G


class ReplyHistoryIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="yobiru gateway indexes ")
        self.addCleanup(self.tmp.cleanup)
        self.gw = FriendsGateway(Path(self.tmp.name))
        self.cfg = G.load_config(self.gw.home)
        self.db = G.open_db(self.gw.home / "gateway.sqlite3")
        self.addCleanup(self.db.close)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("INSERT INTO workers(id, bot_id, epoch, label, created_ts, adapter) "
                            "VALUES('worker', 'bot', 1, 'Synthetic worker', 1, 'native')")
            statement = (
                "INSERT INTO messages(id, conversation_id, bot_id, actor_id, author, op_key, request_hash, "
                "kind, content, state, route, created_ts, draft, draft_worker) "
                "VALUES(?, ?, 'bot', 'actor', 'caller', ?, 'hash', 'chat', '{}', ?, 'agent', 1, ?, ?)"
            )
            self.db.execute(statement, ("first", "target", "first", "review", '{"text":"first"}', "worker"))
            self.db.executemany(statement, (
                ("history-%d" % i, "unrelated-%d" % i, "history", "completed", None, None)
                for i in range(20000)
            ))
            self.db.execute(statement, ("second", "target", "second", "review", '{"text":"second"}', "worker"))

    def assert_conversation_plan(self):
        plan = self.db.execute(
            "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM messages WHERE conversation_id=? AND state='completed' "
            "AND route='agent' AND rowid < (SELECT rowid FROM messages WHERE id=?)", ("target", "second")
        ).fetchall()
        self.assertIn("conversation_id=?", plan[0]["detail"], [row["detail"] for row in plan])

    def assert_reply_releases(self, store):
        api = G.Api(store)
        for index, mid in enumerate(("first", "second")):
            with self.subTest(message=mid), store.tx() as db:
                msg = db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
                calls = []
                db.set_progress_handler(lambda: calls.append(None), 100)
                try:
                    api.release_draft(db, msg, {"id": "decision-" + mid}, 1000 + index)
                finally:
                    db.set_progress_handler(None, 0)
                # Count VM work rather than elapsed time. A global state scan costs over 100,000 instructions
                # for this fixture; the bounded conversation query and all release writes need fewer than 1,000.
                self.assertLess(len(calls), 100, "reply release examined unrelated conversation history")
                result = db.execute("SELECT state, result, draft, release_decision FROM messages WHERE id=?",
                                    (mid,)).fetchone()
                self.assertEqual(("completed", '{"text":"%s"}' % mid, None, "decision-" + mid), tuple(result))
                evidence = json.loads(db.execute("SELECT evidence FROM workers WHERE id='worker'").fetchone()[0])
                self.assertEqual(G.C.iso(1000 + index), evidence["reply_tested_at"])
                self.assertEqual(bool(index), "multiturn_tested_at" in evidence)
                if index:
                    self.assertEqual(G.C.iso(1001), evidence["multiturn_tested_at"])

    def test_init_keeps_reply_history_conversation_scoped(self):
        # Check the database produced by init before startup has any opportunity to add missing indexes.
        self.assert_conversation_plan()
        self.assert_reply_releases(G.Store(self.gw.home, self.cfg))

    def test_startup_adds_reply_history_index_to_existing_database(self):
        # Recreate the original PR's database: sweep indexes exist, but the conversation/state index does not.
        self.db.execute("DROP INDEX IF EXISTS messages_conversation_state")
        before = self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        version = self.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        G.Store(self.gw.home, self.cfg)
        store = G.Store(self.gw.home, self.cfg)  # repeated startup must be harmless
        self.assertEqual(before, self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
        self.assertEqual(version, self.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])
        self.assert_conversation_plan()
        self.assert_reply_releases(store)


if __name__ == "__main__":
    unittest.main()
