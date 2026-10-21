"""Run the friend-facing verification script against the real isolated HTTP core."""
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

import test_shared_requests as shared
from support.pkg import REPO, SUPPORT


class SharedVerificationDriverTests(unittest.TestCase):
    def setUp(self):
        self.transport = shared.SharedRequestTests("runTest")
        self.transport.setUp()
        self.addCleanup(self.transport.doCleanups)
        self.root = self.transport.root
        self.workdir = self.root / "friend verification"
        self.wrapper = self.root / "loopback client core.py"
        self.wrapper.write_text(
            "import runpy, sys\n"
            "sys.argv.insert(1, %r)\n"
            "runpy.run_path(%r, run_name='__main__')\n" %
            (str(REPO / "tailmsg.py"), str(self.transport.harness)))
        self.fake_cli = self.root / "tailscale test CLI"
        self.fake_cli.write_text(
            "#!%s\nimport runpy, sys\n"
            "sys.argv[1:1] = ['--state', %r]\n"
            "runpy.run_path(%r, run_name='__main__')\n" %
            (sys.executable, str(self.transport.states["alpha"]), str(SUPPORT / "fake_tailscale.py")))
        self.fake_cli.chmod(0o700)
        self.processes = []
        self.addCleanup(self.stop_drivers)

    def stop_drivers(self):
        for process in self.processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)

    def start_driver(self, timeout=20, peer=None, extra=None):
        args = [sys.executable, str(REPO / "tools" / "verify_shared_request.py"),
                "--peer", peer or self.transport.address(), "--core", str(self.wrapper),
                "--workdir", str(self.workdir), "--tailscale", str(self.fake_cli), "--timeout", str(timeout)]
        process = subprocess.Popen(args, cwd=str(self.root), env=self.transport.environment("alpha", extra),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.processes.append(process)
        return process

    def finish_driver(self, process, expected=0, timeout=30):
        stdout, stderr = process.communicate(timeout=timeout)
        self.assertEqual(expected, process.returncode, stdout + stderr)
        return stdout, stderr

    def journal(self):
        return json.loads((self.workdir / "verification.json").read_text())

    def await_request(self, process, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end and process.poll() is None:
            records = list((self.transport.homes["bravo"] / "inbox").glob("*.json"))
            if records:
                self.assertEqual(1, len(records))
                return json.loads(records[0].read_text())
            time.sleep(0.05)
        stdout, stderr = process.communicate(timeout=5)
        self.fail("verification did not reach destination: " + stdout + stderr)

    def respond(self, record, text=None, attachment=None, state="completed"):
        journal = self.journal()
        mid = record["envelope"]["id"]
        fixture = self.transport.homes["bravo"] / "inbox" / mid / "files" / journal["fixture"]
        fetch = self.transport.tm("bravo", "fetch", mid)
        self.assertEqual(0, fetch.returncode, fetch.stdout + fetch.stderr)
        self.assertEqual(journal["sha256"], shared.digest(fixture.read_bytes()))
        marker = "YOBIRU_SHARED_OK " + journal["nonce"] + " " + journal["sha256"]
        result = self.transport.tm_json("bravo", "request", "respond", mid,
                                       marker if text is None else text, "--state", state,
                                       "--file", str(attachment or fixture))
        return result

    def assert_passed(self, request_id, stdout):
        journal = self.journal()
        self.assertIn("PASS:", stdout)
        self.assertTrue(journal["passed"])
        self.assertFalse(journal["client_receiver_started"])
        self.assertEqual("pull", journal["transport"])
        self.assertEqual("completed", journal["result_state"])
        self.assertEqual(request_id, journal["request_id"])
        self.assertEqual(journal["sha256"], journal["response_sha256"])
        self.assertGreater(journal["bytes"], 2 * 1024 * 1024)
        downloaded = list((self.workdir / "client" / "inbox" / journal["response_id"] / "files").glob("*"))
        self.assertEqual(1, len(downloaded))
        self.assertEqual((self.workdir / journal["fixture"]).read_bytes(), downloaded[0].read_bytes())
        self.assertEqual([], list((self.workdir / "client" / "inbox").glob("*.json")))
        self.transport.no_reverse_connections()

    def test_script_sends_random_fixture_and_verifies_reply_and_identical_download(self):
        process = self.start_driver()
        record = self.await_request(process)
        self.assertEqual("pull", record["envelope"]["request"]["transport"])
        self.assertEqual("verification", record["envelope"]["from_agent"])
        self.respond(record)
        stdout, _ = self.finish_driver(process)
        self.assert_passed(record["envelope"]["id"], stdout)
        # Repeating a successful acceptance command never creates another request.
        self.finish_driver(self.start_driver())
        self.assertEqual(1, len(list((self.transport.homes["bravo"] / "inbox").glob("*.json"))))

    def test_timeout_then_same_workdir_resume_uses_original_request_and_fixture(self):
        process = self.start_driver(timeout=10)
        record = self.await_request(process)
        stdout, _ = self.finish_driver(process, expected=2)
        self.assertIn("outcome remains unknown", stdout)
        original = self.journal()
        self.assertFalse(original["passed"])
        self.transport.restart()
        self.respond(record)
        stdout, _ = self.finish_driver(self.start_driver())
        self.assert_passed(record["envelope"]["id"], stdout)
        self.assertEqual(original["nonce"], self.journal()["nonce"])
        self.assertEqual(original["sha256"], self.journal()["sha256"])
        self.assertEqual(1, len(list((self.transport.homes["bravo"] / "inbox").glob("*.json"))))

    def test_upload_response_loss_recovers_saved_id_and_resumes_without_duplicate(self):
        marker = self.root / "driver lost upload response"
        first = self.start_driver(extra={"TEST_LOSE_RESPONSE": str(marker)})
        _, stderr = self.finish_driver(first, expected=2)
        self.assertIn("resum", stderr.lower())
        self.assertTrue(marker.exists())
        journal = self.journal()
        self.assertTrue(journal["request_id"])
        self.assertFalse(journal["passed"])
        self.transport.restart()
        process = self.start_driver()
        record = self.await_request(process)
        self.assertEqual(journal["request_id"], record["envelope"]["id"])
        self.respond(record)
        stdout, _ = self.finish_driver(process)
        self.assert_passed(journal["request_id"], stdout)

    def test_matching_marker_embedded_in_other_text_is_not_a_success(self):
        process = self.start_driver()
        record = self.await_request(process)
        journal = self.journal()
        marker = "YOBIRU_SHARED_OK " + journal["nonce"] + " " + journal["sha256"]
        self.respond(record, text="This result was not verified: " + marker)
        stdout, stderr = self.finish_driver(process, expected=2)
        self.assertIn("exactly match", stderr)
        self.assertNotIn("PASS:", stdout)
        self.assertFalse(self.journal()["passed"])

    def test_changed_reply_attachment_fails_even_when_text_matches(self):
        process = self.start_driver()
        record = self.await_request(process)
        changed = self.root / "changed bytes.bin"
        changed.write_bytes(b"not the original generated fixture")
        self.respond(record, attachment=changed)
        stdout, stderr = self.finish_driver(process, expected=2)
        self.assertIn("exactly the original fixture", stderr)
        self.assertNotIn("PASS:", stdout)
        self.assertFalse(self.journal()["passed"])


if __name__ == "__main__":
    unittest.main()
