"""Small text attachments: opt-in through the `files` capability (checked by presence, also for empty files),
validated by the gateway before any effect, bounded by daily and retained byte quotas that retention and deletion
release, bound into the request and draft hashes, invisible to the worker before admission, and withheld again on
every path once `files` is withdrawn. The sender CLI reads only safe files below an allowed root through anchored
descriptors; the recipient CLI revalidates everything and writes only into a new private folder.

Real bot_gateway.py subprocesses and the real CLI, synthetic homes and files in a temporary directory, loopback.
"""
import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from support.public_harness import C, FriendsGateway, cli

import bot_client as P  # noqa: E402

LIMITS = {"min_deadline_seconds": 1, "read_timeout_seconds": 5, "messages_per_minute_per_actor": 1000,
          "messages_per_hour_per_actor": 5000, "pending_per_contact": 100, "new_conversations_per_hour_per_bot": 1000,
          "new_contacts_per_hour_per_bot": 1000, "enroll_per_hour_per_ip": 1000, "enroll_per_hour": 1000,
          "bot_enroll_per_hour": 1000, "bot_enroll_per_hour_per_ip": 1000, "agent_messages_per_hour_per_bot": 1000,
          "conversations_per_contact": 50, "attachment_bytes_per_day_per_caller": 500,
          "attachment_retained_bytes_per_caller": 300, "attachment_total_bytes": 32768}
FILES_POLICY = {"offered": ["faq", "chat", "files"], "visitor": ["faq", "chat", "files"]}


def item(name="notes.md", content="hello", size=None):
    return {"name": name, "content": content if size is None else "x" * size}


class AttachmentCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="yobiru attachments test ")
        cls.root = Path(cls._tmp.name)
        cls.gw = FriendsGateway(cls.root, limits=LIMITS).start()
        cls.owner = cls.gw.owner_token

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls._tmp.cleanup()

    def handle(self, tag=""):
        return "t%s%s" % (hashlib.sha256(self.id().encode()).hexdigest()[:12], "-" + tag if tag else "")

    def files_bot(self, files=True, tag=""):
        bot_id = self.gw.bot(self.handle(tag))
        if files:
            self.gw.policy(bot_id, **FILES_POLICY)
        return bot_id

    def chat(self, cid, token, attachments, text="see attached", op_key=None):
        return self.gw.send(cid, token, kind="chat", text=text, attachments=attachments, op_key=op_key)

    def stored(self, cid):
        return len(self.gw.owner_view(cid)["messages"])

    def err(self, result):
        return result[0], result[1].get("error")


