"""Deferred caller bodies remain retrievable through remote MCP and the local encrypted proxy."""
import base64
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support.public_harness import C, FriendsGateway

import bot_client as P
import bot_e2e as E
import bot_gateway as G
import bot_mcp as M


class CallerMcpBodyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="yobiru caller mcp ")
        self.addCleanup(tmp.cleanup)
        self.gw = FriendsGateway(Path(tmp.name)).start()
        self.addCleanup(self.gw.stop)
        self.bot = self.gw.bot("caller-mcp-bodies")
        self.gw.policy(self.bot, offered=["chat", "files"], visitor=["chat", "files"])
        self.bot_secret, self.bot_key = E.generate()
        self.actor_secret, self.actor_key = E.generate()
        self.gw.ok("PATCH", "/v1/owner/bots/%s" % self.bot, {"e2e_key": self.bot_key}, self.gw.owner_token)
        self.actor, self.token = self.gw.actor()
        self.gw.ok("POST", "/v1/actor/key", {"e2e_key": self.actor_key}, self.token)
        self.cid = self.gw.conversation(self.bot, self.token)
        self.gw.contact(self.bot, self.actor, "trust")
        self.worker, _ = self.gw.native(self.bot)
        raw = b"%PDF-1.7\n" + b"0" * 4087
        self.files = [{"name": name, "data": base64.b64encode(raw).decode()}
                      for name in ("first.pdf", "second.pdf")]
        bind = {"bot_id": self.bot, "caller": self.actor, "conversation_id": self.cid}
        self.content = P.e2e_body("msg", "Synthetic request", self.files, self.actor_secret,
                                   [self.bot_key, self.actor_key], bind)
        sent = self.gw.ok("POST", "/v1/conversations/%s/messages" % self.cid,
                          dict(self.content, kind="chat", op_key="caller-mcp-bodies-1"), self.token)
        self.mid = sent["message_id"]
        status, job = self.gw.claim_job(self.worker, self.mid)
        self.assertEqual(200, status, job)
        self.reply = P.e2e_body("reply", "Synthetic reply", self.files, self.bot_secret,
                                 [self.bot_key, self.actor_key], dict(bind, message_id=self.mid))
        status, finished = self.gw.finish(self.worker, job, reply=self.reply)
        self.assertEqual(200, status, finished)
        if finished["state"] == "review":
            self.assertEqual(200, self.gw.approve_reply(self.bot, self.mid)[0])
        store = G.Store(self.gw.home, G.load_config(self.gw.home))
        self.api = G.Api(store)
        entry = {"actor": {"actor_id": self.actor, "e2e_keys": [
                    {"secret": self.actor_secret, "public": self.actor_key}]},
                 "e2e_peers": {self.bot: {"key": self.bot_key, "kid": E.kid(self.bot_key)}}}
        self.state = SimpleNamespace(creds=lambda: {"v": 1, "gateways": {self.gw.url: entry}})

    def message(self, name="yobiru_message_status", args=None, rid=2):
        return {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {
            "name": name, "arguments": args if args is not None else {"message_id": self.mid}}}

    def rpc(self, message, token=None):
        bridge = G.McpGateway(self.api, token or self.token, "127.0.0.1")
        return M.handle(message, "caller", bridge.call)

    def deferred(self, message=None):
        with patch.object(C, "MAX_RESPONSE_BYTES", 9000):
            answer = self.rpc(message or self.message())
            self.assertLessEqual(len(M.dumps(answer).encode()), C.MAX_RESPONSE_BYTES)
        self.assertFalse(answer["result"]["isError"], answer)
        return answer

    def test_remote_caller_tools_expose_body_retrieval_under_current_permissions(self):
        for message, key in ((self.message(), None),
                             (self.message("yobiru_operation_status", {"conversation_id": self.cid,
                                                                      "op_key": "caller-mcp-bodies-1"}), None),
                             (self.message("yobiru_history", {"conversation_id": self.cid}), "messages"),
                             (self.message("yobiru_inbox", {}), "items")):
            data = self.deferred(message)["result"]["structuredContent"]
            view = data[key][0] if key else data
            self.assertIsNone(view["content"])
            self.assertIsNone(view["result"])
            self.assertEqual("/v1/messages/%s/result" % self.mid, view["result_url"])
        row = self.gw.sql("SELECT served_ts, caller_ack_ts FROM messages WHERE id=?", (self.mid,))[0]
        self.assertEqual((None, None), (row["served_ts"], row["caller_ack_ts"]))
        for part, expected in (("content", self.content), ("result", self.reply)):
            args = {"message_id": self.mid, "part": part}
            body = self.rpc(self.message("yobiru_message_body", args))["result"]
            self.assertFalse(body["isError"], body)
            data = body["structuredContent"]
            self.assertEqual((self.mid, self.cid, expected), (data["message_id"], data["conversation_id"], data[part]))
        self.assertIsNotNone(self.gw.sql("SELECT served_ts FROM messages WHERE id=?", (self.mid,))[0]["served_ts"])
        _, other = self.gw.actor()
        args = {"message_id": self.mid, "part": "result"}
        denied = self.rpc(self.message("yobiru_message_body", args), other)["result"]
        self.assertEqual("not_found", denied["structuredContent"]["error"])
        self.gw.policy(self.bot, offered=["chat"], visitor=["chat"])
        denied = self.rpc(self.message("yobiru_message_body", args))["result"]
        self.assertEqual("not_found", denied["structuredContent"]["error"])
        args["part"] = "content"
        redacted = self.rpc(self.message("yobiru_message_body", args))["result"]["structuredContent"]["content"]
        self.assertNotIn("sealed_files", redacted)
        self.assertTrue(redacted["attachments_withheld"])
        invalid = self.rpc(self.message("yobiru_message_body", {"message_id": self.mid, "part": "draft"}))["result"]
        self.assertEqual("invalid_arguments", invalid["structuredContent"]["error"])
        self.assertNotIn("yobiru_message_body", {t["name"] for t in M.tools_for("host")})

    def proxy(self, messages, answer, api_call=None):
        protocol = io.BytesIO()
        stdin = SimpleNamespace(buffer=io.BytesIO(b"".join(M.dumps(m).encode() + b"\n" for m in messages)))
        with patch.object(P, "mcp_identity", return_value=(self.gw.url, self.token, "Synthetic caller")), \
                patch.object(P.os, "dup", return_value=99), patch.object(P.os, "dup2"), \
                patch.object(P.os, "fdopen", return_value=protocol), patch.object(P.sys, "stdin", stdin), \
                patch.object(P.sys, "stdout", io.StringIO()), patch.object(P.sys, "stderr", io.StringIO()), \
                patch.object(C, "post_raw", return_value=(200, "application/json", M.dumps(answer).encode())), \
                patch.object(C, "api_call", side_effect=api_call or C.api_call) as calls:
            P.cmd_mcp(self.state, SimpleNamespace())
        return [json.loads(line) for line in protocol.getvalue().splitlines()], calls.call_args_list

    def test_proxy_hydrates_when_every_encrypted_body_was_deferred_and_preserves_media_blocks(self):
        answer = self.deferred()
        self.assertNotIn('"sealed"', M.dumps(answer))
        answers, calls = self.proxy([self.message()], answer)
        result = answers[0]["result"]
        self.assertFalse(result["isError"], result)
        data = result["structuredContent"]
        self.assertEqual(("Synthetic request", "Synthetic reply"), (data["content"]["text"], data["result"]["text"]))
        self.assertEqual([True, True], [data[p]["e2e"]["opened"] for p in ("content", "result")])
        self.assertNotIn("content_url", data)
        self.assertNotIn("result_url", data)
        self.assertEqual(5, len(result["content"]))
        files = data["content"]["attachments"] + data["result"]["attachments"]
        self.assertEqual([1, 2, 3, 4], [f["content_block"] for f in files])
        self.assertEqual(["resource"] * 4, [b["type"] for b in result["content"][1:]])
        self.assertTrue(all(c.args[1] == "GET" for c in calls), "hydration never acknowledges a response")
        self.assertIsNone(self.gw.sql("SELECT caller_ack_ts FROM messages WHERE id=?", (self.mid,))[0]["caller_ack_ts"])

    def test_proxy_body_failure_is_a_tool_error_and_keeps_the_protocol_running(self):
        answer = self.deferred()
        api_call = C.api_call

        def unavailable(base, method, path, *args, **kwargs):
            if path.endswith("/result"):
                raise C.ApiError(404, "not_found")
            return api_call(base, method, path, *args, **kwargs)

        answers, calls = self.proxy([self.message(), {"jsonrpc": "2.0", "id": 3, "method": "ping"}],
                                    answer, unavailable)
        self.assertEqual([2, 3], [a["id"] for a in answers])
        self.assertTrue(answers[0]["result"]["isError"])
        self.assertEqual("body_unavailable", answers[0]["result"]["structuredContent"]["error"])
        self.assertEqual({}, answers[1]["result"])
        self.assertTrue(all(c.args[1] == "GET" for c in calls))
        row = self.gw.sql("SELECT served_ts, caller_ack_ts FROM messages WHERE id=?", (self.mid,))[0]
        self.assertEqual((None, None), (row["served_ts"], row["caller_ack_ts"]))


