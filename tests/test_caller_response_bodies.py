"""Caller responses stay readable when both sides send maximum sealed attachments."""
import base64
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from support.public_harness import C, FriendsGateway
from test_friends_e2e import E2ECase

import bot_client as P
import bot_e2e as E
import bot_gateway as G
import bot_mcp as M


def wire_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


class CallerResponseBodyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="yobiru caller bodies ")
        self.addCleanup(tmp.cleanup)
        self.gw = FriendsGateway(Path(tmp.name)).start()
        self.addCleanup(self.gw.stop)
        self.bot = self.gw.bot("caller-bodies")
        self.gw.policy(self.bot, offered=["chat", "files"], visitor=["chat", "files"])
        self.bot_secret, self.bot_key = E.generate()
        self.actor_secret, self.actor_key = E.generate()
        self.gw.ok("PATCH", "/v1/owner/bots/%s" % self.bot, {"e2e_key": self.bot_key}, self.gw.owner_token)
        self.actor, self.token = self.gw.actor()
        self.gw.ok("POST", "/v1/actor/key", {"e2e_key": self.actor_key}, self.token)
        self.cid = self.gw.conversation(self.bot, self.token)
        self.gw.contact(self.bot, self.actor, "trust")
        self.worker, _ = self.gw.native(self.bot)

    def exchange(self, size, op_key="caller-bodies-1"):
        raw = b"%PDF-1.7\n" + b"0" * (size - len(b"%PDF-1.7\n"))
        files = [{"name": name, "media_type": "application/pdf", "data": base64.b64encode(raw).decode()}
                 for name in ("first.pdf", "second.pdf")]
        bind = {"bot_id": self.bot, "caller": self.actor, "conversation_id": self.cid}
        request = P.e2e_body("msg", "Synthetic sealed request", files, self.actor_secret,
                             [self.bot_key, self.actor_key], bind)
        sent = self.gw.ok("POST", "/v1/conversations/%s/messages" % self.cid,
                          dict(request, kind="chat", op_key=op_key), self.token)
        self.mid = sent["message_id"]
        status, job = self.gw.claim_job(self.worker, self.mid)
        self.assertEqual(200, status, job)
        reply = P.e2e_body("reply", "Synthetic sealed reply", files, self.bot_secret,
                           [self.bot_key, self.actor_key], dict(bind, message_id=self.mid))
        status, finished = self.gw.finish(self.worker, job, reply=reply)
        self.assertEqual(200, status, finished)
        if finished["state"] == "review":
            self.assertEqual(200, self.gw.approve_reply(self.bot, self.mid)[0])
        return raw, request, reply

    def test_maximum_sealed_request_and_reply_are_retrievable_on_every_caller_route(self):
        raw, request, reply = self.exchange(G.LIMIT_DEFAULTS["attachment_media_max_bytes"])
        self.assertGreater(wire_size({"content": request, "result": reply}), C.MAX_RESPONSE_BYTES)
        routes = [("/v1/messages/%s" % self.mid, lambda v: [v]),
                  ("/v1/conversations/%s/operations/caller-bodies-1" % self.cid, lambda v: [v]),
                  ("/v1/conversations/%s" % self.cid, lambda v: v["messages"]),
                  ("/v1/inbox", lambda v: v["items"])]
        for path, messages in routes:
            with self.subTest(path=path):
                view = self.gw.ok("GET", path, token=self.token)
                self.assertLessEqual(wire_size(view), C.MAX_RESPONSE_BYTES)
                message = messages(view)[0]
                self.assertTrue(any(message.get(part + "_url") for part in ("content", "result")))
                complete = P.call(self.gw.url, "GET", path, token=self.token)
                message = messages(complete)[0]
                for part, expected in (("content", request), ("result", reply)):
                    self.assertEqual(expected, message[part])
                    self.assertNotIn(part + "_url", message)
        for body, sender in ((message["content"], self.actor_key), (message["result"], self.bot_key)):
            plain = C.strict_loads(E.open_sealed(body["sealed_files"], self.actor_secret, sender, P.E2E_CTX))
            self.assertEqual(2, len(plain["attachments"]))
            self.assertTrue(all(hashlib.sha256(base64.b64decode(a["data"])).digest() ==
                                hashlib.sha256(raw).digest() for a in plain["attachments"]))
        # The MCP facade must fit its full JSON-RPC wrapper too, without duplicating sealed ciphertext.
        for name, args, part, expected in (
                ("yobiru_message_status", {"message_id": self.mid}, "result", reply),
                ("yobiru_message_body", {"message_id": self.mid, "part": "content"}, "content", request)):
            rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
            headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json",
                       "MCP-Protocol-Version": M.LATEST_VERSION}
            status, _, wire = C.post_raw(self.gw.url, "/mcp", M.dumps(rpc).encode(), headers)
            self.assertEqual(200, status)
            self.assertLessEqual(len(wire), C.MAX_RESPONSE_BYTES)
            answer = C.strict_loads(wire)["result"]
            self.assertFalse(answer["isError"])
            self.assertEqual(expected, answer["structuredContent"][part])

    def bounded(self, budget=5000):
        store = G.Store(self.gw.home, G.load_config(self.gw.home))
        api, r = G.Api(store), G.Request()
        r.params, r.now = {"mid": self.mid}, C.now_ts()
        with store.tx() as db:
            r.subject = api.authenticate(db, G.CALLER, self.token, r.now)
            with patch.object(C, "MAX_RESPONSE_BYTES", budget):
                return api.message_get(db, r)[1]

    def test_links_recheck_identity_grants_and_deleted_conversations(self):
        self.exchange(8192)
        view = self.bounded()
        self.assertLessEqual(wire_size(view), 5000)
        self.assertIsNone(view["content"])
        self.assertIsNone(view["result"])
        self.assertIsNone(self.gw.sql("SELECT served_ts FROM messages WHERE id=?", (self.mid,))[0]["served_ts"])
        _, other = self.gw.actor()
        for part in ("content", "result"):
            path = view[part + "_url"]
            self.assertEqual(401, self.gw.call("GET", path)[0])
            self.assertEqual(404, self.gw.call("GET", path, token=other)[0])
        self.gw.ok("GET", view["content_url"], token=self.token)
        self.assertIsNone(self.gw.sql("SELECT served_ts FROM messages WHERE id=?", (self.mid,))[0]["served_ts"])
        self.gw.ok("GET", view["result_url"], token=self.token)
        self.assertIsNotNone(self.gw.sql("SELECT served_ts FROM messages WHERE id=?", (self.mid,))[0]["served_ts"])
        self.assertEqual(200, self.gw.call("POST", "/v1/messages/%s/ack" % self.mid, {}, self.token)[0])
        self.gw.policy(self.bot, offered=["chat"], visitor=["chat"])
        content = self.gw.ok("GET", view["content_url"], token=self.token)["content"]
        self.assertNotIn("sealed_files", content)
        self.assertEqual(404, self.gw.call("GET", view["result_url"], token=self.token)[0])
        self.gw.ok("DELETE", "/v1/owner/conversations/%s" % self.cid, token=self.gw.owner_token)
        self.assertEqual(404, self.gw.call("GET", view["content_url"], token=self.token)[0])

    def test_aggregate_budget_counts_wrappers_and_only_records_inline_replies_as_served(self):
        self.exchange(8192, op_key="caller-bodies-first")
        first = self.mid
        self.exchange(8192, op_key="caller-bodies-second")
        store = G.Store(self.gw.home, G.load_config(self.gw.home))
        api, r = G.Api(store), G.Request()
        r.params, r.now = {"cid": self.cid}, C.now_ts()
        # Enough for one body, but not for all four bodies or both results.
        budget = 40000
        with store.tx() as db:
            r.subject = api.authenticate(db, G.CALLER, self.token, r.now)
            with patch.object(C, "MAX_RESPONSE_BYTES", budget):
                status, view = api.conversation_get(db, r)
        self.assertEqual(200, status)
        self.assertLessEqual(wire_size(view), budget)
        self.assertEqual([first, self.mid], [m["message_id"] for m in view["messages"]])
        self.assertEqual(1, sum(m.get("result") is not None for m in view["messages"]))
        for m in view["messages"]:
            served = self.gw.sql("SELECT served_ts FROM messages WHERE id=?", (m["message_id"],))[0]["served_ts"]
            self.assertEqual(m.get("result") is not None, served is not None)
            self.assertEqual(served is not None, m["lifecycle"]["response_served_at"] is not None)

    def test_empty_decline_result_still_counts_as_served_and_can_be_acknowledged(self):
        bot = self.gw.bot("empty-decline")
        self.gw.policy(bot, offered=["faq"], visitor=["faq"])
        worker = self.gw.worker(bot)
        cid = self.gw.conversation(bot, self.token)
        status, sent = self.gw.send(cid, self.token, kind="faq", text="Synthetic plain request")
        self.assertEqual(202, status)
        mid = sent["message_id"]
        job = self.gw.claim(worker)[0]
        self.assertEqual(200, self.gw.start_work(worker, job)[0])
        status, finished = self.gw.finish(worker, job, outcome="declined", code="no_answer", reply={})
        self.assertEqual(200, status, finished)
        view = self.gw.ok("GET", "/v1/messages/%s" % mid, token=self.token)
        self.assertEqual({}, view["result"])
        self.assertIsNotNone(view["lifecycle"]["response_served_at"])
        self.assertEqual(200, self.gw.call("POST", "/v1/messages/%s/ack" % mid, {}, self.token)[0])


