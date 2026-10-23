"""Owner history loads deferred bodies safely; reply context stays independent of media."""
import copy
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from support.public_harness import C

import bot_client as P  # noqa: E402


class OwnerConversationClientTests(unittest.TestCase):
    base = "http://127.0.0.1"
    token = "synthetic-owner"
    bot = "bot_" + "a" * 16
    actor = "act_" + "b" * 16
    cid = "cnv_" + "c" * 16
    mid = "msg_" + "d" * 16
    later = "msg_" + "e" * 16

    @property
    def history_path(self):
        return "/v1/owner/conversations/%s" % self.cid

    def body_path(self, mid, part):
        return "%s/messages/%s/%s" % (self.history_path, mid, part)

    def history(self, messages=()):
        return {"ok": True, "conversation_id": self.cid, "bot_id": self.bot,
                "actor_id": self.actor, "actor_e2e_key": "synthetic-public-key",
                "messages": list(messages), "notice": "Synthetic untrusted content notice"}

    def body(self, mid, content):
        return {"ok": True, "conversation_id": self.cid, "message_id": mid,
                "bot_id": self.bot, "actor_id": self.actor, "content": content}

    def test_context_does_not_fetch_conversation_bodies(self):
        context = {k: v for k, v in self.history().items() if k not in ("messages", "notice")}
        context["latest_caller_message_id"] = self.later
        with patch.object(C, "api_call", return_value=(200, context)) as calls:
            result = P.owner_conversation_context(self.base, self.token, self.cid)
        self.assertEqual(context, result)
        self.assertEqual([self.history_path + "/context"], [c.args[2] for c in calls.call_args_list])
        self.assertEqual(self.token, calls.call_args.args[4])

    def test_legacy_context_fallback_keeps_keys_and_finds_last_caller_without_loading_bodies(self):
        messages = [{"message_id": self.mid, "author": "caller", "content": None,
                     "content_url": self.body_path(self.mid, "content")},
                    {"message_id": self.later, "author": "caller", "content": {"text": "later"}},
                    {"message_id": "msg_" + "f" * 16, "author": "owner", "content": {"text": "owner"}}]
        history = self.history(messages)
        with patch.object(C, "api_call", side_effect=[(404, {"ok": False, "error": "not_found"}),
                                                      (200, history)]) as calls:
            result = P.owner_conversation_context(self.base, self.token, self.cid)
        for field in ("conversation_id", "bot_id", "actor_id", "actor_e2e_key"):
            self.assertEqual(history[field], result[field])
        self.assertEqual(self.later, result["latest_caller_message_id"])
        self.assertEqual([self.history_path + "/context", self.history_path],
                         [c.args[2] for c in calls.call_args_list])

    def test_legacy_empty_history_has_no_continuation_anchor(self):
        with patch.object(C, "api_call", side_effect=[(404, {"ok": False, "error": "not_found"}),
                                                      (200, self.history())]):
            result = P.owner_conversation_context(self.base, self.token, self.cid)
        self.assertIsNone(result["latest_caller_message_id"])

    def test_context_falls_back_only_for_a_missing_endpoint(self):
        for status, code in ((401, "unauthorized"), (403, "forbidden"), (429, "rate_limited"),
                             (500, "internal_error"), (404, "response_too_large"), (None, "unreachable")):
            with self.subTest(status=status, code=code), redirect_stdout(io.StringIO()) as output, \
                    patch.object(C, "api_call", side_effect=C.ApiError(status, code)) as calls:
                with self.assertRaises(SystemExit) as raised:
                    P.owner_conversation_context(self.base, self.token, self.cid)
                self.assertEqual(2, raised.exception.code)
                self.assertEqual(1, calls.call_count, "a failure must not trigger a large history fetch")
                self.assertIn(code, output.getvalue())

    def test_legacy_inline_history_needs_no_additional_requests(self):
        history = self.history([{"message_id": self.mid, "author": "caller",
                                 "content": {"text": "Synthetic inline request"},
                                 "result": {"text": "Synthetic inline reply"}}])
        expected = copy.deepcopy(history)
        with patch.object(C, "api_call", return_value=(200, history)) as calls:
            result = P.owner_conversation_view(self.base, self.token, self.cid)
        self.assertEqual(expected, result)
        self.assertEqual([self.history_path], [c.args[2] for c in calls.call_args_list])

    def test_deferred_request_result_and_draft_are_restored_without_losing_history(self):
        request = {"text": "Synthetic request", "attachments": [{"name": "request.txt", "content": "file"}]}
        result = {"text": "Synthetic released reply"}
        draft = {"text": "Synthetic draft", "attachments": [{"name": "draft.txt", "content": "draft file"}]}
        inline = {"text": "Later request"}
        messages = [{"message_id": self.mid, "author": "caller", "content": None, "result": None,
                     "content_url": self.body_path(self.mid, "content"),
                     "result_url": self.body_path(self.mid, "result"), "state": "completed"},
                    {"message_id": self.later, "author": "caller", "content": inline, "draft": None,
                     "draft_url": self.body_path(self.later, "draft"), "state": "review",
                     "draft_hash": "synthetic-draft-hash"}]
        responses = {self.history_path: self.history(messages),
                     self.body_path(self.mid, "content"): self.body(self.mid, request),
                     self.body_path(self.mid, "result"): self.body(self.mid, result),
                     self.body_path(self.later, "draft"): self.body(self.later, draft)}
        with patch.object(C, "api_call", side_effect=lambda base, method, path, *args, **kwargs:
                          (200, responses[path])) as calls:
            restored = P.owner_conversation_view(self.base, self.token, self.cid)
        self.assertEqual([self.mid, self.later], [m["message_id"] for m in restored["messages"]])
        first, second = restored["messages"]
        self.assertEqual(request, first["content"])
        self.assertEqual(result, first["result"])
        self.assertEqual(inline, second["content"])
        self.assertEqual(draft, second["draft"])
        self.assertEqual("synthetic-draft-hash", second["draft_hash"])
        self.assertEqual("synthetic-public-key", restored["actor_e2e_key"])
        self.assertEqual("Synthetic untrusted content notice", restored["notice"])
        self.assertTrue(all(not any(part + "_url" in m for part in ("content", "result", "draft"))
                            for m in restored["messages"]))
        self.assertCountEqual(responses, [c.args[2] for c in calls.call_args_list])
        self.assertTrue(all(c.args[1] == "GET" and c.args[4] == self.token for c in calls.call_args_list))

    def test_deferred_links_cannot_redirect_owner_credentials_or_select_another_body(self):
        expected = self.body_path(self.mid, "content")
        links = ["https://example.invalid/body", "//example.invalid/body",
                 expected.replace(self.cid, "cnv_" + "f" * 16),
                 expected.replace(self.mid, self.later), self.body_path(self.mid, "result"),
                 expected + "?redirect=1", expected + "/../result"]
        for link in links:
            history = self.history([{"message_id": self.mid, "content": None, "content_url": link}])
            with self.subTest(link=link), patch.object(C, "api_call", return_value=(200, history)) as calls:
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                    P.owner_conversation_view(self.base, self.token, self.cid)
                self.assertEqual(2, raised.exception.code)
                self.assertEqual(1, calls.call_count, "reject untrusted links before sending the owner token")

    def test_deferred_body_message_ids_are_validated_before_requesting_them(self):
        for mid in ("msg_invalid", "bot_" + "d" * 16, self.mid + "/../../other"):
            history = self.history([{"message_id": mid, "content": None,
                                     "content_url": self.body_path(mid, "content")}])
            with self.subTest(mid=mid), patch.object(C, "api_call", return_value=(200, history)) as calls:
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                    P.owner_conversation_view(self.base, self.token, self.cid)
                self.assertEqual(2, raised.exception.code)
                self.assertEqual(1, calls.call_count)

    def test_deferred_body_identity_must_match_the_original_history(self):
        for field, bad in (("message_id", self.later), ("conversation_id", "cnv_" + "f" * 16),
                           ("bot_id", "bot_" + "f" * 16), ("actor_id", "act_" + "f" * 16)):
            for value in (bad, None):
                history = self.history([{"message_id": self.mid, "content": None,
                                         "content_url": self.body_path(self.mid, "content")}])
                body = self.body(self.mid, {"text": "Synthetic wrong-body content"})
                if value is None:
                    del body[field]
                else:
                    body[field] = value
                with self.subTest(field=field, missing=value is None), \
                        patch.object(C, "api_call", side_effect=[(200, history), (200, body)]) as calls:
                    with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                        P.owner_conversation_view(self.base, self.token, self.cid)
                    self.assertEqual(2, raised.exception.code)
                    self.assertEqual(2, calls.call_count)
                    self.assertIsNone(history["messages"][0]["content"], "do not install a mismatched body")


if __name__ == "__main__":
    unittest.main()
