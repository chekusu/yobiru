"""Deferred owner history preserves sealed bodies and the owner-only conversation boundary."""
import base64
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from support.public_harness import C
from test_friends_client import FriendsClientCase
from test_friends_e2e import E2ECase

import bot_client as P
import bot_gateway as G


class SealedOwnerConversationTests(E2ECase):
    def test_deferred_requests_results_and_drafts_open_with_complete_files(self):
        bot, _ = self.bot_with_host()
        self.y("caller", "connect", "%s/@%s" % (self.gw.url, self.handle), "--name", "Synthetic sender")
        raw = b"%PDF-1.7\n%synthetic history fixture\n" + b"0" * 16384
        attachment = self.files / "history.pdf"
        attachment.write_bytes(raw)
        for role in ("caller", "host"):
            self.y(role, "attachments", "allow", str(self.files))

        mids = []
        for i in range(3):
            sent = self.y("caller", "chat", self.handle, "Synthetic sealed request %d" % i,
                          "--attach", str(attachment))
            mid, cid = sent["message_id"], sent["conversation_id"]
            mids.append(mid)
            if i == 2:
                continue
            self.y("owner", "review", "approve-inbound", self.handle, mid)
            self.y("host", "job", "claim", bot, mid)
            self.y("host", "job", "reply", bot, mid, "--text", "Synthetic sealed reply %d" % i,
                   "--attach", str(attachment))
            if i == 0:
                draft_hash = self.gw.sql("SELECT draft_hash FROM messages WHERE id=?", (mid,))[0]["draft_hash"]
                self.y("owner", "review", "approve-reply", self.handle, mid, "--draft-hash", draft_hash)

        before = self.gw.sql("SELECT id, state, review_in, revealed_ts, lease_hash, result, draft "
                             "FROM messages WHERE conversation_id=? ORDER BY rowid", (cid,))
        path = "/v1/owner/conversations/%s" % cid
        inline = self.gw.owner_view(cid)
        budget = 8192
        self.assertGreater(len(json.dumps(inline).encode()), budget)
        store = G.Store(self.gw.home, G.load_config(self.gw.home))
        api = G.Api(store)
        with store.tx() as db:
            request = G.Request()
            request.params, request.now = {"cid": cid}, C.now_ts()
            request.subject = api.authenticate(db, ("owner",), self.gw.owner_token, request.now)
            with patch.object(C, "MAX_RESPONSE_BYTES", budget):
                status, bounded = api.owner_conversation_get(db, request)
        self.assertEqual(200, status)
        self.assertLessEqual(len(json.dumps(bounded, ensure_ascii=False, separators=(",", ":")).encode()), budget)
        self.assertEqual(mids, [m["message_id"] for m in bounded["messages"]])
        links = [m[part + "_url"] for m in bounded["messages"] for part in ("content", "result", "draft")
                 if m.get(part + "_url")]
        self.assertEqual(5, len(links), "all three sealed requests, the result and the draft must be deferred")
        self.assertEqual(inline["messages"][1]["review"], bounded["messages"][1]["review"])

        real_call = C.api_call

        def serve_bounded_history(base, method, url, *args, **kwargs):
            if url == path:
                self.assertEqual("GET", method)
                return 200, copy.deepcopy(bounded)
            return real_call(base, method, url, *args, **kwargs)

        # Only the initial response is substituted to exercise a small response budget. Each deferred
        # body uses the real HTTP endpoint; the normal CLI then authenticates and opens every envelope.
        with patch.object(C, "api_call", side_effect=serve_bounded_history) as calls, \
                patch.object(P, "out", wraps=P.out) as output:
            shown = self.y("owner", "owner", "conversation", cid)
        self.assertCountEqual([path] + links, [c.args[2] for c in calls.call_args_list])
        complete = output.call_args.args[0]
        self.assertEqual(["completed", "review", "queued"], [m["state"] for m in complete["messages"]])
        bodies = [(m["content"], "Synthetic sealed request %d" % i)
                  for i, m in enumerate(complete["messages"])]
        bodies += [(complete["messages"][0]["result"], "Synthetic sealed reply 0"),
                   (complete["messages"][1]["draft"], "Synthetic sealed reply 1")]
        for body, text in bodies:
            self.assertEqual(text, body["text"])
            self.assertTrue(body["e2e"]["opened"], body["e2e"])
            self.assertNotIn("attachments_withheld", body)
            self.assertEqual(1, len(body["attachments"]))
            file = body["attachments"][0]
            self.assertEqual("history.pdf", file["name"])
            self.assertEqual(raw, base64.b64decode(file["data"]))
            self.assertEqual((len(raw), hashlib.sha256(raw).hexdigest()), (file["bytes"], file["sha256"]))
        self.assertNotIn("sealed_files", json.dumps(complete))
        self.assertTrue(all("data" not in m["content"]["attachments"][0] for m in shown["messages"]))
        self.assertEqual("pending", complete["messages"][2]["review"]["inbound"])
        self.assertEqual(before, self.gw.sql("SELECT id, state, review_in, revealed_ts, lease_hash, result, draft "
                                           "FROM messages WHERE conversation_id=? ORDER BY rowid", (cid,)))