class GatewayTests(AttachmentCase):
    def test_files_are_opt_in_and_checked_by_presence(self):
        plain = self.files_bot(files=False)
        _, token = self.gw.actor()
        cid = self.gw.conversation(plain, token)
        for files in ([item()], [item(content="")]):
            self.assertEqual((403, "capability_not_granted"), self.err(self.chat(cid, token, files)), files)
        wtoken, _ = self.gw.native(plain)
        mid = self.gw.chat(cid, token, "text only")
        self.assertEqual(200, self.gw.admit(plain, mid)[0])
        status, job = self.gw.claim_job(wtoken, mid)
        status, obj = self.gw.call("POST", "/v1/worker/messages/%s/finish" % mid,
                                   {"lease_id": job["lease"]["lease_id"], "fence": job["lease"]["fence"],
                                    "outcome": "completed", "reply": {"text": "x", "attachments": [item(content="")]}},
                                   wtoken)
        self.assertEqual((403, "files_not_granted"), (status, obj["error"]), "an empty file is still a file")
        offering = self.files_bot(tag="offering")
        cid2 = self.gw.conversation(offering, token)
        self.assertEqual((400, "attachments_not_supported"), self.err(self.gw.send(
            cid2, token, kind="faq", text="q", attachments=[item()])))
        self.assertEqual(0, self.stored(cid2))

    def test_names_and_contents_are_validated_before_any_effect(self):
        bot_id = self.files_bot()
        _, token = self.gw.actor()
        cid = self.gw.conversation(bot_id, token)
        bad = [[item("../a.md")], [item("a/b.md")], [item(".env")], [item("CON.txt")], [item("x.exe")],
               [item("evil‮txt.md")], [item("zero​width.md")], [item(content="nul\x00")],
               [item(content="bell\x07")], [item(size=16385)], [item("%d.md" % i) for i in range(9)],
               [item("%d.md" % i, size=16000) for i in range(3)], [item("A.md"), item("a.md")],
               [dict(item(), path="/etc/passwd")], [dict(item(), url="https://example.com/x")], [],
               "notes.md", [{"name": "x.md"}], [item(content=None)]]
        for files in bad:
            status, obj = self.chat(cid, token, files)
            self.assertEqual(400, status, (files, obj))
        self.assertEqual(0, self.stored(cid), "nothing was stored")
        self.assertEqual(202, self.chat(cid, token, [item("ok.md", ""), item("README", "plain\r\nlines\t.")])[0])

    def test_daily_and_retained_quotas_are_checked_first_and_released_by_deletion(self):
        bot_id = self.files_bot()
        _, token = self.gw.actor()
        cid = self.gw.conversation(bot_id, token)
        keys = [C.new_token("ticket")[4:24] for _ in range(3)]
        for key in keys:
            self.assertEqual(202, self.chat(cid, token, [item(size=100)], op_key=key)[0])
        status, obj = self.chat(cid, token, [item(size=100)])
        self.assertEqual((429, "attachment_quota", "retained"), (status, obj["error"], obj.get("scope")))
        self.assertEqual(200, self.chat(cid, token, [item(size=100)], op_key=keys[0])[0], "a retry is free")
        self.gw.ok("DELETE", "/v1/owner/conversations/%s" % cid, None, self.owner)
        cid2 = self.gw.conversation(bot_id, token)
        self.assertEqual([202, 202], [self.chat(cid2, token, [item(size=100)])[0] for _ in range(2)])
        status, obj = self.chat(cid2, token, [item(size=100)])
        self.assertEqual((429, "attachment_quota", "daily"), (status, obj["error"], obj.get("scope")),
                         "500 bytes a day: deletion released the stored bytes, not the daily quota")

    def test_a_reply_counts_toward_the_callers_retained_exposure(self):
        bot_id = self.files_bot()
        wtoken, _ = self.gw.native(bot_id)
        _, token = self.gw.actor()
        cid = self.gw.conversation(bot_id, token)
        status, sent = self.chat(cid, token, [item(size=200)])
        self.assertEqual(202, status, sent)
        mid = sent["message_id"]
        self.assertEqual(200, self.gw.admit(bot_id, mid)[0])
        status, job = self.gw.claim_job(wtoken, mid)
        finish = {"lease_id": job["lease"]["lease_id"], "fence": job["lease"]["fence"], "outcome": "completed"}
        status, obj = self.gw.call("POST", "/v1/worker/messages/%s/finish" % mid,
                                   dict(finish, reply={"text": "x", "attachments": [item("big.md", size=150)]}), wtoken)
        self.assertEqual((429, "attachment_quota", "retained"), (status, obj["error"], obj.get("scope")))
        status, obj = self.gw.call("POST", "/v1/worker/messages/%s/finish" % mid,
                                   dict(finish, reply={"text": "x", "attachments": [item("small.md", size=50)]}), wtoken)
        self.assertEqual(200, status, obj)

    def test_before_admission_the_worker_learns_nothing_about_attachments(self):
        bot_id = self.files_bot()
        wtoken, _ = self.gw.native(bot_id)
        _, token = self.gw.actor()
        cid = self.gw.conversation(bot_id, token)
        mid = self.chat(cid, token, [item("SECRETNAME.md", "SECRET-CONTENT")])[1]["message_id"]
        listing = json.dumps(self.gw.ok("GET", "/v1/worker/jobs", None, wtoken))
        for leak in ("SECRETNAME", "SECRET-CONTENT", '"attachments"', "bytes"):
            self.assertNotIn(leak, listing, leak)
        pending = [x for x in self.gw.reviews(bot_id)["inbound"] if x["job_id"] == mid][0]
        self.assertEqual({"count": 1, "bytes": 14}, pending["attachments_pending"])
        self.assertEqual("SECRET-CONTENT", pending["message"]["attachments"][0]["content"],
                         "a message request is shown to the owner in full; the agent's listing stays metadata only")
        self.assertEqual(200, self.gw.admit(bot_id, mid)[0])
        job = [j for j in self.gw.ok("GET", "/v1/worker/jobs", None, wtoken)["jobs"] if j["job_id"] == mid][0]
        self.assertEqual(1, job["attachments"])
        self.assertNotIn("SECRETNAME", json.dumps(job))
        status, claimed = self.gw.claim_job(wtoken, mid)
        self.assertEqual("SECRET-CONTENT", claimed["untrusted_content"]["attachments"][0]["content"])

    def test_withdrawing_files_withholds_attachments_everywhere(self):
        bot_id = self.files_bot()
        wtoken, _ = self.gw.native(bot_id)
        actor_id, token = self.gw.actor()
        cid = self.gw.conversation(bot_id, token)
        mid = self.chat(cid, token, [item("question.md", "Q-CONTENT")])[1]["message_id"]
        self.assertEqual(200, self.gw.admit(bot_id, mid)[0])
        status, job = self.gw.claim_job(wtoken, mid)
        self.assertEqual(200, self.gw.call("POST", "/v1/worker/messages/%s/finish" % mid,
                                           {"lease_id": job["lease"]["lease_id"], "fence": job["lease"]["fence"],
                                            "outcome": "completed",
                                            "reply": {"text": "answer", "attachments": [item("a.md", "A-CONTENT")]}},
                                           wtoken)[0])
        self.assertEqual(200, self.gw.approve_reply(bot_id, mid)[0])
        self.assertEqual("A-CONTENT", self.gw.ok("GET", "/v1/messages/%s" % mid, None,
                                                 token)["result"]["attachments"][0]["content"])
        self.assertEqual(200, self.gw.contact(bot_id, actor_id, "restrict", ["faq", "chat"])[0])
        view = self.gw.ok("GET", "/v1/messages/%s" % mid, None, token)
        self.assertIn("result_withheld", view)
        self.assertEqual({"count": 1, "reason": "files_not_granted"}, view["content"]["attachments_withheld"])
        self.assertNotIn("CONTENT", json.dumps(view))
        later = self.gw.chat(cid, token, "text only now")
        self.assertEqual(200, self.gw.admit(bot_id, later)[0])
        status, job = self.gw.claim_job(wtoken, later)
        self.assertEqual(200, status, job)
        self.assertTrue(job["context_reset"])
        self.assertNotIn("CONTENT", json.dumps(job["transcript"]))
        self.assertEqual(200, self.gw.contact(bot_id, actor_id, "unrestrict")[0])
        self.assertIn("result_withheld", self.gw.ok("GET", "/v1/messages/%s" % mid, None, token),
                      "widening again revives nothing")


