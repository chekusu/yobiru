"""Authenticated /info discovery, session projection, native-name lookups and hostile peers.

Receivers run in-process on 127.0.0.0/8 with a stubbed Tailscale identity; peers are tiny local HTTP
servers. Nothing here touches the real Tailscale, port 7766, ~/.tailmsg, a Codex runtime or another machine.
"""
import http.client
import importlib.util
import ipaddress
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from support.numeric_http import ThreadingHTTPServer
from pathlib import Path

from support.pkg import REPO

UUID = "0192f0c4-1d2e-7a3b-8c4d-5e6f7a8b9c0d"
OTHER_UUID = "0192f0c4-1d2e-7a3b-8c4d-000000000000"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


core = load("tailmsg_discovery_under_test", REPO / "tailmsg.py")
legacy = load("tailmsg_pre_agents_fixture", REPO / "tests" / "fixtures" / "tailmsg_v1_pre_agents.py")
Y = load("yobiru_cli_under_test", REPO / "plugins" / "yobiru" / "scripts" / "yobiru.py")
for mod in (core, legacy):
    mod.log = lambda *a: None
    mod.TAILNET_NETS.append(ipaddress.ip_network("127.0.0.0/8"))


def ident(node):
    return {"ip": "127.0.0.1", "node": node, "host": node.split(".")[0], "tags": [], "user": ""}


class Receiver(object):
    """A real tailmsg receiver handler in this process; `caller` decides who Tailscale says is calling."""

    def __init__(self, module, home, cfg, caller="alpha.example.ts.net", me="bravo.example.ts.net"):
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.json").write_text(json.dumps(cfg))
        self.ctx = module.Ctx(str(home))
        self.caller = caller
        self.ctx.whois = lambda ip: ident(self.caller)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), module.make_handler(self.ctx, me))
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", path)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, json.loads(body)

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


SECRET_CFG = {
    "allow": ["alpha"], "default_agent": "codex", "self_agent": "codex",
    "agents": {"codex": {"wake_adapter": {"type": "exec", "argv": ["/opt/private/wake-codex", "--token=abc"]}},
               "grok": {"wake_adapter": {"type": "webhook", "url_file": "/srv/private/url",
                                         "secret_file": "/srv/private/secret"}},
               "helper": {}},
    "wake_adapter": {"type": "webhook", "url_env": "PRIVATE_URL_ENV"}}


class InfoEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "bravo home"
        self.rx = Receiver(core, self.home, SECRET_CFG)
        self.addCleanup(self.rx.close)
        self.lookups = []

        def fake_lookup(home, binding):
            self.lookups.append(binding["session_id"])
            return {"status": "ok", "name": "Triage inbox"}
        self.rx.ctx.native_name = lambda b: core.cached_native_name(
            self.rx.ctx.home, b, self.rx.ctx._name_cache, self.rx.ctx._name_guard, self.rx.ctx._name_lookup,
            lookup=fake_lookup)
        (self.home / "sessions.json").write_text(json.dumps({"v": 1, "future": {"kept": True}, "agents": {
            "grok": {"runtime": "grok", "session_id": "grok-chat-42", "display_name": "Research desk",
                     "confirmed_title": "Weekly X signals", "confirmed_at": "2026-10-11T00:00:00Z",
                     "private_note": "/srv/private/notes"}}}))
        (self.home / "codex-bridge.json").write_text(json.dumps({
            "dispatcher_thread_id": UUID, "codex_path": "/opt/private/codex", "codex_home": "/srv/private/codex",
            "codex_remote": "unix:///srv/private/app.sock", "allow_replies": True}))
        (self.home / "codex-local").mkdir()
        (self.home / "codex-local" / "codex_bridge.py").write_text("# stand-in; lookups are faked here\n")
        (self.home / "inbox").mkdir(exist_ok=True)
        (self.home / "inbox" / "abcdef0123456789.json").write_text(json.dumps(
            {"envelope": {"text": "PRIVATE MESSAGE BODY"}, "received": {}}))

    def test_unauthorized_peer_is_refused_before_any_lookup(self):
        self.rx.caller = "mallory.example.ts.net"
        status, body = self.rx.get("/info")
        self.assertEqual(403, status)
        self.assertEqual({"ok": False, "error": "peer not in allow list"}, body)
        self.assertEqual([], self.lookups)
        self.assertEqual(200, self.rx.get("/health")[0])          # /health stays as it was

    def test_projection_is_minimal_and_hides_configuration(self):
        status, body = self.rx.get("/info")
        self.assertEqual(200, status)
        text = json.dumps(body)
        for secret in ("/opt/private", "/srv/private", "--token", "PRIVATE_URL_ENV", "PRIVATE MESSAGE BODY",
                       "allow_replies", "wake_adapter", "private_note", "alpha", "unix://"):
            self.assertNotIn(secret, text)
        self.assertEqual(("yobiru", core.PRODUCT_VERSION, 1, "bravo.example.ts.net"),
                         (body["product"], body["version"], body["v"], body["node"]))
        agents = {a["agent"]: a for a in body["agents"]}
        self.assertEqual(["codex", "grok", "helper"], [a["agent"] for a in body["agents"]])
        codex = agents["codex"]["session"]
        self.assertEqual((UUID, "codex-bridge", "Triage inbox", "ok", "unknown"),
                         (codex["id"], codex["id_source"], codex["native_name"], codex["native_name_status"],
                          codex["activity"]))
        grok = agents["grok"]["session"]
        self.assertEqual(("grok-chat-42", "configured", "Research desk", "Weekly X signals", None, "not_supported"),
                         (grok["id"], grok["id_source"], grok["display_name"], grok["confirmed_title"],
                          grok["native_name"], grok["native_name_status"]))
        self.assertIsNone(agents["helper"]["session"])
        self.assertEqual([UUID], self.lookups)                      # one lookup, only for the bridge session
        self.rx.get("/info")
        self.assertEqual([UUID], self.lookups)                      # cached afterwards

    def test_this_machine_may_ask_itself(self):
        self.rx.caller = "bravo.example.ts.net"
        self.assertEqual(200, self.rx.get("/info")[0])

    def test_configured_codex_binding_cannot_override_the_bridge(self):
        (self.home / "sessions.json").write_text(json.dumps({"agents": {
            "codex": {"runtime": "codex", "session_id": OTHER_UUID, "display_name": "Wrong one"}}}))
        bound, problems = core.session_bindings(self.home, ["codex"])
        self.assertEqual(UUID, bound["codex"]["session_id"])
        self.assertIsNone(bound["codex"]["display_name"])
        self.assertTrue(any("conflicts" in p for p in problems))

    def test_agent_list_truncation_is_signalled(self):
        many = {"allow": ["alpha"], "agents": {"a%02d" % i: {} for i in range(40)}}
        rx = Receiver(core, Path(self.tmp.name) / "many", many)
        self.addCleanup(rx.close)
        status, body = rx.get("/info")
        self.assertEqual((32, 40, True), (len(body["agents"]), body["agents_total"], body["agents_truncated"]))
        info, _ = Y.validate_info(json.dumps(body).encode())
        self.assertTrue(any("truncated" in w for w in info["warnings"]))


class NativeNameCacheTests(unittest.TestCase):
    def setUp(self):
        self.cache, self.guard, self.single = {}, threading.Lock(), threading.Lock()
        self.bridge = {"runtime": "codex", "session_id": UUID, "id_source": "codex-bridge"}

    def call(self, binding, lookup, wait=1.5):
        return core.cached_native_name("/nonexistent", binding, self.cache, self.guard, self.single,
                                       lookup=lookup, wait=wait)

    def test_unsupported_source_is_never_cached_and_does_not_mask_the_bridge(self):
        generic = dict(self.bridge, id_source="configured")
        self.assertEqual("not_supported", self.call(generic, lambda h, b: self.fail("no lookup"))["status"])
        self.assertEqual({}, self.cache)
        res = self.call(self.bridge, lambda h, b: {"status": "ok", "name": "Native"})
        self.assertEqual(("ok", "Native"), (res["status"], res["name"]))

    def test_rename_is_seen_after_the_ttl(self):
        names = ["First"]
        self.assertEqual("First", self.call(self.bridge, lambda h, b: {"status": "ok", "name": names[0]})["name"])
        names[0] = "Renamed"
        self.assertEqual("First", self.call(self.bridge, lambda h, b: {"status": "ok", "name": names[0]})["name"])
        key = next(iter(self.cache))
        self.cache[key] = (0, self.cache[key][1])                    # expire it
        self.assertEqual("Renamed", self.call(self.bridge, lambda h, b: {"status": "ok", "name": names[0]})["name"])

    def test_slow_lookup_answers_pending_then_stale_never_blocks(self):
        gate = threading.Event()

        def slow(h, b):
            gate.wait(5)
            return {"status": "ok", "name": "Late"}
        t0 = time.monotonic()
        self.assertEqual("pending", self.call(self.bridge, slow, wait=0.2)["status"])
        self.assertLess(time.monotonic() - t0, 1.5)
        gate.set()
        for _ in range(50):
            if self.cache:
                break
            time.sleep(0.05)
        res = self.call(self.bridge, slow)
        self.assertEqual(("Late", True), (res["name"], res["cached"]))
        key = next(iter(self.cache))
        self.cache[key] = (0, self.cache[key][1])
        gate.clear()
        res = self.call(self.bridge, slow, wait=0.1)
        self.assertEqual(("Late", True), (res["name"], res.get("stale")))
        view = Y.validate_session_view(core.session_view(dict(self.bridge, display_name=None, confirmed_title=None),
                                                         res))
        self.assertTrue(view["native_name_stale"])
        self.assertIn("stale", " ".join(Y.session_lines(view)))
        gate.set()


