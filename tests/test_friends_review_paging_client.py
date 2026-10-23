"""The owner CLI reads complete review pages and acts independently of the queue."""
import base64
import hashlib
import io
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from test_friends_client import FriendsClientCase
from test_friends_e2e import E2ECase

import bot_client as P
import bot_gateway as G


class ReviewPagingClientTests(FriendsClientCase):
    def assert_no_review_listing(self, calls):
        self.assertFalse(any("/reviews" in c.args[2] for c in calls.call_args_list))

    def test_large_queue_is_complete_and_actions_use_the_selected_message(self):
        bot = self.owner_bot("large-review")
        self.gw.policy(bot, offered=["chat", "files"], visitor=["chat", "files"])
        size = G.LIMIT_DEFAULTS["attachment_media_max_bytes"]
        raw = b"%PDF-1.7\n" + b"0" * (size - len(b"%PDF-1.7\n"))
        data = base64.b64encode(raw).decode("ascii")
        attachments = [{"name": name, "data": data} for name in ("first.pdf", "second.pdf")]
        mids, callers = [], []
        for i in range(3):
            _, caller = self.gw.actor()
            callers.append(caller)
            cid = self.gw.conversation(bot, caller)
            status, sent = self.gw.send(cid, caller, kind="chat", text="large request %d" % i,
                                       attachments=attachments)
            self.assertEqual(202, status, sent)
            mids.append(sent["message_id"])

        with patch.object(P.C, "api_call", wraps=P.C.api_call) as calls, patch.object(P, "out", wraps=P.out) as output:
            shown = self.y("owner", "review", "list", "large-review")
        page_paths = [c.args[2] for c in calls.call_args_list if "/reviews" in c.args[2]]
        self.assertGreater(len(page_paths), 1, "the CLI must follow the byte-bounded review pages")
        self.assertEqual(mids, [item["job_id"] for item in shown["inbound"]])
        self.assertIsNone(shown["next"])
        complete = output.call_args.args[0]
        for i, item in enumerate(complete["inbound"]):
            self.assertEqual("large request %d" % i, item["message"]["text"])
            self.assertEqual(G.REQUEST_TRUST, item["content_trust"])
            self.assertEqual(["first.pdf", "second.pdf"],
                             [a["name"] for a in item["message"]["attachments"]])
            for attachment in item["message"]["attachments"]:
                self.assertEqual(size, attachment["bytes"])
                self.assertEqual(hashlib.sha256(raw).hexdigest(), attachment["sha256"])
                self.assertEqual(data, attachment["data"], "the CLI must not lose a page's attachment data")
        for item in shown["inbound"]:
            self.assertTrue(all("data" not in a for a in item["message"]["attachments"]))

        # The owner must be able to inspect omitted media on any page before admitting the request.
        saved = self.y("owner", "review", "attachments", "large-review", mids[-1])
        self.assertEqual(["first.pdf", "second.pdf"], [f["name"] for f in saved["files"]])
        for file in saved["files"]:
            self.assertEqual(raw, Path(file["path"]).read_bytes())
        self.assertEqual("pending", self.gw.ok("GET", "/v1/owner/messages/%s" % mids[-1], None,
                                              self.gw.owner_token)["review"]["inbound"])

        with patch.object(P, "call", wraps=P.call) as calls:
            approved = self.y("owner", "review", "approve-inbound", "large-review", mids[-1])
        self.assertEqual("approved", approved["inbound"])
        self.assert_no_review_listing(calls)
        with patch.object(P, "call", wraps=P.call) as calls:
            accepted = self.y("owner", "review", "accept", "large-review", mids[-1])
            blocked = self.y("owner", "review", "block", "large-review", mids[-1])
        self.assertTrue(accepted["inbound_preapproved"])
        self.assertEqual("revoked", blocked["status"])
        self.assert_no_review_listing(calls)
        with patch.object(P, "call", wraps=P.call) as calls:
            rejected = self.y("owner", "review", "reject-inbound", "large-review", mids[0],
                              "--text", "Synthetic owner decline")
        self.assertEqual("declined", rejected["state"])
        self.assert_no_review_listing(calls)
        reply = self.gw.ok("GET", "/v1/messages/%s" % mids[0], None, callers[0])
        self.assertEqual("Synthetic owner decline", reply["result"]["text"])
        self.assert_no_credentials_printed()


class SealedReviewTargetTests(E2ECase):
    def test_sealed_decline_resolves_caller_key_without_loading_reviews(self):
        self.bot_with_host(files=False)
        self.y("caller", "connect", "%s/@%s" % (self.gw.url, self.handle), "--name", "Sealed caller")
        mid = self.y("caller", "chat", self.handle, "Synthetic sealed request")["message_id"]
        self.y("owner", "review", "ignore", self.handle, mid)
        with patch.object(P, "call", wraps=P.call) as calls:
            result = self.y("owner", "review", "reject-inbound", self.handle, mid,
                            "--text", "Synthetic sealed decline")
        self.assertEqual("declined", result["state"])
        self.assertFalse(any("/reviews" in c.args[2] for c in calls.call_args_list))
        self.assert_opaque(self.stored(mid), "Synthetic sealed request", "Synthetic sealed decline")
        self.assertEqual("Synthetic sealed decline", self.y("caller", "status", mid)["result"]["text"])


