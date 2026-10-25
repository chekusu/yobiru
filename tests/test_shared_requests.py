"""One-way sharing over real HTTP with fake Tailscale and isolated local state.

Only the destination runs a receiver. Its socket.connect is instrumented to fail
and record every outbound attempt, so successful requests cannot accidentally
depend on a reverse route. No test can use real Tailscale or a production home.
"""
import copy
import contextlib
import datetime as dt
import hashlib
import http.client
import json
import os
import secrets
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler
from support.numeric_http import ThreadingHTTPServer
from pathlib import Path

from support.pkg import REPO, SUPPORT, free_port, python_env


ALPHA_ID = "shared-alpha-stable"
BRAVO_ID = "destination-bravo-stable"
CHARLIE_ID = "unrelated-charlie-stable"


def principal(stable_id):
    return "node:" + stable_id.encode("utf-8").hex()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def status_node(name, stable_id):
    return {"ID": stable_id, "DNSName": name + ".example.ts.net.", "HostName": name,
            "TailscaleIPs": ["127.0.0.1"], "Online": True, "UserID": 1001}


def identity(name="", stable_id=ALPHA_ID, address="127.0.0.1/32", user=1001):
    return {"Node": {"Name": name + ".example.ts.net." if name else "", "StableID": stable_id,
                     "ID": 101, "User": user, "Addresses": [address], "Hostinfo": {}, "Tags": []},
            "UserProfile": {"ID": user, "LoginName": "alpha@example.com"}}


HARNESS = r'''
import importlib.util
import ipaddress
import json
import os
import socket
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("tailmsg", sys.argv[1])
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
core.TAILNET_NETS.append(ipaddress.ip_network("127.0.0.0/8"))
if os.environ.get("TEST_CLOCK"):
    actual_now = core.now_utc
    def now():
        path = Path(os.environ["TEST_CLOCK"])
        return core.parse_iso(path.read_text().strip()) if path.exists() else actual_now()
    core.now_utc = now
if os.environ.get("TEST_FORBID_OUTBOUND"):
    def forbidden_connect(self, address):
        with open(os.environ["TEST_FORBID_OUTBOUND"], "a") as stream:
            stream.write(json.dumps(address) + "\n")
        raise OSError("test: destination must never initiate a reverse connection")
    socket.socket.connect = forbidden_connect
if os.environ.get("TEST_LOSE_RESPONSE"):
    original_request = core.http.client.HTTPConnection.request
    original_response = core.http.client.HTTPConnection.getresponse
    def request(self, method, url, *args, **kwargs):
        self._test_request = (method, url)
        return original_request(self, method, url, *args, **kwargs)
    def getresponse(self):
        result = original_response(self)
        marker = Path(os.environ["TEST_LOSE_RESPONSE"])
        if getattr(self, "_test_request", (None,))[0] == "PUT" and not marker.exists():
            marker.write_text("accepted upload response lost")
            result.read()
            self.close()
            raise ConnectionResetError("test: upload succeeded but its response was lost")
        return result
    core.http.client.HTTPConnection.request = request
    core.http.client.HTTPConnection.getresponse = getresponse
if os.environ.get("TEST_REPLACE_DESTINATION_AFTER"):
    # After the Nth response to a matching route, this machine's Tailscale now reports another stable
    # identity for the same destination address: the destination was replaced mid-flight.
    when_method, when_prefix, when_count = os.environ["TEST_REPLACE_DESTINATION_AFTER"].split(" ")
    replace_request = core.http.client.HTTPConnection.request
    replace_response = core.http.client.HTTPConnection.getresponse
    seen = []
    def request(self, method, url, *args, **kwargs):
        self._test_route = (method, url)
        return replace_request(self, method, url, *args, **kwargs)
    def getresponse(self):
        result = replace_response(self)
        method, url = getattr(self, "_test_route", (None, ""))
        if method == when_method and url.startswith(when_prefix):
            seen.append(url)
            if len(seen) == int(when_count):
                path = Path(os.environ["TEST_DESTINATION_STATE"])
                data = json.loads(path.read_text())
                data["whois"]["Node"]["StableID"] = os.environ["TEST_REPLACEMENT_ID"]
                path.write_text(json.dumps(data))
        return result
    core.http.client.HTTPConnection.request = request
    core.http.client.HTTPConnection.getresponse = getresponse
if os.environ.get("TEST_REQUEST_LOG"):
    log_request = core.http.client.HTTPConnection.request
    def request(self, method, url, *args, **kwargs):
        with open(os.environ["TEST_REQUEST_LOG"], "a") as stream:
            stream.write(json.dumps([method, url]) + "\n")
        return log_request(self, method, url, *args, **kwargs)
    core.http.client.HTTPConnection.request = request
if os.environ.get("TEST_REPLACE_DESTINATION_WHILE_HASHING"):
    # The destination is replaced while the saved upload snapshot is being hashed.
    real_sha256_file = core.sha256_file
    def sha256_file(path):
        result = real_sha256_file(path)
        if "sending" in Path(path).parts:
            state = Path(os.environ["TEST_DESTINATION_STATE"])
            data = json.loads(state.read_text())
            data["whois"]["Node"]["StableID"] = os.environ["TEST_REPLACEMENT_ID"]
            state.write_text(json.dumps(data))
        return result
    core.sha256_file = sha256_file
if os.environ.get("TEST_UPLOAD_RECHECK_UNAVAILABLE"):
    # The receiver's identity lookup fails only for its recheck after an upload body was read.
    real_fresh_whois = core.fresh_whois
    def fresh_whois(ctx, ip):
        if Path(os.environ["TEST_UPLOAD_RECHECK_UNAVAILABLE"]).exists() and sys._getframe(1).f_code.co_name == "do_PUT":
            raise core.TailscaleUnavailable("test: identity lookup unavailable after the upload")
        return real_fresh_whois(ctx, ip)
    core.fresh_whois = fresh_whois
if os.environ.get("TEST_STAGE_FAULT"):
    # One failure while a pull request is staged or saved; the other fault names need no patch.
    fault = os.environ["TEST_STAGE_FAULT"]
    if fault == "second_copy":
        real_private_open, opened = core.private_open, []
        def private_open(path, *args, **kwargs):
            if "sending" in Path(path).parts:
                opened.append(path)
                if len(opened) == 2:
                    raise OSError("test: the second snapshot cannot be written")
            return real_private_open(path, *args, **kwargs)
        core.private_open = private_open
    elif fault == "fsync":
        real_fsync = core.os.fsync
        def fsync(fd):
            if sys._getframe(1).f_code.co_name == "prepare_pull_attachments":
                raise OSError("test: a snapshot fsync failed")
            return real_fsync(fd)
        core.os.fsync = fsync
    elif fault == "validation":
        real_validate_env = core.validate_env
        core.validate_env = lambda env: "test: rejected after staging" if core.is_pull_request(env) else real_validate_env(env)
    elif fault == "save_before":
        def save_outgoing_request(*args, **kwargs):
            raise OSError("test: failed before the request was saved")
        core.save_outgoing_request = save_outgoing_request
    elif fault.startswith("save_after"):
        real_save = core.save_outgoing_request
        def save_outgoing_request(*args, **kwargs):
            real_save(*args, **kwargs)
            if fault == "save_after_unreadable":
                def unavailable(*args, **kwargs):
                    raise core.sqlite3.OperationalError("test: ledger unavailable")
                core.sqlite3.connect = unavailable
            raise OSError("test: failed after the request was saved")
        core.save_outgoing_request = save_outgoing_request
sys.argv = sys.argv[1:]
sys.exit(core.main())
'''


class SharedRequestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="yobiru one-way sharing ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        user_home = self.root / "isolated user"
        user_home.mkdir()
        empty_path = self.root / "empty PATH"
        empty_path.mkdir()
        self.env = python_env({"HOME": str(user_home), "PATH": str(empty_path), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
        self.env["TEST_CLOCK"] = str(self.root / "test clock")
        self.harness = self.root / "one_way_harness.py"
        self.harness.write_text(HARNESS)
        self.reverse_attempts = self.root / "forbidden outbound.jsonl"
        self.homes, self.states = {}, {}
        self.port = free_port()
        self.client_port = free_port()
        while self.client_port == self.port:
            self.client_port = free_port()
        self.server = None
        self.addCleanup(self.stop)
        for name, own_id, other, other_id, port in (
                ("alpha", ALPHA_ID, "bravo", BRAVO_ID, self.client_port),
                ("bravo", BRAVO_ID, "alpha", ALPHA_ID, self.port)):
            home = self.root / (name + " data")
            home.mkdir()
            self.homes[name] = home
            # The destination's status has no route/name for the shared source.
            peers = {"peer": status_node(other, other_id)} if name == "alpha" else {}
            state = {"status": {"BackendState": "Running", "Self": status_node(name, own_id), "Peer": peers},
                     "whois": identity("bravo", BRAVO_ID) if name == "alpha" else identity(),
                     "calls": str(self.root / (name + " tailscale calls.jsonl"))}
            self.states[name] = self.root / (name + " tailscale.json")
            self.states[name].write_text(json.dumps(state))
            config = {"port": port, "bind": "127.0.0.1", "access_policy": "tailnet",
                      "self_agent": "worker", "default_agent": "worker", "agents": {"worker": {}},
                      "tailscale_cmd": [sys.executable, str(SUPPORT / "fake_tailscale.py"), "--state", str(self.states[name])]}
            (home / "config.json").write_text(json.dumps(config))
        self.start()

    def command(self, name, *args):
        return [sys.executable, str(self.harness), str(REPO / "tailmsg.py"), "--home", str(self.homes[name])] + list(args)

    def environment(self, name, extra=None):
        env = dict(self.env)
        if name == "bravo":
            env["TEST_FORBID_OUTBOUND"] = str(self.reverse_attempts)
        env.update(extra or {})
        return env

    def start(self):
        self.assertIsNone(self.server)
        log_path = self.root / "receiver.log"
        with log_path.open("a") as stream:
            self.server = subprocess.Popen(self.command("bravo", "serve"), cwd=str(self.root),
                                           env=self.environment("bravo", getattr(self, "receiver_extra", None)),
                                           stdout=stream,
                                           stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + 10
        while self.server.poll() is None and time.monotonic() < deadline:
            try:
                if self.http("GET", "/health")[0] == 200:
                    return
            except OSError:
                pass
            time.sleep(0.05)
        self.fail("destination receiver did not start: " + log_path.read_text())

    def stop(self):
        proc, self.server = self.server, None
        if proc is not None:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)

    def restart(self):
        self.stop()
        self.start()

    def tm(self, name, *args, extra=None):
        return subprocess.run(self.command(name, *args), cwd=str(self.root), env=self.environment(name, extra),
                              capture_output=True, text=True, timeout=20)

    def tm_json(self, name, *args, extra=None):
        result = self.tm(name, *args, extra=extra)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def http(self, method, path, body=None, headers=None):
        headers = dict(headers or {})
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            headers.setdefault("Content-Type", "application/json")
        elif body is not None:
            headers.setdefault("Content-Type", "application/octet-stream")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            content = response.read()
            if "json" in (response.getheader("Content-Type") or ""):
                content = json.loads(content)
            return response.status, content
        finally:
            connection.close()

    def address(self):
        return "bravo:%d/worker" % self.port

    def state(self, mid, refresh=True):
        args = ["request", "status", mid, "--json"]
        if refresh:
            args.append("--refresh")
        return self.tm_json("alpha", *args)

    def request(self, *files):
        args = ["request", "send", self.address(), "Inspect the uploaded sample", "--transport", "pull", "--wake", "none"]
        for path in files:
            args += ["--file", str(path)]
        result = self.tm_json("alpha", *args)
        self.assertEqual(202, result["status"])
        return result["id"]

    def rows(self, name, sql, args=()):
        with contextlib.closing(sqlite3.connect(str(self.homes[name] / "requests" / "ledger.sqlite3"))) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(sql, args)]

    def set_identity(self, whois):
        state = json.loads(self.states["bravo"].read_text())
        state["whois"] = whois
        self.states["bravo"].write_text(json.dumps(state))
        self.restart()  # also clears the authentication cache

    def no_reverse_connections(self):
        self.assertFalse(self.reverse_attempts.exists(), self.reverse_attempts.read_text() if self.reverse_attempts.exists() else "")
        self.assertEqual([], self.rows("bravo", "SELECT id FROM response_outbox"))

    def envelope(self, data=b"sample", name="sample.bin"):
        mid, token = secrets.token_hex(8), secrets.token_urlsafe(32)
        return {"v": 1, "id": mid, "from": principal(ALPHA_ID), "to": "bravo.example.ts.net",
                "ts": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "kind": "request", "text": "Inspect staged sample", "agent": "worker", "from_agent": "worker", "wake": "none",
                "request": {"timeout_seconds": 900, "reply_port": self.client_port, "transport": "pull",
                            "uploads": [{"token": token, "name": name, "sha256": digest(data), "size": len(data),
                                         "mime": "application/octet-stream"}]}}

    def upload_path(self, env):
        return "/requests/%s/uploads/%s" % (env["id"], env["request"]["uploads"][0]["token"])

    def final_with_file(self):
        mid = self.request()
        artifact = self.root / "private result.txt"
        data = b"Private result for the original shared node.\n" * 100
        artifact.write_bytes(data)
        self.tm_json("bravo", "reply", mid, "Finished", "--file", str(artifact))
        response = self.state(mid)["response"]
        return mid, response, data

    def test_nameless_shared_peer_round_trip_has_no_client_receiver_or_reverse_connections(self):
        source = self.root / "input sample.bin"
        data = bytes(range(256)) * 1024 + "Shared request \u2713".encode()
        source.write_bytes(data)
        mid = self.request(source)
        source.write_bytes(b"changed after upload")
        record = self.tm_json("bravo", "show", mid)
        self.assertEqual(principal(ALPHA_ID), record["envelope"]["from"])
        self.assertEqual(ALPHA_ID, record["received"]["peer"]["stable_id"])
        fetched = self.tm("bravo", "fetch", mid)
        self.assertEqual(0, fetched.returncode, fetched.stdout + fetched.stderr)
        received = self.homes["bravo"] / "inbox" / mid / "files" / source.name
        self.assertEqual(digest(data), digest(received.read_bytes()))
        self.assertEqual("received", self.state(mid)["state"])
        self.tm_json("bravo", "request", "respond", mid, "Working on the uploaded data", "--state", "running")
        self.assertEqual("running", self.state(mid)["state"])
        self.restart()
        self.assertEqual("running", self.state(mid)["state"])
        artifact = self.root / "verified result.txt"
        result_data = "The result from one-way sharing \u2713\n".encode() * 4096
        artifact.write_bytes(result_data)
        self.tm_json("bravo", "reply", mid, "Completed with the verified result", "--file", str(artifact))
        artifact.write_bytes(b"edited after response snapshot")
        self.restart()
        state = self.state(mid)
        self.assertEqual("completed", state["state"])
        self.assertTrue(state["outcome_known"])
        self.assertEqual("Completed with the verified result", state["response"]["text"])
        response_id = state["response"]["id"]
        fetched = self.tm("alpha", "fetch", response_id)
        self.assertEqual(0, fetched.returncode, fetched.stdout + fetched.stderr)
        downloaded = self.homes["alpha"] / "inbox" / response_id / "files" / artifact.name
        self.assertEqual(len(result_data), downloaded.stat().st_size)
        self.assertEqual(digest(result_data), digest(downloaded.read_bytes()))
        self.assertEqual([], list((self.homes["alpha"] / "inbox").glob("*.json")))
        self.no_reverse_connections()

    def test_resume_after_lost_upload_response_and_restart_preserves_id_and_snapshot(self):
        source = self.root / "resume snapshot.bin"
        data = b"immutable upload snapshot\x00" * 8192
        source.write_bytes(data)
        marker = self.root / "lost response marker"
        failed = self.tm("alpha", "request", "send", self.address(), "Resume safely", "--transport", "pull",
                         "--wake", "none", "--file", str(source), extra={"TEST_LOSE_RESPONSE": str(marker)})
        self.assertNotEqual(0, failed.returncode, failed.stdout + failed.stderr)
        self.assertTrue(marker.exists(), failed.stdout + failed.stderr)
        rows = self.rows("alpha", "SELECT id FROM requests WHERE direction='outgoing'")
        self.assertEqual(1, len(rows))
        mid = rows[0]["id"]
        source.write_bytes(b"source was changed before resuming")
        self.restart()
        resumed = self.tm_json("alpha", "request", "resume", mid)
        self.assertIn(resumed["status"], (200, 202))
        self.assertEqual(mid, resumed["id"])
        repeated = self.tm_json("alpha", "request", "resume", mid)
        self.assertEqual(mid, repeated["id"])
        self.assertEqual(1, len(list((self.homes["bravo"] / "inbox").glob("*.json"))))
        self.assertEqual("received", self.state(mid)["state"])
        self.assertEqual(data, (self.homes["bravo"] / "inbox" / mid / "files" / source.name).read_bytes())
        self.no_reverse_connections()

    def replaced_midflight(self, after, files):
        """Send a pull request whose destination identity changes after `after` (method, route prefix, count)."""
        sources = []
        for index in range(files):
            source = self.root / ("private input %d.txt" % index)
            source.write_bytes(b"SYNTHETIC-PRIVATE input %d for the original receiver only\n" % index)
            sources.append(source)
        args = ["request", "send", self.address(), "Midflight replacement", "--transport", "pull", "--wake", "none"]
        for source in sources:
            args += ["--file", str(source)]
        failed = self.tm("alpha", *args, extra={"TEST_REPLACE_DESTINATION_AFTER": " ".join(after),
                                                "TEST_DESTINATION_STATE": str(self.states["alpha"]),
                                                "TEST_REPLACEMENT_ID": CHARLIE_ID})
        self.assertNotEqual(0, failed.returncode, failed.stdout + failed.stderr)
        self.assertIn("request destination stable identity changed", failed.stderr)
        self.assertIn("request resume", failed.stderr)
        mid = self.rows("alpha", "SELECT id FROM requests WHERE direction='outgoing'")[0]["id"]
        self.assertEqual([{"submission": "unknown"}],
                         self.rows("alpha", "SELECT submission FROM requests WHERE direction='outgoing'"))
        return mid, sources

    def restore_destination(self):
        state = json.loads(self.states["alpha"].read_text())
        state["whois"] = identity("bravo", BRAVO_ID)
        self.states["alpha"].write_text(json.dumps(state))

    def uploaded(self, mid):
        directory = self.homes["bravo"] / "requests" / "uploads" / mid
        return sorted(p.name for p in directory.iterdir() if ".part-" not in p.name) if directory.is_dir() else []

    def assert_resumes_with_the_same_id(self, mid, sources):
        self.assertFalse((self.homes["bravo"] / "inbox" / (mid + ".json")).exists(), "nothing was committed")
        self.restore_destination()
        resumed = self.tm_json("alpha", "request", "resume", mid)
        self.assertEqual((mid, 202), (resumed["id"], resumed["status"]))
        for source in sources:
            self.assertEqual(source.read_bytes(),
                             (self.homes["bravo"] / "inbox" / mid / "files" / source.name).read_bytes())
        self.assertEqual(1, len(list((self.homes["bravo"] / "inbox").glob("*.json"))))

    def test_destination_replaced_after_prepare_receives_no_input_and_the_same_request_resumes(self):
        mid, sources = self.replaced_midflight(("POST", "/requests/prepare", "1"), files=1)
        self.assertEqual([], self.uploaded(mid), "a private input was uploaded after the destination changed")
        self.assert_resumes_with_the_same_id(mid, sources)

    def test_destination_replaced_between_uploads_receives_no_further_input(self):
        mid, sources = self.replaced_midflight(("PUT", "/requests/", "1"), files=2)
        self.assertEqual(1, len(self.uploaded(mid)), "only the upload before the change reached the receiver")
        self.assert_resumes_with_the_same_id(mid, sources)

    def test_destination_replaced_after_the_last_upload_is_not_committed(self):
        mid, sources = self.replaced_midflight(("PUT", "/requests/", "1"), files=1)
        self.assertEqual(1, len(self.uploaded(mid)))
        self.assert_resumes_with_the_same_id(mid, sources)

    def test_destination_replaced_while_hashing_the_snapshot_never_opens_an_upload_connection(self):
        source = self.root / "large private input.bin"
        source.write_bytes(b"SYNTHETIC-PRIVATE " * 65536)
        requests = self.root / "sender requests.jsonl"
        failed = self.tm("alpha", "request", "send", self.address(), "Hash window", "--transport", "pull",
                         "--wake", "none", "--file", str(source),
                         extra={"TEST_REPLACE_DESTINATION_WHILE_HASHING": "1", "TEST_REQUEST_LOG": str(requests),
                                "TEST_DESTINATION_STATE": str(self.states["alpha"]), "TEST_REPLACEMENT_ID": CHARLIE_ID})
        self.assertNotEqual(0, failed.returncode, failed.stdout + failed.stderr)
        self.assertIn("request destination stable identity changed", failed.stderr)
        sent = [tuple(json.loads(line)) for line in requests.read_text().splitlines()]
        self.assertEqual([], [r for r in sent if r[0] == "PUT"], "no upload connection after the identity changed")
        self.assertIn(("POST", "/requests/prepare"), sent)
        mid = self.rows("alpha", "SELECT id FROM requests WHERE direction='outgoing'")[0]["id"]
        self.assertEqual([], self.uploaded(mid))
        upload_dir = self.homes["bravo"] / "requests" / "uploads" / mid
        self.assertEqual([], list(upload_dir.iterdir()) if upload_dir.is_dir() else [], "no upload bytes at all")
        self.assert_resumes_with_the_same_id(mid, [source])

    def test_identity_outage_after_an_upload_answers_503_and_the_same_request_resumes(self):
        marker = self.root / "identity outage"
        marker.write_text("on")
        self.receiver_extra = {"TEST_UPLOAD_RECHECK_UNAVAILABLE": str(marker)}
        self.restart()
        source = self.root / "input.txt"
        source.write_bytes(b"SYNTHETIC input\n")
        failed = self.tm("alpha", "request", "send", self.address(), "Outage", "--transport", "pull",
                         "--wake", "none", "--file", str(source))
        self.assertNotEqual(0, failed.returncode, failed.stdout + failed.stderr)
        self.assertIn("HTTP 503", failed.stderr)
        self.assertIn("request resume", failed.stderr)
        mid = self.rows("alpha", "SELECT id FROM requests WHERE direction='outgoing'")[0]["id"]
        self.assertEqual([], self.uploaded(mid), "the upload was discarded, not kept")
        marker.unlink()                                   # the lookup recovers
        resumed = self.tm_json("alpha", "request", "resume", mid)
        self.assertEqual((mid, 202), (resumed["id"], resumed["status"]))
        self.assertEqual(source.read_bytes(), (self.homes["bravo"] / "inbox" / mid / "files" / source.name).read_bytes())

    def test_pull_inputs_and_results_stay_private_under_umask_022(self):
        def mode(path):
            return oct(path.stat().st_mode & 0o777)
        previous = os.umask(0o022)
        try:
            self.restart()                                 # the receiver runs with umask 022 as well
            source = self.root / "private input.txt"
            source.write_bytes(b"SYNTHETIC-PRIVATE input\n")
            mid = self.request(source)
            artifact = self.root / "private result.txt"
            artifact.write_bytes(b"SYNTHETIC-PRIVATE result\n")
            self.tm_json("bravo", "reply", mid, "Finished", "--file", str(artifact))
            response = self.state(mid)["response"]
            fetched = self.tm("alpha", "fetch", response["id"])
            self.assertEqual(0, fetched.returncode, fetched.stdout + fetched.stderr)
        finally:
            os.umask(previous)
        alpha, bravo = self.homes["alpha"], self.homes["bravo"]
        sending = alpha / "requests" / "sending" / mid
        uploads = bravo / "requests" / "uploads" / mid
        results = [p for p in (bravo / "outbox").glob("*/files") if p.is_dir()]
        self.assertEqual(1, len(results))
        downloads = alpha / "inbox" / response["id"] / "files"
        for directory in (sending, uploads, bravo / "inbox" / mid, bravo / "inbox" / mid / "files", results[0],
                          results[0].parent, downloads.parent, downloads):
            self.assertEqual("0o700", mode(directory), directory)
        files = list(sending.iterdir()) + list(uploads.iterdir()) + list((bravo / "inbox" / mid / "files").iterdir())
        files += list(results[0].iterdir()) + list(downloads.iterdir())
        self.assertEqual(5, len(files))
        for path in files:
            self.assertEqual("0o600", mode(path), path)

    def test_identical_reply_retry_with_files_is_idempotent_and_leaves_no_copy(self):
        # Reported in the PR #19 discussion: an identical `reply --file` retry was refused and left a
        # persistent copy that no sweep removes.
        mid = self.request()
        artifact = self.root / "result.txt"
        original = b"same bytes\n" * 100
        artifact.write_bytes(original)
        outbox = self.homes["bravo"] / "outbox"
        first = self.tm_json("bravo", "reply", mid, "Finished", "--file", str(artifact))
        copies = sorted(outbox.iterdir())
        self.assertEqual(1, len(copies))
        self.assertEqual(first, self.tm_json("bravo", "reply", mid, "Finished", "--file", str(artifact)))
        artifact.write_bytes(b"different bytes")
        changed = self.tm("bravo", "reply", mid, "Finished", "--file", str(artifact))
        self.assertEqual(1, changed.returncode)
        self.assertIn("durable final result", changed.stderr)
        self.assertEqual(copies, sorted(outbox.iterdir()))
        response = self.state(mid)["response"]
        self.assertEqual(first["response_id"], response["id"])
        fetched = self.tm("alpha", "fetch", response["id"])
        self.assertEqual(0, fetched.returncode, fetched.stdout + fetched.stderr)
        self.assertEqual(original, (self.homes["alpha"] / "inbox" / response["id"] / "files" / "result.txt").read_bytes())

    def test_upload_requires_preparation_correct_token_complete_size_and_hash(self):
        data = b"validated staged upload"
        env = self.envelope(data)
        route = self.upload_path(env)
        self.assertIn(self.http("PUT", route, data)[0], (404, 409))
        self.assertIn(self.http("POST", "/requests/prepare", env)[0], (200, 201))
        self.assertEqual(200, self.http("POST", "/requests/prepare", env)[0])
        self.assertIn(self.http("POST", "/msg", env)[0], (400, 409))
        wrong = route.rsplit("/", 1)[0] + "/" + secrets.token_urlsafe(32)
        self.assertEqual(404, self.http("PUT", wrong, data)[0])
        for bad in (b"x" * len(data), data[:-1], data + b"x"):
            with self.subTest(upload_bytes=len(bad)):
                self.assertIn(self.http("PUT", route, bad)[0], (400, 409, 413, 422))
                self.assertFalse((self.homes["bravo"] / "inbox" / (env["id"] + ".json")).exists())
        self.assertIn(self.http("PUT", route, data)[0], (200, 201))
        self.assertEqual(200, self.http("PUT", route, data)[0])
        changed = copy.deepcopy(env)
        changed["request"]["uploads"][0]["sha256"] = "0" * 64
        self.assertEqual(409, self.http("POST", "/requests/prepare", changed)[0])
        self.assertEqual(202, self.http("POST", "/msg", env)[0])
        self.assertEqual(200, self.http("POST", "/msg", env)[0])
        self.assertEqual(data, (self.homes["bravo"] / "inbox" / env["id"] / "files" / "sample.bin").read_bytes())
        self.no_reverse_connections()

    def test_status_upload_and_result_are_bound_to_original_stable_identity(self):
        mid, response, secret = self.final_with_file()
        file_path = urllib.parse.urlparse(response["fetch"][0]["url"]).path
        staged = self.envelope()
        self.assertIn(self.http("POST", "/requests/prepare", staged)[0], (200, 201))
        self.set_identity(identity("alpha", CHARLIE_ID))  # same claimed DNS name, different authenticated node
        code, body = self.http("GET", "/requests/" + mid)
        self.assertEqual(404, code)
        self.assertNotIn("Finished", json.dumps(body))
        code, content = self.http("GET", file_path)
        self.assertEqual(404, code)
        self.assertNotEqual(secret, content)
        self.assertIn(self.http("PUT", self.upload_path(staged), b"sample")[0], (403, 404, 409))
        self.assertIn(self.http("POST", "/requests/prepare", staged)[0], (403, 409))
        self.set_identity(identity("renamed-alpha", ALPHA_ID))
        self.assertEqual(200, self.http("GET", "/requests/" + mid)[0])
        self.assertEqual((200, secret), self.http("GET", file_path))
        self.no_reverse_connections()

    def test_forged_principal_and_unverified_shared_identity_are_rejected(self):
        for sender in (principal(CHARLIE_ID), "alpha.example.ts.net", "bravo.example.ts.net"):
            with self.subTest(sender=sender):
                env = self.envelope()
                env["from"] = sender
                self.assertIn(self.http("POST", "/requests/prepare", env)[0], (400, 403))
        for raw in (identity(stable_id=""), identity(address="127.0.0.2/32"), {"Node": {"Name": ""}}):
            with self.subTest(identity=raw):
                self.set_identity(raw)
                self.assertEqual(403, self.http("GET", "/info")[0])
        self.assertEqual([], list((self.homes["bravo"] / "inbox").glob("*.json")))

    def test_a_pull_request_that_fails_before_it_is_saved_leaves_no_snapshot(self):
        # Until the outgoing request is durably saved, no ledger row, printed ID or resume path refers to its
        # snapshots: every earlier failure removes them. After the save they stay, even when the ledger then
        # cannot be read, and resume sends the original bytes although the source changed.
        config, ledger = self.homes["alpha"] / "config.json", self.homes["alpha"] / "requests" / "ledger.sqlite3"
        sending, original = self.homes["alpha"] / "requests" / "sending", config.read_text()
        one, two = self.root / "one.bin", self.root / "two.bin"

        def outgoing():
            return {row["id"] for row in self.rows("alpha", "SELECT id FROM requests WHERE direction='outgoing'")} \
                if ledger.exists() else set()
        for fault in ("oversize", "cumulative", "missing_later", "second_copy", "fsync", "validation",
                      "policy_denied", "save_before", "save_after", "save_after_unreadable"):
            with self.subTest(fault):
                cfg, files = json.loads(original), [one]
                one.write_bytes(b"SYNTHETIC private input\n")
                if fault in ("oversize", "cumulative"):
                    cfg["max_fetch_bytes"] = 1 << 20
                    one.write_bytes(b"X" * ((1 << 20) + 1 if fault == "oversize" else 700000))
                if fault in ("cumulative", "missing_later", "second_copy"):
                    files.append(two)
                    two.write_bytes(b"Y" * 700000 if fault == "cumulative" else b"SYNTHETIC second input\n")
                    if fault == "missing_later":
                        two.unlink()
                if fault == "policy_denied":
                    cfg.update(access_policy="allowlist", allow=[])
                config.write_text(json.dumps(cfg))
                staged_before = set(sending.iterdir()) if sending.exists() else set()
                saved_before = outgoing()
                args = ["request", "send", self.address(), "Inspect", "--transport", "pull", "--wake", "none"]
                for path in files:
                    args += ["--file", str(path)]
                failed = self.tm("alpha", *args, extra={"TEST_STAGE_FAULT": fault})
                config.write_text(original)
                self.assertNotEqual(0, failed.returncode, failed.stdout)
                staged = (set(sending.iterdir()) if sending.exists() else set()) - staged_before
                saved = outgoing() - saved_before
                if not fault.startswith("save_after"):
                    self.assertEqual((set(), set()), (staged, saved), failed.stderr)
                    continue
                self.assertEqual([1], [len(staged)], failed.stderr)
                [directory] = staged
                self.assertEqual({directory.name}, saved)
                one.write_bytes(b"changed after the snapshot\n")
                resumed = self.tm_json("alpha", "request", "resume", directory.name)
                self.assertEqual((directory.name, 202), (resumed["id"], resumed["status"]))
                received = self.homes["bravo"] / "inbox" / directory.name / "files" / "one.bin"
                self.assertEqual(b"SYNTHETIC private input\n", received.read_bytes())

    def test_push_and_pull_from_one_named_peer_share_its_wake_budget(self):
        # A named peer that can push and pull gets one hourly `now` budget per agent, in either order: the
        # receiver records a pull sender by its stable principal and a push sender by its DNS name.
        config = self.homes["bravo"] / "config.json"
        cfg = json.loads(config.read_text())
        cfg.update(wake_rate=1, wake_policy={"default_max": "now", "peers": {}})
        config.write_text(json.dumps(cfg))
        self.set_identity(identity("alpha", ALPHA_ID))                  # restarts with an empty hourly window

        def push():
            return self.tm_json("alpha", "send", self.address(), "Pushed note", "--wake", "now")["id"]

        def pull():
            return self.tm_json("alpha", "request", "send", self.address(), "Pulled request", "--transport", "pull",
                                "--wake", "now")["id"]

        def effective(mid):
            return json.loads((self.homes["bravo"] / "inbox" / (mid + ".json")).read_text())["received"]["wake"]["effective"]
        self.assertEqual(["now", "batch", "batch"], [effective(push()), effective(push()), effective(pull())])
        self.restart()                                                  # the in-memory window starts over
        self.assertEqual(["now", "batch", "batch"], [effective(pull()), effective(pull()), effective(push())])

    def test_poll_rejects_destination_replacement_with_same_dns_name(self):
        mid = self.request()
        self.assertEqual("received", self.state(mid)["state"])
        self.tm_json("bravo", "reply", mid, "Original destination completed the work")
        path = self.states["alpha"]
        state = json.loads(path.read_text())
        state["whois"] = identity("bravo", CHARLIE_ID)
        path.write_text(json.dumps(state))
        refreshed = self.state(mid)
        self.assertEqual("unavailable", refreshed["refresh"])
        self.assertEqual("received", refreshed["state"])
        self.assertFalse(refreshed["outcome_known"])
        self.assertIn("identity", refreshed["refresh_error"])
        state["whois"] = identity("bravo", BRAVO_ID)
        path.write_text(json.dumps(state))
        self.assertEqual("completed", self.state(mid)["state"])
        self.no_reverse_connections()

    def test_revoked_access_denies_status_and_file_even_with_valid_token(self):
        mid, response, _ = self.final_with_file()
        route = urllib.parse.urlparse(response["fetch"][0]["url"]).path
        staged = self.envelope()
        self.assertIn(self.http("POST", "/requests/prepare", staged)[0], (200, 201))
        path = self.homes["bravo"] / "config.json"
        cfg = json.loads(path.read_text())
        cfg["deny"] = ["user:alpha@example.com"]
        path.write_text(json.dumps(cfg))
        self.restart()
        for method, path, body in (("GET", "/requests/" + mid, None), ("GET", route, None),
                                   ("PUT", self.upload_path(staged), b"sample"),
                                   ("POST", "/requests/prepare", self.envelope())):
            with self.subTest(path=path):
                self.assertEqual(403, self.http(method, path, body)[0])
        self.no_reverse_connections()

    def test_removing_allowlist_entry_revokes_pull_file_token(self):
        path = self.homes["bravo"] / "config.json"
        cfg = json.loads(path.read_text())
        cfg.update(access_policy="allowlist", allow=["user:alpha@example.com"])
        path.write_text(json.dumps(cfg))
        self.restart()
        mid, response, expected = self.final_with_file()
        route = urllib.parse.urlparse(response["fetch"][0]["url"]).path
        self.assertEqual((200, expected), self.http("GET", route))
        cfg["allow"] = []
        path.write_text(json.dumps(cfg))
        self.restart()
        self.assertEqual(403, self.http("GET", "/requests/" + mid)[0])
        self.assertEqual(403, self.http("GET", route)[0])
        self.no_reverse_connections()

    def test_fetch_rejects_destination_replacement_with_same_dns_name(self):
        _, response, expected = self.final_with_file()
        item = response["fetch"][0]
        target = self.homes["alpha"] / "inbox" / response["id"] / "files" / item["name"]
        path = self.states["alpha"]
        state = json.loads(path.read_text())
        state["whois"] = identity("bravo", CHARLIE_ID)
        path.write_text(json.dumps(state))
        result = self.tm("alpha", "fetch", response["id"])
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("identity", (result.stdout + result.stderr).lower())
        self.assertFalse(target.exists())
        state["whois"] = identity("bravo", BRAVO_ID)
        path.write_text(json.dumps(state))
        result = self.tm("alpha", "fetch", response["id"])
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(digest(expected), digest(target.read_bytes()))
        self.no_reverse_connections()

    def test_reassigned_source_ip_cannot_use_cached_principal_without_restart(self):
        mid, response, secret = self.final_with_file()
        route = urllib.parse.urlparse(response["fetch"][0]["url"]).path
        staged = self.envelope()
        self.assertEqual(201, self.http("POST", "/requests/prepare", staged)[0])
        self.assertEqual(200, self.http("GET", "/requests/" + mid)[0])
        self.assertEqual((200, secret), self.http("GET", route))
        # Deliberately retain the running receiver and its freshly populated whois cache.
        path = self.states["bravo"]
        state = json.loads(path.read_text())
        state["whois"] = identity("alpha", CHARLIE_ID)
        path.write_text(json.dumps(state))
        self.assertEqual(404, self.http("GET", "/requests/" + mid)[0])
        self.assertEqual(404, self.http("GET", route)[0])
        self.assertEqual(404, self.http("PUT", self.upload_path(staged), b"sample")[0])
        self.assertEqual(403, self.http("POST", "/requests/prepare", staged)[0])
        self.assertEqual(403, self.http("POST", "/msg", staged)[0])
        self.assertFalse((self.homes["bravo"] / "inbox" / (staged["id"] + ".json")).exists())
        state["whois"] = identity("renamed-alpha", ALPHA_ID)
        path.write_text(json.dumps(state))
        self.assertEqual(200, self.http("GET", "/requests/" + mid)[0])
        self.assertEqual((200, secret), self.http("GET", route))
        self.no_reverse_connections()

    def test_expired_uncommitted_uploads_are_closed_and_late_commit_never_restarts(self):
        clock = Path(self.env["TEST_CLOCK"])
        current = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        clock.write_text(current.strftime("%Y-%m-%dT%H:%M:%SZ"))
        staged = self.envelope()
        staged["request"]["timeout_seconds"] = 30
        accepted = self.envelope(b"accepted")
        accepted["request"]["timeout_seconds"] = 30
        for env, body in ((staged, b"sample"), (accepted, b"accepted")):
            env["ts"] = current.strftime("%Y-%m-%dT%H:%M:%SZ")
            self.assertEqual(201, self.http("POST", "/requests/prepare", env)[0])
            self.assertEqual(201, self.http("PUT", self.upload_path(env), body)[0])
        self.assertEqual(202, self.http("POST", "/msg", accepted)[0])
        clock.write_text((current + dt.timedelta(seconds=31)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        code, error = self.http("POST", "/requests/prepare", staged)
        self.assertEqual(409, code)
        self.assertIn("deadline", error["error"])
        self.assertEqual(404, self.http("PUT", self.upload_path(staged), b"sample")[0])
        # A commit already in flight can arrive late; it records an explicit timeout.
        self.assertEqual(202, self.http("POST", "/msg", staged)[0])
        self.assertEqual("timed_out", self.http("GET", "/requests/" + staged["id"])[1]["request"]["response"]["response"]["state"])
        self.tm_json("bravo", "request", "maintain")
        before = self.rows("bravo", "SELECT id,state,seq FROM requests ORDER BY id")
        for env in (staged, accepted):
            code, body = self.http("POST", "/requests/prepare", env)
            self.assertEqual(200, code)
            self.assertTrue(body["committed"])
            self.assertEqual(200, self.http("POST", "/msg", env)[0])
        self.assertEqual(before, self.rows("bravo", "SELECT id,state,seq FROM requests ORDER BY id"))
        self.assertTrue(all(row["state"] == "timed_out" for row in before))
        self.no_reverse_connections()

    def test_wrong_file_token_and_encoded_traversal_do_not_expose_files(self):
        _, response, secret = self.final_with_file()
        route = urllib.parse.urlparse(response["fetch"][0]["url"]).path
        parts = route.split("/")
        token = parts[2]
        for path in ("/f/%s/%s" % (secrets.token_urlsafe(32), parts[-1]),
                     "/f/%s/%%2e%%2e%%2fconfig.json" % token,
                     "/f/%s/%%2e%%2e%%5cconfig.json" % token,
                     "/f/%s/.." % token):
            with self.subTest(path=path):
                code, content = self.http("GET", path)
                self.assertEqual(404, code)
                self.assertNotEqual(secret, content)
        for name in ("../escape.bin", "..\\escape.bin", "/absolute.bin", "..", "a\x00b"):
            with self.subTest(upload_name=name):
                env = self.envelope(name=name)
                self.assertEqual(400, self.http("POST", "/requests/prepare", env)[0])
        self.assertFalse((self.homes["bravo"] / "escape.bin").exists())
        self.no_reverse_connections()

    def test_download_hash_or_size_mismatch_never_installs_corrupt_result(self):
        _, response, original = self.final_with_file()
        item = response["fetch"][0]
        token = urllib.parse.urlparse(item["url"]).path.split("/")[2]
        source = self.homes["bravo"] / "outbox" / token / "files" / item["name"]
        target = self.homes["alpha"] / "inbox" / response["id"] / "files" / item["name"]
        for bad in (b"x" * len(original), original[:-1]):
            with self.subTest(download_bytes=len(bad)):
                source.write_bytes(bad)
                result = self.tm("alpha", "fetch", response["id"])
                self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertRegex(result.stdout.lower(), "mismatch|sha256|size")
                self.assertFalse(target.exists())
                self.assertFalse(target.with_name(target.name + ".part").exists())
        self.no_reverse_connections()

    def test_pull_capability_is_required_without_silent_push_fallback(self):
        self.stop()
        posts = []

        class LegacyHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                data = json.dumps({"node": "bravo.example.ts.net", "capabilities": ["request-lifecycle-v1"]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                posts.append(self.path)
                self.send_error(500)

        server = ThreadingHTTPServer(("127.0.0.1", self.port), LegacyHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = self.tm("alpha", "request", "send", self.address(), "Requires one-way mode", "--transport", "pull", "--wake", "none")
            self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertRegex((result.stdout + result.stderr).lower(), "request-pull-v1|capabilit|upgrade|support")
            self.assertEqual([], posts)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
