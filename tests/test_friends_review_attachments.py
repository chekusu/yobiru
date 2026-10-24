"""Owners can inspect request files without admitting a sender or releasing a job."""
import base64
import hashlib
import json
import stat
import subprocess
import sys
from pathlib import Path

from support.public_harness import PUBLIC, plain_env
from test_friends_client import FriendsClientCase, cli


PDF = b"%PDF-1.7\n%synthetic pending request\n" + b"0" * 100
FILES = {"request.pdf": PDF, "notes.txt": b"Synthetic request notes\n"}
BUNDLED_CLIENT = PUBLIC.parent / "plugins" / "yobiru" / "scripts" / "public_bots" / "bot_client.py"


class ReviewAttachmentTests(FriendsClientCase):
    def prepare(self):
        bot = self.owner_bot("review-files")
        self.gw.policy(bot, offered=["chat", "files"],
                       visitor=["chat", "files"])
        return bot

    def plain_request(self, bot, kind="chat", attachments=True):
        _, caller = self.gw.actor()
        cid = self.gw.conversation(bot, caller)
        files = [{"name": "request.pdf", "data": base64.b64encode(PDF).decode("ascii")},
                 {"name": "notes.txt", "content": FILES["notes.txt"].decode()}] if attachments else []
        status, sent = self.gw.send(cid, caller, kind=kind, text="Synthetic request",
                                   **({"attachments": files} if files else {}))
        self.assertIn(status, (201, 202), sent)
        return sent["message_id"], caller

    def export(self, mid, bundled=False):
        args = ["review", "attachments", "review-files", mid]
        if not bundled:
            return self.y("owner", *args)
        result = subprocess.run([sys.executable, str(BUNDLED_CLIENT), "--home", str(self.homes["owner"])] + args,
                                capture_output=True, text=True, timeout=30,
                                env=plain_env(self.homes["owner"]), cwd=str(self.root))
        self.assertEqual(0, result.returncode, result.stderr)
        self.outputs.append(result.stdout + result.stderr)
        return json.loads(result.stdout)

    def assert_saved(self, saved, bot, mid):
        self.assertTrue(saved["ok"])
        self.assertEqual({"bot_id": bot, "message_id": mid}, saved["source"])
        self.assertIn("untrusted", saved["content_trust"])
        folder = Path(saved["folder"])
        self.assertEqual(0o700, stat.S_IMODE(folder.stat().st_mode))
        self.assertEqual(set(FILES), {item["name"] for item in saved["files"]})
        for item in saved["files"]:
            path, expected = Path(item["path"]), FILES[item["name"]]
            self.assertEqual(folder, path.parent)
            self.assertEqual(expected, path.read_bytes())
            self.assertEqual((len(expected), hashlib.sha256(expected).hexdigest()),
                             (item["bytes"], item["sha256"]))
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertNotIn(base64.b64encode(PDF).decode("ascii"), json.dumps(saved))

    def request_state(self, mid):
        return self.gw.sql("SELECT state, review_in, lease_hash, lease_worker, result, revealed_ts "
                           "FROM messages WHERE id=?", (mid,))[0]

    def test_plain_pending_request_files_are_exported_by_source_and_bundled_client(self):
        bot = self.prepare()
        worker, _ = self.gw.native(bot)
        mid, caller = self.plain_request(bot)
        before = self.request_state(mid)
        shown = self.y("owner", "review", "list", "review-files")["inbound"][0]
        self.assertNotIn("data", shown["message"]["attachments"][0])
        self.assertIn("review attachments", shown["message"]["attachments"][0]["data_omitted"])
        for bundled in (False, True):
            with self.subTest(bundled=bundled):
                self.assert_saved(self.export(mid, bundled), bot, mid)
                self.assertEqual(before, self.request_state(mid))
                status, refused = self.gw.claim_job(worker, mid)
                self.assertEqual((409, "review_required"), (status, refused.get("error")))
                caller_view = self.gw.ok("GET", "/v1/messages/%s" % mid, token=caller)
                self.assertIsNone(caller_view.get("result"))
                self.assertEqual("queued", caller_view["state"])
        self.assert_no_credentials_printed()

    def test_sealed_first_contact_files_are_verified_and_exported_before_review(self):
        bot = self.prepare()
        self.native("review-files", bot)
        folder = self.root / "request-files"
        folder.mkdir()
        for name, content in FILES.items():
            (folder / name).write_bytes(content)
        for role, bundled in (("caller", False), ("other", True)):
            with self.subTest(bundled=bundled):
                self.y(role, "connect", self.gw.url + "/@review-files", "--name", "Synthetic sealed sender")
                self.y(role, "attachments", "allow", str(folder))
                mid = self.y(role, "chat", "review-files", "Synthetic sealed request",
                             "--attach", str(folder / "request.pdf"),
                             "--attach", str(folder / "notes.txt"))["message_id"]
                stored = self.gw.sql("SELECT content FROM messages WHERE id=?", (mid,))[0]["content"]
                self.assertIn("sealed_files", json.loads(stored))
                self.assertNotIn("Synthetic sealed request", stored)
                before = self.request_state(mid)
                # No review listing first: export must carry the caller key and conversation binding itself.
                self.assert_saved(self.export(mid, bundled), bot, mid)
                self.assertEqual(before, self.request_state(mid))
                refused = self.y("host", "job", "claim", bot, mid, expect=2)
                self.assertEqual("review_required", refused["error"])
                self.assertIsNone(self.y(role, "status", mid).get("result"))
        self.assert_no_credentials_printed()

    def test_an_old_clients_notice_files_are_exported_without_admitting_it(self):
        bot = self.prepare()
        mid, _ = self.plain_request(bot, kind="notice")             # since 0.7.14 a direct message like any other
        before = self.request_state(mid)
        self.assert_saved(self.export(mid), bot, mid)
        self.assertEqual(before, self.request_state(mid))
        self.assertEqual([mid], [item["job_id"] for item in self.gw.reviews(bot)["inbound"]])

    def test_tampered_sealed_files_are_refused_without_exporting_or_admitting(self):
        self.prepare()
        self.y("caller", "connect", self.gw.url + "/@review-files", "--name", "Synthetic sealed sender")
        folder = self.root / "sealed-files"
        folder.mkdir()
        file = folder / "request.pdf"
        file.write_bytes(PDF)
        self.y("caller", "attachments", "allow", str(folder))
        mid = self.y("caller", "chat", "review-files", "Synthetic sealed request",
                     "--attach", str(file))["message_id"]
        content = json.loads(self.gw.sql("SELECT content FROM messages WHERE id=?", (mid,))[0]["content"])
        ciphertext = content["sealed_files"]["ct"]
        content["sealed_files"]["ct"] = ("A" if ciphertext[0] != "A" else "B") + ciphertext[1:]
        self.gw.sql("UPDATE messages SET content=? WHERE id=?", (json.dumps(content), mid))
        before = self.request_state(mid)
        code, _, error = cli(self.homes["owner"], "review", "attachments", "review-files", mid)
        self.assertEqual(2, code)
        self.assertIn("unverified", error)
        self.assertFalse((self.homes["owner"] / "public" / "received").exists())
        self.assertEqual(before, self.request_state(mid))

    def test_wrong_selected_bot_and_requests_without_files_write_nothing(self):
        bot = self.prepare()
        mid, _ = self.plain_request(bot)
        other = self.y("owner", "bot", "create", "--handle", "other-bot", "--name", "Synthetic other")["bot_id"]
        code, _, error = cli(self.homes["owner"], "review", "attachments", "other-bot", mid)
        self.assertEqual(2, code)
        self.assertIn(other, error)
        self.assertFalse((self.homes["owner"] / "public" / "received").exists())
        empty, _ = self.plain_request(bot, attachments=False)
        code, _, error = cli(self.homes["owner"], "review", "attachments", "review-files", empty)
        self.assertEqual(2, code)
        self.assertIn("attachments", error)
        self.assertFalse((self.homes["owner"] / "public" / "received").exists())

    def test_request_content_is_only_available_to_its_owner(self):
        bot = self.prepare()
        worker, _ = self.gw.native(bot)
        mid, caller = self.plain_request(bot)
        _, other_owner, source = self.gw.enroll_bot("review-outsider")
        path = "/v1/owner/messages/%s/review-content/message" % mid
        for token, expected in ((None, 401), (caller, 401), (worker, 401), (source, 401), (other_owner, 404)):
            with self.subTest(expected=expected):
                status, obj = self.gw.call("GET", path, token=token)
                self.assertEqual(expected, status)
                self.assertNotIn("content", obj)
                self.assertNotIn("attachments", obj)