class ReviewPageLinkTests(unittest.TestCase):
    bot = "bot_" + "a" * 16
    mid = "msg_" + "b" * 16

    def page(self, **extra):
        return dict({"ok": True, "bot_id": self.bot, "inbound": [], "replies": [], "uncertain": [],
                     "next": None}, **extra)

    def test_split_request_is_hydrated_and_review_categories_survive_pagination(self):
        first = "/v1/owner/bots/%s/reviews/full" % self.bot
        second = "/v1/owner/bots/%s/reviews/after/%s" % (self.bot, self.mid)
        body = "/v1/owner/messages/%s/review-content/message" % self.mid
        draft = {"text": "Synthetic draft", "attachments": [{"name": "draft.txt", "content": "draft file"}]}
        content = {"text": "Synthetic request", "attachments": [{"name": "request.txt", "content": "request file"}]}
        reply = {"job_id": self.mid, "message": None, "message_url": body, "draft": draft,
                 "draft_hash": "synthetic-draft-hash"}
        inbound = {"job_id": "msg_" + "c" * 16, "message": {"text": "Another request"}}
        uncertain = {"job_id": "msg_" + "d" * 16, "state": "uncertain"}
        responses = {first: self.page(replies=[reply], next=second),
                     body: {"ok": True, "bot_id": self.bot, "message_id": self.mid, "content": content},
                     second: self.page(inbound=[inbound], uncertain=[uncertain])}
        with patch.object(P.C, "api_call", side_effect=lambda base, method, path, *args, **kwargs: (200, responses[path])) as calls:
            result = P.owner_reviews("http://127.0.0.1", "synthetic-owner", self.bot)
        self.assertEqual([first, body, second], [c.args[2] for c in calls.call_args_list])
        self.assertEqual([inbound], result["inbound"])
        self.assertEqual([uncertain], result["uncertain"])
        self.assertEqual(content, result["replies"][0]["message"])
        self.assertEqual(draft, result["replies"][0]["draft"])
        self.assertEqual("synthetic-draft-hash", result["replies"][0]["draft_hash"])
        self.assertNotIn("message_url", result["replies"][0])
        self.assertIsNone(result["next"])

    def test_older_full_body_gateway_still_has_all_pages_after_negotiation_fallback(self):
        legacy = "/v1/owner/bots/%s/reviews" % self.bot
        next_path = legacy + "/after/" + self.mid
        first = {"job_id": self.mid, "message": {"text": "First request"}}
        last = {"job_id": "msg_" + "c" * 16, "message": {"text": "Last request"}}
        replies = {legacy + "/full": (404, {"ok": False, "error": "not_found"}),
                   legacy: (200, self.page(inbound=[first], next=next_path)),
                   next_path: (200, self.page(inbound=[last]))}
        with patch.object(P.C, "api_call", side_effect=lambda base, method, path, *args, **kwargs:
                          replies[path]) as calls:
            result = P.owner_reviews("http://127.0.0.1", "synthetic-owner", self.bot)
        self.assertEqual([legacy + "/full", legacy, next_path], [c.args[2] for c in calls.call_args_list])
        self.assertEqual([first, last], result["inbound"])
        self.assertIsNone(result["next"])

    def test_continuations_cannot_redirect_the_owner_credential(self):
        for link in ("https://example.invalid/reviews", "/v1/owner/bots/bot_" + "c" * 16 +
                     "/reviews/after/" + self.mid):
            with self.subTest(link=link), patch.object(P.C, "api_call", return_value=(200, self.page(next=link))) as calls:
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    P.owner_reviews("http://127.0.0.1", "synthetic-owner", self.bot)
                self.assertEqual(1, calls.call_count)

    def test_request_body_links_cannot_redirect_the_owner_credential(self):
        item = {"job_id": self.mid, "message": None, "message_url": "https://example.invalid/body"}
        with patch.object(P.C, "api_call", return_value=(200, self.page(replies=[item]))) as calls:
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                P.owner_reviews("http://127.0.0.1", "synthetic-owner", self.bot)
            self.assertEqual(1, calls.call_count)

    def test_repeated_continuation_is_rejected_instead_of_looping(self):
        link = "/v1/owner/bots/%s/reviews/after/%s" % (self.bot, self.mid)
        with patch.object(P.C, "api_call", side_effect=[(200, self.page(next=link)), (200, self.page(next=link))]) as calls:
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                P.owner_reviews("http://127.0.0.1", "synthetic-owner", self.bot)
            self.assertEqual(2, calls.call_count)


if __name__ == "__main__":
    unittest.main()
