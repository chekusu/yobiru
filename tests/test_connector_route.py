"""A computer that reaches the tailnet only outbound, through its host's managed Tailscale connector.

Grok Bot's official Tailscale plugin (and similar hosted connectors) gives the computer a route to tailnet
addresses and MagicDNS names, but no local Tailscale client: no `tailscale status`, `whois` or receiver.
Such a sender learns its own identity from the receiver it calls (`/info` caller, SPEC §9.1); the receiver
still authenticates it with its own Tailscale. The sender here has a tailscale_cmd that would log any call,
and every test proves it was never run.
"""
import importlib.util
import json
import sys
import unittest
from pathlib import Path

import test_shared_requests as shared
from support.pkg import REPO

spec = importlib.util.spec_from_file_location("tailmsg_connector_under_test", str(REPO / "tailmsg.py"))
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)

GROK_ID = shared.ALPHA_ID


class ConnectorRouteTests(unittest.TestCase):
    def setUp(self):
        self.t = shared.SharedRequestTests("runTest")
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)
        self.root = self.t.root
        # The receiver's own Tailscale sees the connector's device as an ordinary named tailnet node.
        self.t.set_identity(shared.identity("grokbot-user-1", GROK_ID))
        self.calls = self.root / "connector computer tailscale calls.jsonl"
        fake_state = self.root / "connector fake tailscale.json"
        fake_state.write_text(json.dumps({"calls": str(self.calls), "status_error": "no local client here"}))
        config_path = self.t.homes["alpha"] / "config.json"
        config = json.loads(config_path.read_text())
        config.update(tailnet_route="connector",
                      tailscale_cmd=[sys.executable, str(shared.SUPPORT / "fake_tailscale.py"), "--state", str(fake_state)])
        config_path.write_text(json.dumps(config))
        self.extra = {"TEST_MAGICDNS": "127.0.0.1"}
        self.address = "bravo.example.ts.net:%d/worker" % self.t.port

    def tearDown(self):
        self.assertFalse(self.calls.exists(), "the connector computer ran its tailscale CLI")

    def alpha(self, *args):
        return self.t.tm("alpha", *args, extra=self.extra)

    def alpha_json(self, *args):
        result = self.alpha(*args)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_round_trip_with_files_uses_pull_and_the_receivers_view_of_the_sender(self):
        source = self.root / "signals.json"
        data = b'{"reports": 3}\n' * 500
        source.write_bytes(data)
        sent = self.alpha_json("request", "send", self.address, "Reproduce these reports", "--wake", "none",
                               "--file", str(source))
        self.assertEqual((202, "pull", "accepted"), (sent["status"], sent["transport"], sent["submission"]))
        mid = sent["id"]
        record = self.t.tm_json("bravo", "show", mid)
        self.assertEqual(shared.principal(GROK_ID), record["envelope"]["from"])
        self.assertEqual(GROK_ID, record["received"]["peer"]["stable_id"])
        self.assertEqual(0, self.t.tm("bravo", "fetch", mid).returncode)
        received = self.t.homes["bravo"] / "inbox" / mid / "files" / source.name
        self.assertEqual(shared.digest(data), shared.digest(received.read_bytes()))
        result = self.root / "results.json"
        result_data = b'{"reproduced": 2}\n' * 300
        result.write_bytes(result_data)
        self.t.tm_json("bravo", "reply", mid, "Two of three reproduce", "--file", str(result))
        state = self.alpha_json("request", "status", mid, "--refresh", "--json")
        self.assertEqual(("completed", "ok"), (state["state"], state["refresh"]))
        response_id = state["response"]["id"]
        fetched = self.alpha("fetch", response_id)
        self.assertEqual(0, fetched.returncode, fetched.stdout + fetched.stderr)
        downloaded = self.t.homes["alpha"] / "inbox" / response_id / "files" / result.name
        self.assertEqual(shared.digest(result_data), shared.digest(downloaded.read_bytes()))
        self.t.no_reverse_connections()

    def test_resume_reuses_the_same_request(self):
        sent = self.alpha_json("request", "send", self.address, "Resume me", "--wake", "none")
        resumed = self.alpha_json("request", "resume", sent["id"])
        self.assertEqual(sent["id"], resumed["id"])
        self.assertEqual(1, len(list((self.t.homes["bravo"] / "inbox").glob("*.json"))))

    def test_only_tracked_pull_requests_to_a_full_magicdns_name(self):
        for args in (("send", self.address, "plain message"),
                     ("request", "send", self.address, "push", "--transport", "push"),
                     ("request", "send", self.address, "legacy", "--legacy-request")):
            result = self.alpha(*args)
            self.assertNotEqual(0, result.returncode, args)
            self.assertIn("Tailscale connector", result.stderr)
        short = self.alpha("request", "send", "bravo:%d/worker" % self.t.port, "short name")
        self.assertNotEqual(0, short.returncode)
        self.assertIn("full MagicDNS name", short.stderr)
        self.assertEqual([], list((self.t.homes["bravo"] / "inbox").glob("*.json")))

    def test_no_receiver_and_no_peer_listing(self):
        for args in (("serve",), ("peers",)):
            result = self.alpha(*args)
            self.assertNotEqual(0, result.returncode, args)
            self.assertIn("Tailscale connector", result.stderr)

    def test_receiver_that_refuses_the_connector_device_creates_nothing(self):
        config_path = self.t.homes["bravo"] / "config.json"
        config = json.loads(config_path.read_text())
        config.update(access_policy="allowlist", allow=["someone-else"])
        config_path.write_text(json.dumps(config))
        result = self.alpha("request", "send", self.address, "Not allowed", "--wake", "none")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("access denied", result.stderr)
        self.assertFalse((self.t.homes["alpha"] / "requests" / "ledger.sqlite3").exists() and
                         self.t.rows("alpha", "SELECT id FROM requests"))


