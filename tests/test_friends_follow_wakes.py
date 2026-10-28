"""Follow wakes are off unless the connector opts in.

With --wake-on-follows, becoming mutual friends wakes each opted-in owner once, whichever side followed
back. A one-way follow never wakes. The listing omits follows unless the poll asks, so a connector that
has not opted in is unchanged. An older gateway that rejects the parameter still wakes jobs.
"""
import io
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support.public_harness import C, FriendsGateway, cli

import bot_client as P


def bot_id(char):
    return "bot_" + char * 16


class FollowWakeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="yobiru follow wakes ")
        cls.gw = FriendsGateway(Path(cls._tmp.name)).start()
        cls.n = 0

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls._tmp.cleanup()

    def handle(self, prefix):
        self.__class__.n += 1
        return "%s%d" % (prefix, self.__class__.n)

    def token_for(self, home, bot):
        creds = json.loads((Path(home) / "public" / "credentials.json").read_text())
        for entry in creds["gateways"].values():
            pool = entry.get("connectors") or {}
            if bot in pool:
                return pool[bot]["token"]
        self.fail("no connector credential for %s" % bot)

    def listing(self, token, follows=None):
        path = "/v1/connector/jobs" + (("?follows=%s" % follows) if follows else "")
        return self.gw.ok("GET", path, None, token)

    def poll(self, token, cursor, wait, follows=None):
        query = "wait=%d&cursor=%s" % (wait, cursor)
        if follows:
            query += "&follows=%s" % follows
        started = time.monotonic()
        status, obj = C.api_call(self.gw.url, "GET", "/v1/connector/jobs?" + query, None, token,
                                 timeout=wait + 10)
        return status, obj, time.monotonic() - started

    def configure(self, home, bot, log, follows=False):
        script = "import sys; open(sys.argv[1], 'a').write(' '.join(sys.argv[2:]) + '\\n')"
        flags = ["--wake-on-follows"] if follows else []
        args = ["connector", "config", bot] + flags + [
            "--exec", sys.executable, "-c", script, str(log), "{kind}", "{phase}", "{friend_bot_id}"]
        code, out, err = cli(home, *args)
        self.assertEqual(0, code, (args, err, out))

    def connector_for(self, prefix, follows=False):
        handle = self.handle(prefix)
        bot, source = self.gw.owned_bot(handle)
        _, wid = self.gw.native(bot)
        home = Path(self._tmp.name) / ("home-%s" % handle)
        log = Path(self._tmp.name) / ("wakes-%s.log" % handle)
        token_file = Path(self._tmp.name) / ("conn-%s.token" % handle)
        code, out, err = cli(home, "owner", "login", self.gw.url, "--token-file",
                             str(self.gw.home / "owner.token"))
        self.assertEqual(0, code, err)
        code, out, err = cli(home, "connector", "bind", bot, wid, "--token-file", str(token_file))
        self.assertEqual(0, code, err)
        code, out, err = cli(home, "connector", "login", self.gw.url, "--token-file", str(token_file))
        self.assertEqual(0, code, err)
        self.configure(home, bot, log, follows)
        return bot, source, handle, home, log, self.token_for(home, bot)

    def run_once(self, home, bot):
        code, out, err = cli(home, "connector", "run", bot, "--once")
        self.assertEqual(0, code, err)
        return out

    def lines(self, log):
        return log.read_text().splitlines() if log.is_file() else []

    def assert_no_name(self, obj, *names):
        blob = json.dumps(obj)
        for name in names:
            self.assertNotIn(name, blob)
        self.assertNotIn("Synthetic", blob, "no display name")

    def test_with_the_switch_off_a_friendship_changes_nothing(self):
        bot, source, _, home, log, token = self.connector_for("off")
        other, other_source = self.gw.owned_bot(self.handle("pal"))
        self.assertEqual(201, self.gw.follow(source, other)[0])
        self.assertEqual(201, self.gw.follow(other_source, bot)[0])
        plain = self.listing(token)
        self.assertNotIn("follows", plain, "a connector that did not ask gets today's listing")
        stranger, stranger_source = self.gw.owned_bot(self.handle("fan"))
        followed = []
        timer = threading.Timer(0.3, lambda: followed.append(self.gw.follow(stranger_source, bot)))
        timer.start()
        self.addCleanup(timer.cancel)
        status, during, took = self.poll(token, plain["cursor"], 1)
        timer.join(5)
        self.assertEqual(200, status, during)
        self.assertEqual(201, followed[0][0], followed)
        self.assertGreaterEqual(took, 0.9, "a follow does not end the wait when the switch is off")
        self.assertNotIn("follows", during)
        self.assertEqual(plain["cursor"], during["cursor"])
        self.assertEqual([], self.run_once(home, bot)["woken"])
        self.assertEqual([], self.lines(log))

    def test_a_follow_back_wakes_once_and_following_someone_does_not(self):
        bot, source, _, home, log, token = self.connector_for("back", follows=True)
        other, other_source = self.gw.owned_bot(self.handle("guoyu"))
        before = self.listing(token, "mutual")
        self.assertEqual([], before["follows"])
        followed = []
        timer = threading.Timer(0.3, lambda: followed.append(self.gw.follow(source, other)))
        timer.start()
        self.addCleanup(timer.cancel)
        status, during, took = self.poll(token, before["cursor"], 1, "mutual")
        timer.join(5)
        self.assertEqual(200, status, during)
        self.assertEqual(201, followed[0][0], followed)
        self.assertGreaterEqual(took, 0.9, "following someone who does not follow back does not end the wait")
        self.assertEqual([], during["follows"])
        self.assertEqual([], self.run_once(home, bot)["woken"])
        self.assertEqual([], self.lines(log))

        waiting = self.listing(token, "mutual")
        arrived = []
        timer = threading.Timer(0.4, lambda: arrived.append(self.gw.follow(other_source, bot)))
        timer.start()
        self.addCleanup(timer.cancel)
        status, woken, took = self.poll(token, waiting["cursor"], 8, "mutual")
        timer.join(5)
        self.assertEqual((200, 201), (status, arrived[0][0]))
        self.assertLess(took, 3, "their follow-back ends the held poll")
        self.assertEqual([{"bot_id": other, "revision": 1, "kind": "friends"}], woken["follows"])
        self.assert_no_name(woken, "guoyu")
        plain = self.listing(token)
        self.assertNotIn("follows", plain, "an older connector still sees no follows")
        status, quiet, took = self.poll(token, plain["cursor"], 1)
        self.assertGreaterEqual(took, 0.9)
        self.assertEqual(plain["cursor"], quiet["cursor"])
        first = self.run_once(home, bot)
        self.assertEqual([{"ref": other, "kind": "follow", "phase": "friends", "outcome": "accepted"}],
                         first["woken"])
        self.assertEqual([], first["recovered"])
        self.assert_no_name(first, "guoyu")
        second = self.run_once(home, bot)
        self.assertEqual([], second["woken"], "a restart does not wake again")
        self.assertEqual([], second["recovered"])
        self.assertEqual(["yobiru.follow friends %s" % other], self.lines(log))
        self.assertEqual([], self.listing(token, "mutual")["follows"])
        again = self.gw.follow(other_source, bot)
        self.assertEqual((200, True), (again[0], again[1]["duplicate"]))
        self.assertEqual([], self.run_once(home, bot)["woken"])
        self.assertEqual(1, len(self.lines(log)))

    def test_following_them_back_wakes_once(self):
        bot, source, _, home, log, token = self.connector_for("rev", follows=True)
        other, other_source = self.gw.owned_bot(self.handle("nyx"))
        self.assertEqual(201, self.gw.follow(other_source, bot)[0])
        self.assertEqual([], self.listing(token, "mutual")["follows"], "a one-way follower is not a friendship")
        self.assertEqual([], self.run_once(home, bot)["woken"])
        self.assertEqual(201, self.gw.follow(source, other)[0])
        listed = self.listing(token, "mutual")
        self.assertEqual([{"bot_id": other, "revision": 1, "kind": "friends"}], listed["follows"])
        self.assert_no_name(listed, "nyx")
        self.assertEqual([other], [item["ref"] for item in self.run_once(home, bot)["woken"]])
        self.assertEqual([], self.run_once(home, bot)["woken"])
        self.assertEqual(["yobiru.follow friends %s" % other], self.lines(log))

    def test_two_new_friendships_wake_twice(self):
        bot, source, _, home, log, token = self.connector_for("pair", follows=True)
        friends = []
        for _ in range(2):
            other, other_source = self.gw.owned_bot(self.handle("duo"))
            self.assertEqual(201, self.gw.follow(source, other)[0])
            self.assertEqual(201, self.gw.follow(other_source, bot)[0])
            friends.append(other)
        listed = [item["bot_id"] for item in self.listing(token, "mutual")["follows"]]
        self.assertEqual(sorted(friends), sorted(listed))
        woken = [item["ref"] for item in self.run_once(home, bot)["woken"]]
        self.assertEqual(sorted(friends), sorted(woken))
        self.assertEqual([], self.run_once(home, bot)["woken"])
        self.assertEqual(sorted("yobiru.follow friends %s" % friend for friend in friends),
                         sorted(self.lines(log)))

    def test_one_way_follows_never_wake(self):
        bot, _, _, home, log, token = self.connector_for("burst", follows=True)
        before = self.listing(token, "mutual")
        for _ in range(5):
            _, follower_source = self.gw.owned_bot(self.handle("spam"))
            self.assertEqual(201, self.gw.follow(follower_source, bot)[0])
        self.assertEqual([], self.listing(token, "mutual")["follows"])
        self.assertEqual(before["cursor"], self.listing(token, "mutual")["cursor"])
        self.assertEqual([], self.run_once(home, bot)["woken"])
        self.assertEqual([], self.lines(log))

    def test_unfollow_does_not_wake_and_following_again_wakes_once(self):
        bot, source, _, home, log, token = self.connector_for("again", follows=True)
        other, other_source = self.gw.owned_bot(self.handle("fan"))
        self.assertEqual(201, self.gw.follow(source, other)[0])
        self.assertEqual(201, self.gw.follow(other_source, bot)[0])
        self.run_once(home, bot)
        self.assertEqual(200, self.gw.unfollow(other_source, bot)[0])
        self.assertEqual([], self.listing(token, "mutual")["follows"])
        self.assertEqual([], self.run_once(home, bot)["woken"])
        self.assertEqual(1, len(self.lines(log)))
        self.assertEqual(201, self.gw.follow(other_source, bot)[0])
        self.assertEqual([{"bot_id": other, "revision": 1, "kind": "friends"}],
                         self.listing(token, "mutual")["follows"],
                         "the revision is this bot's own edge, not the edge that followed back")
        self.run_once(home, bot)
        self.assertEqual([], self.run_once(home, bot)["woken"])
        self.assertEqual(["yobiru.follow friends %s" % other] * 2, self.lines(log))

    def test_a_restart_during_the_wake_is_uncertain_and_is_not_repeated(self):
        bot, source, _, home, log, token = self.connector_for("crash", follows=True)
        other, other_source = self.gw.owned_bot(self.handle("lost"))
        self.assertEqual(201, self.gw.follow(source, other)[0])
        self.assertEqual(201, self.gw.follow(other_source, bot)[0])
        wake_id = "lost-follow-01"
        self.gw.ok("POST", "/v1/connector/follows/%s/wake" % other,
                   {"step": "intent", "wake_id": wake_id, "phase": "friends", "route": "exec"}, token)
        P.State(home).save("connector-ledger.json", {"v": 1, "wakes": {"%s:%s" % (bot, other): {
            "kind": "follow", "ref": other, "phase": "friends", "wake_id": wake_id, "step": "intended"}}})
        code, report, err = cli(home, "connector", "run", bot, "--once")
        self.assertEqual(0, code, err)
        self.assertEqual([{"ref": other, "outcome": "uncertain"}], report["recovered"])
        self.assertEqual([], report["woken"])
        self.assertEqual([], self.lines(log), "a wake that may have happened is not sent again")
        self.assertIn(other, err)
        self.assertIn("will not be retried", err)
        row = self.gw.sql("SELECT wake_state FROM follows WHERE src=? AND dst=?", (bot, other))[0]
        self.assertEqual("uncertain", row["wake_state"])
        self.assertEqual([], self.listing(token, "mutual")["follows"])
        again = self.run_once(home, bot)
        self.assertEqual([], again["woken"])
        self.assertEqual([], again["recovered"])
        self.assertEqual([], self.lines(log))

    def test_friendships_from_before_the_upgrade_are_not_announced(self):
        bot, source, _, home, log, token = self.connector_for("upgrade", follows=True)
        old, old_source = self.gw.owned_bot(self.handle("oldfan"))
        self.assertEqual(201, self.gw.follow(source, old)[0])
        self.assertEqual(201, self.gw.follow(old_source, bot)[0])
        self.gw.stop()
        try:
            self.gw.sql("DELETE FROM meta WHERE key='follow_wakes'")
            self.gw.sql("UPDATE follows SET wake_state='none'")
        finally:
            self.gw.start()
        self.assertEqual([], self.listing(token, "mutual")["follows"])
        self.assertEqual([], self.run_once(home, bot)["woken"])
        self.assertEqual([], self.lines(log))
        fresh, fresh_source = self.gw.owned_bot(self.handle("newfan"))
        self.assertEqual(201, self.gw.follow(source, fresh)[0])
        self.assertEqual(201, self.gw.follow(fresh_source, bot)[0])
        self.assertEqual([fresh], [item["bot_id"] for item in self.listing(token, "mutual")["follows"]])
        self.assertEqual([fresh], [item["ref"] for item in self.run_once(home, bot)["woken"]])
        self.assertEqual(["yobiru.follow friends %s" % fresh], self.lines(log))

    def test_both_owners_are_woken_once_when_both_opt_in(self):
        bot, source, _, home, log, token = self.connector_for("botha", follows=True)
        other, other_source, _, other_home, other_log, other_token = self.connector_for("bothb", follows=True)
        self.assertEqual(201, self.gw.follow(source, other)[0])
        self.assertEqual(201, self.gw.follow(other_source, bot)[0])
        self.assertEqual([other], [item["bot_id"] for item in self.listing(token, "mutual")["follows"]])
        self.assertEqual([bot], [item["bot_id"] for item in self.listing(other_token, "mutual")["follows"]])
        self.assertEqual([other], [item["ref"] for item in self.run_once(home, bot)["woken"]])
        self.assertEqual([], self.listing(token, "mutual")["follows"])
        self.assertEqual([bot], [item["bot_id"] for item in self.listing(other_token, "mutual")["follows"]],
                         "the first owner's wake does not consume the second's")
        self.assertEqual([bot], [item["ref"] for item in self.run_once(other_home, other)["woken"]])
        self.assertEqual([], self.run_once(home, bot)["woken"])
        self.assertEqual([], self.run_once(other_home, other)["woken"])
        self.assertEqual([], self.run_once(home, bot)["recovered"])
        self.assertEqual([], self.run_once(other_home, other)["recovered"])
        self.assertEqual(["yobiru.follow friends %s" % other], self.lines(log))
        self.assertEqual(["yobiru.follow friends %s" % bot], self.lines(other_log))

    def test_only_the_owner_who_opted_in_is_woken(self):
        bot, source, _, home, log, token = self.connector_for("onea", follows=True)
        other, other_source, _, other_home, other_log, other_token = self.connector_for("oneb", follows=False)
        self.assertEqual(201, self.gw.follow(other_source, bot)[0])
        self.assertEqual(201, self.gw.follow(source, other)[0])
        self.assertEqual([other], [item["bot_id"] for item in self.listing(token, "mutual")["follows"]])
        self.assertNotIn("follows", self.listing(other_token))
        self.assertEqual([other], [item["ref"] for item in self.run_once(home, bot)["woken"]])
        self.assertEqual([], self.run_once(other_home, other)["woken"])
        self.assertEqual([], self.lines(other_log))
        self.assertEqual([], self.run_once(home, bot)["woken"])
        self.assertEqual([], self.run_once(other_home, other)["woken"])
        self.assertEqual(["yobiru.follow friends %s" % other], self.lines(log))

    def test_a_refused_follow_wake_is_due_again_only_after_the_backoff(self):
        bot, source, _, home, log, token = self.connector_for("backoff", follows=True)
        other, other_source = self.gw.owned_bot(self.handle("later"))
        self.assertEqual(201, self.gw.follow(source, other)[0])
        self.assertEqual(201, self.gw.follow(other_source, bot)[0])
        wake_id = "refused-follow-01"
        for step in ({"step": "intent", "wake_id": wake_id, "phase": "friends", "route": "exec"},
                     {"step": "result", "wake_id": wake_id, "outcome": "refused"}):
            self.gw.ok("POST", "/v1/connector/follows/%s/wake" % other, step, token)
        self.assertEqual([], self.listing(token, "mutual")["follows"])
        self.assertEqual([], self.run_once(home, bot)["woken"])
        self.assertEqual([], self.lines(log))
        self.gw.sql("UPDATE follows SET wake_ts=1 WHERE src=? AND dst=?", (bot, other))
        self.assertEqual([other], [item["bot_id"] for item in self.listing(token, "mutual")["follows"]])
        self.assertEqual([other], [item["ref"] for item in self.run_once(home, bot)["woken"]])
        self.assertEqual(["yobiru.follow friends %s" % other], self.lines(log))
        self.assertEqual([], self.listing(token, "mutual")["follows"])

    def test_the_mutual_listing_uses_the_friend_wake_index(self):
        bot, _, _, _, _, _ = self.connector_for("index", follows=True)
        plan = self.gw.sql("EXPLAIN QUERY PLAN SELECT f.dst, f.revision, f.wake_state, f.wake_seq, f.wake_ts "
                           "FROM follows f JOIN follows r ON r.src=f.dst AND r.dst=f.src AND r.state='active' "
                           "WHERE f.src=? AND f.state='active' AND f.wake_state IN ('none', 'refused') "
                           "ORDER BY f.updated_ts, f.rowid", (bot,))
        detail = " ".join(row["detail"] for row in plan)
        self.assertIn("follows_friend_wake", detail)

    def test_a_pre_column_follows_table_gains_per_owner_wake_columns(self):
        tmp = tempfile.TemporaryDirectory(prefix="yobiru follow migrate ")
        self.addCleanup(tmp.cleanup)
        gw = FriendsGateway(Path(tmp.name)).start()
        self.addCleanup(gw.stop)
        bot, source = gw.owned_bot("migbot")
        old, old_source = gw.owned_bot("migold")
        self.assertEqual(201, gw.follow(source, old)[0])
        self.assertEqual(201, gw.follow(old_source, bot)[0])
        _, wid = gw.native(bot)
        home = Path(tmp.name) / "home"
        log = Path(tmp.name) / "wakes.log"
        token_file = Path(tmp.name) / "conn.token"
        code, _, err = cli(home, "owner", "login", gw.url, "--token-file", str(gw.home / "owner.token"))
        self.assertEqual(0, code, err)
        code, _, err = cli(home, "connector", "bind", bot, wid, "--token-file", str(token_file))
        self.assertEqual(0, code, err)
        code, _, err = cli(home, "connector", "login", gw.url, "--token-file", str(token_file))
        self.assertEqual(0, code, err)
        self.configure(home, bot, log, follows=True)
        gw.stop()
        db = sqlite3.connect(str(gw.home / "gateway.sqlite3"))
        try:
            db.execute("BEGIN")
            db.execute("ALTER TABLE follows RENAME TO follows_pre")
            db.execute("CREATE TABLE follows (src TEXT NOT NULL, dst TEXT NOT NULL, state TEXT NOT NULL, "
                       "revision INTEGER NOT NULL, created_ts INTEGER NOT NULL, updated_ts INTEGER NOT NULL, "
                       "PRIMARY KEY (src, dst))")
            db.execute("INSERT INTO follows(src, dst, state, revision, created_ts, updated_ts) "
                       "SELECT src, dst, state, revision, created_ts, updated_ts FROM follows_pre")
            db.execute("DROP TABLE follows_pre")
            db.execute("CREATE INDEX follows_dst ON follows(dst, state)")
            db.execute("DELETE FROM meta WHERE key='follow_wakes'")
            db.execute("COMMIT")
        finally:
            db.close()
        gw.start()
        cols = {row["name"] for row in gw.sql("PRAGMA table_info(follows)")}
        self.assertTrue({"wake_state", "wake_id", "wake_seq", "wake_ts", "wake_detail"} <= cols)
        self.assertNotIn("mutual_wake", cols)
        names = [row["name"] for row in gw.sql("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='follows'")]
        self.assertIn("follows_friend_wake", names)
        self.assertNotIn("follows_wake", names)
        creds = json.loads((home / "public" / "credentials.json").read_text())
        token = next(entry["connectors"][bot]["token"] for entry in creds["gateways"].values()
                     if bot in (entry.get("connectors") or {}))
        self.assertEqual([], gw.ok("GET", "/v1/connector/jobs?follows=mutual", None, token)["follows"])
        self.assertEqual([], self.run_once(home, bot)["woken"])
        fresh, fresh_source = gw.owned_bot("mignew")
        self.assertEqual(201, gw.follow(source, fresh)[0])
        self.assertEqual(201, gw.follow(fresh_source, bot)[0])
        self.assertEqual([fresh], [item["bot_id"] for item in
                                   gw.ok("GET", "/v1/connector/jobs?follows=mutual", None, token)["follows"]])
        gw.stop()
        db = sqlite3.connect(str(gw.home / "gateway.sqlite3"))
        try:
            db.execute("DROP INDEX IF EXISTS follows_friend_wake")
            db.execute("CREATE INDEX follows_wake ON follows(dst, wake_state) WHERE state='active'")
        finally:
            db.close()
        gw.start()
        names = [row["name"] for row in gw.sql("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='follows'")]
        self.assertIn("follows_friend_wake", names)
        self.assertNotIn("follows_wake", names)
        self.assertEqual([fresh], [item["ref"] for item in self.run_once(home, bot)["woken"]])


