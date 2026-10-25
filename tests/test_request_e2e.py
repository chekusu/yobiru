"""Request lifecycle through real HTTP, isolated homes, and fake Tailscale only.

The source runtime is used so these tests also run before release artifacts are
regenerated. Each receiver has its own home, process, and loopback-only port.
No test uses the user's environment, real Tailscale, or the production port.
"""
import http.client
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
from http.server import BaseHTTPRequestHandler
from support.numeric_http import ThreadingHTTPServer
from pathlib import Path

from support.pkg import REPO, SUPPORT, free_port, python_env


def node(name, peer):
    return {"BackendState": "Running", "Self": {
        "ID": name + "-stable", "DNSName": "%s.example.ts.net." % name, "HostName": name,
        "TailscaleIPs": ["127.0.0.1"], "Online": True}, "Peer": {"p1": {
            "DNSName": "%s.example.ts.net." % peer, "HostName": peer,
            "TailscaleIPs": ["127.0.0.1"], "Online": True}}}


def whois(name):
    return {"Node": {"Name": "%s.example.ts.net." % name,
                     "Hostinfo": {"Hostname": name}, "Tags": []},
            "UserProfile": {"LoginName": "%s@example.com" % name}}


class RequestWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="yobiru request e2e ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        user = self.root / "isolated user home"
        user.mkdir()
        no_tools = self.root / "empty PATH"
        no_tools.mkdir()
        self.env = python_env({"HOME": str(user), "PATH": str(no_tools),
                               "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
        self.port = {"alpha": free_port(), "bravo": free_port()}
        while self.port["alpha"] == self.port["bravo"]:
            self.port["bravo"] = free_port()
        self.home, self.processes, self.state = {}, {}, {}
        self.addCleanup(self.stop_all)
        for name, peer, agent in (("alpha", "bravo", "grok"), ("bravo", "alpha", "codex")):
            home = self.root / (name + " data")
            home.mkdir()
            self.home[name] = home
            state = self.root / ("fake-%s.json" % name)
            state.write_text(json.dumps({"status": node(name, peer), "whois": whois(peer),
                                         "calls": str(self.root / (name + " tailscale calls.jsonl"))}))
            self.state[name] = state
            config = {"port": self.port[name], "bind": "127.0.0.1", "access_policy": "tailnet",
                      "self_agent": agent, "default_agent": agent, "agents": {agent: {}},
                      "tailscale_cmd": [sys.executable, str(SUPPORT / "fake_tailscale.py"),
                                        "--state", str(state)]}
            (home / "config.json").write_text(json.dumps(config))
        for name in self.home:
            self.start(name)

    def command(self, name, *args):
        return [sys.executable, str(SUPPORT / "loopback_harness.py"), str(REPO / "tailmsg.py"),
                "--home", str(self.home[name])] + list(args)

    def start(self, name):
        self.assertNotIn(name, self.processes)
        log_path = self.root / (name + " serve.log")
        with log_path.open("a") as log:
            proc = subprocess.Popen(self.command(name, "serve"), cwd=str(self.root), env=self.env,
                                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        self.processes[name] = proc
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and proc.poll() is None:
            try:
                if self.http(name, "GET", "/health")[0] == 200:
                    return
            except OSError:
                pass
            time.sleep(0.05)
        self.fail("%s receiver did not start: %s" % (name, log_path.read_text()))

    def stop(self, name):
        proc = self.processes.pop(name, None)
        if proc is None:
            return
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)

    def stop_all(self):
        for name in list(self.processes):
            self.stop(name)

    def tm(self, name, *args):
        return subprocess.run(self.command(name, *args), cwd=str(self.root), env=self.env,
                              capture_output=True, text=True, timeout=20)

    def tm_json(self, name, *args):
        out = self.tm(name, *args)
        self.assertEqual(0, out.returncode, out.stdout + out.stderr)
        try:
            return json.loads(out.stdout)
        except ValueError:
            self.fail("not JSON: %r / %r" % (out.stdout, out.stderr))

    def http(self, name, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port[name], timeout=2)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        try:
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def address(self, name):
        agent = "grok" if name == "alpha" else "codex"
        return "%s:%d/%s" % (name, self.port[name], agent)

    def inbox(self, name):
        return [json.loads(path.read_text()) for path in (self.home[name] / "inbox").glob("*.json")]

    def rows(self, name, query, parameters=()):
        with sqlite3.connect(str(self.home[name] / "requests" / "ledger.sqlite3")) as database:
            database.row_factory = sqlite3.Row
            return [dict(row) for row in database.execute(query, parameters)]

    def status(self, name, mid, refresh=False):
        args = ["request", "status", mid, "--json"]
        if refresh:
            args.append("--refresh")
        return self.tm_json(name, *args)

    def await_state(self, name, mid, expected, timeout=10):
        deadline = time.monotonic() + timeout
        state = None
        while time.monotonic() < deadline:
            state = self.status(name, mid)
            if state["state"] == expected:
                return state
            time.sleep(0.1)
        self.fail("%s did not reach %s: %r" % (mid, expected, state))

    def send_request(self, timeout=900):
        out = self.tm_json("alpha", "request", "send", self.address("bravo"), "compute the answer",
                           "--timeout-seconds", str(timeout), "--wake", "none")
        self.assertEqual(202, out["status"])
        return out["id"]

    def test_receipt_running_final_and_query_survive_receiver_restart(self):
        mid = self.send_request()
        state = self.await_state("alpha", mid, "received")
        self.assertFalse(state["outcome_known"])
        self.assertEqual("request-receipt", state["response"]["kind"])
        self.assertEqual("none", state["response"]["wake"])

        premature = self.tm("bravo", "ack", mid)
        self.assertNotEqual(0, premature.returncode, "ack silently discarded an unfinished request")
        record = self.tm_json("bravo", "show", mid)
        self.assertNotIn("acked_at", record)

        self.tm_json("bravo", "request", "respond", mid, "started", "--state", "running")
        self.await_state("alpha", mid, "running")
        self.stop("bravo")
        self.start("bravo")
        self.assertEqual("running", self.status("alpha", mid, refresh=True)["state"])

        self.tm_json("bravo", "request", "respond", mid, "the answer is 42", "--state", "completed")
        self.stop("bravo")
        self.start("bravo")
        state = self.status("alpha", mid, refresh=True)
        self.assertEqual("completed", state["state"])
        self.assertTrue(state["outcome_known"])
        self.assertEqual("the answer is 42", state["response"]["text"])
        final_id = state["response"]["id"]
        repeated = self.http("alpha", "POST", "/msg", state["response"])
        self.assertEqual(200, repeated[0])
        self.assertTrue(repeated[1]["duplicate"])
        conflicting = dict(state["response"])
        conflicting["text"] = "same response ID with different content"
        self.assertEqual(409, self.http("alpha", "POST", "/msg", conflicting)[0])
        conflicting.update(id="later-final-0001", response={"state": "failed", "seq": 999})
        self.assertEqual(202, self.http("alpha", "POST", "/msg", conflicting)[0])
        self.assertEqual(final_id, self.status("alpha", mid)["response"]["id"])
        self.assertEqual([], self.rows("alpha", "SELECT id FROM response_outbox"), "response caused a reply loop")
        self.assertEqual(0, self.tm("bravo", "ack", mid).returncode)
        conflict = self.tm("bravo", "request", "respond", mid, "a different answer", "--state", "failed")
        self.assertNotEqual(0, conflict.returncode)
        self.assertEqual(final_id, self.status("alpha", mid, refresh=True)["response"]["id"])

    def test_final_is_queued_offline_and_retried_after_both_receivers_restart(self):
        mid = self.send_request()
        self.await_state("alpha", mid, "received")
        self.stop("alpha")
        self.tm_json("bravo", "request", "respond", mid, "durable offline result", "--state", "completed")
        pending = self.rows("bravo", "SELECT id,envelope,delivered_at FROM response_outbox WHERE request_id=?", (mid,))
        final_rows = [row for row in pending if json.loads(row["envelope"])["response"]["state"] == "completed"]
        self.assertEqual(1, len(final_rows))
        self.assertIsNone(final_rows[0]["delivered_at"])
        expected_id = final_rows[0]["id"]
        self.stop("bravo")
        self.start("bravo")
        self.start("alpha")
        state = self.await_state("alpha", mid, "completed", timeout=15)
        self.assertEqual(expected_id, state["response"]["id"])
        self.assertEqual("durable offline result", state["response"]["text"])
        events = self.rows("alpha", "SELECT id FROM response_events WHERE id=?", (expected_id,))
        self.assertEqual([{"id": expected_id}], events)
        # Idempotent local replay must preserve the response ID, not generate another result.
        self.tm_json("bravo", "request", "respond", mid, "durable offline result", "--state", "completed")
        replay = self.rows("bravo", "SELECT id FROM response_outbox WHERE request_id=?", (mid,))
        self.assertEqual(2, len(replay))  # one receipt, one final result

    def test_timeout_remains_unknown_then_accepts_a_late_final_result(self):
        mid = self.send_request(timeout=2)
        state = self.await_state("alpha", mid, "timed_out", timeout=5)
        self.assertFalse(state["outcome_known"])
        self.assertTrue(state["overdue"])
        remote = self.status("alpha", mid, refresh=True)
        self.assertEqual("timed_out", remote["state"])
        self.assertFalse(remote["outcome_known"])
        self.tm_json("bravo", "request", "respond", mid, "finished after deadline", "--state", "completed")
        state = self.await_state("alpha", mid, "completed")
        self.assertTrue(state["outcome_known"])
        self.assertTrue(state["late"])
        self.assertEqual("finished after deadline", state["response"]["text"])

    def test_existing_send_and_reply_commands_participate_in_request_lifecycle(self):
        sent = self.tm_json("alpha", "send", self.address("bravo"), "use the normal CLI", "--kind", "request",
                            "--wake", "none")
        mid = sent["id"]
        self.await_state("alpha", mid, "received")
        artifact = self.root / "final report.txt"
        expected = ("The verified result ✓\n" * 100).encode("utf-8")
        artifact.write_bytes(expected)
        config_path = self.home["bravo"] / "config.json"
        config = json.loads(config_path.read_text())
        config["max_msg_bytes"] = 1024
        config_path.write_text(json.dumps(config))
        oversized = self.tm("bravo", "reply", mid, "x" * 2048, "--file", str(artifact))
        self.assertNotEqual(0, oversized.returncode)
        self.assertEqual("received", self.status("alpha", mid, refresh=True)["state"])
        self.assertEqual(1, len(self.rows("bravo", "SELECT id FROM response_outbox WHERE request_id=?", (mid,))))
        self.tm_json("bravo", "reply", mid, "ordinary reply is the final result", "--file", str(artifact))
        artifact.write_text("changed after sending; the result must still expose its snapshot")
        state = self.await_state("alpha", mid, "completed")
        self.assertTrue(state["outcome_known"])
        self.assertEqual("ordinary reply is the final result", state["response"]["text"])
        response_id = state["response"]["id"]
        result = self.tm_json("alpha", "show", response_id)
        self.assertEqual(response_id, result["envelope"]["id"])
        fetched = self.tm("alpha", "fetch", response_id)
        self.assertEqual(0, fetched.returncode, fetched.stdout + fetched.stderr)
        downloaded = self.home["alpha"] / "inbox" / response_id / "files" / artifact.name
        self.assertEqual(expected, downloaded.read_bytes())
        self.assertEqual([], self.inbox("alpha"), "protocol result was routed to the ordinary agent inbox")

    def test_refresh_when_peer_offline_preserves_unknown_timeout_state(self):
        # Leave time to observe the receipt and stop the peer before its real
        # deadline; a two-second window can expire during hosted-runner scheduling.
        mid = self.send_request(timeout=30)
        self.await_state("alpha", mid, "received")
        self.stop("bravo")
        self.await_state("alpha", mid, "timed_out", timeout=35)
        state = self.status("alpha", mid, refresh=True)
        self.assertEqual("timed_out", state["state"])
        self.assertEqual("unavailable", state["refresh"])
        self.assertFalse(state["outcome_known"])
        self.assertIn("refresh_error", state)

    def test_notification_does_not_create_a_response_or_request_record(self):
        out = self.tm_json("alpha", "send", self.address("bravo"), "FYI only", "--kind", "notification", "--wake", "none")
        self.assertEqual(202, out["status"])
        self.assertEqual(0, self.tm("bravo", "ack", out["id"]).returncode)
        self.assertEqual([], self.rows("bravo", "SELECT id FROM requests"))
        self.assertEqual([], self.rows("bravo", "SELECT id FROM response_outbox"))
        self.assertEqual([], self.rows("alpha", "SELECT id FROM response_events"))
        self.assertEqual([], self.inbox("alpha"))

    def test_cross_peer_status_and_forged_response_are_denied(self):
        mid = self.send_request()
        received = self.await_state("alpha", mid, "received")["response"]
        invalid = dict(received)
        invalid.update(id="oversized-seq-01", text="not a valid final result", kind="request-result",
                       response={"state": "completed", "seq": 2 ** 100})
        status, _ = self.http("alpha", "POST", "/msg", invalid)
        self.assertEqual(400, status)
        self.assertEqual("received", self.status("alpha", mid)["state"])
        forged = dict(received)
        forged.update(id="forged-agent-001", from_agent="another-agent", text="forged result",
                      kind="request-result", response={"state": "completed", "seq": 999})
        status, _ = self.http("alpha", "POST", "/msg", forged)
        self.assertIn(status, (400, 403, 409))
        self.assertEqual("received", self.status("alpha", mid)["state"])

        # Authenticate the same loopback caller as a distinct tailnet peer by changing
        # fake LocalAPI state and restarting, thereby also clearing the whois cache.
        for name in ("alpha", "bravo"):
            self.stop(name)
            state = json.loads(self.state[name].read_text())
            state["whois"] = whois("charlie")
            self.state[name].write_text(json.dumps(state))
            self.start(name)
        status, body = self.http("bravo", "GET", "/requests/" + mid)
        self.assertEqual(404, status)
        self.assertNotIn("compute the answer", json.dumps(body))
        forged.update(id="forged-peer-0001", from_agent="codex")
        forged["from"] = "charlie.example.ts.net"
        status, _ = self.http("alpha", "POST", "/msg", forged)
        self.assertIn(status, (400, 403, 409))
        state = self.status("alpha", mid)
        self.assertEqual("received", state["state"])
        self.assertFalse(state["outcome_known"])

    def test_already_expired_request_never_wakes_a_generic_adapter(self):
        self.stop("bravo")
        wake_log = self.root / "expired wake.txt"
        adapter = self.root / "record_wake.py"
        adapter.write_text("from pathlib import Path\nPath(%r).write_text('executed')\n" % str(wake_log))
        config_path = self.home["bravo"] / "config.json"
        config = json.loads(config_path.read_text())
        config["agents"]["codex"]["wake_adapter"] = {"type": "exec", "argv": [sys.executable, str(adapter)]}
        config["wake_policy"] = {"default_max": "now", "peers": {}}
        config_path.write_text(json.dumps(config))
        self.start("bravo")
        mid = "expired-request-01"
        request = {"v": 1, "id": mid, "from": "alpha.example.ts.net", "to": "bravo.example.ts.net",
                   "ts": "2020-01-01T00:00:00Z", "kind": "request", "text": "do not start this expired work",
                   "agent": "codex", "from_agent": "grok", "wake": "now",
                   "request": {"timeout_seconds": 1, "reply_port": self.port["alpha"]}}
        status, _ = self.http("bravo", "POST", "/msg", request)
        self.assertEqual(202, status)
        status, body = self.http("bravo", "GET", "/requests/" + mid)
        self.assertEqual(200, status)
        self.assertEqual("timed_out", body["request"]["state"])
        self.assertFalse(body["request"]["outcome_known"])
        time.sleep(1.1)
        self.assertFalse(wake_log.exists(), "receiver started a generic adapter after the request deadline")
        manual_wake = self.tm("bravo", "wake", mid)
        self.assertNotEqual(0, manual_wake.returncode)
        self.assertFalse(wake_log.exists(), "manual wake bypassed the request deadline")

    def test_malformed_status_refresh_keeps_the_last_authenticated_state(self):
        mid = self.send_request()
        self.await_state("alpha", mid, "received")
        self.stop("bravo")
        payload = []

        class MalformedStatusHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(payload[0]).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", self.port["bravo"]), MalformedStatusHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for body in ({"ok": True, "request": None}, {"ok": True, "request": []},
                         {"ok": True, "request": {"response": 42}}):
                with self.subTest(body=body):
                    payload[:] = [body]
                    state = self.status("alpha", mid, refresh=True)
                    self.assertEqual("received", state["state"])
                    self.assertEqual("unavailable", state["refresh"])
                    self.assertFalse(state["outcome_known"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_legacy_receiver_cannot_silently_downgrade_a_request(self):
        self.stop("bravo")
        posted = []

        class LegacyHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"ok": True, "v": 1, "product": "yobiru", "version": "0.4.4",
                                   "node": "bravo.example.ts.net", "agents": []}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                posted.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                body = b'{"ok": true}'
                self.send_response(202)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", self.port["bravo"]), LegacyHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for send_args in (("request", "send"), ("send",)):
                with self.subTest(command=send_args):
                    args = list(send_args) + [self.address("bravo"), "a final result is required"]
                    if send_args == ("send",):
                        args += ["--kind", "request"]
                    out = self.tm("alpha", *args)
                    self.assertNotEqual(0, out.returncode, out.stdout + out.stderr)
                    self.assertRegex((out.stdout + out.stderr).lower(), "capabilit|upgrade|unsupported|support")
            self.assertEqual([], posted, "A tracked request was silently sent to a legacy receiver")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