class ConnectorIdentityUnitTests(unittest.TestCase):
    """Ctx.connector_whois against receiver answers that a sender cannot verify by itself."""

    def ctx(self, answer):
        ctx = object.__new__(core.Ctx)
        ctx.cfg = {"tailnet_route": "connector", "port": 7766}
        ctx.connector_self, ctx._connector_hosts = None, {"100.64.0.2": "mac.example.ts.net"}
        self.calls = []

        def http_json(ip, port, method, path, **kwargs):
            self.calls.append((ip, port, method, path, kwargs.get("host")))
            return answer
        self.addCleanup(setattr, core, "http_json", core.http_json)
        core.http_json = http_json
        return ctx

    def info(self, **change):
        body = {"ok": True, "node": "mac.example.ts.net", "node_id": core.stable_peer_id("MacStable"),
                "capabilities": [core.REQUEST_CAPABILITY, core.REQUEST_PULL_CAPABILITY, core.CALLER_IDENTITY_CAPABILITY],
                "caller": {"node": "grokbot-user-1.example.ts.net", "id": core.stable_peer_id("GrokStable")}}
        body.update(change)
        return {k: v for k, v in body.items() if v is not None}

    def test_identity_comes_from_the_receiver_and_names_this_computer(self):
        ctx = self.ctx((200, self.info()))
        ident = ctx.connector_whois("100.64.0.2", 7766)
        self.assertEqual(("mac.example.ts.net", "MacStable"), (ident["node"], ident["stable_id"]))
        self.assertEqual(core.stable_peer_id("MacStable"), core.peer_identity(ident))
        self.assertEqual({"node": "grokbot-user-1.example.ts.net", "stable_id": "GrokStable", "ips": []}, ctx.self_node())
        self.assertEqual([("100.64.0.2", 7766, "GET", "/info", "mac.example.ts.net")], self.calls)

    def test_refusal_is_no_identity(self):
        self.assertIsNone(self.ctx((403, {"ok": False})).connector_whois("100.64.0.2", 7766))

    def test_older_or_malformed_receivers_are_refused(self):
        for answer, message in (
                ((200, self.info(capabilities=[core.REQUEST_CAPABILITY, core.REQUEST_PULL_CAPABILITY])), "0.7.26"),
                ((200, self.info(node_id=None)), "no usable identity"),
                ((200, self.info(caller={"node": "x"})), "no usable identity"),
                ((200, self.info(node_id="node:zz")), "no usable identity"),
                ((200, self.info(node="mac example")), "no usable identity"),
                ((500, {"ok": False}), "HTTP 500")):
            with self.subTest(message=message):
                ctx = self.ctx(answer)
                with self.assertRaises(RuntimeError) as caught:
                    ctx.connector_whois("100.64.0.2", 7766)
                self.assertIn(message, str(caught.exception))
                with self.assertRaises(RuntimeError):
                    ctx.self_node()

    def test_stable_id_wire_spelling_round_trips_only_when_canonical(self):
        for raw in ("nAbC123", "x" * 128):
            self.assertEqual(raw, core.stable_id_from_peer(core.stable_peer_id(raw)))
        for bad in ("node:", "node:6", "node:ZZ", "node:2f2f", "6e6f6465", None, 7):
            self.assertIsNone(core.stable_id_from_peer(bad))

    def test_names_must_be_full_magicdns_names(self):
        for bad in ("mac", "100.64.0.2", "mac.example.com", "mac.ts.net.evil", "-mac.example.ts.net"):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError) as caught:
                core.connector_resolve(bad)
            self.assertIn("full MagicDNS name", str(caught.exception))

    def test_route_must_be_known(self):
        with self.assertRaises(RuntimeError):
            core.parse_config({"tailnet_route": "proxy"})
        self.assertEqual("connector", core.parse_config({"tailnet_route": "connector"})[0]["tailnet_route"])


ypec = importlib.util.spec_from_file_location("yobiru_connector_under_test",
                                              str(REPO / "plugins" / "yobiru" / "scripts" / "yobiru.py"))
Y = importlib.util.module_from_spec(ypec)
ypec.loader.exec_module(Y)


