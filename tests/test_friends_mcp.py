"""The MCP facade: Streamable HTTP at the gateway's /mcp (JSON responses, no SSE) and the local stdio proxy.

Protocol and transport rules (version negotiation, notifications, 405 without SSE, Origin, Accept, Content-Type,
bearer roles, protocol header, batches, sizes), tool isolation between caller and native-binding credentials,
typed schemas and argument validation, idempotent and immutable-reference sends (a retry can never reach another
bot after a handle moved), a conversation that must belong to the named bot, the full supervised flow with
attachments, decisions and acknowledgements, revocation, and a stdio proxy that keeps stdout to JSON-RPC lines.

Real bot_gateway.py subprocesses, a real bot_client.py subprocess for stdio, synthetic homes, loopback only.
"""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support.pkg import free_port
from support.public_harness import C, PUBLIC, FriendsGateway, cli, plain_env

LIMITS = {"min_deadline_seconds": 1, "read_timeout_seconds": 5, "messages_per_minute_per_actor": 1000,
          "messages_per_hour_per_actor": 5000, "pending_per_contact": 100, "new_conversations_per_hour_per_bot": 1000,
          "new_contacts_per_hour_per_bot": 1000, "enroll_per_hour_per_ip": 1000, "enroll_per_hour": 1000,
          "bot_enroll_per_hour": 1000, "bot_enroll_per_hour_per_ip": 1000, "agent_messages_per_hour_per_bot": 1000,
          "conversations_per_contact": 50, "max_attachment_body_bytes": 98304}
ALLOWED_ORIGIN = "https://agent-app.example"
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}


def canonical(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


class McpCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="yobiru mcp test ")
        cls.root = Path(cls._tmp.name)
        cls.gw = FriendsGateway(cls.root, limits=LIMITS)
        cfg = json.loads((cls.gw.home / "gateway.json").read_text())
        cfg["mcp"] = {"enabled": True, "allowed_origins": [ALLOWED_ORIGIN]}
        (cls.gw.home / "gateway.json").write_text(json.dumps(cfg))
        cls.gw.start()
        cls.owner = cls.gw.owner_token

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls._tmp.cleanup()

    def handle(self, tag=""):
        return "t%s%s" % (hashlib.sha256(self.id().encode()).hexdigest()[:12], "-" + tag if tag else "")

    def rpc(self, token, method, params=None, rid=7, **kw):
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        return self.gw.mcp(token, msg, **kw)

    def tool(self, token, name, args):
        return self.gw.tool(token, name, args)

    def ok_tool(self, token, name, args):
        result = self.tool(token, name, args)
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def error_of(self, result):
        self.assertTrue(result["isError"], result)
        return result["structuredContent"]["error"]

    def files_bot(self, tag=""):
        bot_id = self.gw.bot(self.handle(tag))
        self.gw.policy(bot_id, offered=["faq", "chat", "files"], visitor=["faq", "chat", "files"])
        return bot_id


