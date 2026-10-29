"""Muse outbound adapter: read-only checks and real one-way pull transport."""
import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import patch

import test_shared_requests as shared
from support.numeric_http import ThreadingHTTPServer
from support.pkg import REPO, Package, snapshot

SCRIPTS = REPO / "plugins" / "yobiru" / "scripts"

def load(name):
    spec = importlib.util.spec_from_file_location(name, str(SCRIPTS / (name + ".py")))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MuseCheckTests(unittest.TestCase):
    def setUp(self):
        self.adapter, self.cli = load("yobiru_muse"), load("yobiru")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "not created"

    def status(self, **fields):
        own = {"ID": "muse-stable-id", "DNSName": "muse.example.ts.net.", "TailscaleIPs": ["100.64.0.1"]}
        own.update(fields)
        return json.dumps({"BackendState": "Running", "Self": own})

    def test_missing_cli_is_read_only_and_points_to_official_connector_and_friends(self):
        with patch.object(self.cli, "run_tailscale", return_value=(None, "not found")) as run:
            out = self.adapter.check(self.home, self.cli)
        self.assertFalse(self.home.exists())
        self.assertFalse(out["local_ready"])
        self.assertIn("official Tailscale connector", out["connect"])
        self.assertIn("Friends", out["fallback"])
        self.assertEqual(5, run.call_args.kwargs["timeout"])
        self.assertFalse(out["receiver_required"])

    def test_missing_stable_identity_never_claims_ready(self):
        self.home.mkdir()
        (self.home / "config.json").write_text("{}")
        with patch.object(self.cli, "run_tailscale", return_value=(self.status(ID=None), None)):
            out = self.adapter.check(self.home, self.cli)
        self.assertFalse(out["local_ready"])
        self.assertEqual("tailscale_identity_unavailable", out["checks"][-1]["check"])
        self.assertEqual(["config.json"], [p.name for p in self.home.iterdir()])

    def test_sender_check_needs_no_receiver_tun_or_home_creation(self):
        with patch.object(self.cli, "run_tailscale", return_value=(self.status(), None)):
            out = self.adapter.check(self.home, self.cli)
        self.assertFalse(out["local_ready"])
        self.assertFalse(self.home.exists())
        self.home.mkdir()
        (self.home / "config.json").write_text("{}")
        with patch.object(self.cli, "run_tailscale", return_value=(self.status(), None)):
            out = self.adapter.check(self.home, self.cli)
        self.assertTrue(out["local_ready"])
        self.assertFalse(out["delivery_verified"])
        self.assertEqual(["config.json"], [p.name for p in self.home.iterdir()])

    def test_send_has_no_push_override_and_keeps_option_like_text_positional(self):
        args = self.adapter.parser().parse_args(["send", "--", "receiver/agent", "--file=/private/secret"])
        cmd = self.adapter.command(args)
        self.assertEqual(["--", "receiver/agent", "--file=/private/secret"], cmd[-3:])
        self.assertEqual("pull", cmd[cmd.index("--transport") + 1])
        self.assertEqual("muse", cmd[cmd.index("--as") + 1])
        with self.assertRaises(SystemExit):
            self.adapter.parser().parse_args(["send", "--transport", "push", "receiver", "text"])

    def test_unavailable_cli_keeps_error_and_never_probes_without_a_peer(self):
        for error in ("not found", "permission denied", "timed out after 5s", "exit 2"):
            with self.subTest(error=error), \
                    patch.object(self.cli, "run_tailscale", return_value=(None, error)) as run, \
                    patch.object(self.cli, "http_request") as http:
                out = self.adapter.check(self.home, self.cli)
                self.assertEqual(["tailscale_unavailable"],
                                 [c["check"] for c in out["checks"] if c["status"] == "error"])
                self.assertEqual([["status", "--json"], ["status"]],
                                 [c.args[1] for c in run.call_args_list])
                self.assertTrue(all(c.kwargs["timeout"] == 5 for c in run.call_args_list))
                self.assertFalse(out["local_ready"])
                self.assertFalse(out["delivery_verified"])
                self.assertFalse(out["peer_checked"])
                self.assertFalse(self.home.exists())
                http.assert_not_called()

    def test_plain_status_success_is_only_a_diagnostic_without_a_peer(self):
        for stdout in ("", "not connected\n", "synthetic plain status\n"):
            with self.subTest(stdout=stdout), \
                    patch.object(self.cli, "run_tailscale", side_effect=[
                        (None, "exit 2"), (stdout, None)]) as run, \
                    patch.object(self.cli, "http_request") as http:
                out = self.adapter.check(self.home, self.cli)
                self.assertIn("tailscale_json_unavailable", [c["check"] for c in out["checks"]])
                self.assertNotIn("tailscale_identity", [c["check"] for c in out["checks"]])
                self.assertEqual([["status", "--json"], ["status"]],
                                 [c.args[1] for c in run.call_args_list])
                self.assertTrue(all(c.kwargs["timeout"] == 5 for c in run.call_args_list))
                self.assertFalse(out["local_ready"])
                self.assertFalse(out["delivery_verified"])
                self.assertFalse(out["peer_checked"])
                self.assertFalse(self.home.exists())
                http.assert_not_called()

    def test_explicit_ip_peer_gets_real_read_only_probes_after_local_identity_failure(self):
        self.home.mkdir()
        (self.home / "config.json").write_text("{}")
        before = snapshot(self.home)
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.command, self.path))
                if self.path == "/health":
                    body = {"ok": True, "v": 1}
                elif self.path == "/info":
                    body = {"ok": True, "v": 1, "product": "yobiru", "version": "0.7.15",
                            "node": "receiver.example.ts.net", "agents": [],
                            "capabilities": ["request-lifecycle-v1", "request-pull-v1"]}
                else:
                    self.send_error(404)
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        real_http = self.cli.http_request

        def loopback(ip, port, method, path, **kwargs):
            self.assertEqual("100.64.0.2", ip)
            self.assertEqual(server.server_port, port)
            self.assertEqual("GET", method)
            return real_http("127.0.0.1", port, method, path, **kwargs)

        failures = (
            ([(None, "unknown flag: --json"), ("100.64.0.1 muse synthetic-user linux -\n", None)],
             "tailscale_json_unavailable"),
            ([("not JSON", None)], "tailscale_invalid_status"),
            ([(self.status(ID=None), None)], "tailscale_identity_unavailable"),
        )
        for replies, expected in failures:
            with self.subTest(check=expected):
                requests.clear()
                output = io.StringIO()
                with patch.object(self.cli, "run_tailscale", side_effect=replies), \
                        patch.object(self.cli, "http_request", side_effect=loopback), \
                        contextlib.redirect_stdout(output):
                    code = self.adapter.main(self.home, ["check", "--peer",
                                             "100.64.0.2:%d/agent" % server.server_port], self.cli)
                out = json.loads(output.getvalue())
                self.assertEqual(2, code, "successful peer probes cannot replace local identity")
                self.assertIn(expected, [c["check"] for c in out["checks"] if c["status"] == "error"])
                self.assertEqual([("GET", "/health"), ("GET", "/info")], requests)
                self.assertTrue(out["peer_checked"])
                self.assertTrue(out["peer_pull_advertised"])
                self.assertFalse(out["local_ready"])
                self.assertFalse(out["delivery_verified"])
                self.assertEqual(before, snapshot(self.home))

    def test_local_json_failure_keeps_peer_transport_error(self):
        with patch.object(self.cli, "run_tailscale", side_effect=[
                (None, "exit 2"), ("synthetic plain status", None)]), \
                patch.object(self.cli, "http_request", side_effect=ConnectionRefusedError("synthetic refusal")):
            out = self.adapter.check(self.home, self.cli, "100.64.0.2:7766")
        errors = [c["check"] for c in out["checks"] if c["status"] == "error"]
        self.assertIn("tailscale_json_unavailable", errors)
        self.assertIn("peer-transport", errors)
        self.assertFalse(out["peer_pull_advertised"])
        self.assertFalse(out["local_ready"])
        self.assertFalse(self.home.exists())

    def test_invalid_config_does_not_query_cli_or_claim_peer_was_checked(self):
        self.home.mkdir()
        (self.home / "config.json").write_text("not JSON")
        before = snapshot(self.home)
        with patch.object(self.cli, "run_tailscale") as run, \
                patch.object(self.cli, "http_request") as http:
            out = self.adapter.check(self.home, self.cli, "100.64.0.2:7766")
        self.assertEqual("config_invalid", out["checks"][-1]["check"])
        self.assertFalse(out["peer_checked"])
        run.assert_not_called()
        http.assert_not_called()
        self.assertEqual(before, snapshot(self.home))

    def test_json_failure_does_not_allow_http_to_a_non_tailnet_address(self):
        with patch.object(self.cli, "run_tailscale", side_effect=[
                (None, "exit 2"), ("synthetic plain status", None)]), \
                patch.object(self.cli, "http_request") as http:
            out = self.adapter.check(self.home, self.cli, "127.0.0.1:7766")
        self.assertIn("peer-target", [c["check"] for c in out["checks"] if c["status"] == "error"])
        self.assertFalse(out["peer_pull_advertised"])
        self.assertFalse(out["local_ready"])
        http.assert_not_called()
        self.assertFalse(self.home.exists())


class MusePackagedRestrictedCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = Package()

    @classmethod
    def tearDownClass(cls):
        cls.pkg.cleanup()

    def test_nonzero_json_status_falls_back_to_plain_status_without_claiming_readiness(self):
        script = self.pkg.root / "restricted tailscale.py"
        script.write_text(textwrap.dedent("""\
            import json
            import sys

            calls, mode = sys.argv[1:3]
            args = sys.argv[3:]
            with open(calls, "a", encoding="utf-8") as log:
                log.write(json.dumps(args) + "\\n")
            if args == ["status", "--json"]:
                if mode == "unknown_flag":
                    sys.stderr.write("unknown flag: --json\\n")
                sys.exit(2)
            if args == ["status"]:
                print("100.64.0.1 muse synthetic-user linux -")
            else:
                sys.exit(2)
            """), encoding="utf-8")
        for mode in ("unknown_flag", "silent"):
            with self.subTest(mode=mode):
                user = self.pkg.new_home("restricted-" + mode)
                home = user / "muse data"
                home.mkdir()
                calls = self.pkg.root / ("restricted-" + mode + "-calls.jsonl")
                (home / "config.json").write_text(json.dumps({"tailscale_cmd": [
                    sys.executable, str(script), str(calls), mode]}), encoding="utf-8")
                before = snapshot(user, ignore_macos_caches=True)
                result, out = self.pkg.run_json(["--home", str(home), "muse", "check"], user)
                self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                self.assertIn("tailscale_json_unavailable", [c["check"] for c in out["checks"]])
                self.assertNotIn("tailscale_unavailable", [c["check"] for c in out["checks"]])
                self.assertEqual([["status", "--json"], ["status"]],
                                 [json.loads(line) for line in calls.read_text().splitlines()])
                self.assertFalse(out["local_ready"])
                self.assertFalse(out["delivery_verified"])
                self.assertFalse(out["peer_checked"])
                self.assertFalse(out["changed"])
                self.assertNotIn("synthetic-user", result.stdout)
                self.assertEqual(before, snapshot(user, ignore_macos_caches=True))


