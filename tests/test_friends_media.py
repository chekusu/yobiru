"""Media attachments: images, PDFs, audio and video travel as base64 `data` beside text files, checked by extension and
file signature at the sender, the gateway and the recipient; friends (mutual follows) exchange files by default; CLI
output never prints the base64 (the files are saved instead); the gateway's MCP tools hand images to a model as image
content blocks; transcripts carry media metadata only.

Real bot_gateway.py subprocesses and the real CLI, synthetic homes and files in a temporary directory, loopback.
"""
import base64
import hashlib
import io
import itertools
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from support.public_harness import PUBLIC, C, FriendsGateway, cli, plain_env
from test_friends_e2e import E2ECase

import bot_gateway as G  # noqa: E402

import bot_client as P  # noqa: E402
import bot_mcp as M  # noqa: E402

LIMITS = {"min_deadline_seconds": 1, "read_timeout_seconds": 5, "messages_per_minute_per_actor": 1000,
          "messages_per_hour_per_actor": 5000, "pending_per_contact": 100, "new_conversations_per_hour_per_bot": 1000,
          "new_contacts_per_hour_per_bot": 1000, "enroll_per_hour_per_ip": 1000, "enroll_per_hour": 1000,
          "bot_enroll_per_hour": 1000, "bot_enroll_per_hour_per_ip": 1000, "agent_messages_per_hour_per_bot": 1000,
          "conversations_per_contact": 50, "attachment_media_max_bytes": 300000}
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4
JPG = b"\xff\xd8\xff\xe0" + b"\x00\x10JFIF" + bytes(200)
PDF = b"%PDF-1.7\n%menu\n" + b"0" * 100
EXE = b"MZ\x90\x00" + bytes(100)
COUNTER = itertools.count(1)


def b64(raw):
    return base64.b64encode(raw).decode("ascii")


def media(name="menu.png", raw=PNG):
    return {"name": name, "data": b64(raw)}


class MediaCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="yobiru media test ")
        cls.root = Path(cls._tmp.name)
        cls.gw = FriendsGateway(cls.root, limits=LIMITS).start()

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls._tmp.cleanup()

    def friends(self):
        """A (answered by a native host) and B, mutual follows, both on the default policy.
        -> (a, wtoken, b, b_source, conversation of B with A)."""
        n = next(COUNTER)
        a, a_source = self.gw.owned_bot("media-a%d" % n)
        b, b_source = self.gw.owned_bot("media-b%d" % n)
        self.assertEqual(201, self.gw.follow(b_source, a)[0])
        self.assertEqual(201, self.gw.follow(a_source, b)[0])
        wtoken, _ = self.gw.native(a)
        return a, wtoken, b, b_source, self.gw.conversation(a, b_source)

    def chat(self, cid, token, attachments, text="here is the menu"):
        return self.gw.send(cid, token, kind="chat", text=text, attachments=attachments)