class OwnerConversationEndpointAuthorizationTests(FriendsClientCase):
    def test_context_and_bodies_require_this_conversations_owner(self):
        bot = self.gw.bot("history-owner")
        self.gw.policy(bot, offered=["chat"], visitor=["chat"])
        _, caller = self.gw.actor()
        cid = self.gw.conversation(bot, caller)
        status, sent = self.gw.send(cid, caller, kind="chat", text="Synthetic pending private request")
        self.assertEqual(202, status)
        mid = sent["message_id"]
        path = "/v1/owner/conversations/%s" % cid
        content_path = path + "/messages/%s/content" % mid
        worker, _ = self.gw.native(bot)
        source = self.gw.source_for(bot)
        _, other_owner, _ = self.gw.enroll_bot("history-outsider")
        for token, expected in ((None, 401), (caller, 401), (worker, 401), (source, 401), (other_owner, 404)):
            for target in (path + "/context", content_path):
                with self.subTest(expected=expected, endpoint=target.rsplit("/", 1)[-1]):
                    status, refused = self.gw.call("GET", target, token=token)
                    self.assertEqual(expected, status)
                    self.assertNotIn("content", refused)
                    self.assertNotIn("actor_e2e_key", refused)
                    self.assertNotIn("Synthetic pending private request", json.dumps(refused))

        context = self.gw.ok("GET", path + "/context", token=self.gw.owner_token)
        self.assertEqual((cid, bot, mid), (context["conversation_id"], context["bot_id"],
                                         context["latest_caller_message_id"]))
        self.assertNotIn("messages", context)
        content = self.gw.ok("GET", content_path, token=self.gw.owner_token)
        self.assertEqual((cid, mid, "Synthetic pending private request"),
                         (content["conversation_id"], content["message_id"], content["content"]["text"]))
        self.assertEqual("pending", self.gw.owner_view(cid)["messages"][0]["review"]["inbound"])

        other_cid = self.gw.conversation(bot, caller)
        status, other_sent = self.gw.send(other_cid, caller, kind="chat", text="Another synthetic conversation")
        self.assertEqual(202, status)
        for target in (path + "/messages/%s/content" % other_sent["message_id"],
                       path + "/messages/%s/result" % mid, path + "/messages/%s/draft" % mid):
            self.assertEqual(404, self.gw.call("GET", target, token=self.gw.owner_token)[0])

        self.gw.ok("DELETE", path, token=self.gw.owner_token)
        for target in (path + "/context", content_path):
            status, refused = self.gw.call("GET", target, token=self.gw.owner_token)
            self.assertEqual((404, "not_found"), (status, refused["error"]))
            self.assertNotIn("content", refused)
        self.assertEqual(other_cid, self.gw.ok("GET", "/v1/owner/conversations/%s/context" % other_cid,
                                              token=self.gw.owner_token)["conversation_id"])


if __name__ == "__main__":
    unittest.main()
