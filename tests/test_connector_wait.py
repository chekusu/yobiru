"""Connector polls that wait for a change, and a connector that outlives a gateway restart.

The gateway names each connector listing with a cursor. A poll that sends the cursor back with `wait` is held until
the listing differs (a new job, a reply to what the bot sent, a binding check, a refused wake's retry time) or the
wait is over, without taking a request slot. `connector run` uses it when the gateway offers a cursor, polls an
older gateway as before, and tries again after an outage instead of exiting.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from support.public_harness import C, PUBLIC, FriendsGateway, plain_env, wait_until

import bot_gateway as G

LIMITS = {"wake_retry_seconds": 1, "min_deadline_seconds": 1}


def timed(fn):
    t0 = time.monotonic()
    out = fn()
    return out, time.monotonic() - t0


class WaitCase(unittest.TestCase):
    processes, limits = 1, LIMITS

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="yobiru connector wait ")
        self.addCleanup(tmp.cleanup)
        self.gw = FriendsGateway(Path(tmp.name), limits=self.limits)
        if self.processes > 1:
            cfg = json.loads((self.gw.home / "gateway.json").read_text())
            cfg["processes"] = self.processes
            (self.gw.home / "gateway.json").write_text(json.dumps(cfg))
        self.gw.start()
        self.addCleanup(self.gw.stop)
        gw = self.gw
        self.alpha, self.beta = gw.bot("alpha"), gw.bot("beta")
        self.sa, self.sb = gw.source_for(self.alpha), gw.source_for(self.beta)
        gw.follow(self.sa, self.beta)
        gw.follow(self.sb, self.alpha)
        self.wtok, self.wid = gw.native(self.beta)
        self.ctok = self.connector(self.beta, self.wid)

    def connector(self, bot_id, wid):
        return self.gw.ok("POST", "/v1/owner/bots/%s/workers/%s/connector" % (bot_id, wid), {},
                          self.gw.owner_token)["token"]

    def poll(self, token=None, wait=None, cursor=None, timeout=45):
        query = "&".join(x for x in ("wait=%d" % wait if wait is not None else "",
                                     "cursor=%s" % cursor if cursor else "") if x)
        return C.api_call(self.gw.url, "GET", "/v1/connector/jobs" + ("?" + query if query else ""), None,
                          token or self.ctok, timeout=timeout)

    def listing(self, token=None):
        status, obj = self.poll(token)
        self.assertEqual(200, status, obj)
        return obj

    def later(self, seconds, fn):
        t = threading.Timer(seconds, fn)
        t.start()
        self.addCleanup(t.cancel)


class GatewayWaitTests(WaitCase):
    def test_an_unchanged_listing_is_held_until_the_wait_is_over(self):
        first = self.listing()
        self.assertEqual(first["cursor"], self.listing()["cursor"], "the same listing has the same cursor")
        (status, obj), took = timed(lambda: self.poll(wait=1, cursor=first["cursor"]))
        self.assertEqual((200, first["cursor"]), (status, obj["cursor"]))
        self.assertGreaterEqual(took, 0.9)
        for wait, cursor in ((5, "0" * 32), (5, None), (0, first["cursor"])):
            with self.subTest(wait=wait, cursor=cursor):
                (status, _), took = timed(lambda: self.poll(wait=wait, cursor=cursor))
                self.assertEqual(200, status)
                self.assertLess(took, 1, "an outdated cursor, no cursor or no wait: answered at once")

    def test_new_work_ends_the_wait_at_once(self):
        first = self.listing()
        self.later(0.5, lambda: self.gw.chat(self.gw.conversation(self.beta, self.sa), self.sa, "hello"))
        (status, obj), took = timed(lambda: self.poll(wait=20, cursor=first["cursor"]))
        self.assertEqual(200, status)
        self.assertEqual(1, len(obj["jobs"]))
        self.assertNotEqual(first["cursor"], obj["cursor"])
        self.assertLess(took, 3)

    def test_a_reply_to_what_the_bot_sent_and_a_binding_check_end_the_wait(self):
        gw = self.gw
        awtok, awid = gw.native(self.alpha)
        actok = self.connector(self.alpha, awid)
        mid = gw.chat(gw.conversation(self.beta, self.sa), self.sa, "a question from alpha")
        before = self.listing(actok)
        status, job = gw.claim_job(self.wtok, mid)
        self.assertEqual(200, status, job)

        def answer():
            self.assertEqual(200, gw.reply_job(self.wtok, job, "an answer from beta")[0])
        self.later(0.5, answer)
        (status, obj), took = timed(lambda: self.poll(actok, wait=20, cursor=before["cursor"]))
        self.assertEqual([mid], [r["message_id"] for r in obj["replies"]])
        self.assertLess(took, 3)
        self.later(0.5, lambda: gw.ok("POST", "/v1/owner/bots/%s/workers/%s/check" % (self.alpha, awid), {},
                                      gw.owner_token))
        (status, obj2), took = timed(lambda: self.poll(actok, wait=20, cursor=obj["cursor"]))
        self.assertEqual(1, len(obj2["checks"]))
        self.assertLess(took, 3)

    def test_a_refused_wake_ends_the_wait_when_it_may_be_tried_again(self):
        gw = self.gw
        mid = gw.chat(gw.conversation(self.beta, self.sa), self.sa, "hello")
        phase = self.listing()["jobs"][0]["phase"]
        for step in ({"step": "intent", "wake_id": "wake-refused-1", "phase": phase},
                     {"step": "result", "wake_id": "wake-refused-1", "outcome": "refused"}):
            self.assertEqual(200, gw.call("POST", "/v1/connector/messages/%s/wake" % mid, step, self.ctok)[0])
        waiting = self.listing()
        self.assertEqual([], waiting["jobs"])
        (status, obj), took = timed(lambda: self.poll(wait=20, cursor=waiting["cursor"]))
        self.assertEqual([(mid, 1)], [(j["job_id"], j["wake_attempts"]) for j in obj["jobs"]])
        self.assertLess(took, 4, "the retry time, a second after the refusal, ends the wait")

    def test_wait_and_cursor_are_checked_and_other_routes_take_no_query(self):
        for query, error in (("wait=31", "invalid_wait"), ("wait=-1", "invalid_wait"), ("wait=x", "invalid_wait"),
                             ("cursor=ABC", "invalid_wait"), ("wait=1&wait=2", "query_not_supported"),
                             ("limit=5", "query_not_supported")):
            with self.subTest(query=query):
                status, obj = C.api_call(self.gw.url, "GET", "/v1/connector/jobs?" + query, None, self.ctok)
                self.assertEqual((400, error), (status, obj["error"]))
        status, obj = self.gw.call("GET", "/v1/health?wait=1")
        self.assertEqual((400, "query_not_supported"), (status, obj["error"]))


class WaitingPlacesTests(WaitCase):
    limits = dict(LIMITS, max_concurrent_requests=2, max_waiting_connectors=3)

    def test_waiting_polls_leave_the_request_slots_free_and_have_their_own_limit(self):
        cursor = self.listing()["cursor"]
        took, lock = [], threading.Lock()

        def wait():
            (status, _), t = timed(lambda: self.poll(wait=3, cursor=cursor))
            with lock:
                took.append(t)
                self.assertEqual(200, status)
        threads = [threading.Thread(target=wait) for _ in range(5)]
        for t in threads:
            t.start()
            time.sleep(0.2)                         # one at a time: the two request slots are not the limit here
        time.sleep(0.5)
        for _ in range(5):
            self.assertEqual(200, self.gw.call("GET", "/v1/health")[0], "two slots, five waiting polls")
        for t in threads:
            t.join()
        self.assertEqual([False, False, True, True, True], sorted(t >= 2.5 for t in took),
                         "three places to wait; the other two polls were answered at once")


class MultiProcessWaitTests(WaitCase):
    processes = 3

    def test_a_change_written_by_any_process_wakes_polls_in_every_process(self):
        cursor = self.listing()["cursor"]
        took, lock = [], threading.Lock()

        def wait():
            (_, obj), t = timed(lambda: self.poll(wait=20, cursor=cursor))
            with lock:
                took.append((t, len(obj["jobs"])))
        threads = [threading.Thread(target=wait) for _ in range(9)]
        for t in threads:
            t.start()
        time.sleep(1)
        self.gw.chat(self.gw.conversation(self.beta, self.sa), self.sa, "hello")
        for t in threads:
            t.join()
        self.assertEqual([1] * 9, [n for _, n in took])
        self.assertLess(max(t for t, _ in took), 4)


class ChangeTriggerTests(unittest.TestCase):
    def test_writes_move_the_change_sequence_of_the_bots_they_concern(self):
        with tempfile.TemporaryDirectory(prefix="yobiru change triggers ") as tmp:
            gw = FriendsGateway(Path(tmp))
            G.Store(gw.home, G.load_config(gw.home))         # creates the table and its triggers
            db = gw.db()
            try:
                def seqs():
                    return dict(db.execute("SELECT bot_id, seq FROM connector_changes").fetchall())
                db.execute("INSERT INTO bots(id, owner_id, handle, display_name, description, policy, state, "
                           "worker_epoch, created_ts, updated_ts) VALUES('bot_a', 'own_a', 'aaa', 'A', '', '{}', "
                           "'published', 0, 1, 1)")
                db.execute("INSERT INTO workers(id, bot_id, epoch, label, created_ts) VALUES('wrk_a', 'bot_a', 0, "
                           "'w', 1)")
                self.assertEqual({}, seqs(), "creating a bot or a worker changes no listing")
                db.execute("INSERT INTO messages(id, conversation_id, bot_id, actor_id, author, op_key, request_hash, "
                           "kind, content, state, created_ts) VALUES('m1', 'c1', 'bot_a', 'bot_b', 'caller', 'k', "
                           "'h', 'chat', '{}', 'queued', 1)")
                self.assertEqual({"bot_a": 1, "bot_b": 2}, seqs(), "the bot's work and the sender's reply")
                db.execute("UPDATE messages SET state='completed' WHERE id='m1'")
                db.execute("INSERT INTO checks(id, worker_id, created_ts, expires_ts) VALUES('chk1', 'wrk_a', 1, 9)")
                self.assertEqual({"bot_a": 5, "bot_b": 4}, seqs())
                db.execute("UPDATE checks SET answered_ts=2 WHERE id='chk1'")
                self.assertEqual(6, seqs()["bot_a"], "a check that is answered leaves the listing")
                db.execute("UPDATE bots SET state='draft' WHERE id='bot_a'")
                self.assertEqual(7, seqs()["bot_a"])
            finally:
                db.close()


def host_command(log):
    return [sys.executable, "-c", "import sys; open(sys.argv[1], 'a').write(sys.argv[2] + '\\n')", str(log),
            "{job_id}"]


class ConnectorRunTests(WaitCase):
    def run_connector(self, *extra):
        home = self.gw.home.parent / "connector-home"
        token_file = self.gw.home.parent / "connector.token"
        token_file.write_text(self.ctok)
        os.chmod(str(token_file), 0o600)
        cli = [sys.executable, str(PUBLIC / "bot_client.py"), "--home", str(home)]
        env = plain_env(self.gw.home.parent)
        for args in (["connector", "login", self.gw.url, "--token-file", str(token_file)],
                     ["connector", "config", self.beta, "--exec"] + host_command(self.wakes_log)):
            r = subprocess.run(cli + args, capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(0, r.returncode, r.stderr)
        err = (self.gw.home.parent / "connector.err").open("w")
        self.addCleanup(err.close)
        proc = subprocess.Popen(cli + ["connector", "run", self.beta] + list(extra), stdout=subprocess.DEVNULL,
                                stderr=err, env=env)
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait()))
        return proc

    def setUp(self):
        WaitCase.setUp(self)
        self.wakes_log = self.gw.home.parent / "wakes.log"

    def wakes(self):
        return self.wakes_log.read_text().split() if self.wakes_log.exists() else []

    def polls(self):
        log = (self.gw.home.parent / "gateway.log").read_text()
        return log.count("GET connector_jobs")

    def test_the_connector_waits_for_work_and_wakes_the_agent_within_a_second(self):
        self.run_connector("--poll-seconds", "60", "--wait-seconds", "20")
        self.assertTrue(wait_until(lambda: self.polls() >= 1))     # the first listing; the next poll waits
        time.sleep(1)
        mid = self.gw.chat(self.gw.conversation(self.beta, self.sa), self.sa, "hello")
        sent = time.monotonic()
        self.assertTrue(wait_until(lambda: self.wakes() == [mid], timeout=5))
        self.assertLess(time.monotonic() - sent, 3, "a 60 second poll interval: only the wait explains this")
        time.sleep(1)
        self.assertLessEqual(self.polls(), 6, "a waiting connector does not poll in a loop")

    def test_the_connector_outlives_a_gateway_restart(self):
        proc = self.run_connector("--poll-seconds", "60", "--wait-seconds", "20")
        self.assertTrue(wait_until(lambda: self.polls() >= 1))
        time.sleep(1)
        self.gw.stop()
        time.sleep(2)
        self.gw.start()
        mid = self.gw.chat(self.gw.conversation(self.beta, self.sa), self.sa, "after the restart")
        self.assertTrue(wait_until(lambda: self.wakes() == [mid], timeout=20))
        self.assertIsNone(proc.poll(), "still running")
        self.assertIn("trying again", (self.gw.home.parent / "connector.err").read_text())


class OldGateway(BaseHTTPRequestHandler):
    """A gateway from before cursors: no cursor in the listing and no query accepted. `script` can answer a few
    polls with an error first, `jobs` is listed once, `posts` answers wake reports before they succeed."""
    script, paths, jobs, posts, reports = [], [], [], [], []

    def do_GET(self):
        type(self).paths.append(self.path)
        if self.path == "/v1/connector":
            return self.answer(200, {"ok": True, "bot_id": "bot_aaaaaaaaaaaaaaaa", "worker_id": "wrk_aaaaaaaaaaaaaaaa",
                                     "handle": "@old"})
        if type(self).script:
            return self.answer(*type(self).script.pop(0))
        if "?" in self.path:
            return self.answer(400, {"ok": False, "error": "query_not_supported"})
        jobs, type(self).jobs = type(self).jobs, []
        return self.answer(200, {"ok": True, "bot_id": "bot_aaaaaaaaaaaaaaaa", "handle": "@old", "jobs": jobs,
                                 "checks": [], "replies": []})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).reports.append((self.path, body["step"], body["wake_id"]))
        if type(self).posts:
            return self.answer(*type(self).posts.pop(0))
        return self.answer(200, {"ok": True})

    def answer(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class OlderGatewayTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="yobiru old gateway ")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        OldGateway.script, OldGateway.paths, OldGateway.jobs, OldGateway.posts, OldGateway.reports = [], [], [], [], []
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), OldGateway)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        token_file = self.root / "connector.token"
        token_file.write_text(C.new_token("connector"))
        os.chmod(str(token_file), 0o600)
        self.cli = [sys.executable, str(PUBLIC / "bot_client.py"), "--home", str(self.root / "home")]
        self.env = plain_env(self.root)
        for args in (["connector", "login", self.url, "--token-file", str(token_file)],
                     ["connector", "config", "bot_aaaaaaaaaaaaaaaa", "--manual"]):
            r = subprocess.run(self.cli + args, capture_output=True, text=True, env=self.env, timeout=60)
            self.assertEqual(0, r.returncode, r.stderr)

    def run_for(self, seconds, *extra):
        proc = subprocess.Popen(self.cli + ["connector", "run", "bot_aaaaaaaaaaaaaaaa"] + list(extra),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env, text=True)
        try:
            out, err = proc.communicate(timeout=seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            return None, err
        return proc.returncode, err

    def jobs_polls(self):
        return [p for p in OldGateway.paths if p.startswith("/v1/connector/jobs")]

    def test_an_older_gateway_is_polled_every_poll_interval(self):
        code, _ = self.run_for(2, "--poll-seconds", "0.5")
        self.assertIsNone(code, "still running")
        polls = self.jobs_polls()
        self.assertTrue(3 <= len(polls) <= 6, polls)
        self.assertEqual([], [p for p in polls if "?" in p], "no cursor, so never a wait")

    def test_an_outage_is_waited_out_and_a_refused_credential_stops_the_connector(self):
        OldGateway.script = [(503, {"ok": False, "error": "busy"}), (502, {"ok": False, "error": "bad_gateway"})]
        code, err = self.run_for(6, "--poll-seconds", "0.5")
        self.assertIsNone(code, err)
        self.assertEqual(2, err.count("trying again"), err)
        self.assertGreaterEqual(len(self.jobs_polls()), 4)
        OldGateway.paths, OldGateway.script = [], [(401, {"ok": False, "error": "unauthorized"})]
        code, err = self.run_for(10, "--poll-seconds", "0.5")
        self.assertEqual(2, code, err)
        self.assertEqual(1, len(self.jobs_polls()))

    def test_a_result_the_gateway_missed_is_reported_once_it_answers_again(self):
        OldGateway.jobs = [{"job_id": "msg_aaaaaaaaaaaaaaaa", "phase": "ready", "wake_attempts": 0}]
        OldGateway.posts = [(200, {"ok": True}), (503, {"ok": False, "error": "busy"})]
        code, err = self.run_for(3, "--poll-seconds", "0.5")
        self.assertIsNone(code, err)
        steps = [step for _, step, _ in OldGateway.reports]
        self.assertEqual(["intent", "result", "result"], steps, "the missed result is sent again, then nothing")
        self.assertEqual(1, len({wake_id for _, _, wake_id in OldGateway.reports}), "one wake, one id")
        self.assertEqual({"/v1/connector/messages/msg_aaaaaaaaaaaaaaaa/wake"}, {p for p, _, _ in OldGateway.reports})


if __name__ == "__main__":
    unittest.main()