class ConnectorSetupAndDoctorTests(unittest.TestCase):
    """The packaged command: setup, onboard, doctor and agents on the connector route never ask Tailscale."""

    def setUp(self):
        self.t = shared.SharedRequestTests("runTest")
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)
        self.home = self.t.root / "grok data"
        self.calls = self.t.root / "grok tailscale calls.jsonl"
        state = self.t.root / "grok fake tailscale.json"
        state.write_text(json.dumps({"calls": str(self.calls), "status_error": "no local client here"}))
        self.ts = json.dumps([sys.executable, str(shared.SUPPORT / "fake_tailscale.py"), "--state", str(state)])

    def tearDown(self):
        self.assertFalse(self.calls.exists(), "the connector computer ran its tailscale CLI")

    def y(self, *args):
        import subprocess
        return subprocess.run([sys.executable, str(REPO / "plugins" / "yobiru" / "scripts" / "yobiru.py"),
                               "--home", str(self.home)] + list(args), cwd=str(self.t.root),
                              env=self.t.environment("alpha"), capture_output=True, text=True, timeout=30)

    def configured(self):
        plan = self.y("setup", "--self-agent", "grok", "--tailnet-route", "connector", "--tailscale-cmd", self.ts,
                      "--json")
        self.assertEqual(0, plan.returncode, plan.stdout + plan.stderr)
        self.assertFalse((self.home / "config.json").exists())
        self.assertIn("official Tailscale connector", plan.stdout)
        applied = self.y("setup", "--self-agent", "grok", "--tailnet-route", "connector", "--tailscale-cmd", self.ts,
                         "--apply")
        self.assertEqual(0, applied.returncode, applied.stdout + applied.stderr)
        config = json.loads((self.home / "config.json").read_text())
        self.assertEqual(("connector", "grok", "tailnet"),
                         (config["tailnet_route"], config["self_agent"], config["access_policy"]))

    def test_setup_onboard_and_doctor(self):
        self.configured()
        doctor = self.y("doctor", "--json")
        report = json.loads(doctor.stdout)
        checks = {c["check"]: c for c in report["checks"]}
        self.assertEqual("ok", checks["tailnet-route"]["status"])
        self.assertNotIn("tailscale", checks)
        self.assertNotIn("receiver", checks)
        self.assertNotIn("shields-up", checks)
        self.assertIn("Friends", checks["incoming"]["fix"])
        self.assertEqual(0, report["errors"], doctor.stdout)
        onboard = json.loads(self.y("onboard", "--json").stdout)
        ids = [s["id"] for s in onboard["steps"]]
        self.assertEqual(["python", "tailscale-connector", "config", "incoming", "verify"], ids)
        self.assertNotIn("receiver", ids)
        self.assertEqual("verify", onboard["next"])
        agents = self.y("agents")
        self.assertEqual(1, agents.returncode)
        self.assertIn("doctor --peer", agents.stdout)

    def test_switching_an_existing_local_install_needs_replace(self):
        self.y("setup", "--self-agent", "grok", "--tailnet-route", "local", "--apply")
        refused = self.y("setup", "--tailnet-route", "connector", "--apply")
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("--replace tailnet_route", refused.stdout + refused.stderr)
        self.assertEqual("local", json.loads((self.home / "config.json").read_text())["tailnet_route"])
        switched = self.y("setup", "--tailnet-route", "connector", "--replace", "tailnet_route", "--apply")
        self.assertEqual(0, switched.returncode, switched.stdout + switched.stderr)
        self.assertEqual("connector", json.loads((self.home / "config.json").read_text())["tailnet_route"])

    def test_peer_check_reports_the_receivers_view_of_this_computer(self):
        import ipaddress
        import socket
        core = Y.core()
        net = ipaddress.ip_network("127.0.0.0/8")
        core.TAILNET_NETS.append(net)
        self.addCleanup(core.TAILNET_NETS.remove, net)
        real = socket.getaddrinfo
        self.addCleanup(setattr, socket, "getaddrinfo", real)
        socket.getaddrinfo = lambda host, *a, **k: real("127.0.0.1" if host.endswith(".example.ts.net") else host, *a, **k)
        self.t.set_identity(shared.identity("grokbot-user-1", GROK_ID))
        checks = {c["check"]: c for c in Y.peer_doctor_checks({"tailnet_route": "connector", "cfg": {}},
                                                              "bravo.example.ts.net", self.t.port)}
        self.assertEqual("ok", checks["peer-target"]["status"])
        self.assertEqual("ok", checks["peer-identity"]["status"], checks)
        self.assertEqual("ok", checks["peer-connector"]["status"])
        self.assertIn("grokbot-user-1.example.ts.net", checks["peer-connector"]["detail"])
        short = {c["check"]: c for c in Y.peer_doctor_checks({"tailnet_route": "connector", "cfg": {}}, "bravo", 7766)}
        self.assertEqual("error", short["peer-target"]["status"])


if __name__ == "__main__":
    unittest.main()
