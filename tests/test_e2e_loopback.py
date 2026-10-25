"""Addressed messaging between two isolated homes through the unpacked plugin, loopback only.

Two receivers (alpha, bravo) run the bundled tailmsg.py on 127.0.0.1 with free ports, a fake tailscale
CLI per home, and the YOBIRU_PYTHON interpreter override pointing at a shim that treats 127.0.0.0/8 as
tailnet addresses. Nothing here touches the real Tailscale, port 7766, ~/.tailmsg or another machine.
"""
import http.client
import json
import os
import signal
import subprocess
import sys
import time
import unittest

from support.pkg import SUPPORT, Package, free_port

SHIM = """#!/bin/sh
case "$1" in
  */tailmsg.py) exec "$TM_TEST_REAL_PYTHON" "$TM_TEST_HARNESS" "$@" ;;
  *) exec "$TM_TEST_REAL_PYTHON" "$@" ;;
esac
"""
ADAPTER = """#!/bin/sh
printf '%%s %%s %%s\\n' "$TAILMSG_AGENT" "$TAILMSG_MSG_ID" "$#" >> '%s'
"""


def node(name, peer):
    return {"BackendState": "Running",
            "Self": {"ID": name + "-stable", "DNSName": "%s.example.ts.net." % name, "HostName": name, "TailscaleIPs": ["127.0.0.1"],
                     "Online": True},
            "Peer": {"p1": {"DNSName": "%s.example.ts.net." % peer, "HostName": peer, "OS": "linux",
                            "TailscaleIPs": ["127.0.0.1"], "Online": True}}}


def whois(name):
    return {"Node": {"Name": "%s.example.ts.net." % name, "Hostinfo": {"Hostname": name}, "Tags": []},
            "UserProfile": {"LoginName": "%s@example.com" % name}}