class OldGatewayFollowTests(unittest.TestCase):
    def drive(self, listing, bot=None, follows=False, old_gateway=False):
        tmp = tempfile.TemporaryDirectory(prefix="yobiru follow old gateway ")
        self.addCleanup(tmp.cleanup)
        st = P.State(Path(tmp.name))
        posts, gets = [], []
        bot = bot or bot_id("c")
        route = {"type": "manual", "wake_on_follows": follows}

        def request(base, method, path, body, token, timeout=15):
            if method == "GET":
                gets.append(path)
                if old_gateway and "follows=" in path:
                    return {"ok": False, "error": "query_not_supported", "http_status": 400}
                return listing
            posts.append((path, body.get("step"), body.get("phase")))
            return {"ok": True, "duplicate": False}

        args = SimpleNamespace(once=True, wait_seconds=25, idle_exit=None, poll_seconds=5)
        with patch.object(P, "request", side_effect=request), \
                patch.object(P, "perform_wake", return_value=("accepted", "manual")) as wake:
            P.connector_run(st, args, "http://127.0.0.1:9", bot, {"token": "t", "handle": "@old"}, route)
        return posts, wake, gets

    def test_the_switch_off_does_not_ask_and_does_not_wake(self):
        follower = bot_id("b")
        posts, wake, gets = self.drive({"ok": True, "jobs": [], "checks": [], "replies": [], "follows": [
            {"bot_id": follower, "revision": 1, "kind": "friends"}]})
        self.assertEqual(["/v1/connector/jobs"], gets)
        self.assertEqual([], posts)
        wake.assert_not_called()

    def test_an_old_gateway_that_rejects_follows_still_wakes_a_job(self):
        job = "msg_" + "a" * 16
        posts, wake, gets = self.drive({"ok": True, "jobs": [{"job_id": job, "phase": "ready"}],
                                        "checks": [], "replies": []}, follows=True, old_gateway=True)
        self.assertEqual(["/v1/connector/jobs?follows=mutual", "/v1/connector/jobs"], gets)
        self.assertEqual(1, wake.call_count)
        self.assertEqual("yobiru.job", wake.call_args[0][1]["type"])
        self.assertNotIn("friend_bot_id", wake.call_args[0][1])
        self.assertNotIn("follower_bot_id", wake.call_args[0][1])
        self.assertEqual([("/v1/connector/messages/%s/wake" % job, "intent"),
                          ("/v1/connector/messages/%s/wake" % job, "result")],
                         [(path, step) for path, step, _ in posts])

    def test_a_follow_entry_that_is_not_a_friendship_is_not_woken(self):
        follower = bot_id("d")
        posts, wake, gets = self.drive({"ok": True, "jobs": [], "checks": [], "replies": [], "follows": [
            {"bot_id": "@guoyu", "revision": 1, "kind": "friends"},
            {"bot_id": follower, "revision": 1},
            {"bot_id": follower, "revision": 1, "kind": "follow"},
            "nope", None]}, follows=True)
        self.assertEqual(["/v1/connector/jobs?follows=mutual"], gets)
        self.assertEqual([], posts)
        wake.assert_not_called()

    def test_one_loop_wakes_each_friend_once(self):
        friend = bot_id("e")
        posts, wake, gets = self.drive({"ok": True, "jobs": [], "checks": [], "replies": [], "follows": [
            {"bot_id": friend, "revision": 1, "kind": "friends"},
            {"bot_id": friend, "revision": 1, "kind": "friends"}]}, follows=True)
        self.assertEqual(["/v1/connector/jobs?follows=mutual"], gets)
        self.assertEqual(1, wake.call_count)
        self.assertEqual(friend, wake.call_args[0][1]["friend_bot_id"])
        self.assertEqual([("/v1/connector/follows/%s/wake" % friend, "intent"),
                          ("/v1/connector/follows/%s/wake" % friend, "result")],
                         [(path, step) for path, step, _ in posts])

    def test_query_not_supported_for_the_cursor_does_not_stop_follow_polls(self):
        friend = bot_id("f")
        tmp = tempfile.TemporaryDirectory(prefix="yobiru follow cursor ")
        self.addCleanup(tmp.cleanup)
        st = P.State(Path(tmp.name))
        gets, wakes = [], []
        cursor = "ab" * 16
        args = SimpleNamespace(once=False, wait_seconds=1, idle_exit=None, poll_seconds=30)
        route = {"type": "manual", "wake_on_follows": True}

        def request(base, method, path, body, token, timeout=15):
            if method == "GET":
                gets.append(path)
                if "cursor=" in path:
                    return {"ok": False, "error": "query_not_supported", "http_status": 400}
                if len([item for item in gets if "follows=" in item and "cursor=" not in item]) == 1:
                    return {"ok": True, "jobs": [], "checks": [], "replies": [], "cursor": cursor}
                args.once = True
                return {"ok": True, "jobs": [], "checks": [], "replies": [], "cursor": cursor, "follows": [
                    {"bot_id": friend, "revision": 1, "kind": "friends"}]}
            return {"ok": True, "duplicate": False}

        def perform(route, body):
            wakes.append(body)
            return "accepted", "manual"

        with patch.object(P, "request", side_effect=request), patch.object(P, "perform_wake", side_effect=perform):
            P.connector_run(st, args, "http://127.0.0.1:9", bot_id("a"), {"token": "t", "handle": "@old"}, route)
        self.assertEqual(1, len(wakes))
        self.assertEqual(friend, wakes[0]["friend_bot_id"])
        self.assertTrue(any("follows=mutual" in path and "cursor=" not in path for path in gets[1:]))
        self.assertGreaterEqual(sum(1 for path in gets if "follows=mutual" in path), 2)

    def test_a_live_uncertain_follow_wake_is_printed_and_not_sent_again(self):
        friend = bot_id("g")
        tmp = tempfile.TemporaryDirectory(prefix="yobiru follow uncertain ")
        self.addCleanup(tmp.cleanup)
        st = P.State(Path(tmp.name))
        posts = []

        def request(base, method, path, body, token, timeout=15):
            if method == "GET":
                return {"ok": True, "jobs": [], "checks": [], "replies": [], "follows": [
                    {"bot_id": friend, "revision": 1, "kind": "friends"}]}
            posts.append(body.get("outcome") or body.get("step"))
            return {"ok": True, "duplicate": False}

        args = SimpleNamespace(once=True, wait_seconds=25, idle_exit=None, poll_seconds=5)
        err = io.StringIO()
        with patch.object(P, "request", side_effect=request), \
                patch.object(P, "perform_wake", return_value=("uncertain", "exec: timeout")) as wake, \
                redirect_stderr(err):
            P.connector_run(st, args, "http://127.0.0.1:9", bot_id("a"), {"token": "t", "handle": "@old"},
                            {"type": "exec", "argv": ["x"], "wake_on_follows": True})
        self.assertEqual(1, wake.call_count)
        self.assertEqual(["intent", "uncertain"], posts)
        text = err.getvalue()
        self.assertIn(friend, text)
        self.assertIn("will not be retried", text)
        self.assertEqual({}, st.load("connector-ledger.json")["wakes"])

    def test_wake_body_names_the_friend_id_and_no_name(self):
        follower = bot_id("b")
        owner = bot_id("a")
        body = P.wake_body("follow", "http://127.0.0.1:9", owner, "@hacken", follower, "friends", "w" * 12)
        self.assertEqual("yobiru.follow", body["type"])
        self.assertEqual(follower, body["friend_bot_id"])
        self.assertNotIn("follower_bot_id", body)
        self.assertEqual("friends", body["phase"])
        self.assertNotIn("job_id", body)
        blob = json.dumps(body)
        self.assertNotIn("guoyu", blob)
        self.assertNotIn("Synthetic", blob)
        self.assertIn(owner, body["instructions"])
        self.assertIn("that bot id", body["instructions"])
        self.assertNotIn("owner followers", body["instructions"])
