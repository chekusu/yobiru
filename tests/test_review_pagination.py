"""Owner review pages keep complete requests within the shipped client's response limit."""
import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support.public_harness import C, FriendsGateway

import bot_gateway as G  # noqa: E402
import bot_e2e as E  # noqa: E402


class ReviewPaginationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="yobiru review pages ")
        cls.gw = FriendsGateway(Path(cls.tmp.name)).start()

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls.tmp.cleanup()

    def test_multiple_maximum_media_requests_are_complete_across_bounded_pages(self):
        bot, _ = self.gw.owned_bot("review-large-pages")
        self.gw.policy(bot, offered=["chat", "files"], visitor=["chat", "files"])
        _, token = self.gw.actor()
        cid = self.gw.conversation(bot, token)
        size = G.LIMIT_DEFAULTS["attachment_media_max_bytes"]
        raw = b"%PDF-1.7\n" + b"0" * (size - len(b"%PDF-1.7\n"))
        data = base64.b64encode(raw).decode("ascii")
        attachments = [{"name": name, "data": data} for name in ("first.pdf", "second.pdf")]
        mids = []
        for i in range(3):
            status, sent = self.gw.send(cid, token, kind="chat", text="request %d" % i,
                                       attachments=attachments)
            self.assertEqual(202, status, sent)
            mids.append(sent["message_id"])

        path, pages, received = "/v1/owner/bots/%s/reviews/full" % bot, 0, []
        while path:
            page = self.gw.ok("GET", path, None, self.gw.owner_token)
            wire = json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.assertLessEqual(len(wire), C.MAX_RESPONSE_BYTES)
            self.assertTrue(page["inbound"], "a continuation must advance the review queue")
            for item in page["inbound"]:
                received.append(item["job_id"])
                self.assertEqual(G.REQUEST_TRUST, item["content_trust"])
                self.assertEqual(["first.pdf", "second.pdf"],
                                 [a["name"] for a in item["message"]["attachments"]])
                for attachment in item["message"]["attachments"]:
                    self.assertEqual(size, attachment["bytes"])
                    self.assertEqual(hashlib.sha256(raw).hexdigest(), attachment["sha256"])
                    self.assertEqual(data, attachment["data"])
            path = page.get("next")
            pages += 1
            self.assertLessEqual(pages, 3)
        self.assertGreater(pages, 1)
        self.assertEqual(mids, received, "pagination must neither omit nor repeat requests")

    def test_cursor_remains_usable_after_review_and_cannot_cross_bots(self):
        bot, _ = self.gw.owned_bot("review-page-cursor")
        other, _ = self.gw.owned_bot("review-other-cursor")
        _, token = self.gw.actor()
        cid = self.gw.conversation(bot, token)
        first = self.gw.chat(cid, token, "first request")
        second = self.gw.chat(cid, token, "second request")
        path = "/v1/owner/bots/%s/reviews/after/%s" % (bot, first)
        self.gw.ok("POST", "/v1/owner/messages/%s/review" % first, {"action": "ignore"}, self.gw.owner_token)
        page = self.gw.ok("GET", path, None, self.gw.owner_token)
        self.assertEqual([second], [item["job_id"] for item in page["inbound"]])
        self.assertIsNone(page["next"])
        self.assertEqual(404, self.gw.call("GET", path.replace(bot, other), None, self.gw.owner_token)[0])
        _, other_owner, _ = self.gw.enroll_bot("review-outsider")
        self.assertEqual(404, self.gw.call("GET", path, None, other_owner)[0])

    def test_sealed_request_and_draft_can_be_fetched_separately_without_content_loss(self):
        """Exercise the two-body overflow with a smaller budget and genuine sealed bodies, avoiding a large
        dependency-free encryption workload; the maximum media test above uses the actual wire limit."""
        bot, _ = self.gw.owned_bot("review-split-sealed")
        self.gw.policy(bot, offered=["chat", "files"], visitor=["chat", "files"])
        worker, _ = self.gw.native(bot)
        bot_secret, bot_key = E.generate()
        actor_secret, actor_key = E.generate()
        self.gw.ok("PATCH", "/v1/owner/bots/%s" % bot, {"e2e_key": bot_key}, self.gw.owner_token)
        _, token = self.gw.actor()
        self.gw.ok("POST", "/v1/actor/key", {"e2e_key": actor_key}, token)
        cid = self.gw.conversation(bot, token)
        attachment_text = "synthetic file\n" * 300
        files = json.dumps({"attachments": [{"name": "note.txt", "content": attachment_text}]}).encode()

        def body(secret, text):
            return {"sealed": E.seal(json.dumps({"text": text}).encode(), secret,
                                     [bot_key, actor_key], "review-page-text"),
                    "sealed_files": E.seal(files, secret, [bot_key, actor_key], "review-page-files"),
                    "files": {"count": 1, "bytes": len(attachment_text)}}

        request_content = body(actor_secret, "request")
        draft = body(bot_secret, "answer")
        status, sent = self.gw.send(cid, token, kind="chat", text=None, **request_content)
        self.assertEqual(202, status, sent)
        mid = sent["message_id"]
        self.assertEqual(200, self.gw.admit(bot, mid)[0])
        status, claimed = self.gw.claim_job(worker, mid)
        self.assertEqual(200, status, claimed)
        self.gw.ok("POST", "/v1/worker/messages/%s/finish" % mid,
                   {"lease_id": claimed["lease"]["lease_id"], "fence": claimed["lease"]["fence"],
                    "outcome": "completed", "reply": draft}, worker)
        full = self.gw.reviews(bot)
        budget = len(json.dumps(full).encode()) - len(json.dumps(request_content).encode()) // 2
        store = G.Store(self.gw.home, G.load_config(self.gw.home))
        api = G.Api(store)
        with store.tx() as db:
            request = G.Request()
            request.params, request.now = {"bid": bot, "full": "/full"}, C.now_ts()
            request.subject = api.authenticate(db, ("owner",), self.gw.owner_token, request.now)
            with mock.patch.object(C, "MAX_RESPONSE_BYTES", budget):
                status, page = api.reviews(db, request)
        self.assertEqual(200, status)
        self.assertLessEqual(len(json.dumps(page).encode()), budget)
        self.assertIsNone(page["next"])
        item = page["replies"][0]
        self.assertIsNone(item["message"])
        self.assertEqual(draft, item["draft"])
        self.assertEqual(full["replies"][0]["draft_hash"], item["draft_hash"])
        fetched = self.gw.ok("GET", item["message_url"], None, self.gw.owner_token)
        self.assertEqual(request_content, fetched["content"])
        self.assertEqual(files, E.open_sealed(fetched["content"]["sealed_files"], bot_secret,
                                            actor_key, "review-page-files"))
        self.assertEqual(files, E.open_sealed(item["draft"]["sealed_files"], actor_secret,
                                            bot_key, "review-page-files"))
        _, other_owner, _ = self.gw.enroll_bot("review-split-outsider")
        self.assertEqual(404, self.gw.call("GET", item["message_url"], None, other_owner)[0])
        self.assertEqual(401, self.gw.call("GET", item["message_url"], None, token)[0])


if __name__ == "__main__":
    unittest.main()
