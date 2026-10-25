"""Muse outbound adapter: read-only checks and real one-way pull transport."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_shared_requests as shared
from support.pkg import REPO

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