class McpResultBudgetTests(unittest.TestCase):
    def test_large_sealed_body_occurs_once_in_a_bounded_mcp_reply(self):
        ciphertext = "synthetic-ciphertext-" + "a" * 25000
        body = {"ok": True, "message_id": "msg_" + "a" * 16, "conversation_id": "cnv_" + "b" * 16,
                "content": {"sealed": {"ct": ciphertext}}}
        message = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
            "name": "yobiru_message_body", "arguments": {"message_id": body["message_id"], "part": "content"}}}
        with patch.object(C, "MAX_RESPONSE_BYTES", 40000):
            answer = M.handle(message, "caller", lambda name, args: (200, copy.deepcopy(body)))
            wire = M.dumps(answer).encode()
            self.assertLessEqual(len(wire), C.MAX_RESPONSE_BYTES)
        self.assertEqual(1, wire.count(ciphertext.encode()))
        self.assertEqual(body["content"], answer["result"]["structuredContent"]["content"])
        self.assertIn("structuredContent", answer["result"]["content"][0]["text"])
        self.assertFalse(answer["result"]["isError"])

    def test_jsonrpc_envelope_is_included_in_the_limit(self):
        message = {"jsonrpc": "2.0", "id": "request-" + "a" * 6000, "method": "tools/call", "params": {
            "name": "yobiru_inbox", "arguments": {}}}
        with patch.object(C, "MAX_RESPONSE_BYTES", 9000):
            answer = M.handle(message, "caller", lambda name, args: (200, {"ok": True, "items": ["b" * 4000]}))
            self.assertLessEqual(len(M.dumps(answer).encode()), C.MAX_RESPONSE_BYTES)
        self.assertTrue(answer["result"]["isError"])
        self.assertEqual("response_too_large", answer["result"]["structuredContent"]["error"])


if __name__ == "__main__":
    unittest.main()