def wait_for(cond, timeout=15.0, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


def health(port):
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        c.request("GET", "/health")
        r = c.getresponse()
        r.read()
        return r.status == 200
    except OSError:
        return False


class LoopbackWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = pkg = Package()
        root = pkg.root
        cls.user = pkg.new_home("e2e user")
        shim = root / "loopback python"
        shim.write_text(SHIM)
        shim.chmod(0o755)
        cls.extra = {"YOBIRU_PYTHON": str(shim), "TM_TEST_REAL_PYTHON": sys.executable,
                     "TM_TEST_HARNESS": str(SUPPORT / "loopback_harness.py")}
        adapters = root / "adapter scripts"
        adapters.mkdir()
        cls.logs = {}
        for agent in ("codex", "helper"):
            cls.logs[agent] = root / ("%s wake.log" % agent)
            script = adapters / ("%s.sh" % agent)
            script.write_text(ADAPTER % cls.logs[agent])
            script.chmod(0o755)
        cls.port = {"alpha": free_port(), "bravo": free_port()}
        cls.home = {"alpha": root / "alpha data", "bravo": root / "bravo data"}
        for name, peer in (("alpha", "bravo"), ("bravo", "alpha")):
            state = pkg.fake_state(name, status=node(name, peer), whois=whois(peer))
            cfg = {"port": cls.port[name], "bind": "127.0.0.1", "allow": [peer], "self_agent": None,
                   "tailscale_cmd": [sys.executable, str(SUPPORT / "fake_tailscale.py"), "--state", str(state)],
                   "wake_backoff_s": 0.1, "hook_timeout_s": 10}
            if name == "alpha":
                cfg.update(self_agent="grok", default_agent="grok", agents={"grok": {}})
            else:
                # A fresh 0.4 receiver needs no per-peer allow rule. Alpha remains a legacy installation.
                cfg.pop("allow")
                cfg.update(self_agent="codex", default_agent="codex",
                           agents={"codex": {"wake_adapter": {"type": "exec", "argv": [str(adapters / "codex.sh")]}},
                                   "helper": {"wake_adapter": {"type": "exec", "argv": [str(adapters / "helper.sh")]}}},
                           wake_policy={"default_max": "batch", "peers": {"alpha": "now"}})
            cls.home[name].mkdir()
            (cls.home[name] / "config.json").write_text(json.dumps(cfg, indent=2))
        cls.procs = []
        for name in ("alpha", "bravo"):
            log = open(str(root / ("%s serve.log" % name)), "w")
            cls.procs.append(subprocess.Popen(
                ["/bin/sh", str(pkg.launcher), "--home", str(cls.home[name]), "serve"],
                env=pkg.env(cls.user, extra=cls.extra), cwd=str(pkg.work), stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True))
            log.close()
        for name in ("alpha", "bravo"):
            if not wait_for(lambda: health(cls.port[name])):
                cls.tearDownClass()
                raise RuntimeError("%s receiver did not start: %s" % (
                    name, (root / ("%s serve.log" % name)).read_text()))

    @classmethod
    def tearDownClass(cls):
        for p in cls.procs:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except OSError:
                pass
            p.wait(timeout=10)
        cls.procs = []
        cls.pkg.cleanup()

    def tm(self, who, *args):
        r = self.pkg.run(["--home", str(self.home[who])] + list(args), self.user, extra=self.extra)
        return r

    def tm_json(self, who, *args):
        r = self.tm(who, *args)
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        return json.loads(r.stdout.splitlines()[-1] if args[0] in ("send", "reply") else r.stdout)

    def wake_lines(self, agent):
        p = self.logs[agent]
        return p.read_text().splitlines() if p.exists() else []

    def inbox_records(self, who):
        d = self.home[who] / "inbox"
        return [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))] if d.is_dir() else []

    def test_1_addressed_message_with_file_wakes_only_that_agent(self):
        report = self.pkg.root / "my report.txt"
        report.write_bytes(("weekly report ✓\n" * 400).encode("utf-8"))
        out = self.tm_json("alpha", "send", "bravo:%d/codex" % self.port["bravo"], "please review",
                           "--file", str(report), "--wake", "now")
        self.assertEqual((202, "codex", 1), (out["status"], out["agent"], out["files"]))
        mid = out["id"]
        self.assertTrue(wait_for(lambda: len(self.wake_lines("codex")) == 1), self.wake_lines("codex"))
        self.assertEqual(["codex %s 1" % mid], self.wake_lines("codex"))
        self.assertEqual([], self.wake_lines("helper"))

        pending = self.tm("bravo", "inbox", "--pending", "--agent", "codex").stdout
        self.assertIn(mid, pending)
        self.assertNotIn(mid, self.tm("bravo", "inbox", "--pending", "--agent", "helper").stdout)

        rec = self.tm_json("bravo", "show", mid)
        self.assertEqual("alpha.example.ts.net", rec["received"]["peer"]["node"])
        self.assertEqual(("codex", "known"), (rec["received"]["agent"], rec["received"]["agent_status"]))
        self.assertEqual("now", rec["received"]["wake"]["effective"])
        self.assertEqual("grok", rec["envelope"]["from_agent"])
        self.assertTrue(wait_for(lambda: self.tm_json("bravo", "show", mid).get("wake_delivery", {}).get("ok")))

        r = self.tm("bravo", "fetch", mid)
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        got = self.home["bravo"] / "inbox" / mid / "files" / "my report.txt"
        self.assertEqual(report.read_bytes(), got.read_bytes())

        before = len(self.inbox_records("alpha"))
        rep = self.tm_json("bravo", "reply", mid, "done, thanks", "--port", str(self.port["alpha"]))
        self.assertEqual((202, "grok"), (rep["status"], rep["agent"]))      # routed back to the sender's agent
        replies = [x for x in self.inbox_records("alpha") if x["envelope"].get("reply_to") == mid]
        self.assertEqual(1, len(replies))
        self.assertEqual("bravo.example.ts.net", replies[0]["received"]["peer"]["node"])
        self.assertEqual(("grok", "known"), (replies[0]["received"]["agent"], replies[0]["received"]["agent_status"]))
        self.assertEqual("codex", replies[0]["envelope"]["from_agent"])

        r = self.tm("bravo", "ack", mid)
        self.assertEqual(0, r.returncode)
        self.assertNotIn(mid, self.tm("bravo", "inbox", "--pending", "--agent", "codex").stdout)
        self.assertEqual(before + 1, len(self.inbox_records("alpha")))     # ack is local: nothing sent back
        self.assertEqual(1, len(self.wake_lines("codex")))

    def test_2_unknown_agent_is_stored_but_wakes_nobody(self):
        codex_before = len(self.wake_lines("codex"))
        out = self.tm_json("alpha", "send", "bravo:%d/nobody" % self.port["bravo"], "who is this for?",
                           "--wake", "now")
        self.assertEqual(202, out["status"])
        rec = self.tm_json("bravo", "show", out["id"])
        self.assertEqual(("nobody", "unknown"), (rec["received"]["agent"], rec["received"]["agent_status"]))
        self.assertEqual("batch", rec["received"]["wake"]["effective"])
        time.sleep(1.0)
        self.assertEqual(codex_before, len(self.wake_lines("codex")))
        self.assertEqual([], self.wake_lines("helper"))
        self.assertIn("@nobody?", self.tm("bravo", "inbox").stdout)

    def test_3_wake_request_is_capped_by_the_receiver(self):
        codex_before = len(self.wake_lines("codex"))
        out = self.tm_json("bravo", "send", "alpha:%d" % self.port["alpha"], "fyi", "--wake", "now")
        rec = self.tm_json("alpha", "show", out["id"])
        self.assertEqual(("now", "batch"), (rec["received"]["wake"]["requested"], rec["received"]["wake"]["effective"]))
        self.assertIn("wake_policy", rec["received"]["wake"]["reason"])
        self.assertEqual(codex_before, len(self.wake_lines("codex")))

    def test_4_doctor_and_status_see_the_running_receiver(self):
        r = self.tm("bravo", "doctor", "--json")
        d = json.loads(r.stdout)
        checks = {c["check"]: c for c in d["checks"]}
        self.assertEqual("ok", checks["receiver"]["status"], checks["receiver"])
        self.assertIn("bravo.example.ts.net", checks["receiver"]["detail"])
        self.assertEqual("ok", checks["wake @codex"]["status"])
        self.assertIn(checks["maintenance"]["status"], ("ok", "info"), checks["maintenance"])   # healthy or starting
        st = self.tm_json("bravo", "status", "--json")
        self.assertIn(st["receiver"]["status"], ("healthy", "starting"))
        self.assertTrue(st["receiver"]["maintenance"]["supported"])
        self.assertEqual(["codex", "helper"], [a["agent"] for a in st["agents"]])
        self.assertTrue(st["receiver"]["ok"])

    def test_5_agents_shows_versions_and_sessions_over_authenticated_info(self):
        (self.home["bravo"] / "sessions.json").write_text(json.dumps({"v": 1, "agents": {
            "helper": {"runtime": "grok", "session_id": "chat-helper-1", "display_name": "Helper desk"}}}))
        r = self.tm("alpha", "agents", "--json", "--peer", "bravo:%d" % self.port["bravo"])
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        rows = json.loads(r.stdout)["machines"]
        self.assertEqual(["bravo.example.ts.net"], [x["node"] for x in rows])
        bravo = rows[0]
        self.assertEqual(("ok", "allowed", self.pkg.product["version"]),
                         (bravo["receiver"], bravo["send_permission"], bravo["version"]))
        agents = {a["agent"]: a for a in bravo["agents"]}
        self.assertEqual(["codex", "helper"], sorted(agents))
        self.assertIsNone(agents["codex"]["session"])
        self.assertEqual(("chat-helper-1", "Helper desk", "configured", "unknown"),
                         (agents["helper"]["session"]["id"], agents["helper"]["session"]["display_name"],
                          agents["helper"]["session"]["id_source"], agents["helper"]["session"]["activity"]))
        self.assertNotIn("adapter", r.stdout)                         # no adapter paths cross the network
        text = self.tm("alpha", "peers", "--peer", "bravo:%d" % self.port["bravo"]).stdout
        self.assertIn('alias "Helper desk"', text)
        self.assertIn("chat-helper-1", text)

    def test_6_policy_migration_and_deny_apply_without_receiver_restart(self):
        before = [p.pid for p in self.procs]
        old = self.tm_json("alpha", "policy", "show", "--json")
        self.assertTrue(old["legacy_allowlist"])
        self.assertEqual("allowlist", old["policy"]["access_policy"])
        self.tm_json("alpha", "policy", "set", "--mode", "tailnet", "--apply", "--json")
        self.tm_json("bravo", "policy", "set", "--mode", "tailnet", "--deny", "alpha", "--apply", "--json")
        out = self.tm("alpha", "send", "bravo:%d/codex" % self.port["bravo"], "blocked", "--wake", "none")
        self.assertEqual(403, json.loads(out.stdout.splitlines()[-1])["status"])
        self.tm_json("bravo", "policy", "set", "--mode", "tailnet", "--clear-deny", "--apply", "--json")
        sent = self.tm_json("alpha", "send", "bravo:%d/codex" % self.port["bravo"], "allowed again", "--wake", "none")
        self.assertEqual(202, sent["status"])
        self.assertEqual(before, [p.pid for p in self.procs])
        self.assertTrue(all(p.poll() is None for p in self.procs))


if __name__ == "__main__":
    unittest.main()
