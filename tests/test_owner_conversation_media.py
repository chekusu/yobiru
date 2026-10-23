"""Large pending requests must not prevent owners from reading history or sending their own reply."""
import base64
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from support.public_harness import C, FriendsGateway, PUBLIC, plain_env
from test_friends_client import FriendsClientCase

import bot_client as P
import bot_gateway as G


class OwnerConversationMediaTests(FriendsClientCase):
    def setUp(self):
        # Use the real default pending, attachment and storage quotas for this reproduction.
        self._tmp = tempfile.TemporaryDirectory(prefix="yobiru owner history ")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.gw = FriendsGateway(self.root).start()
        self.addCleanup(self.gw.stop)
        self.homes = {role: self.root / role for role in ("owner", "caller", "host", "other")}
        self.outputs = []

    def test_maximum_pending_media_history_and_reply_remain_usable(self):
        bot = self.owner_bot("owner-history")
        self.gw.policy(bot, offered=["chat", "files"], visitor=["chat", "files"])
        worker, _ = self.gw.native(bot)
        _, caller = self.gw.actor()
        cid = self.gw.conversation(bot, caller)
        size = G.LIMIT_DEFAULTS["attachment_media_max_bytes"]
        raw = b"%PDF-1.7\n" + b"0" * (size - len(b"%PDF-1.7\n"))
        data = base64.b64encode(raw).decode("ascii")
        attachments = [{"name": name, "data": data} for name in ("first.pdf", "second.pdf")]
        mids = []
        for i in range(3):
            status, sent = self.gw.send(cid, caller, kind="chat", text="Synthetic request %d" % i,
                                       attachments=attachments)
            self.assertEqual(202, status, sent)
            mids.append(sent["message_id"])

        path = "/v1/owner/conversations/%s" % cid
        status, headers, wire = self.gw.raw("GET", path, headers={"Authorization": "Bearer " + self.gw.owner_token})
        self.assertEqual(200, status)
        self.assertLessEqual(len(wire), C.MAX_RESPONSE_BYTES)
        self.assertEqual(len(wire), int(headers["content-length"]))
        history = json.loads(wire)
        self.assertEqual(mids, [m["message_id"] for m in history["messages"]])
        self.assertNotIn("next", history, "legacy clients must see the complete message list")
        self.assertTrue(any(m.get("content_url") for m in history["messages"]))
        for i, message in enumerate(history["messages"]):
            content = message["content"]
            if content is None:
                content = self.gw.ok("GET", message["content_url"], token=self.gw.owner_token)["content"]
            self.assertEqual("Synthetic request %d" % i, content["text"])
            self.assertEqual(["first.pdf", "second.pdf"], [a["name"] for a in content["attachments"]])
            for attachment in content["attachments"]:
                self.assertEqual((size, hashlib.sha256(raw).hexdigest(), data),
                                 (attachment["bytes"], attachment["sha256"], attachment["data"]))

        # Existing CLI commands hydrate every request and omit only the printable base64 media data.
        shown = self.y("owner", "owner", "conversation", cid)
        self.assertEqual(mids, [m["message_id"] for m in shown["messages"]])
        for i, message in enumerate(shown["messages"]):
            self.assertEqual("Synthetic request %d" % i, message["content"]["text"])
            self.assertEqual(2, len(message["content"]["attachments"]))
            self.assertTrue(all(a["bytes"] == size for a in message["content"]["attachments"]))
            self.assertNotIn("content_url", message)

        # Sending a reply must succeed even if full history cannot be fetched at all.
        api_call, calls = C.api_call, []

        def without_history(base, method, route, *args, **kwargs):
            calls.append(route)
            self.assertNotEqual(path, route, "owner reply must not download conversation history")
            self.assertFalse(route.startswith(path + "/messages/"), "owner reply must not hydrate bodies")
            return api_call(base, method, route, *args, **kwargs)

        with mock.patch.object(C, "api_call", side_effect=without_history):
            replied = self.y("owner", "owner", "reply", cid, "Synthetic owner reply", "--op-key", "history-reply-1")
        self.assertIn(path + "/context", calls)
        self.assertEqual("Synthetic owner reply", replied["content"]["text"])
        received = self.gw.ok("GET", "/v1/messages/%s" % replied["message_id"], token=caller)
        self.assertEqual("Synthetic owner reply", received["content"]["text"])

        # Also exercise the distributed client, whose source is synced by the build check.
        bundled = PUBLIC.parent / "plugins" / "yobiru" / "scripts" / "public_bots" / "bot_client.py"
        for args in (("owner", "conversation", cid), ("owner", "reply", cid, "Bundled owner reply")):
            run = subprocess.run([sys.executable, str(bundled), "--home", str(self.homes["owner"]), *args],
                                 capture_output=True, text=True, timeout=30,
                                 env=plain_env(self.homes["owner"]), cwd=str(self.root))
            self.assertEqual(0, run.returncode, run.stdout + run.stderr)
            self.outputs.append(run.stdout + run.stderr)
            result = json.loads(run.stdout)
            if args[1] == "conversation":
                self.assertEqual(mids, [m["message_id"] for m in result["messages"] if m["author"] == "caller"])
                self.assertTrue(all(m["content"] is not None for m in result["messages"]))

        context = self.gw.ok("GET", path + "/context", token=self.gw.owner_token)
        self.assertNotIn("messages", context)
        self.assertEqual(mids[-1], context["latest_caller_message_id"])
        self.assertLess(len(json.dumps(context).encode()), 10000)
        for mid in mids:
            state = self.gw.sql("SELECT state, review_in, revealed_ts FROM messages WHERE id=?", (mid,))[0]
            self.assertEqual("queued", state["state"])
            self.assertNotEqual("approved", state["review_in"])
            self.assertIsNone(state["revealed_ts"])
            status, refused = self.gw.claim_job(worker, mid)
            self.assertEqual(409, status)
            self.assertEqual("review_required" if mid == mids[0] else "earlier_turn_pending", refused.get("error"))
        self.assert_no_credentials_printed()