class SenderPathTests(AttachmentCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="work-", dir=str(self.root)))
        self.home = self.work / "home"
        self.project = self.work / "project"
        self.outside = self.work / "outside"
        for d in (self.project / "sub", self.project / ".git", self.project / "secrets", self.project / "dir.md",
                  self.outside):
            d.mkdir(parents=True)
        files = {"notes.md": "hello", "sub/deep.md": "deep", ".env": "TOKEN=1", ".git/config": "x", "secrets/a.txt": "s",
                 "id_rsa": "key", "credentials.json": "{}", "big.md": "x" * 16385, "ctrl.md": "bell\x07",
                 "run.sh": "echo", "CON.txt": "dev"}
        for name, text in files.items():
            (self.project / name).write_text(text)
        (self.project / "bin.md").write_bytes(b"\xff\xfe\x00")
        (self.outside / "secret.md").write_text("OUTSIDE-SECRET")
        (self.outside / "plain.md").write_text("OUTSIDE-SECRET")
        os.symlink(str(self.outside / "secret.md"), str(self.project / "link-out.md"))
        os.symlink(str(self.project / "notes.md"), str(self.project / "link-in.md"))
        os.symlink(str(self.outside), str(self.project / "linkdir"))
        os.mkfifo(str(self.project / "fifo.md"))
        bot_id = self.files_bot()
        self.alias = cli(self.home, "connect", "%s/@bot_%s" % (self.gw.url, bot_id[4:]), "--alias", "target",
                         "--name", "Synthetic sender")[1]["alias"]

    def send(self, *paths):
        args = ["chat", self.alias, "with files"]
        for path in paths:
            args += ["--attach", str(path)]
        return cli(self.home, *args)

    def test_roots_must_be_project_folders(self):
        for root in (os.sep, str(Path.home()), str(self.home), str(self.project / ".git"), str(self.project / "secrets"),
                     str(self.work / "missing")):
            code, obj, err = cli(self.home, "attachments", "allow", root)
            self.assertEqual(2, code, (root, obj, err))
        code, obj, err = self.send(self.project / "notes.md")
        self.assertIn("no attachment root is allowed", err)

    def test_only_safe_files_below_the_root_are_read(self):
        self.assertEqual(0, cli(self.home, "attachments", "allow", str(self.project))[0])
        refused = {".env": "hidden", ".git/config": "hidden", "secrets/a.txt": "secret", "id_rsa": "credentials",
                   "credentials.json": "credentials", "big.md": "larger", "bin.md": "UTF-8", "ctrl.md": "control",
                   "run.sh": "text file", "CON.txt": "reserved", "dir.md": "regular", "fifo.md": "regular",
                   "link-out.md": "symbolic link", "link-in.md": "symbolic link", "linkdir/plain.md": "symbolic link",
                   "../outside/secret.md": "traversal", str(self.outside / "secret.md"): "outside"}
        for rel, reason in refused.items():
            path = rel if rel.startswith(os.sep) or rel.startswith("..") else str(self.project / rel)
            code, obj, err = self.send(path)
            self.assertEqual(2, code, (rel, obj))
            self.assertIn(reason, err, rel)
            self.assertIn("nothing was sent", err)
        self.assertEqual([], P.SentLog(P.State(self.home)).open_ops(), "no send was even recorded")
        cwd = os.getcwd()
        os.chdir(str(self.project))
        self.addCleanup(os.chdir, cwd)
        code, sent, err = self.send("notes.md", "sub/deep.md")
        self.assertEqual(0, code, err)
        self.assertEqual("queued", sent["state"])
        self.assertNotIn("OUTSIDE-SECRET", json.dumps(sent))

    def test_a_replaced_root_is_refused(self):
        self.assertEqual(0, cli(self.home, "attachments", "allow", str(self.project))[0])
        os.rename(str(self.project), str(self.work / "moved"))
        (self.project).mkdir()
        (self.project / "notes.md").write_text("planted")
        code, obj, err = self.send(self.project / "notes.md")
        self.assertEqual(2, code)
        self.assertIn("replaced", err)