def process_state(pid):
    """'running', 'zombie' or 'gone'. kill(pid, 0) also succeeds for a process that was killed but not yet reaped
    by its parent, which can last indefinitely under a container init that does not reap orphans. The state is
    therefore read from /proc, or from ps where there is no /proc."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "gone"
    except PermissionError:
        pass                                           # it exists, owned by another user
    try:
        state = Path("/proc/%d/stat" % pid).read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        ps = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
        if ps.returncode != 0 or not ps.stdout.strip():
            return "gone"
        state = ps.stdout.strip()[0]
    return "zombie" if state in ("Z", "X") else "running"


GRANDCHILD_SCRIPT = ("import subprocess, sys, time\n"
                     "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                     "open(%r, 'w').write(str(p.pid))\n"
                     "time.sleep(60)\n")


@unittest.skipUnless(os.name == "posix", "process groups")
class BoundedLookupTests(unittest.TestCase):
    def assert_ended(self, pid):
        for _ in range(50):
            if process_state(pid) != "running":
                return
            try:                                       # reap it if it became this process's zombie
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
            time.sleep(0.1)
        os.kill(pid, signal.SIGKILL)
        self.fail("the app-server stand-in survived the lookup timeout")

    def test_timeout_kills_the_whole_process_group(self):
        with tempfile.TemporaryDirectory() as d:
            pidfile = Path(d) / "grandchild.pid"
            t0 = time.monotonic()
            code, out, err = core.run_bounded([sys.executable, "-c", GRANDCHILD_SCRIPT % str(pidfile)], timeout=1.0)
            self.assertIsNone(code)
            self.assertLess(time.monotonic() - t0, 10)
            self.assert_ended(int(pidfile.read_text()))

    def test_process_state_counts_a_zombie_as_ended_although_kill_0_succeeds(self):
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        self.assertEqual("running", process_state(live.pid))
        done = subprocess.Popen([sys.executable, "-c", "pass"])
        try:
            for _ in range(100):
                if process_state(done.pid) == "zombie":
                    break
                time.sleep(0.05)
            self.assertEqual("zombie", process_state(done.pid))
            os.kill(done.pid, 0)                       # the old check: a zombie still "exists"
        finally:
            done.wait()
        self.assertEqual("gone", process_state(done.pid))

    @unittest.skipUnless(sys.platform.startswith("linux"), "PR_SET_CHILD_SUBREAPER is Linux-only")
    def test_timeout_kill_is_seen_under_an_init_that_never_reaps(self):
        # The fresh verifier box: the killed stand-in's parent dies too, and its new parent never reaps it.
        # A subreaper that never calls wait() plays that init; the stand-in must count as ended.
        with tempfile.TemporaryDirectory() as d:
            pidfile = Path(d) / "grandchild.pid"
            helper = ("import ctypes, importlib.util, sys, time\n"
                      "if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:\n"
                      "    sys.exit(3)\n"
                      "spec = importlib.util.spec_from_file_location('core', %r)\n"
                      "core = importlib.util.module_from_spec(spec)\n"
                      "spec.loader.exec_module(core)\n"
                      "core.run_bounded([sys.executable, '-c', %r], timeout=1.0)\n"
                      "print('done', flush=True)\n"
                      "time.sleep(60)\n" % (str(REPO / "tailmsg.py"), GRANDCHILD_SCRIPT % str(pidfile)))
            init = subprocess.Popen([sys.executable, "-c", helper], stdout=subprocess.PIPE, text=True)
            self.addCleanup(init.stdout.close)
            self.addCleanup(init.wait)
            self.addCleanup(init.kill)
            self.assertEqual("done", init.stdout.readline().strip(), "the non-reaping init could not start")
            pid = int(pidfile.read_text())
            self.assert_ended(pid)
            self.assertEqual("zombie", process_state(pid), "killed, and left unreaped by its new parent")

    def test_output_is_bounded(self):
        code, out, _ = core.run_bounded([sys.executable, "-c", "import sys; sys.stdout.write('x' * 2000000)"],
                                        timeout=20, limit=1000)
        self.assertIsNone(code)
        self.assertLessEqual(len(out), 1000)

    def test_lookup_reads_only_the_bound_session(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / "codex-local").mkdir()
            stub = home / "codex-local" / "codex_bridge.py"
            binding = {"runtime": "codex", "session_id": UUID, "id_source": "codex-bridge"}
            for out, want in (({"thread_id": UUID, "name": "Inbox \x1b[31mred\x1b[0m"}, ("ok", "Inbox [31mred[0m")),
                              ({"thread_id": OTHER_UUID, "name": "Other"}, ("unavailable", None)),
                              ({"thread_id": UUID, "name": None}, ("unnamed", None))):
                stub.write_text("import json\nprint(json.dumps(%r))\n" % (out,))
                res = core.lookup_native_name(home, binding)
                self.assertEqual(want, (res["status"], res.get("name")))
            stub.write_text("import sys\nsys.stderr.write('codex_bridge: socket_stale\\n')\nsys.exit(1)\n")
            self.assertEqual("socket_stale", core.lookup_native_name(home, binding)["error"])


class PeerServer(object):
    """A local stand-in for another machine's receiver with scripted answers."""

    def __init__(self, routes):
        self.routes = routes
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                route = outer.routes.get(self.path)
                if route is None:
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b'{"ok": false, "error": "not found"}')
                    return
                if callable(route):
                    return route(self)
                status, body = route
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def info_body(**over):
    body = {"ok": True, "v": 1, "product": "yobiru", "version": "0.3.0", "node": "bravo.example.ts.net",
            "agents": [{"agent": "codex", "default": True, "session": {
                "runtime": "codex", "id": UUID, "id_source": "codex-bridge", "display_name": None,
                "confirmed_title": None, "native_name": "Triage", "native_name_status": "ok",
                "native_name_checked_at": "2026-10-11T00:00:00Z", "activity": "unknown"}}]}
    body.update(over)
    return body