class MuseOneWayTests(unittest.TestCase):
    def setUp(self):
        self.transport = shared.SharedRequestTests("runTest")
        self.transport.setUp()
        self.addCleanup(self.transport.doCleanups)
        self.root = self.transport.root
        self.shim = self.root / "muse core shim"
        self.shim.write_text("#!%s\nimport runpy\nrunpy.run_path(%r, run_name='__main__')\n" %
                             (sys.executable, str(self.transport.harness)))
        self.shim.chmod(0o700)

    def muse(self, *args, extra=None):
        env = self.transport.environment("alpha", dict(extra or {}, YOBIRU_PYTHON=str(self.shim)))
        return subprocess.run([sys.executable, str(SCRIPTS / "yobiru.py"), "--home",
                               str(self.transport.homes["alpha"]), "muse"] + list(args),
                              cwd=str(self.root), env=env, capture_output=True, text=True, timeout=25)

    def ok(self, *args):
        result = self.muse(*args)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_real_receiver_result_and_attachment_without_reverse_connection(self):
        fixture = self.root / "hello from Muse.txt"
        fixture.write_text("synthetic Muse attachment")
        sent = self.ok("send", "--file", str(fixture), "--", self.transport.address(), "synthetic request")
        self.assertEqual("pull", sent["transport"])
        mid = sent["id"]
        rec = json.loads((self.transport.homes["bravo"] / "inbox" / (mid + ".json")).read_text())
        self.assertEqual("muse", rec["envelope"]["from_agent"])
        response = self.transport.tm_json("bravo", "request", "respond", mid, "synthetic answer", "--state", "completed", "--file", str(fixture))
        status = self.ok("status", mid)
        self.assertEqual("completed", status["state"])
        self.assertTrue(status["outcome_known"])
        self.assertEqual("synthetic answer", status["response"]["text"])
        self.ok("read", response["response_id"])
        fetched = self.muse("fetch", response["response_id"])
        self.assertEqual(0, fetched.returncode, fetched.stdout + fetched.stderr)
        files = self.transport.homes["alpha"] / "inbox" / response["response_id"] / "files"
        self.assertEqual(fixture.read_bytes(), (files / fixture.name).read_bytes())
        self.transport.no_reverse_connections()

    def test_lost_upload_response_recovers_same_id_and_no_duplicate_task(self):
        fixture = self.root / "upload.txt"
        fixture.write_text("synthetic uncertain upload")
        marker = self.root / "lost response"
        result = self.muse("send", "--file", str(fixture), "--", self.transport.address(), "recover once",
                           extra={"TEST_LOSE_RESPONSE": str(marker)})
        self.assertNotEqual(0, result.returncode)
        self.assertTrue(marker.exists())
        rows = self.ok("list")
        self.assertEqual(1, len(rows))
        mid = rows[0]["id"]
        resumed = self.ok("resume", mid)
        self.assertEqual(mid, resumed["id"])
        self.assertEqual("accepted", resumed["submission"])
        self.assertEqual(1, len(list((self.transport.homes["bravo"] / "inbox").glob("*.json"))))
        self.transport.tm_json("bravo", "request", "respond", mid, "recovered", "--state", "completed")
        self.assertEqual("completed", self.ok("status", mid)["state"])
        self.assertEqual(1, len(self.ok("list")))
        self.transport.no_reverse_connections()