class ExportTests(AttachmentCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="export-", dir=str(self.root)))
        self.st = P.State(self.home)
        self.received = self.home / "public" / "received"

    def folders(self):
        return sorted(os.listdir(str(self.received))) if self.received.is_dir() else []

    def export(self, items):
        try:
            with redirect_stderr(io.StringIO()):
                return P.export_attachments(self.st, items, {"test": True})
        except SystemExit as e:
            return e

    def test_every_item_is_revalidated_before_anything_is_written(self):
        sha = lambda text: hashlib.sha256(text.encode()).hexdigest()  # noqa: E731
        bad = [[item(content="nul\x00")], [item("%d.md" % i) for i in range(9)], [item(size=20000)],
               [item("a.md"), item("A.md")], [dict(item(), bytes=99)], [dict(item(), sha256=sha("other"))],
               ["not an item"], [dict(item(), path="/etc/passwd")], [item(content=7)], [item(".env")],
               [item("x.html")], "notes.md"]
        for items in bad:
            self.assertIsInstance(self.export(items), SystemExit, items)
            self.assertEqual([], self.folders(), items)
        saved = self.export([dict(item("a.md", "first"), bytes=5, sha256=sha("first")), item("B.txt", "second")])
        self.assertEqual(1, len(self.folders()))
        folder = Path(saved["folder"])
        self.assertEqual(0o700, stat.S_IMODE(folder.stat().st_mode))
        for f in saved["files"]:
            mode = os.lstat(f["path"]).st_mode
            self.assertEqual(0o600, stat.S_IMODE(mode))
        self.assertEqual({"a.md": "first", "B.txt": "second"},
                         {f["name"]: Path(f["path"]).read_text() for f in saved["files"]})

    def test_a_planted_link_is_never_followed(self):
        target = Path(tempfile.mkdtemp(prefix="elsewhere-", dir=str(self.root)))
        (self.home / "public").mkdir(parents=True)
        os.symlink(str(target), str(self.received))
        self.assertIsInstance(self.export([item()]), SystemExit)
        self.assertEqual([], os.listdir(str(target)))


