"""tools/friends_acceptance.py, the driver for the Beta's acceptance checklist (docs/UPGRADING-0.6.md, section 5):
the whole checklist with its scripted host, and a remote host that never shows up, which must end as a timeout after
a complete cleanup, never as a failed check.

Real gateway and resource-service subprocesses on loopback through the plugin's own launcher, synthetic homes in a
temporary directory. The scripted host is a protocol fixture, never evidence of any real host.
"""
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support.pkg import REPO, free_port

TOOL = REPO / "tools" / "friends_acceptance.py"
TOKEN_PATTERN = re.compile(r"yb[oawvtcsn]_[A-Za-z0-9_-]{43}")


class AcceptanceToolTests(unittest.TestCase):
    def run_tool(self, *args, timeout=600):
        tmp = tempfile.TemporaryDirectory(prefix="yobiru acceptance ")
        self.addCleanup(tmp.cleanup)
        report = Path(tmp.name) / "report.json"
        r = subprocess.run([sys.executable, str(TOOL), "--report", str(report)] + list(args), capture_output=True,
                           text=True, timeout=timeout, cwd=tmp.name)
        return r, json.loads(report.read_text()), Path(tmp.name)

    def test_the_whole_checklist_passes_with_the_scripted_host(self):
        r, report, _ = self.run_tool()
        self.assertEqual(0, r.returncode, r.stdout[-3000:] + r.stderr[-2000:])
        self.assertEqual("pass", report["result"])
        self.assertEqual(list(range(1, 13)), sorted({c["item"] for c in report["checks"]}))
        self.assertTrue(all(c["ok"] for c in report["checks"]))
        self.assertEqual({"approve_inbound": 1, "approve_reply": 1}, report["reviews"], "only the follower is reviewed")
        self.assertIsNone(TOKEN_PATTERN.search(r.stdout + r.stderr + json.dumps(report)))

    def test_a_remote_host_that_never_acts_is_a_timeout_after_a_full_cleanup(self):
        gw, res = free_port(), free_port()
        handoff = tempfile.TemporaryDirectory(prefix="yobiru handoff ")
        self.addCleanup(handoff.cleanup)
        r, report, _ = self.run_tool("--host", "remote", "--public-url", "http://127.0.0.1:%d" % gw,
                                     "--gateway-port", str(gw), "--resource-public-url", "http://127.0.0.1:%d" % res,
                                     "--resource-port", str(res), "--handoff", handoff.name, "--wait-minutes", "0.05")
        self.assertEqual(3, r.returncode, r.stdout[-3000:] + r.stderr[-2000:])
        self.assertEqual("host_timeout", report["result"])
        self.assertIn("binding check", report["error"])
        self.assertTrue(all(c["ok"] for c in report["checks"]), "nothing failed: the host just never acted")
        self.assertEqual(2, len([c for c in report["checks"] if c["item"] == 12]))
        self.assertEqual(["RUNBOOK.md"], sorted(p.name for p in Path(handoff.name).iterdir()),
                         "the handoff keeps the instructions, never a credential")
        self.assertIsNone(TOKEN_PATTERN.search((Path(handoff.name) / "RUNBOOK.md").read_text()))


if __name__ == "__main__":
    unittest.main()