class CallerClientBodyTests(E2ECase):
    def test_status_history_inbox_and_verify_hydrate_before_opening_or_acknowledging(self):
        bot, _ = self.bot_with_host()
        self.y("caller", "connect", "%s/@%s" % (self.gw.url, self.handle))
        attachment = self.files / "caller.pdf"
        attachment.write_bytes(b"%PDF-1.7\n" + b"0" * 8192)
        for role in ("caller", "host"):
            self.y(role, "attachments", "allow", str(self.files))
        sent = self.y("caller", "chat", self.handle, "Synthetic deferred request", "--attach", str(attachment))
        mid, cid = sent["message_id"], sent["conversation_id"]
        self.y("owner", "review", "approve-inbound", self.handle, mid)
        self.y("host", "job", "claim", bot, mid)
        self.y("host", "job", "reply", bot, mid, "--text", "Synthetic deferred reply", "--attach", str(attachment))
        draft_hash = self.gw.sql("SELECT draft_hash FROM messages WHERE id=?", (mid,))[0]["draft_hash"]
        self.y("owner", "review", "approve-reply", self.handle, mid, "--draft-hash", draft_hash)
        store = G.Store(self.gw.home, G.load_config(self.gw.home))
        api, real_call, paths = G.Api(store), C.api_call, []

        def bounded_call(base, method, path, *args, **kwargs):
            paths.append(path)
            status, view = real_call(base, method, path, *args, **kwargs)
            if method == "GET" and path in ("/v1/messages/%s" % mid, "/v1/conversations/%s" % cid, "/v1/inbox"):
                messages = view.get("messages", view.get("items", [view]))
                with store.tx() as db, patch.object(C, "MAX_RESPONSE_BYTES", 5000):
                    status, view = api.caller_response(db, view, messages, C.now_ts())
                self.assertTrue(all(m.get("content_url") and m.get("result_url") for m in messages))
            return status, view

        with patch.object(C, "api_call", side_effect=bounded_call):
            status = self.y("caller", "status", mid)
            history = self.y("caller", "history", self.handle)["messages"][0]
            inbox = self.y("caller", "inbox")["items"][0]
            self.assertTrue(self.y("caller", "verify", mid)["verified"])
        for message in (status, history, inbox):
            for part, text in (("content", "Synthetic deferred request"), ("result", "Synthetic deferred reply")):
                self.assertEqual(text, message[part]["text"])
                self.assertTrue(message[part]["e2e"]["opened"])
                self.assertEqual("caller.pdf", message[part]["attachments"][0]["name"])
                self.assertNotIn(part + "_url", message)
        ack = paths.index("/v1/messages/%s/ack" % mid)
        self.assertLess(paths.index("/v1/messages/%s/result" % mid), ack)
        self.assertLess(paths.index("/v1/messages/%s/content" % mid), ack)