class ValidationTests(unittest.TestCase):
    def check(self, items, media_max=C.MEDIA_MAX_BYTES):
        return C.attachments_error(items, 8, 16384, 8 * 1024 * 1024, media_max)

    def test_signatures_must_match_the_extension(self):
        ok = [media("a.png"), media("b.JPG", JPG), media("c.pdf", PDF), media("d.webp", b"RIFF\x10\x00\x00\x00WEBPVP8 "),
              media("e.gif", b"GIF89a" + bytes(10)), media("f.mp3", b"ID3\x04" + bytes(10)),
              media("g.mp4", b"\x00\x00\x00\x18ftypisom" + bytes(12)),
              {"name": "notes.md", "content": "hi"}]
        clean, total, err = self.check(ok)
        self.assertIsNone(err)
        self.assertEqual(len(ok), len(clean))
        self.assertEqual(sum(len(base64.b64decode(x["data"])) if "data" in x else 2 for x in ok), total)
        for bad in ([media("menu.png", EXE)], [media("menu.jpg", PNG)], [media("menu.pdf", b"<html>")],
                    [media("menu.heic", b"\x00\x00\x00\x18ftypisom" + bytes(12))], [media("x.svg", b"<svg/>")],
                    [media("x.exe", EXE)], [media("x.html", b"<html>")], [media("notes.md", b"hi")],
                    [{"name": "menu.png", "content": "not base64 data"}],
                    [{"name": "menu.png", "data": b64(PNG)[:-2] + "\n=="}], [{"name": "menu.png", "data": "####"}],
                    [{"name": "menu.png", "data": b64(PNG).rstrip("=") + "A"}], [{"name": "menu.png", "data": ""}],
                    [{"name": "menu.png", "data": b64(PNG), "content": "x"}], [media("a.png"), media("A.PNG")]):
            self.assertIsNotNone(self.check(bad)[2], bad)
        self.assertIsNone(self.check([media("h.heic", b"\x00\x00\x00\x18ftypheic" + bytes(12))])[2])
        self.assertIn("larger", self.check([media("big.png", PNG + bytes(2000))], media_max=1000)[2])

    def test_views_and_sizes(self):
        item = media()
        view = C.attachment_view(item)
        self.assertEqual({"name": "menu.png", "bytes": len(PNG), "sha256": hashlib.sha256(PNG).hexdigest(),
                          "media_type": "image/png", "data": item["data"]}, view)
        self.assertNotIn("data", C.attachment_view(item, content=False))
        self.assertEqual(len(PNG), C.attachment_bytes([item]))
        self.assertEqual({"name": "a.md", "bytes": 2, "sha256": hashlib.sha256(b"hi").hexdigest(), "content": "hi"},
                         C.attachment_view({"name": "a.md", "content": "hi"}), "a text view is unchanged")
        omitted = C.omit_media_data({"x": [view]}, "hint")["x"][0]
        self.assertEqual("hint", omitted["data_omitted"])
        self.assertNotIn("data", omitted)

    def test_mcp_results_carry_images_as_image_blocks(self):
        view = C.attachment_view(media())
        pdf = C.attachment_view(media("menu.pdf", PDF))
        res = M.tool_result(200, {"ok": True, "untrusted_content": {"text": "menu", "attachments": [view, pdf]}})
        self.assertEqual(["text", "image", "resource"], [b["type"] for b in res["content"]])
        self.assertEqual({"type": "image", "data": view["data"], "mimeType": "image/png"}, res["content"][1])
        self.assertEqual(("application/pdf", pdf["data"]), (res["content"][2]["resource"]["mimeType"],
                                                            res["content"][2]["resource"]["blob"]))
        shown = res["structuredContent"]["untrusted_content"]["attachments"]
        self.assertEqual([1, 2], [x["content_block"] for x in shown])
        self.assertNotIn(view["data"], res["content"][0]["text"])


class GatewayTests(MediaCase):
    def test_friends_exchange_media_by_default_and_strangers_do_not(self):
        a, wtoken, b, b_source, cid = self.friends()
        status, sent = self.chat(cid, b_source, [media(), media("menu.pdf", PDF)])
        self.assertEqual(202, status, sent)
        mid = sent["message_id"]
        if [x for x in self.gw.reviews(a)["inbound"] if x["job_id"] == mid]:
            self.assertEqual(200, self.gw.admit(a, mid)[0])
        status, job = self.gw.claim_job(wtoken, mid)
        self.assertEqual(200, status, job)
        files = job["untrusted_content"]["attachments"]
        self.assertEqual(["image/png", "application/pdf"], [f["media_type"] for f in files])
        self.assertEqual(PNG, base64.b64decode(files[0]["data"]))
        reply = {"text": "here is my photo", "attachments": [media("dish.jpg", JPG)]}
        status, done = self.gw.call("POST", "/v1/worker/messages/%s/finish" % mid,
                                    {"lease_id": job["lease"]["lease_id"], "fence": job["lease"]["fence"],
                                     "outcome": "completed", "reply": reply}, wtoken)
        self.assertEqual(200, status, done)
        if done["state"] == "review":
            self.assertEqual(200, self.gw.approve_reply(a, mid)[0])
        view = self.gw.ok("GET", "/v1/messages/%s" % mid, None, b_source)
        self.assertEqual(JPG, base64.b64decode(view["result"]["attachments"][0]["data"]))
        # the next turn's transcript names the earlier files but does not repeat their data
        later = self.gw.chat(cid, b_source, "thanks")
        if [x for x in self.gw.reviews(a)["inbound"] if x["job_id"] == later]:
            self.assertEqual(200, self.gw.admit(a, later)[0])
        status, job2 = self.gw.claim_job(wtoken, later)
        self.assertEqual(200, status, job2)
        turns = json.dumps(job2["transcript"])
        self.assertIn("menu.png", turns)
        self.assertNotIn(files[0]["data"][:40], turns)
        # a stranger (a visitor) gets no files from the default policy, and no large body either
        _, visitor = self.gw.actor()
        vcid = self.gw.conversation(a, visitor)
        self.assertEqual((403, "capability_not_granted"),
                         (lambda s, o: (s, o.get("error")))(*self.chat(vcid, visitor, [media()])))
        status, obj = self.chat(vcid, visitor, [media("big.png", PNG * 30)])
        self.assertEqual(413, status, obj)

    def test_an_unfollow_withholds_files_but_keeps_the_rest_of_the_conversation(self):
        a, wtoken, b, b_source, cid = self.friends()
        a_source = self.gw.source_for(a)

        def answered(attachments):
            status, sent = self.chat(cid, b_source, attachments) if attachments else (202, {
                "message_id": self.gw.chat(cid, b_source, "text only")})
            self.assertEqual(202, status, sent)
            mid = sent["message_id"]
            if [x for x in self.gw.reviews(a)["inbound"] if x["job_id"] == mid]:
                self.assertEqual(200, self.gw.admit(a, mid)[0])
            status, job = self.gw.claim_job(wtoken, mid)
            self.assertEqual(200, status, job)
            status, done = self.gw.reply_job(wtoken, job, "answer")
            if done.get("state") == "review":
                self.assertEqual(200, self.gw.approve_reply(a, mid)[0])
            return mid
        plain, with_file = answered(None), answered([media()])
        self.assertEqual(200, self.gw.unfollow(a_source, b)[0])
        self.assertEqual("answer", self.gw.ok("GET", "/v1/messages/%s" % plain, None, b_source)["result"]["text"])
        view = self.gw.ok("GET", "/v1/messages/%s" % with_file, None, b_source)
        self.assertIn("result_withheld", view)
        self.assertEqual("files_not_granted", view["content"]["attachments_withheld"]["reason"])

    def test_a_forged_file_is_refused_before_any_effect(self):
        a, _, _, b_source, cid = self.friends()
        for bad in ([media("menu.png", EXE)], [media("menu.png", PNG * 400)], [{"name": "menu.png", "content": "x"}]):
            status, obj = self.chat(cid, b_source, bad)
            self.assertEqual((400, "invalid_attachments"), (status, obj.get("error")), bad)
        self.assertEqual([], self.gw.owner_view(cid)["messages"])


