"""A request process that holds too much memory is replaced, without refusing or cutting a request.

With `processes` above 1, the first process reads each request process's private memory (RssAnon in /proc) every
MEMORY_CHECK_SECONDS. Past `limits.process_memory_mb` it starts another request process first, then tells the old
one (SIGUSR1) to stop accepting; the old one finishes what it holds, held connector polls included, and exits. The
other processes go on accepting from the same socket meanwhile. One process is replaced at a time.
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from support.public_harness import C, PUBLIC, FriendsGateway, wait_until

import bot_gateway as G

LINUX = sys.platform.startswith("linux")
# The gateway as `serve` runs it, with the memory check every 0.3 s instead of every 30 s.
FAST_CHECKS = [sys.executable, "-c",
               "import sys; sys.path.insert(0, %r); import bot_gateway as G; G.MEMORY_CHECK_SECONDS = 0.3; "
               "sys.exit(G.main(sys.argv[1:]))" % str(PUBLIC)]
# The same, with every newer process reading a little heavier, as a process forked late can (it starts with what the
# supervising process holds by then). Seen on a CI runner: replacements were replaced again and the first ones stayed.
NEWER_HEAVIER = [sys.executable, "-c",
                 "import sys; sys.path.insert(0, %r); import bot_gateway as G; G.MEMORY_CHECK_SECONDS = 0.3; "
                 "read = G.private_memory; "
                 "G.private_memory = lambda pid: None if read(pid) is None else read(pid) + (pid << 12); "
                 "sys.exit(G.main(sys.argv[1:]))" % str(PUBLIC)]


def children(pid):
    out = []
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                stat = Path("/proc/%s/stat" % entry).read_text()
            except OSError:
                continue
            fields = stat.rsplit(")", 1)[1].split()
            if int(fields[1]) == pid and fields[0] != "Z":
                out.append(int(entry))
    return sorted(out)


class SlowHandler(BaseHTTPRequestHandler):
    started = None

    def do_GET(self):
        SlowHandler.started.set()
        time.sleep(1.0)
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class DrainTests(unittest.TestCase):
    def setUp(self):
        SlowHandler.started = threading.Event()
        self.srv = G.BoundedServer(("127.0.0.1", 0), SlowHandler, 4, socket.AF_INET)
        self.addCleanup(self.srv.server_close)
        self.serving = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.serving.start()
        self.url = "http://127.0.0.1:%d/" % self.srv.server_address[1]

    def test_a_stopped_server_finishes_the_requests_it_holds(self):
        answers = []
        client = threading.Thread(target=lambda: answers.append(urllib.request.urlopen(self.url, timeout=10).read()))
        client.start()
        self.assertTrue(SlowHandler.started.wait(5))
        self.srv.shutdown()                   # no new connections from here on
        self.serving.join(5)
        self.assertFalse(self.srv.drain(0.1), "the request is still running")
        t0 = time.monotonic()
        self.assertTrue(self.srv.drain(5))
        self.assertLess(time.monotonic() - t0, 2)
        client.join(5)
        self.assertEqual([b'{"ok":true}'], answers, "the held request got its whole answer")

    def test_an_idle_server_drains_at_once(self):
        self.srv.shutdown()
        self.assertTrue(self.srv.drain(0))


class SettingTests(unittest.TestCase):
    def test_process_memory_mb_takes_a_positive_number_or_zero(self):
        self.assertEqual(512, G.LIMIT_DEFAULTS["process_memory_mb"])
        with tempfile.TemporaryDirectory(prefix="yobiru recycle setting ") as tmp:
            gw = FriendsGateway(Path(tmp))
            path = gw.home / "gateway.json"
            cfg = json.loads(path.read_text())
            for value, ok in ((0, True), (2048, True), (-1, False), ("512", False)):
                path.write_text(json.dumps(dict(cfg, limits={"process_memory_mb": value})))
                if ok:
                    self.assertEqual(value, G.load_config(gw.home)["limits"]["process_memory_mb"])
                else:
                    with self.assertRaises(ValueError):
                        G.load_config(gw.home)

    @unittest.skipUnless(LINUX, "reads /proc")
    def test_private_memory_reads_proc(self):
        self.assertGreater(G.private_memory(os.getpid()), 1 << 20)
        self.assertIsNone(G.private_memory(2 ** 22 + 7))


@unittest.skipUnless(LINUX, "reads process trees and memory from /proc")
class RecycleTests(unittest.TestCase):
    memory_mb = 1                             # every process is over this: they are replaced one after another
    prefix = FAST_CHECKS

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="yobiru recycle ")
        self.addCleanup(tmp.cleanup)
        self.gw = FriendsGateway(Path(tmp.name), prefix=self.prefix,
                                 limits={"process_memory_mb": self.memory_mb, "wake_retry_seconds": 1,
                                         "min_deadline_seconds": 1})
        cfg = json.loads((self.gw.home / "gateway.json").read_text())
        cfg["processes"] = 2
        (self.gw.home / "gateway.json").write_text(json.dumps(cfg))
        self.gw.start()
        self.addCleanup(self.gw.stop)
        self.parent = self.gw.service.proc.pid
        self.assertTrue(wait_until(lambda: len(children(self.parent)) >= 3))
        self.log = self.gw.home.parent / ("%s.log" % self.gw.home.name)

    def recycled(self):
        return self.log.read_text().count(" recycled (")


class ReplacingTests(RecycleTests):
    def test_requests_keep_being_answered_while_processes_are_replaced(self):
        first = children(self.parent)
        results, lock = [], threading.Lock()

        def poll():
            for _ in range(25):
                status, _ = self.gw.call("GET", "/v1/health")
                with lock:
                    results.append(status)
                time.sleep(0.02)
        threads = [threading.Thread(target=poll) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual([200] * 200, results)
        self.assertTrue(wait_until(lambda: len(set(first) & set(children(self.parent))) == 1, timeout=20),
                        "only the maintenance process is never replaced: " + self.log.read_text()[-2000:])
        self.assertTrue(wait_until(lambda: self.recycled() >= 2), self.log.read_text()[-3000:])
        counts = []
        for _ in range(20):
            counts.append(len(children(self.parent)))
            time.sleep(0.05)
        self.assertLessEqual(max(counts), 4, "at most one process is being replaced at a time")
        self.assertNotIn("keep exiting", self.log.read_text())
        self.assertNotIn(" exited (", self.log.read_text(), "a replaced process is not counted as a crash")

    def test_a_held_connector_poll_ends_normally(self):
        gw = self.gw
        alpha, beta = gw.bot("alpha"), gw.bot("beta")
        gw.follow(gw.source_for(alpha), beta)
        _, wid = gw.native(beta)
        token = gw.ok("POST", "/v1/owner/bots/%s/workers/%s/connector" % (beta, wid), {}, gw.owner_token)["token"]
        status, listing = C.api_call(gw.url, "GET", "/v1/connector/jobs", None, token)
        self.assertEqual(200, status, listing)
        before = self.recycled()
        held = [p for p in children(self.parent)]
        answers = []

        def wait():
            t0 = time.monotonic()
            status, obj = C.api_call(gw.url, "GET", "/v1/connector/jobs?wait=4&cursor=%s" % listing["cursor"], None,
                                     token, timeout=30)
            answers.append((status, obj.get("cursor"), time.monotonic() - t0))
        polls = [threading.Thread(target=wait) for _ in range(4)]
        for t in polls:
            t.start()
        longest, since = 0.0, None             # a process kept running past its replacement: it holds a poll
        while any(t.is_alive() for t in polls):
            now = time.monotonic()
            if len(children(self.parent)) > 3:
                since = since or now
                longest = max(longest, now - since)
            else:
                since = None
            time.sleep(0.05)
        self.assertEqual(4, len(answers))
        self.assertGreater(longest, 2.5, "a replaced process stayed until the poll it held was answered")
        for status, cursor, took in answers:
            self.assertEqual(200, status)
            self.assertEqual(listing["cursor"], cursor, "nothing changed: the same listing")
            self.assertGreater(took, 3.5, "held for the whole wait, not cut short")
        self.assertTrue(wait_until(lambda: self.recycled() > before), "processes holding polls were replaced")
        self.assertTrue(wait_until(lambda: not set(held[1:]) & set(children(self.parent)), timeout=20))


class NewerHeavierTests(RecycleTests):
    """The oldest process over the limit goes first, even when a newer one holds more."""
    prefix = NEWER_HEAVIER

    def test_the_first_processes_are_replaced_even_when_newer_ones_read_heavier(self):
        first = children(self.parent)
        self.assertTrue(wait_until(lambda: len(set(first) & set(children(self.parent))) == 1, timeout=20),
                        "only the maintenance process is never replaced: " + self.log.read_text()[-2000:])
        self.assertTrue(wait_until(lambda: self.recycled() >= 2), self.log.read_text()[-3000:])


class OffTests(RecycleTests):
    memory_mb = 0

    def test_zero_never_replaces_a_process(self):
        first = children(self.parent)
        time.sleep(1.5)                       # five checks' worth
        self.assertEqual(first, children(self.parent))
        self.assertEqual(0, self.recycled())


if __name__ == "__main__":
    unittest.main()