class ProbeTests(unittest.TestCase):
    def serve(self, routes):
        s = PeerServer(routes)
        self.addCleanup(s.close)
        return s

    def probe(self, s, timeout=2.0):
        return Y.probe_info("127.0.0.1", s.port, timeout, expect_node="bravo.example.ts.net")

    def test_current_receiver(self):
        res = self.probe(self.serve({"/info": (200, info_body())}))
        self.assertEqual("info", res["kind"])
        s = res["info"]["agents"][0]["session"]
        self.assertEqual((UUID, "Triage", "0.3.0"), (s["id"], s["native_name"], res["info"]["version"]))

    def test_old_receiver_404_is_legacy_not_offline(self):
        res = self.probe(self.serve({"/health": (200, {"ok": True, "v": 1, "node": "bravo"})}))
        self.assertEqual(("legacy", 404), (res["kind"], res["http_status"]))
        res = self.probe(self.serve({}))
        self.assertEqual("malformed", res["kind"])

    def test_real_older_receivers_answer_404(self):
        with tempfile.TemporaryDirectory() as d:
            rx = Receiver(legacy, Path(d) / "old", {"allow": ["alpha"]})
            self.addCleanup(rx.close)
            res = Y.probe_info("127.0.0.1", rx.port, 2.0)
            self.assertEqual("legacy", res["kind"])

    def test_403_is_policy_and_closed_port_is_unreachable(self):
        self.assertEqual("blocked", self.probe(self.serve({"/info": (403, {"ok": False})}))["kind"])
        s = self.serve({})
        port = s.port
        s.close()
        self.assertEqual("unreachable", Y.probe_info("127.0.0.1", port, 1.0)["kind"])

    def test_malformed_and_hostile_answers(self):
        for body in (b"not json", b"[]", json.dumps({"ok": True, "v": 1, "product": "other"}).encode(),
                     b"x" * (Y.MAX_REPLY_BYTES + 10)):
            self.assertEqual("malformed", self.probe(self.serve({"/info": (200, body)}))["kind"])
        evil = info_body(node="bravo\x1b[2J.example.ts.net", version="9.9.9; rm -rf /")
        evil["agents"][0]["session"].update(native_name="\x1b]0;owned\x07Real‮name", display_name="a\nb")
        evil["agents"].append({"agent": "x/y", "session": None})
        evil["agents"].append({"agent": "grok", "session": {"runtime": "codex", "id": "not-a-uuid",
                                                            "id_source": "codex-bridge"}})
        res = self.probe(self.serve({"/info": (200, evil)}))
        info = res["info"]
        self.assertIsNone(info["version"])
        s = info["agents"][0]["session"]
        self.assertNotIn("\x1b", json.dumps(info, ensure_ascii=False))
        self.assertNotIn("‮", s["native_name"])
        self.assertEqual("ab", s["display_name"])
        self.assertEqual(["codex", "grok"], [a["agent"] for a in info["agents"]])
        self.assertIsNone(info["agents"][1]["session"])            # a non-UUID Codex id is never shown
        self.assertTrue(info["warnings"])                          # node mismatch

    def test_slow_dripping_peer_hits_the_total_deadline(self):
        def drip(h):
            h.send_response(200)
            h.send_header("Content-Length", "100000")
            h.end_headers()
            try:
                for _ in range(200):
                    h.wfile.write(b"x" * 10)
                    h.wfile.flush()
                    time.sleep(0.1)
            except OSError:
                pass
        s = self.serve({"/info": drip})
        t0 = time.monotonic()
        res = self.probe(s, timeout=0.5)
        self.assertLess(time.monotonic() - t0, 1.5)
        self.assertEqual(("unreachable", "timeout"), (res["kind"], res["error"]))

    def test_http10_close_body_drip_and_slow_headers_hit_the_deadline(self):
        def drip_close(h):                     # HTTP/1.0 + Connection: close: getresponse detaches conn.sock
            h.wfile.write(b"HTTP/1.0 200 OK\r\nContent-Length: 10\r\nConnection: close\r\n\r\n")
            h.wfile.flush()
            try:
                for _ in range(10):
                    time.sleep(0.11)
                    h.wfile.write(b"x")
                    h.wfile.flush()
            except OSError:
                pass

        def slow_headers(h):
            try:
                for line in (b"HTTP/1.0 200 OK\r\n", b"X-A: 1\r\n", b"X-B: 2\r\n", b"X-C: 3\r\n",
                             b"X-D: 4\r\n", b"X-E: 5\r\n", b"\r\n"):
                    h.wfile.write(line)
                    h.wfile.flush()
                    time.sleep(0.15)
            except OSError:
                pass
        for route in (drip_close, slow_headers):
            s = self.serve({"/info": route})
            t0 = time.monotonic()
            res = self.probe(s, timeout=0.2)
            self.assertLess(time.monotonic() - t0, 0.6, route.__name__)
            self.assertEqual(("unreachable", "timeout"), (res["kind"], res["error"]), route.__name__)
            t0 = time.monotonic()
            env = {"v": 1, "id": "abcdef0123456789"}
            outcome = Y.post_notice("127.0.0.1", s.port, "bravo", env, timeout=0.2)
            self.assertLess(time.monotonic() - t0, 0.6, route.__name__)
            self.assertEqual("uncertain", outcome[0])           # sent, answer incomplete: never "not sent"

    def test_session_lines_label_sources(self):
        view = Y.validate_session_view(info_body()["agents"][0]["session"])
        text = "\n".join(Y.session_lines(view))
        self.assertIn('name "Triage" (native codex session name', text)
        self.assertIn(UUID, text)
        self.assertIn("busy/idle: unknown", text)
        self.assertNotIn("inbox", " ".join(Y.session_lines(None)))


if __name__ == "__main__":
    unittest.main()
