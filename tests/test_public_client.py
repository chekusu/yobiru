"""The public bot client: transport safety, retry and recovery identity, local state and worker exclusivity.

Real gateway subprocesses on loopback; in-process loopback servers and a dropping proxy stand in for
slow peers, redirects and lost responses. Concurrency checks use separate processes and kernel locks.
"""
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler
from support.numeric_http import HTTPServer
from pathlib import Path

from support.public_harness import C, PUBLIC, Gateway, plain_env, wait_until

import bot_client as P  # noqa: E402

TOKEN_PATTERN = re.compile(r"yb[oawvtc]_[A-Za-z0-9_-]{43}")


def cli(home, *args):
    """bot_client in this process: -> (exit code, parsed JSON stdout or text, stderr)."""
    out, err, code = io.StringIO(), io.StringIO(), 0
    with redirect_stdout(out), redirect_stderr(err):
        try:
            P.main(["--home", str(home)] + list(args))
        except SystemExit as e:
            code = e.code or 0
    text = out.getvalue()
    try:
        return code, json.loads(text), err.getvalue()
    except ValueError:
        return code, text, err.getvalue()


def cli_process(home, *args, env=None, timeout=60):
    r = subprocess.run([sys.executable, str(PUBLIC / "bot_client.py"), "--home", str(home)] + list(args),
                       capture_output=True, text=True, env=env, timeout=timeout)
    return r


class Recorder(object):
    """A loopback HTTP server that records every request and answers with a scripted handler."""

    def __init__(self, respond):
        self.seen = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def handle_any(self):
                outer.seen.append({"method": self.command, "path": self.path,
                                   "authorization": self.headers.get("Authorization")})
                respond(self)

            do_GET = do_POST = handle_any
        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class DroppingProxy(object):
    """Forwards HTTP requests to the gateway. For POST .../messages it can drop the request (never
    forwarded) or drop the response (forwarded and committed, then the client connection is closed)."""

    def __init__(self, upstream_port):
        self.upstream, self.mode, self.forwarded = upstream_port, "pass", 0
        self.holding, self.release = threading.Event(), threading.Event()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.url = "http://127.0.0.1:%d" % self.sock.getsockname()[1]
        self.alive = True
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while self.alive:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def handle(self, conn):
        with conn:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                data += chunk
            head, _, body = data.partition(b"\r\n\r\n")
            m = re.search(rb"(?i)content-length:\s*(\d+)", head)
            length = int(m.group(1)) if m else 0
            while len(body) < length:
                body += conn.recv(65536)
            target = head.split(b" ")[0] == b"POST" and b"/messages " in head.split(b"\r\n")[0]
            if target and self.mode == "drop_request":
                return
            if target and self.mode == "hold":
                self.holding.set()
                self.release.wait(30)
            with socket.create_connection(("127.0.0.1", self.upstream)) as up:
                up.sendall(head + b"\r\n\r\n" + body)
                response = b""
                while True:
                    chunk = up.recv(65536)
                    if not chunk:
                        break
                    response += chunk
            if target:
                self.forwarded += 1
            if target and self.mode == "drop_response":
                return
            conn.sendall(response)

    def close(self):
        self.alive = False
        self.sock.close()


