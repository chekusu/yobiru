"""`yobiru guard`: Claude Code hooks that let the owner allow one blocked tool call by signing it with a passkey.

The relay is a real gateway on loopback; a software passkey plays yobiru.com/approve. Events are the JSON Claude Code
sends its hooks (PermissionDenied, PreToolUse, SessionStart)."""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from support.pkg import REPO
from support.public_harness import C, Gateway
from support.softpasskey import SoftPasskey

SCRIPTS = REPO / "plugins" / "yobiru" / "scripts"
spec = importlib.util.spec_from_file_location("guard_core", REPO / "tailmsg.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
spec = importlib.util.spec_from_file_location("yobiru_guard_under_test", SCRIPTS / "yobiru_guard.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
guard.load(core)

HOST = "tyo.example.ts.net"
NODE = core.stable_peer_id("nTyo1CNTRL")
COMMAND = 'curl -X POST https://deploy.example/apps -H "Authorization: Bearer $TOKEN" -d @app.json'


def key_text(passkey):
    return core.owner_key_text(core.owner_key_record(passkey.key()))


class Page(object):
    """What yobiru.com/approve does with a link, with a software passkey."""

    def __init__(self, link):
        frag = urllib.parse.parse_qs(urllib.parse.urlsplit(link).fragment)
        relay = frag["r"][0]
        self.relay = relay if "://" in relay else "https://" + relay
        self.did, self.key = guard._ap.drop_parts(core.unb64u(frag["s"][0]))
        status, obj = C.api_call(self.relay, "GET", "/v1/approval-drops/" + self.did)
        assert status == 200, (status, obj)
        self.ask = guard._ap.unseal(self.key, self.did, "ask", obj["ask"])
        self.raw = core.unb64u(self.ask["intent"], core.DROP_SLOT_MAX)

    def intent(self):
        return json.loads(self.raw.decode("utf-8"))

    def answer(self, obj):
        sealed = guard._ap.seal(self.key, self.did, "answer", obj)
        return C.post_raw(self.relay, "/v1/approval-drops/%s/answer" % self.did, sealed.encode("ascii"),
                          {"Content-Type": "text/plain"})[0]

    def approve(self, passkey, raw=None):
        return self.answer({"v": 1, "approval": passkey.approve(raw or self.raw)})


def link_of(out):
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    return next(w for w in reason.split() if w.startswith("https://yobiru.com/approve/#"))


class GuardFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="yobiru guard test ")
        cls.gw = Gateway(Path(cls._tmp.name)).start()

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls._tmp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir()
        self.phone = SoftPasskey(label="phone")
        self.cfg = guard.Config(self.home, HOST, NODE, [key_text(self.phone)], lang="zh-CN", relay=self.gw.url)

    def event(self, name, command=COMMAND, cwd="/opt/app", **extra):
        ev = {"hook_event_name": name, "session_id": "s1", "cwd": cwd, "permission_mode": "auto",
              "tool_name": "Bash", "tool_input": {"command": command, "description": "Register the app"}}
        ev.update(extra)
        return ev

    def deny_then_ask(self, command=COMMAND):
        out = guard.handle(self.cfg, self.event("PermissionDenied", command, reason="[Production Deploy]"))
        self.assertEqual(out, {"hookSpecificOutput": {"hookEventName": "PermissionDenied", "retry": True}})
        out = guard.handle(self.cfg, self.event("PreToolUse", command))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        return out

    def test_denied_call_signed_once_then_allowed_once(self):
        asked = self.deny_then_ask()
        reason = asked["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("passkey", reason)
        self.assertIn(" guard wait ", reason)
        page = Page(link_of(asked))
        intent = page.intent()
        self.assertEqual(intent["type"], core.APPROVAL_TYPE)
        self.assertEqual(intent["to"], {"host": HOST, "agent": "claude-code", "node": NODE})
        self.assertEqual(intent["from"], {"host": HOST, "node": NODE})
        self.assertEqual((intent["uses"], intent["files"]), (1, []))
        b = guard.binding("Bash", {"command": COMMAND}, "/opt/app")
        self.assertEqual(intent["instruction"],
                         "Claude Code 执行一次这条命令：\n\n%s\n\n目录：/opt/app\n调用校验：sha256 %s"
                         % (COMMAND, guard.call_digest(b)))
        self.assertEqual(page.ask["credentials"], [self.phone.key()["credential_id"]])

        # Not signed yet: still refused, same link.
        again = guard.handle(self.cfg, self.event("PreToolUse"))
        self.assertEqual(again["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(link_of(again), link_of(asked))

        self.assertEqual(page.approve(self.phone), 201)
        # A different description is the same call; the signature lets it through once.
        ok = guard.handle(self.cfg, self.event("PreToolUse", description="retry"))
        self.assertEqual(ok["hookSpecificOutput"]["permissionDecision"], "allow")
        self.assertIn(core.owner_key_record(self.phone.key())["fingerprint"],
                      ok["hookSpecificOutput"]["additionalContext"])
        # Used: the same call is Claude Code's business again.
        self.assertIsNone(guard.handle(self.cfg, self.event("PreToolUse")))

        # Replaying the used approval through a planted record does not work.
        approval = self.phone.approve(page.raw)
        guard.write_json(guard.record_path(self.home, guard.call_digest(b)),
                         {"v": 1, "call": guard.call_digest(b), "state": "signed", "asked_at": guard.iso(guard.now()),
                          "approval": approval})
        out = guard.handle(self.cfg, self.event("PreToolUse"))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("already used", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_signature_covers_only_that_call(self):
        page = Page(link_of(self.deny_then_ask()))
        page.approve(self.phone)
        # Another command, or the same one in another folder, is not covered and is not the guard's business.
        self.assertIsNone(guard.handle(self.cfg, self.event("PreToolUse", COMMAND + " --force")))
        self.assertIsNone(guard.handle(self.cfg, self.event("PreToolUse", cwd="/")))
        # A record planted for another call with this approval is refused.
        other = guard.binding("Bash", {"command": "rm -rf /"}, "/opt/app")
        guard.write_json(guard.record_path(self.home, guard.call_digest(other)),
                         {"v": 1, "call": guard.call_digest(other), "state": "signed",
                          "asked_at": guard.iso(guard.now()), "approval": self.phone.approve(page.raw)})
        out = guard.handle(self.cfg, self.event("PreToolUse", "rm -rf /"))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("different call", out["hookSpecificOutput"]["permissionDecisionReason"])
        # The real call still runs once.
        ok = guard.handle(self.cfg, self.event("PreToolUse"))
        self.assertEqual(ok["hookSpecificOutput"]["permissionDecision"], "allow")

    def test_untrusted_passkey_or_other_computer_is_refused(self):
        page = Page(link_of(self.deny_then_ask()))
        stranger = SoftPasskey(label="stranger")
        page.approve(stranger)
        out = guard.handle(self.cfg, self.event("PreToolUse"))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("does not trust", out["hookSpecificOutput"]["permissionDecisionReason"])

        # The owner's own signature, but for Claude Code on another computer.
        page = Page(link_of(self.deny_then_ask()))
        intent = page.intent()
        intent["to"] = {"host": "other.example.ts.net", "agent": "claude-code"}
        b = guard.binding("Bash", {"command": COMMAND}, "/opt/app")
        guard.write_json(guard.record_path(self.home, guard.call_digest(b)),
                         {"v": 1, "call": guard.call_digest(b), "state": "signed", "asked_at": guard.iso(guard.now()),
                          "approval": self.phone.approve(intent)})
        out = guard.handle(self.cfg, self.event("PreToolUse"))
        self.assertIn("not Claude Code on this computer", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_declined_stays_declined(self):
        page = Page(link_of(self.deny_then_ask()))
        self.assertEqual(page.answer({"v": 1, "declined": True}), 201)
        for _ in range(2):
            out = guard.handle(self.cfg, self.event("PreToolUse"))
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertIn("declined", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_expired_request_is_asked_again(self):
        first = link_of(self.deny_then_ask())
        b = guard.binding("Bash", {"command": COMMAND}, "/opt/app")
        path = guard.record_path(self.home, guard.call_digest(b))
        rec = json.loads(path.read_text())
        rec["expires_at"] = "2020-01-01T00:00:00Z"
        guard.write_json(path, rec)
        out = guard.handle(self.cfg, self.event("PreToolUse"))
        self.assertNotEqual(link_of(out), first)

    def test_old_denial_is_forgotten(self):
        guard.handle(self.cfg, self.event("PermissionDenied"))
        b = guard.binding("Bash", {"command": COMMAND}, "/opt/app")
        path = guard.record_path(self.home, guard.call_digest(b))
        rec = json.loads(path.read_text())
        rec["denied_at"] = "2020-01-01T00:00:00Z"
        guard.write_json(path, rec)
        self.assertIsNone(guard.handle(self.cfg, self.event("PreToolUse")))

    def test_protect_rule_asks_without_a_denial(self):
        cfg = guard.Config(self.home, HOST, NODE, [key_text(self.phone)], relay=self.gw.url, protect=[r"^git push\b"])
        self.assertIsNone(guard.handle(cfg, self.event("PreToolUse", "git status")))
        out = guard.handle(cfg, self.event("PreToolUse", "git push origin main"))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        page = Page(link_of(out))
        self.assertTrue(page.intent()["instruction"].startswith("Claude Code runs this command once:\n\ngit push"))
        page.approve(self.phone)
        out = guard.handle(cfg, self.event("PreToolUse", "git push origin main"))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "allow")
        # Protected calls need a signature every time.
        out = guard.handle(cfg, self.event("PreToolUse", "git push origin main"))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_without_keys_denials_are_left_alone(self):
        cfg = guard.Config(self.home, HOST, NODE, [], relay=self.gw.url)
        self.assertIsNone(guard.handle(cfg, self.event("PermissionDenied")))
        self.assertIsNone(guard.handle(cfg, self.event("PreToolUse")))

    def test_relay_failure_never_allows(self):
        guard.handle(self.cfg, self.event("PermissionDenied"))
        cfg = guard.Config(self.home, HOST, NODE, [key_text(self.phone)], relay="http://127.0.0.1:9")
        out = guard.handle(cfg, self.event("PreToolUse"))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("Nothing ran", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_wait_and_forget_commands(self):
        asked = self.deny_then_ask()
        ref = asked["hookSpecificOutput"]["permissionDecisionReason"].split(" guard wait ")[1].split()[0]

        def run(*argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = guard.main(self.home, list(argv))
            return code, json.loads(buf.getvalue())

        self.assertEqual(run("wait", ref, "--timeout", "0")[0], 3)
        listed = run("list")[1]["calls"]
        self.assertEqual([(c["ref"], c["state"]) for c in listed], [(ref, "waiting")])
        Page(link_of(asked)).approve(self.phone)
        with mock.patch.object(guard, "POLL_SECONDS", 0.1):
            code, done = run("wait", ref, "--timeout", "5")
        self.assertEqual((code, done["state"]), (0, "signed"))
        code, gone = run("forget", ref)
        self.assertEqual((code, gone["state"]), (0, "forgotten"))
        self.assertIsNone(guard.handle(self.cfg, self.event("PreToolUse")))
        self.assertEqual(run("wait", ref)[0], 2)

    def test_session_start_note(self):
        out = guard.handle(self.cfg, {"hook_event_name": "SessionStart", "source": "startup"})
        self.assertIn("yobiru.com/approve", out["hookSpecificOutput"]["additionalContext"])
        quiet = guard.Config(self.home, HOST, NODE, [key_text(self.phone)], note=False)
        self.assertIsNone(guard.handle(quiet, {"hook_event_name": "SessionStart"}))


class RenderTests(unittest.TestCase):
    def check(self, text):
        now = core.now_utc()
        intent = {"type": core.APPROVAL_TYPE, "v": 1, "to": {"host": HOST, "agent": "claude-code"},
                  "nonce": "AAAAAAAAAAAAAAAAAAAAAA", "issued_at": core.iso(now),
                  "expires_at": core.iso(now + core.dt.timedelta(hours=1)), "instruction": text, "files": [], "uses": 1}
        core.validate_intent(intent)
        self.assertLessEqual(len(core.json_canonical(intent).encode()), core.INTENT_MAX_BYTES)

    def test_hidden_characters_are_shown(self):
        b = guard.binding("Bash", {"command": "echo ok‮​\r; rm x"}, "/tmp")
        text = guard.render("en", b)
        self.assertIn("echo ok\\u{202E}\\u{200B}\\u{000D}; rm x", text)
        self.check(text)

    def test_long_input_is_cut_but_bound(self):
        b = guard.binding("Write", {"file_path": "/etc/app.conf", "content": "x = 1\n" * 2000}, "/")
        for lang in guard.LANGS:
            text = guard.render(lang, b)
            self.assertLessEqual(len(text), 4000)
            self.assertIn(guard.call_digest(b), text)
            self.check(text)
        self.assertIn("more characters not shown", guard.render("en", b))
        emoji = guard.binding("Bash", {"command": "echo " + "\U0001F600" * 3000}, "/")
        self.check(guard.render("en", emoji))

    def test_edit_and_other_tools(self):
        edit = guard.binding("Edit", {"file_path": "/a.py", "old_string": "x = 1", "new_string": "x = 2",
                                      "replace_all": True}, "/")
        self.assertEqual(guard.render("en", edit),
                         "Claude Code changes this file once:\nFile: /a.py\nEvery occurrence.\n\nReplace:\nx = 1\n\n"
                         "With:\nx = 2\n\nExact call: sha256 %s" % guard.call_digest(edit))
        mcp = guard.binding("mcp__hayabuild__ci_release_add_app", {"repo": "chekusu/visio", "team": "FKSR2JD63B"}, "/")
        text = guard.render("ja", mcp)
        self.assertTrue(text.startswith("Claude Code が mcp__hayabuild__ci_release_add_app を次の入力で1回使います："))
        self.assertIn('"repo": "chekusu/visio"', text)
        # Only a shell's cosmetic fields and folder are special; another tool's description is part of the call.
        self.assertNotEqual(guard.call_digest(guard.binding("Bash", {"command": "ls", "description": "a"}, "/")),
                            guard.call_digest(guard.binding("Bash", {"command": "ls"}, "/x")))
        self.assertEqual(guard.call_digest(guard.binding("Bash", {"command": "ls", "description": "a"}, "/")),
                         guard.call_digest(guard.binding("Bash", {"command": "ls", "timeout": 5}, "/")))
        self.assertNotEqual(guard.call_digest(guard.binding("mcp__x__y", {"description": "a"}, "/")),
                            guard.call_digest(guard.binding("mcp__x__y", {"description": "b"}, "/")))


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.settings = self.root / "claude" / "settings.json"
        self.settings.parent.mkdir()
        self.settings.write_text(json.dumps({"model": "x", "hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command": "cloud-yobiru hook", "timeout": 10}]}]}}))
        self.phone = SoftPasskey(label="phone")

    def run_cli(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = guard.main(self.home, list(argv))
        return code, json.loads(buf.getvalue())

    def test_install_is_idempotent_and_uninstall_keeps_other_hooks(self):
        args = ("install", "--settings", str(self.settings), "--host", "TYO.example.ts.net", "--key",
                key_text(self.phone), "--lang", "zh-CN", "--protect", r"^git push\b")
        code, done = self.run_cli(*args)
        self.assertEqual((code, done["state"], done["computer"]), (0, "installed", "tyo.example.ts.net"))
        self.assertTrue(Path(done["backup"]).is_file())
        code, again = self.run_cli(*args)
        self.assertEqual(again["replaced_entries"], 3)
        settings = json.loads(self.settings.read_text())
        self.assertEqual(settings["model"], "x")
        self.assertEqual(sorted(settings["hooks"]), ["PermissionDenied", "PreToolUse", "SessionStart"])
        self.assertEqual(len(settings["hooks"]["SessionStart"]), 2)
        pre = settings["hooks"]["PreToolUse"]
        self.assertEqual(len(pre), 1)
        self.assertEqual(pre[0]["matcher"], "*")
        command = pre[0]["hooks"][0]["command"]
        found = guard.installed_in(self.settings)
        self.assertEqual((found["computer"], found["keys"], found["lang"], found["protect"], found["script_exists"]),
                         ("tyo.example.ts.net", 1, "zh-CN", [r"^git push\b"], True))

        # The installed command itself works as a hook: nothing to say about an ordinary call, quickly.
        ev = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": "/"}
        started = time.monotonic()
        r = subprocess.run(command, shell=True, input=json.dumps(ev), capture_output=True, text=True, timeout=60)
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        self.assertLess(time.monotonic() - started, 5)
        r = subprocess.run(command, shell=True, input="not json", capture_output=True, text=True, timeout=60)
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        ev = {"hook_event_name": "SessionStart", "source": "startup"}
        r = subprocess.run(command, shell=True, input=json.dumps(ev), capture_output=True, text=True, timeout=60)
        self.assertIn("additionalContext", r.stdout)

        code, removed = self.run_cli("uninstall", "--settings", str(self.settings))
        self.assertEqual((code, removed["removed_entries"]), (0, 3))
        self.assertEqual(json.loads(self.settings.read_text())["hooks"],
                         {"SessionStart": [{"hooks": [{"type": "command", "command": "cloud-yobiru hook",
                                                       "timeout": 10}]}]})
        self.assertEqual(self.run_cli("uninstall", "--settings", str(self.settings))[1]["state"], "not_installed")

    def test_install_needs_an_owner_key(self):
        code, failed = self.run_cli("install", "--settings", str(self.settings), "--host", "tyo")
        self.assertEqual(code, 2)
        self.assertIn("yobiru approval setup", failed["error"])
        code, failed = self.run_cli("install", "--settings", str(self.settings), "--host", "tyo", "--key", "nope")
        self.assertEqual(code, 2)

    def test_install_uses_the_keys_this_data_home_trusts(self):
        self.home.mkdir()
        core.owner_key_add(self.home, self.phone.key())
        with mock.patch.object(guard._ap, "self_identity", lambda home: (HOST, NODE)):
            code, done = self.run_cli("install", "--settings", str(self.settings), "--print")
        self.assertEqual((code, done["state"], done["computer"], done["node"]), (0, "not_written", HOST, NODE))
        self.assertEqual(len(done["keys"]), 1)
        self.assertNotIn("PreToolUse", json.loads(self.settings.read_text())["hooks"])


if __name__ == "__main__":
    unittest.main()