class CallerBodyLinkTests(unittest.TestCase):
    def test_client_refuses_malformed_message_lists_before_fetching_bodies(self):
        mid, cid = "msg_" + "a" * 16, "cnv_" + "b" * 16
        message = {"message_id": mid, "conversation_id": cid, "content": None,
                   "content_url": "/v1/messages/%s/content" % mid}
        for messages in (None, {}, [message, "invalid"]):
            with self.subTest(messages=messages), patch.object(C, "api_ok", return_value=(200, {"messages": messages})) as call:
                with self.assertRaises(SystemExit):
                    P.call("https://example.invalid", "GET", "/v1/conversations/%s" % cid)
                self.assertEqual(1, call.call_count)

    def test_client_refuses_foreign_or_mismatched_body_links(self):
        mid, cid = "msg_" + "a" * 16, "cnv_" + "b" * 16
        view = {"message_id": mid, "conversation_id": cid, "content": None,
                "content_url": "/v1/messages/%s/content" % mid}
        for path in ("https://example.invalid/content", "//example.invalid/content",
                     "/v1/owner/messages/%s" % mid, "/v1/messages/%s/result" % mid):
            with self.subTest(path=path), patch.object(C, "api_ok", return_value=(200, dict(view, content_url=path))) as call:
                with self.assertRaises(SystemExit):
                    P.call("https://example.invalid", "GET", "/v1/messages/%s" % mid)
                self.assertEqual(1, call.call_count)
        for field in ("message_id", "conversation_id"):
            body = {"ok": True, "message_id": mid, "conversation_id": cid, "content": {"text": "synthetic"}}
            body[field] = "wrong"
            with self.subTest(field=field), patch.object(C, "api_ok", side_effect=[(200, copy.deepcopy(view)), (200, body)]):
                with self.assertRaises(SystemExit):
                    P.call("https://example.invalid", "GET", "/v1/messages/%s" % mid)


if __name__ == "__main__":
    unittest.main()