class TransportTests(unittest.TestCase):
    def slow_server(self, version, close_header):
        def respond(h):
            body = b'{"ok": true}'
            h.wfile.write(("%s 200 OK\r\nContent-Type: application/json\r\n%s\r\n" % (
                version, "Connection: close\r\nContent-Length: %d\r\n" % len(body) if close_header else "")).encode())
            for byte in body:
                try:
                    h.wfile.write(bytes([byte]))
                    h.wfile.flush()
                except OSError:
                    break
                time.sleep(0.1)
            h.close_connection = True
        rec = Recorder(respond)
        self.addCleanup(rec.close)
        return rec

    def test_total_deadline_covers_slow_close_delimited_bodies(self):
        for version, close_header in (("HTTP/1.0", False), ("HTTP/1.1", True)):
            rec = self.slow_server(version, close_header)
            started = time.monotonic()
            with self.assertRaises(C.ApiError) as cm:
                C.api_call(rec.url, "GET", "/slow", timeout=0.25)
            self.assertEqual("unreachable", cm.exception.code)
            self.assertIn("deadline", cm.exception.detail)
            self.assertLess(time.monotonic() - started, 0.6, version)
        fast = Recorder(lambda h: (h.send_response(200), h.send_header("Content-Length", "11"), h.end_headers(),
                                   h.wfile.write(b'{"ok":true}')))
        self.addCleanup(fast.close)
        self.assertEqual((200, {"ok": True}), C.api_call(fast.url, "GET", "/fast", timeout=2))

    def test_redirects_are_refused_and_credentials_stay_with_their_origin(self):
        target = Recorder(lambda h: (h.send_response(200), h.send_header("Content-Length", "11"), h.end_headers(),
                                     h.wfile.write(b'{"ok":true}')))
        self.addCleanup(target.close)
        redirect = Recorder(lambda h: (h.send_response(307), h.send_header("Location", target.url + "/v1/owner"),
                                       h.send_header("Content-Length", "0"), h.end_headers()))
        self.addCleanup(redirect.close)
        token = C.new_token("owner")
        with self.assertRaises(C.ApiError) as cm:
            C.api_call(redirect.url, "GET", "/v1/owner", token=token)
        self.assertEqual("redirect_refused", cm.exception.code)
        self.assertEqual([], target.seen, "the redirect target was never contacted")
        # a stored credential is only sent to the exact gateway URL it belongs to
        tmp = tempfile.TemporaryDirectory(prefix="yobiru client origin ")
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        P.State(home).save("credentials.json", {"v": 1, "gateways": {target.url: {"actor": {
            "actor_id": "act_aaaaaaaaaaaaaaaa", "token": C.new_token("actor"), "display_name": "x"}}}})
        other = Recorder(lambda h: (h.send_response(404), h.send_header("Content-Length", "2"), h.end_headers(),
                                    h.wfile.write(b"{}")))
        self.addCleanup(other.close)
        code, _, err = cli(home, "lookup", other.url + "/@someone")
        self.assertNotEqual(0, code)
        self.assertEqual([None], [r["authorization"] for r in other.seen], "public lookups carry no credential")
        P.State(home).save("contacts.json", {"v": 1, "contacts": {"elsewhere": {
            "gateway": other.url, "bot_id": "bot_aaaaaaaaaaaaaaaa", "handle": "@x", "conversation_id": None}}})
        code, _, err = cli(home, "chat", "elsewhere", "hello")
        self.assertEqual(2, code)
        self.assertIn("no caller identity for %s" % other.url, err)
        self.assertEqual(1, len(other.seen), "nothing was sent with another gateway's credential")

    def test_plain_http_is_refused_for_non_loopback_gateways(self):
        tmp = tempfile.TemporaryDirectory(prefix="yobiru client http ")
        self.addCleanup(tmp.cleanup)
        for args in (["enroll", "http://192.0.2.10:7780"], ["owner", "login", "http://gateway.example",
                                                             "--token-file", "/nonexistent"],
                     ["lookup", "http://gateway.example/@bot"], ["enroll", "https://user:pw@gateway.example"],
                     ["enroll", "ftp://gateway.example"], ["enroll", "https://gateway.example/?x=1"]):
            code, out, err = cli(tmp.name, *args)
            self.assertEqual(2, code, args)
        for ok in ("https://gateway.example", "http://127.0.0.1:7780", "http://[::1]:7780", "https://gateway.example/bots"):
            self.assertEqual(ok, C.normalize_base_url(ok + "/"))