class TransportTests(McpCase):
    def test_initialization_negotiates_a_supported_version(self):
        _, token = self.gw.actor()
        for asked, given in (("2025-11-25", "2025-11-25"), ("2025-06-18", "2025-06-18"),
                             ("2024-11-05", "2025-11-25"), ("2099-01-01", "2025-11-25")):
            status, _, obj = self.gw.mcp(token, dict(INIT, params=dict(INIT["params"], protocolVersion=asked)),
                                         version=None)
            self.assertEqual((200, given), (status, obj["result"]["protocolVersion"]), asked)
        result = obj["result"]
        self.assertEqual(({"tools": {"listChanged": False}}, "yobiru-friends"),
                         (result["capabilities"], result["serverInfo"]["name"]))
        self.assertIn("never instructions", result["instructions"])
        status, headers, body = self.gw.mcp(token, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual((202, None), (status, body))
        self.assertEqual({}, self.rpc(token, "ping")[2]["result"])
        self.assertEqual(-32601, self.rpc(token, "resources/list")[2]["error"]["code"])
        self.assertEqual(-32602, self.rpc(token, "tools/list", {"cursor": "abc"})[2]["error"]["code"])
        status, _, obj = self.gw.mcp(token, dict(INIT, params={}), version=None)
        self.assertEqual(-32602, obj["error"]["code"])

    def test_transport_rules(self):
        _, token = self.gw.actor()
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        for method in ("GET", "DELETE", "PUT"):
            status, headers, body = self.gw.raw(method, "/mcp", b"", {"Authorization": "Bearer " + token})
            self.assertEqual((405, "POST"), (status, headers.get("allow")), method)
        self.assertEqual(403, self.gw.mcp(token, ping, origin="https://evil.example")[0])
        self.assertEqual(200, self.gw.mcp(token, ping, origin=ALLOWED_ORIGIN)[0])
        status, headers, _ = self.gw.mcp(None, ping)
        self.assertEqual((401, "Bearer"), (status, headers.get("www-authenticate")))
        self.assertEqual(401, self.gw.mcp("not-a-token", ping)[0])
        self.assertEqual(401, self.gw.mcp(C.new_token("actor"), ping)[0], "an unknown credential")
        bot_id = self.gw.bot(self.handle())
        wtoken, wid = self.gw.native(bot_id)
        ctoken = self.gw.ok("POST", "/v1/owner/bots/%s/workers/%s/connector" % (bot_id, wid), {}, self.owner)["token"]
        dtoken = self.gw.worker(bot_id)
        for other in (self.owner, ctoken, dtoken, C.new_token("verifier"), C.new_token("ticket")):
            self.assertEqual(403, self.gw.mcp(other, ping)[0], C.token_kind(other))
        self.gw.ok("POST", "/v1/owner/bots/%s/workers/%s/revoke" % (bot_id, wid), {}, self.owner)
        self.assertEqual(401, self.gw.mcp(wtoken, ping)[0], "a revoked binding")
        self.assertEqual(415, self.gw.mcp(token, ping, extra={"Content-Type": "text/plain"})[0])
        self.assertEqual(406, self.gw.mcp(token, ping, extra={"Accept": "text/event-stream"})[0])
        self.assertEqual(-32600, self.gw.mcp(token, None, raw_body=b"[" + json.dumps(ping).encode() + b"]")[2]
                         ["error"]["code"])
        status, _, obj = self.gw.mcp(token, {"jsonrpc": "2.0", "id": 3, "result": {}})
        self.assertEqual((400, -32600), (status, obj["error"]["code"]))
        status, _, obj = self.gw.mcp(token, None, raw_body=b"{not json")
        self.assertEqual((400, -32700, None), (status, obj["error"]["code"], obj["id"]))
        self.assertEqual(400, self.gw.mcp(token, ping, version=None)[0])
        self.assertEqual(400, self.gw.mcp(token, ping, version="2024-11-05")[0])
        self.assertEqual(400, self.gw.mcp(token, {"jsonrpc": "2.0", "id": None, "method": "ping"})[0])
        self.assertEqual(413, self.gw.mcp(token, None, raw_body=b" " * 120000)[0])

    def test_a_disabled_endpoint_does_not_exist(self):
        other = FriendsGateway(self.root, name="disabled", limits=LIMITS)
        cfg = json.loads((other.home / "gateway.json").read_text())
        cfg["mcp"] = {"enabled": False}
        (other.home / "gateway.json").write_text(json.dumps(cfg))
        other.start()
        self.addCleanup(other.stop)
        _, token = other.actor()
        self.assertEqual(404, other.mcp(token, INIT, version=None)[0])
        self.assertIsNone(other.ok("GET", "/@" + other.bot("disabledbot"))["api"]["mcp"])


class ToolTests(McpCase):
    def test_each_credential_sees_only_its_own_tools(self):
        bot_id = self.gw.bot(self.handle())
        wtoken, _ = self.gw.native(bot_id)
        _, token = self.gw.actor()
        caller_tools = self.rpc(token, "tools/list")[2]["result"]["tools"]
        host_tools = self.rpc(wtoken, "tools/list")[2]["result"]["tools"]
        self.assertEqual((17, 8), (len(caller_tools), len(host_tools)))
        self.assertFalse([t["name"] for t in host_tools if "notice" in t["name"]], "notices are messages (0.7.14)")
        names = {t["name"] for t in caller_tools} | {t["name"] for t in host_tools}
        for word in ("review", "approve", "owner", "admin", "grant", "trust", "publish", "exec", "shell", "file_path"):
            self.assertFalse([n for n in names if word in n], word)
        for t in caller_tools + host_tools:
            self.assertEqual(("object", False), (t["inputSchema"]["type"], t["inputSchema"]["additionalProperties"]))
            self.assertEqual("object", t["outputSchema"]["type"])
            self.assertEqual({"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"},
                             set(t["annotations"]))
            self.assertNotIn("url", json.dumps(t["inputSchema"]).lower().replace("urls", ""), t["name"])
        self.assertEqual(-32602, self.rpc(token, "tools/call", {"name": "yobiru_host_jobs", "arguments": {}})[2]
                         ["error"]["code"])
        self.assertEqual(-32602, self.rpc(wtoken, "tools/call", {"name": "yobiru_whoami", "arguments": {}})[2]
                         ["error"]["code"])
        self.assertEqual("visitors_cannot_follow", self.error_of(self.tool(token, "yobiru_follow", {"bot": bot_id})))

    def test_arguments_are_validated_before_anything_runs(self):
        bot_id = self.gw.bot(self.handle())
        _, token = self.gw.actor()
        good = {"bot": bot_id, "op_key": "op-key-0001", "text": "hello"}
        for bad in (dict(good, text="x" * 4001), dict(good, op_key="has space"), dict(good, bot="bot_x\n"),
                    dict(good, bot="@" + self.handle()), dict(good, attachments=[{"name": "a.md", "content": "x",
                                                                                   "path": "/etc/passwd"}]),
                    dict(good, extra=1), dict(good, kind="exec"), {"op_key": "op-key-0001", "text": "hi"},
                    dict(good, deadline_seconds="60"), dict(good, attachments="a.md")):
            self.assertEqual("invalid_arguments", self.error_of(self.tool(token, "yobiru_send_message", bad)), bad)
        self.assertEqual("invalid_text", self.error_of(self.tool(token, "yobiru_send_message",
                                                                 dict(good, text="bell\x07"))))
        self.assertEqual("other_gateway", self.error_of(self.tool(token, "yobiru_lookup_bot",
                                                                  {"bot": "https://203.0.113.9/@someone"})),
                         "a link of another gateway is refused, never fetched")
        self.assertEqual([], self.gw.sql("SELECT id FROM messages WHERE bot_id=?", (bot_id,)))

    def test_sends_are_idempotent_and_reach_only_the_named_bot(self):
        a = self.gw.bot(self.handle("a"))
        b = self.gw.bot(self.handle("b"))
        _, token = self.gw.actor()
        first = self.ok_tool(token, "yobiru_send_message", {"bot": a, "op_key": "op-key-0001", "text": "for A only"})
        again = self.ok_tool(token, "yobiru_send_message", {"bot": a, "op_key": "op-key-0001", "text": "for A only"})
        self.assertEqual((False, True, first["message_id"]), (first["duplicate"], again["duplicate"],
                                                              again["message_id"]))
        self.assertEqual("op_key_conflict", self.error_of(self.tool(
            token, "yobiru_send_message", {"bot": a, "op_key": "op-key-0001", "text": "changed"})))
        b_conv = self.ok_tool(token, "yobiru_send_message", {"bot": b, "op_key": "op-key-0002",
                                                             "text": "for B"})["conversation_id"]
        for args in ({"bot": a, "op_key": "op-key-0003", "text": "For A only", "conversation_id": b_conv},
                     {"bot": a, "op_key": "op-key-0004", "text": "For A only", "kind": "faq",
                      "conversation_id": b_conv},
                     {"bot": a, "op_key": "op-key-0001", "text": "for A only", "conversation_id": b_conv}):
            self.assertEqual("not_found", self.error_of(self.tool(token, "yobiru_send_message", args)), args)
        self.assertEqual(["for B"], [json.loads(m["content"])["text"] for m in self.gw.sql(
            "SELECT content FROM messages WHERE conversation_id=?", (b_conv,))])

    def test_a_retry_after_a_handle_moved_reaches_the_same_bot(self):
        h = self.handle()
        a = self.gw.bot(h)
        _, token = self.gw.actor()
        connected = self.ok_tool(token, "yobiru_connect", {"bot": "%s/@%s" % (self.gw.url, h)})
        self.assertEqual(a, connected["bot"]["bot_id"])
        sent = self.ok_tool(token, "yobiru_send_message", {"bot": connected["bot"]["id_address"],
                                                           "op_key": "op-key-0009", "text": "hi"})
        self.gw.ok("PATCH", "/v1/owner/bots/%s" % a, {"handle": h + "-moved"}, self.owner)
        b = self.gw.bot(h)
        retry = self.ok_tool(token, "yobiru_send_message", {"bot": a, "op_key": "op-key-0009", "text": "hi"})
        self.assertEqual((True, sent["message_id"]), (retry["duplicate"], retry["message_id"]))
        self.assertEqual("invalid_arguments", self.error_of(self.tool(
            token, "yobiru_send_message", {"bot": "%s/@%s" % (self.gw.url, h), "op_key": "op-key-0009", "text": "hi"})))
        self.assertEqual([], self.gw.sql("SELECT id FROM messages WHERE bot_id=?", (b,)), "nothing reached the new holder")
        self.assertEqual(b, self.ok_tool(token, "yobiru_lookup_bot", {"bot": "@" + h})["bot_id"],
                         "lookups follow the handle and return the id to pin")


class FlowTests(McpCase):
    def test_the_supervised_flow_with_attachments_over_mcp(self):
        bot_id = self.files_bot()
        wtoken, wid = self.gw.native(bot_id)
        actor_id, token = self.gw.actor()
        files = [{"name": "question.md", "content": "Q"}]
        sent = self.ok_tool(token, "yobiru_send_message", {"bot": bot_id, "op_key": "flow-0001", "text": "please",
                                                           "attachments": files})
        mid = sent["message_id"]
        self.assertEqual(200, self.gw.admit(bot_id, mid)[0])
        jobs = self.ok_tool(wtoken, "yobiru_host_jobs", {})["jobs"]
        self.assertEqual([(mid, True)], [(j["job_id"], j["claimable"]) for j in jobs])
        job = self.ok_tool(wtoken, "yobiru_host_claim", {"job_id": mid})
        self.assertEqual("Q", job["untrusted_content"]["attachments"][0]["content"])
        lease = {"job_id": mid, "lease_id": job["lease"]["lease_id"], "fence": job["lease"]["fence"]}
        self.assertEqual("started", self.ok_tool(wtoken, "yobiru_host_heartbeat", lease)["state"])
        reply_files = [{"name": "answer.md", "content": "A"}]
        drafted = self.ok_tool(wtoken, "yobiru_host_reply", dict(lease, text="here", attachments=reply_files))
        again = self.ok_tool(wtoken, "yobiru_host_reply", dict(lease, text="here", attachments=reply_files))
        self.assertEqual(("review", True), (drafted["state"], again["duplicate"]))
        self.assertEqual(200, self.gw.approve_reply(bot_id, mid)[0])
        status = self.ok_tool(token, "yobiru_message_status", {"message_id": mid})
        self.assertEqual(("completed", "A"), (status["state"], status["result"]["attachments"][0]["content"]))
        self.assertTrue(status["lifecycle"]["response_served_at"])
        self.assertIsNone(status["lifecycle"]["response_acknowledged_at"])
        acked = self.ok_tool(token, "yobiru_ack", {"message_ids": [mid, "msg_aaaaaaaaaaaaaaaa"]})
        self.assertEqual(([mid], ["msg_aaaaaaaaaaaaaaaa"]), ([x["message_id"] for x in acked["acknowledged"]],
                                                             [x["message_id"] for x in acked["refused"]]))
        decisions = self.ok_tool(token, "yobiru_message_decisions", {"message_id": mid})["decisions"]
        release = [d for d in decisions if d["action"] == "approve_reply"][0]
        digest = hashlib.sha256(canonical({"text": "here", "attachments": reply_files}).encode()).hexdigest()
        verified = self.ok_tool(token, "yobiru_verify_decision", {"decision_id": release["decision_id"],
                                                                  "subject_hash": digest})
        self.assertEqual((True, True), (verified["matches"]["subject_hash"], verified["current"]["authorized_now"]))
        chk = self.gw.ok("POST", "/v1/owner/bots/%s/workers/%s/check" % (bot_id, wid), {}, self.owner)["check_id"]
        self.assertTrue(self.ok_tool(wtoken, "yobiru_host_check_in", {"check_id": chk})["acknowledged"])
        fyi = self.ok_tool(token, "yobiru_send_message", {"bot": bot_id, "op_key": "flow-0002", "text": "fyi"})
        self.assertEqual((False, None), (fyi["reply_required"], fyi["deadline"]), "no message needs a reply")
        jobs = {j["job_id"]: j for j in self.ok_tool(wtoken, "yobiru_host_jobs", {})["jobs"]}
        self.assertEqual("pending", jobs[fyi["message_id"]]["review"]["inbound"], "a message request, as a job")
        self.assertEqual("invalid_arguments", self.error_of(self.tool(
            token, "yobiru_send_message", {"bot": bot_id, "op_key": "flow-0003", "kind": "notice", "text": "fyi"})))

    def test_withdrawn_files_are_withheld_from_mcp_reads(self):
        bot_id = self.files_bot()
        wtoken, _ = self.gw.native(bot_id)
        actor_id, token = self.gw.actor()
        mid = self.ok_tool(token, "yobiru_send_message", {"bot": bot_id, "op_key": "files-001", "text": "file",
                                                          "attachments": [{"name": "q.md", "content": "Q-SECRET"}]})[
            "message_id"]
        self.assertEqual(200, self.gw.admit(bot_id, mid)[0])
        job = self.ok_tool(wtoken, "yobiru_host_claim", {"job_id": mid})
        self.ok_tool(wtoken, "yobiru_host_reply", {"job_id": mid, "lease_id": job["lease"]["lease_id"],
                                                   "fence": job["lease"]["fence"], "text": "a",
                                                   "attachments": [{"name": "a.md", "content": "A-SECRET"}]})
        self.assertEqual(200, self.gw.approve_reply(bot_id, mid)[0])
        self.assertEqual(200, self.gw.contact(bot_id, actor_id, "restrict", ["faq", "chat"])[0])
        for name, args in (("yobiru_message_status", {"message_id": mid}), ("yobiru_inbox", {}),
                           ("yobiru_history", {"conversation_id": job["conversation_id"]})):
            text = json.dumps(self.ok_tool(token, name, args))
            self.assertNotIn("SECRET", text, name)
        self.assertIn("result_withheld", self.ok_tool(token, "yobiru_message_status", {"message_id": mid}))

    def test_revocation_reaches_the_mcp_tools(self):
        bot_id = self.gw.bot(self.handle())
        wtoken, wid = self.gw.native(bot_id)
        actor_id, token = self.gw.actor()
        mid = self.ok_tool(token, "yobiru_send_message", {"bot": bot_id, "op_key": "revoke-01", "text": "x"})["message_id"]
        self.gw.contact(bot_id, actor_id, "block")
        self.assertEqual("not_found", self.error_of(self.tool(token, "yobiru_message_status", {"message_id": mid})))
        self.assertEqual("contact_revoked", self.error_of(self.tool(token, "yobiru_send_message", {
            "bot": bot_id, "op_key": "revoke-02", "text": "again"})))
        self.gw.ok("POST", "/v1/owner/bots/%s/workers/%s/revoke" % (bot_id, wid), {}, self.owner)
        self.assertEqual(401, self.rpc(wtoken, "tools/list")[0])


class StdioTests(McpCase):
    def caller_home(self):
        home = self.root / ("%s-home" % self.handle())
        bot_id = self.gw.bot(self.handle())
        code, obj, err = cli(home, "connect", "%s/@%s" % (self.gw.url, self.handle()), "--name", "Stdio caller")
        self.assertEqual(0, code, err)
        return home, bot_id

    def run_stdio(self, home, lines, *args):
        proc = subprocess.run([sys.executable, str(PUBLIC / "bot_client.py"), "--home", str(home), "mcp"] + list(args),
                              input="".join(lines).encode("utf-8"), capture_output=True, timeout=60,
                              env=plain_env(self.root))
        out = proc.stdout.decode("utf-8").splitlines()
        return proc.returncode, [json.loads(line) for line in out], proc.stderr.decode("utf-8")

    def test_the_stdio_proxy_answers_one_json_line_per_request(self):
        home, bot_id = self.caller_home()
        msgs = [INIT, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "ping"}, {"jsonrpc": "2.0", "id": "three", "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                 "params": {"name": "yobiru_send_message", "arguments": {"bot": bot_id, "op_key": "stdio-01",
                                                                         "text": "over stdio"}}},
                {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "yobiru_host_jobs",
                                                                               "arguments": {}}},
                {"jsonrpc": "2.0", "id": 6, "result": {}}]
        lines = [json.dumps(m) + "\n" for m in msgs] + ["{not json\n", "[1, 2]\n", "\n"]
        code, out, err = self.run_stdio(home, lines)
        self.assertEqual(0, code, err)
        self.assertEqual([1, 2, "three", 4, 5, None, None], [m.get("id") for m in out])
        self.assertEqual("2025-11-25", out[0]["result"]["protocolVersion"])
        self.assertEqual({}, out[1]["result"])
        self.assertEqual(17, len(out[2]["result"]["tools"]))
        self.assertEqual("queued", out[3]["result"]["structuredContent"]["state"])
        self.assertEqual(-32602, out[4]["error"]["code"])
        self.assertEqual((-32700, -32600), (out[5]["error"]["code"], out[6]["error"]["code"]))
        self.assertIn("/mcp", err)
        self.assertNotIn(json.loads((home / "public" / "credentials.json").read_text())["gateways"][self.gw.url]
                         ["actor"]["token"], json.dumps(out) + err, "the credential never reaches the agent")

    def test_the_host_mode_keeps_the_owner_credential_away(self):
        home = self.root / ("%s-host" % self.handle())
        bot_id = self.gw.bot(self.handle())
        token_file = self.root / ("%s.token" % self.handle())
        self.gw.ok("POST", "/v1/owner/bots/%s/workers" % bot_id, {"adapter": "native"}, self.owner)
        obj = self.gw.ok("POST", "/v1/owner/bots/%s/workers" % bot_id, {"adapter": "native", "replace": True},
                         self.owner)
        C.create_private_file(token_file, obj["token"] + "\n")
        self.assertEqual(0, cli(home, "worker", "login", self.gw.url, "--token-file", str(token_file))[0])
        code, out, err = self.run_stdio(home, [json.dumps(INIT) + "\n"], "--host", bot_id)
        self.assertEqual((0, "2025-11-25"), (code, out[0]["result"]["protocolVersion"]))
        self.assertEqual(0, cli(home, "owner", "login", self.gw.url, "--token-file",
                                str(self.gw.home / "owner.token"))[0])
        code, out, err = self.run_stdio(home, [json.dumps(INIT) + "\n"], "--host", bot_id)
        self.assertEqual((2, []), (code, out))
        self.assertIn("owner credential", err)
        code, out, err = self.run_stdio(home, [], "--host", bot_id, "--as", "visitor")
        self.assertEqual(2, code)
        self.assertIn("not allowed with", err)

    def test_an_unreachable_gateway_is_an_explicit_unknown_outcome(self):
        home = self.root / ("%s-dead" % self.handle())
        dead = "http://127.0.0.1:%d" % free_port()
        C.ensure_private_dir(home / "public")
        C.write_private_json(home / "public" / "credentials.json", {"v": 1, "gateways": {dead: {"actor": {
            "actor_id": "act_aaaaaaaaaaaaaaaa", "token": C.new_token("actor")}}}})
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "yobiru_inbox", "arguments": {}}}
        code, out, err = self.run_stdio(home, [json.dumps(INIT) + "\n", json.dumps(call) + "\n"])
        self.assertEqual(0, code, err)
        self.assertEqual(-32603, out[0]["error"]["code"])
        self.assertEqual("gateway_unreachable", out[1]["result"]["structuredContent"]["error"])
        self.assertTrue(out[1]["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