class CliRoundtripTests(AttachmentCase):
    def test_send_review_claim_reply_approve_save_verify_then_revoke(self):
        homes = {r: Path(tempfile.mkdtemp(prefix="%s-" % r, dir=str(self.root))) for r in
                 ("owner", "caller", "host")}
        files = {r: Path(tempfile.mkdtemp(prefix="%s-files-" % r, dir=str(self.root))) for r in ("caller", "host")}
        (files["caller"] / "question.md").write_text("Caller file\n")
        (files["host"] / "answer.md").write_text("Host file\n")

        def y(role, *args, expect=0):
            code, obj, err = cli(homes[role], *args)
            self.assertEqual(expect, code, "%s %s\n%s\n%s" % (role, args, obj, err))
            return obj
        handle = self.handle()
        y("owner", "owner", "login", self.gw.url, "--token-file", str(self.gw.home / "owner.token"))
        bot_id = y("owner", "bot", "create", "--handle", handle, "--name", "Files bot")["bot_id"]
        y("owner", "bot", "publish", handle)
        y("owner", "bot", "edit", handle, "--offered", "faq,chat,files",
          "--visitor-capabilities", "faq,chat,files")
        token_file = homes["owner"] / "native.token"
        y("owner", "worker", "bind", handle, "--adapter", "native", "--token-file", str(token_file))
        y("host", "worker", "login", self.gw.url, "--token-file", str(token_file))
        y("caller", "connect", "%s/@%s" % (self.gw.url, handle), "--name", "Synthetic caller")
        for role in ("caller", "host"):
            y(role, "attachments", "allow", str(files[role]))
        sent = y("caller", "chat", handle, "please read", "--attach", str(files["caller"] / "question.md"))
        mid = sent["message_id"]
        y("owner", "review", "approve-inbound", handle, mid)
        claimed = y("host", "job", "claim", bot_id, mid)
        self.assertEqual("Caller file\n", claimed["untrusted_content"]["attachments"][0]["content"])
        exported = y("host", "job", "attachments", bot_id, mid)
        self.assertEqual("Caller file\n", Path(exported["files"][0]["path"]).read_text())
        y("host", "job", "reply", bot_id, mid, "--text", "see mine", "--attach", str(files["host"] / "answer.md"))
        draft = [x for x in y("owner", "review", "list", handle)["replies"] if x["job_id"] == mid][0]
        self.assertEqual("Host file\n", draft["draft"]["attachments"][0]["content"])
        y("owner", "review", "approve-reply", handle, mid, "--draft-hash", draft["draft_hash"])
        status = y("caller", "status", mid)
        self.assertEqual(("completed", "Host file\n"), (status["state"], status["result"]["attachments"][0]["content"]))
        self.assertTrue(status["client_ack"][0]["acknowledged_at"], "the client acknowledged what it received")
        saved = y("caller", "attachments", "save", mid)
        path = Path(saved["files"][0]["path"])
        self.assertEqual(("Host file\n", 0o600), (path.read_text(), stat.S_IMODE(path.stat().st_mode)))
        self.assertTrue(y("caller", "verify", mid)["verified"])
        before = sorted(os.listdir(str(homes["caller"] / "public" / "received")))
        actor_id = json.loads((homes["caller"] / "public" / "credentials.json").read_text())["gateways"][
            self.gw.url]["actor"]["actor_id"]
        y("owner", "owner", "contact", handle, actor_id, "revoke")
        y("caller", "attachments", "save", mid, expect=2)
        self.assertEqual(before, sorted(os.listdir(str(homes["caller"] / "public" / "received"))),
                         "a revoked caller saves nothing new; copies already received stay its own")


if __name__ == "__main__":
    unittest.main()