class ClientFlowCase(unittest.TestCase):
    LIMITS = {"min_deadline_seconds": 1, "messages_per_minute_per_actor": 1000, "messages_per_hour_per_actor": 5000,
              "pending_per_contact": 1000, "pending_per_bot": 2000, "turns_per_conversation": 1000,
              "max_body_bytes": 65536, "pending_total": 5000}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="yobiru client flow ")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.gw = Gateway(self.root, limits=self.LIMITS).start()
        self.addCleanup(self.gw.stop)
        self.bot_id = self.gw.bot("assistant")
        self.caller = self.root / "caller"

    def enroll(self, url=None, alias="assistant"):
        url = url or self.gw.url
        self.assertEqual(0, cli(self.caller, "enroll", url, "--name", "Caller")[0])
        code, added, err = cli(self.caller, "add", url + "/@assistant", "--alias", alias)
        self.assertEqual(0, code, err)
        return json.loads((self.caller / "public" / "credentials.json").read_text())["gateways"][url]["actor"]


class RetryIdentityTests(ClientFlowCase):
    def test_generated_op_keys_are_always_usable_as_command_arguments(self):
        keys = {P.new_op() for _ in range(2000)}
        self.assertEqual(2000, len(keys))
        self.assertFalse([k for k in keys if k.startswith("-") or not C.OP_KEY_RE.match(k)])

    def test_same_op_key_never_becomes_a_second_message(self):
        self.enroll()
        code, first, _ = cli(self.caller, "chat", "assistant", "repeat me", "--op-key", "retry_same_0001",
                             "--new-conversation")
        self.assertEqual(0, code)
        code, second, _ = cli(self.caller, "chat", "assistant", "repeat me", "--op-key", "retry_same_0001",
                              "--new-conversation")
        self.assertEqual(0, code)
        self.assertEqual((first["message_id"], first["conversation_id"], True),
                         (second["message_id"], second["conversation_id"], second["duplicate"]))
        code, out, err = cli(self.caller, "chat", "assistant", "different text", "--op-key", "retry_same_0001")
        self.assertEqual(2, code)
        self.assertIn("different message", err)
        conversations = self.gw.ok("GET", "/v1/owner/bots/%s/conversations" % self.bot_id, None, self.gw.owner_token)
        self.assertEqual(1, len(conversations["conversations"]), "no second conversation was opened")
        self.assertEqual(1, conversations["conversations"][0]["turns"])

    def test_lost_responses_and_lost_requests_are_reconciled_not_guessed(self):
        proxy = DroppingProxy(self.gw.port)
        self.addCleanup(proxy.close)
        self.enroll(proxy.url)
        # response lost after the gateway committed
        proxy.mode = "drop_response"
        code, lost, _ = cli(self.caller, "chat", "assistant", "committed but unanswered")
        self.assertEqual((2, "unknown_outcome"), (code, lost["error"]))
        op = lost["op_key"]
        self.assertIn("status --op-key %s" % op, lost["next"])
        proxy.mode = "pass"
        pending = cli_process(self.caller, "pending", env=plain_env(self.root))       # a fresh process
        self.assertEqual([(op, "unknown")], [(o["op_key"], o["outcome"]) for o in json.loads(pending.stdout)["operations"]])
        checked = cli_process(self.caller, "status", "--op-key", op, env=plain_env(self.root))
        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
        status = json.loads(checked.stdout)
        self.assertEqual((True, "queued"), (status["stored"], status["state"]))
        code, again, _ = cli(self.caller, "resend", op)
        self.assertEqual((0, status["message_id"], True), (code, again["message_id"], again["duplicate"]))
        self.assertEqual([], cli(self.caller, "pending")[1]["operations"])
        # request lost before it reached the gateway
        proxy.mode = "drop_request"
        code, lost, _ = cli(self.caller, "chat", "assistant", "never arrived")
        self.assertEqual("unknown_outcome", lost["error"])
        proxy.mode = "pass"
        code, status, _ = cli(self.caller, "status", "--op-key", lost["op_key"])
        self.assertEqual((0, "not_found_now", "unknown", 30),
                         (code, status["stored"], status["outcome"], status["retention_days"]),
                         "an observation, never a proof that the earlier dispatch was not stored")
        code, sent, _ = cli(self.caller, "resend", lost["op_key"])
        self.assertEqual((0, False), (code, sent["duplicate"]))
        view = self.gw.owner_view(sent["conversation_id"])
        self.assertEqual(["committed but unanswered", "never arrived"], [m["content"]["text"] for m in view["messages"]])

    def test_later_refusals_never_settle_an_earlier_unknown_outcome(self):
        proxy = DroppingProxy(self.gw.port)
        self.addCleanup(proxy.close)
        actor = self.enroll(proxy.url)
        proxy.mode = "drop_response"
        op = cli(self.caller, "chat", "assistant", "stored, then revoked")[1]["op_key"]
        proxy.mode = "pass"
        self.assertEqual(200, self.gw.contact(self.bot_id, actor["actor_id"], "revoke")[0])
        code, status, _ = cli(self.caller, "status", "--op-key", op)
        self.assertEqual((2, "unknown"), (code, status["stored"]), "an unreadable conversation is not 'absent'")
        code, refused, _ = cli(self.caller, "resend", op)
        self.assertEqual((2, "contact_revoked", "unknown"), (code, refused["error"], refused["outcome"]))
        self.assertIn("cannot settle", refused["note"])
        self.assertEqual([(op, "unknown")], [(o["op_key"], o["outcome"]) for o in cli(self.caller, "pending")[1]["operations"]])
        # a definite refusal on the very first attempt does mean nothing was stored
        self.gw.contact(self.bot_id, actor["actor_id"], "approve", ["faq"])
        self.gw.contact(self.bot_id, actor["actor_id"], "restrict", ["faq"])     # exactly faq, nothing else
        code, refused, _ = cli(self.caller, "chat", "assistant", "leave a message", "--new-conversation")
        self.assertEqual((2, "capability_not_granted", "refused"), (code, refused["error"], refused["outcome"]))
        # and an unpublished bot keeps an unknown op unknown, too
        proxy.mode = "drop_response"
        op2 = cli(self.caller, "chat", "assistant", "faq question", "--faq")[1]["op_key"]
        proxy.mode = "pass"
        self.gw.ok("POST", "/v1/owner/bots/%s/unpublish" % self.bot_id, {}, self.gw.owner_token)
        code, status, err = cli(self.caller, "status", "--op-key", op2)
        self.assertIsInstance(status, dict, (code, status, err))
        self.assertEqual((2, "unknown"), (code, status["stored"]))


    def test_a_crash_after_a_real_commit_never_looks_like_a_first_refusal(self):
        actor = self.enroll()
        script = ("import os, sys\nsys.path.insert(0, %r)\nimport bot_common as C, bot_client as P\n"
                  "real = C.api_call\n"
                  "def commit_then_die(*a, **k):\n"
                  "    result = real(*a, **k)\n"
                  "    if a[1] == 'POST' and a[2].endswith('/messages'):\n"
                  "        os._exit(9)          # the gateway committed; this process never records the answer\n"
                  "    return result\n"
                  "C.api_call = commit_then_die\n"
                  "P.main(['--home', sys.argv[1], 'chat', 'assistant', 'committed, then crashed', "
                  "'--op-key', 'crash_window_01'])\n") % str(PUBLIC)
        r = subprocess.run([sys.executable, "-c", script, str(self.caller)], capture_output=True, text=True,
                           env=plain_env(self.root), timeout=60)
        self.assertEqual(9, r.returncode, r.stderr)
        pending = cli(self.caller, "pending")[1]["operations"]
        self.assertEqual([("crash_window_01", "dispatching")], [(o["op_key"], o["outcome"]) for o in pending],
                         "the dispatch was durable before the request left")
        self.assertEqual(200, self.gw.contact(self.bot_id, actor["actor_id"], "revoke")[0])
        code, refused, _ = cli(self.caller, "resend", "crash_window_01")
        self.assertEqual((2, "contact_revoked", "unknown"), (code, refused["error"], refused["outcome"]))
        stored = [m["content"]["text"] for m in self.gw.owner_view(
            P.SentLog(P.State(self.caller)).get("crash_window_01")["conversation_id"])["messages"]]
        self.assertEqual(["committed, then crashed"], stored, "it was in fact stored: 'unknown' was the honest answer")

    def test_a_lookup_never_overwrites_a_concurrent_confirmation(self):
        proxy = DroppingProxy(self.gw.port)
        self.addCleanup(proxy.close)
        self.enroll(proxy.url)
        proxy.mode = "drop_request"
        op = cli(self.caller, "chat", "assistant", "never arrived at first")[1]["op_key"]
        proxy.mode = "pass"
        real, resent = C.api_call, []

        def lookup_then_concurrent_resend(*args, **kwargs):
            result = real(*args, **kwargs)
            if args[1] == "GET" and "/operations/" in args[2] and not resent:
                C.api_call = real                   # another process resends and commits meanwhile
                resent.append(cli(self.caller, "resend", op))
            return result
        C.api_call = lookup_then_concurrent_resend
        try:
            code, status, _ = cli(self.caller, "status", "--op-key", op)
        finally:
            C.api_call = real
        self.assertEqual(0, resent[0][0])
        self.assertEqual((0, True, resent[0][1]["message_id"]), (code, status["stored"], status["message_id"]),
                         "the authorized 'not found' predates the commit and must not win")
        self.assertEqual("stored", P.SentLog(P.State(self.caller)).get(op)["outcome"])

    def test_a_lookup_during_the_same_in_flight_post_changes_nothing(self):
        proxy = DroppingProxy(self.gw.port)
        self.addCleanup(proxy.close)
        actor = self.enroll(proxy.url)
        proxy.mode = "hold"
        sender = subprocess.Popen([sys.executable, str(PUBLIC / "bot_client.py"), "--home", str(self.caller), "chat",
                                   "assistant", "slow but real", "--op-key", "inflight_op_01"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=plain_env(self.root), text=True)
        self.addCleanup(lambda: sender.poll() is None and sender.kill())
        self.assertTrue(proxy.holding.wait(20), "the POST reached the proxy")
        self.assertEqual("dispatching", P.SentLog(P.State(self.caller)).get("inflight_op_01")["outcome"])
        code, status, _ = cli(self.caller, "status", "--op-key", "inflight_op_01")
        self.assertEqual((0, "not_found_now", "dispatching"), (code, status["stored"], status["outcome"]))
        self.assertEqual("dispatching", P.SentLog(P.State(self.caller)).get("inflight_op_01")["outcome"],
                         "the observation did not make the in-flight op look clean")
        proxy.release.set()
        out, err = sender.communicate(timeout=30)
        self.assertEqual(0, sender.returncode, err)
        record = P.SentLog(P.State(self.caller)).get("inflight_op_01")
        self.assertEqual(("stored", json.loads(out)["message_id"]), (record["outcome"], record["message_id"]))
        # once stored, a later refusal can never downgrade it
        self.gw.contact(self.bot_id, actor["actor_id"], "revoke")
        code, refused, _ = cli(self.caller, "resend", "inflight_op_01")
        self.assertEqual((2, "stored"), (code, refused["outcome"]))

    def test_open_outcome_quota_is_checked_before_any_request(self):
        self.enroll()
        log = P.SentLog(P.State(self.caller))
        for i in range(P.SentLog.MAX_OPEN):
            log.claim({"op_key": "open_op_%04d" % i, "gateway": self.gw.url, "bot_id": self.bot_id,
                       "alias": "assistant", "conversation_id": "cnv_aaaaaaaaaaaaaaaa", "kind": "faq",
                       "payload": {"kind": "faq", "text": str(i)}, "payload_hash": str(i), "sent_at": C.iso(0)})
        before = self.gw.ok("GET", "/v1/owner/bots/%s/conversations" % self.bot_id, None, self.gw.owner_token)
        code, out, err = cli(self.caller, "chat", "assistant", "one more", "--new-conversation")
        self.assertEqual(2, code)
        self.assertIn("Nothing was sent", err)
        self.assertEqual(before, self.gw.ok("GET", "/v1/owner/bots/%s/conversations" % self.bot_id, None,
                                            self.gw.owner_token), "not even a conversation was opened")


class SentLogTests(ClientFlowCase):
    def test_large_multibyte_history_survives_reopen_and_pruning_keeps_open_operations(self):
        self.enroll()
        big = "漢字テキスト" * 666                       # 3996 characters, about 12 KB as UTF-8
        ops = []
        for i in range(130):
            code, sent, err = cli(self.caller, "chat", "assistant", "%03d %s" % (i, big))
            self.assertEqual(0, code, err)
            ops.append(sent["op_key"])
        first = {"op_key": ops[0], "message_id": P.SentLog(P.State(self.caller)).get(ops[0])["message_id"]}
        status = cli_process(self.caller, "status", "--op-key", first["op_key"], env=plain_env(self.root))
        self.assertEqual(0, status.returncode, status.stderr)
        self.assertEqual(first["message_id"], json.loads(status.stdout)["message_id"])
        self.assertGreater((self.caller / "public" / "sent.sqlite3").stat().st_size, 1024 * 1024)
        self.assertEqual(0o600, os.stat(self.caller / "public" / "sent.sqlite3").st_mode & 0o777)
        # resolved rows are pruned oldest first; open (unknown) rows never are
        log = P.SentLog(P.State(self.caller))
        log.db.execute("UPDATE ops SET outcome='unknown', message_id=NULL WHERE op_key=?", (first["op_key"],))
        old_keep = P.SentLog.KEEP_RESOLVED
        P.SentLog.KEEP_RESOLVED = 5
        try:
            code, latest, _ = cli(self.caller, "chat", "assistant", "after pruning")
        finally:
            P.SentLog.KEEP_RESOLVED = old_keep
        rows = [r[0] for r in log.db.execute("SELECT op_key FROM ops ORDER BY seq")]
        self.assertEqual([first["op_key"]] + ops[-5:] + [latest["op_key"]], rows,
                         "the open operation survives; resolved rows are pruned oldest first, by insertion order")

    def test_sends_stop_before_the_network_when_too_many_outcomes_are_open(self):
        proxy = DroppingProxy(self.gw.port)
        self.addCleanup(proxy.close)
        self.enroll(proxy.url)
        proxy.mode = "drop_request"
        old = P.SentLog.MAX_OPEN
        P.SentLog.MAX_OPEN = 2
        try:
            for i in range(2):
                self.assertEqual("unknown_outcome", cli(self.caller, "chat", "assistant", "lost %d" % i)[1]["error"])
            proxy.mode = "pass"
            forwarded = proxy.forwarded
            code, out, err = cli(self.caller, "chat", "assistant", "one more")
        finally:
            P.SentLog.MAX_OPEN = old
        self.assertEqual(2, code)
        self.assertIn("open outcome", err)
        self.assertIn("Nothing was sent", err)
        self.assertEqual(forwarded, proxy.forwarded, "refused before any request left this machine")


class LocalStateTests(ClientFlowCase):
    def test_concurrent_processes_never_lose_contact_updates(self):
        self.enroll()
        for round_ in range(3):
            aliases = ["c%d-%d" % (round_, i) for i in range(4)]
            procs = [subprocess.Popen([sys.executable, str(PUBLIC / "bot_client.py"), "--home", str(self.caller), "add",
                                       self.gw.url + "/@assistant", "--alias", alias],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=plain_env(self.root))
                     for alias in aliases]
            for proc in procs:
                proc.communicate(timeout=60)
                self.assertEqual(0, proc.returncode)
            saved = json.loads((self.caller / "public" / "contacts.json").read_text())["contacts"]
            self.assertTrue(set(aliases) <= set(saved), (aliases, sorted(saved)))

    def test_state_files_are_private_and_outputs_never_contain_credentials(self):
        self.enroll()
        outputs = [cli_process(self.caller, *args, env=plain_env(self.root))
                   for args in (["whoami"] if False else ["contacts"], ["relationship", "assistant"],
                                ["chat", "assistant", "hello", "--faq"], ["pending"])]
        for r in outputs:
            self.assertIsNone(TOKEN_PATTERN.search(r.stdout + r.stderr), r.stdout)
        self.assertEqual(0o700, os.stat(self.caller / "public").st_mode & 0o777)
        for path in (self.caller / "public").iterdir():
            if path.is_file():
                self.assertEqual(0o600, path.stat().st_mode & 0o777, path)
        os.chmod(self.caller / "public" / "credentials.json", 0o644)
        code, out, err = cli(self.caller, "contacts")
        self.assertEqual(0, code, "contacts.json is still readable")
        code, out, err = cli(self.caller, "relationship", "assistant")
        self.assertEqual(2, code)
        self.assertIn("must be mode 0600", err)


class WorkerExclusivityTests(ClientFlowCase):
    def worker_home(self, name="device"):
        home = self.root / name
        token_file = self.root / ("%s.token" % name)
        C.create_private_file(token_file, self.gw.worker(self.bot_id) + "\n")
        self.assertEqual(0, cli(home, "worker", "login", self.gw.url, "--token-file", str(token_file))[0])
        self.assertEqual(0, cli(home, "responder", "init", self.bot_id)[0])
        return home

    def test_a_second_worker_process_cannot_take_over_live_claims(self):
        actor = self.enroll()
        cli(self.caller, "chat", "assistant", "hours?", "--faq")
        home = self.worker_home()
        code, claimed, _ = cli(home, "worker", "claim", self.bot_id)
        mid = claimed["items"][0]["message_id"]
        self.assertEqual(0, cli(home, "worker", "start", self.bot_id, mid)[0])
        holder = subprocess.Popen([sys.executable, "-c", (
            "import sys, time\nsys.path.insert(0, %r)\nimport bot_common as C\n"
            "with C.FileLock(%r, blocking=False):\n    print('held', flush=True)\n    time.sleep(30)\n")
            % (str(PUBLIC), str(home / "public" / (".worker-%s.lock" % self.bot_id)))],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.kill)
        self.assertEqual("held", holder.stdout.readline().strip())
        for args in (["worker", "run", self.bot_id, "--once"], ["worker", "claim", self.bot_id],
                     ["worker", "release", self.bot_id, mid], ["worker", "respond", self.bot_id, mid]):
            r = cli_process(home, *args, env=plain_env(self.root))
            self.assertEqual(2, r.returncode, args)
            self.assertIn("another worker process is active", r.stderr)
        holder.kill()
        holder.wait()
        self.assertEqual("started", self.gw.owner_view(claimed["items"][0]["conversation_id"])["messages"][0]["state"],
                         "the live claim was neither released nor reprocessed")
        code, done, _ = cli(home, "worker", "respond", self.bot_id, mid)
        self.assertEqual((0, "completed"), (code, done["outcome"]))

    def test_a_locally_started_lease_is_revalidated_before_anything_is_computed(self):
        actor = self.enroll()
        code, sent, _ = cli(self.caller, "chat", "assistant", "hours?", "--faq")
        home = self.worker_home()
        cli(home, "worker", "claim", self.bot_id)
        self.assertEqual(0, cli(home, "worker", "start", self.bot_id, sent["message_id"])[0])
        self.assertEqual(200, self.gw.contact(self.bot_id, actor["actor_id"], "revoke")[0])
        code, res, _ = cli(home, "worker", "respond", self.bot_id, sent["message_id"])
        self.assertEqual((False, "start", 409), (res["ok"], res["step"], res["http_status"]))
        self.assertFalse((home / "public" / "worker-inbox").exists(), "revoked content was never written locally")
        self.assertEqual({}, json.loads((home / "public" / "leases.json").read_text())["leases"])


if __name__ == "__main__":
    unittest.main()