class CliTests(MediaCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="work-", dir=str(self.root)))

    def test_send_receive_and_save_through_the_cli(self):
        a, wtoken, _, _, _ = self.friends()
        folder = self.work / "photos"
        folder.mkdir()
        (folder / "menu.png").write_bytes(PNG)
        (folder / "fake.png").write_bytes(EXE)
        home = self.work / "b-home"
        connected = cli(home, "connect", "%s/@bot_%s" % (self.gw.url, a[4:]), "--alias", "a", "--name", "B")[1]
        alias = connected["alias"]
        self.assertEqual(200, self.gw.contact(a, connected["as"], "grant", ["chat", "files"])[0])
        self.assertEqual(0, cli(home, "attachments", "allow", str(folder))[0])
        code, obj, err = cli(home, "chat", alias, "the menu", "--attach", str(folder / "fake.png"))
        self.assertEqual(2, code)
        self.assertIn("not really a .png file", err)
        code, sent, err = cli(home, "chat", alias, "the menu", "--attach", str(folder / "menu.png"))
        self.assertEqual(0, code, err)
        mid = sent["message_id"]
        # the host side: claim prints no base64, then saves the picture as a file
        host = self.work / "a-home"
        st = P.State(host)
        if [x for x in self.gw.reviews(a)["inbound"] if x["job_id"] == mid]:
            self.assertEqual(200, self.gw.admit(a, mid)[0])
        status, job = self.gw.claim_job(wtoken, mid)
        self.assertEqual(200, status, job)
        stashed = P.stash_job_files(st, mid, job)
        self.assertNotIn(b64(PNG)[:40], json.dumps(stashed))
        self.assertTrue(stashed["untrusted_content"]["attachments_stashed"])
        lease = {"job": stashed}
        with redirect_stderr(io.StringIO()):
            files = P.cached_job(st, mid, lease)["untrusted_content"]["attachments"]
            saved = P.export_attachments(st, files, {"job_id": mid})
        self.assertEqual(PNG, Path(saved["files"][0]["path"]).read_bytes())
        self.assertEqual("image/png", saved["files"][0]["media_type"])
        # printed output never carries the base64
        buf = io.StringIO()
        from contextlib import redirect_stdout
        with redirect_stdout(buf):
            P.out(job)
        self.assertNotIn(b64(PNG)[:40], buf.getvalue())
        self.assertIn("attachments save", buf.getvalue())


class SealedMediaTests(E2ECase):
    def test_a_sealed_menu_photo_reaches_the_host_and_back(self):
        bot_id, _ = self.bot_with_host()
        self.y("caller", "connect", "%s/@%s" % (self.gw.url, self.handle), "--name", "Sealed caller")
        (self.files / "menu.png").write_bytes(PNG)
        self.y("caller", "attachments", "allow", str(self.files))
        sent = self.y("caller", "chat", self.handle, "tonight's menu", "--attach", str(self.files / "menu.png"))
        mid = sent["message_id"]
        content = json.loads(self.stored(mid)["content"])
        self.assertEqual({"count": 1, "bytes": len(PNG)}, content["files"])
        self.assertNotIn(b64(PNG)[:24], json.dumps(self.stored(mid)), "the gateway holds ciphertext only")
        self.y("owner", "review", "approve-inbound", self.handle, mid)
        job = self.y("host", "job", "claim", bot_id, mid)
        shown = job["untrusted_content"]["attachments"][0]
        self.assertEqual(("menu.png", "image/png", len(PNG)), (shown["name"], shown["media_type"], shown["bytes"]))
        self.assertNotIn("data", shown)
        leases = (self.homes["host"] / "public" / "leases.json").read_text()
        self.assertLess(len(leases), 20000, "the sealed files live beside the lease, not in it")
        saved = self.y("host", "job", "attachments", bot_id, mid)
        self.assertEqual(PNG, Path(saved["files"][0]["path"]).read_bytes())
        self.assertEqual(PNG, Path(self.y("host", "job", "show", bot_id, mid) and saved["files"][0]["path"])
                         .read_bytes())
        (self.files / "dish.jpg").write_bytes(JPG)
        self.y("host", "attachments", "allow", str(self.files))
        self.y("host", "job", "reply", bot_id, mid, "--text", "and a photo", "--attach", str(self.files / "dish.jpg"))
        draft = [x for x in self.y("owner", "review", "list", self.handle)["replies"] if x["job_id"] == mid][0]
        self.y("owner", "review", "approve-reply", self.handle, mid, "--draft-hash", draft["draft_hash"])
        got = self.y("caller", "attachments", "save", mid)
        self.assertEqual(JPG, Path(got["files"][0]["path"]).read_bytes())
        second = self.y("caller", "chat", self.handle, "thanks")["message_id"]
        self.y("owner", "review", "approve-inbound", self.handle, second)
        job2 = self.y("host", "job", "claim", bot_id, second)
        first = job2["transcript"][0]
        self.assertEqual({"count": 1, "reason": "earlier_turn"}, first["attachments_omitted"])
        self.assertEqual("tonight's menu", first["text"])


class MigrationTests(unittest.TestCase):
    def test_schema_5_gives_friends_files_unless_the_owner_changed_the_preset(self):
        old = {"offered": ["faq", "leave_message", "calendar_slots", "chat"], "visitor": ["faq"], "follower": ["faq"],
               "friend": ["chat", "faq", "leave_message"]}
        self.assertTrue(G.friend_files_policy(old))
        self.assertEqual((["faq", "leave_message", "chat", "files"], ["faq", "leave_message", "calendar_slots", "chat",
                                                                     "files"]), (old["friend"], old["offered"]))
        custom = {"offered": ["faq", "chat"], "visitor": [], "follower": [], "friend": ["chat"]}
        self.assertFalse(G.friend_files_policy(custom))
        self.assertEqual(["chat"], custom["friend"])
        with tempfile.TemporaryDirectory(prefix="yobiru migration 5-6 ") as tmp:
            gw = FriendsGateway(Path(tmp)).start()
            try:
                plain = gw.bot("plain-old")
                tuned = gw.bot("tuned-old", policy={"offered": ["faq", "chat"], "friend": ["faq"]})
            finally:
                gw.stop()
            db = gw.db()
            try:
                # the schema-5 default, which still had leave_message (today's API reads that name as chat)
                pol = json.loads(gw.sql("SELECT policy FROM bots WHERE id=?", (plain,))[0]["policy"])
                pol.update(offered=["faq", "leave_message", "chat"], friend=["faq", "leave_message", "chat"])
                db.execute("UPDATE bots SET policy=? WHERE id=?", (json.dumps(pol), plain))
                db.execute("UPDATE meta SET value='5' WHERE key='schema_version'")
            finally:
                db.close()
            done = subprocess.run([sys.executable, str(PUBLIC / "bot_gateway.py"), "--gateway-home", str(gw.home),
                                   "migrate"], capture_output=True, text=True, env=plain_env(Path(tmp)), timeout=60)
            self.assertEqual(0, done.returncode, done.stderr)
            summary = json.loads(done.stdout)
            self.assertEqual(("8", "5", 1), (summary["schema"], summary["from_schema"], summary["friend_files_bots"]))
            pols = {r["id"]: json.loads(r["policy"]) for r in gw.sql("SELECT id, policy FROM bots")}
            self.assertEqual((["faq", "chat", "files"], ["faq", "chat", "files"]),
                             (pols[plain]["friend"], pols[plain]["offered"]), "files added, then leave_message merged")
            self.assertEqual(["faq"], pols[tuned]["friend"])


if __name__ == "__main__":
    unittest.main()
