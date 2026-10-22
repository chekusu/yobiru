"""Current review commands remain usable with a gateway's pre-metadata review API."""
import io
import re
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from test_friends_client import FriendsClientCase, cli
from test_friends_e2e import E2ECase

import bot_client as P


def legacy_review_api():
    """Exercise real gateway actions with the pre-PR metadata route and review response shape."""
    api_call = P.C.api_call

    def legacy(base, method, path, *args, **kwargs):
        if method == "GET" and re.fullmatch(r"/v1/owner/messages/msg_[a-z2-7]{16}", path):
            return 404, {"ok": False, "error": "not_found"}
        status, obj = api_call(base, method, path, *args, **kwargs)
        if method == "GET" and re.fullmatch(r"/v1/owner/bots/bot_[a-z2-7]{16}/reviews", path):
            obj.pop("next", None)
            for item in obj["inbound"]:
                for key in ("message", "content_trust", "display_name_untrusted"):
                    item.pop(key, None)
        return status, obj

    return patch.object(P.C, "api_call", side_effect=legacy)


class LegacyReviewTests(FriendsClientCase):
    def request(self):
        bot = self.owner_bot("alice")
        self.native("alice", bot)
        self.y("caller", "connect", self.gw.url + "/@alice", "--name", "Synthetic visitor")
        mid = self.y("caller", "chat", "alice", "Synthetic request")["message_id"]
        return bot, mid

    def test_approve_inbound_without_message_metadata_endpoint(self):
        bot, mid = self.request()
        with legacy_review_api():
            approved = self.y("owner", "review", "approve-inbound", "alice", mid)
        self.assertEqual("approved", approved["inbound"])
        claimed = self.y("host", "job", "claim", bot, mid)
        self.assertEqual(mid, claimed["message_id"])

    def test_decline_note_without_message_metadata_endpoint(self):
        _, mid = self.request()
        with legacy_review_api():
            result = self.y("owner", "review", "reject-inbound", "alice", mid, "--text", "Synthetic decline")
        self.assertEqual("declined", result["state"])
        self.assertEqual("Synthetic decline", self.y("caller", "status", mid)["result"]["text"])
        self.assert_no_credentials_printed()

    def test_fallback_does_not_apply_review_to_another_bot(self):
        _, mid = self.request()
        self.y("owner", "bot", "create", "--handle", "other", "--name", "Synthetic other bot")
        for action, options in (("approve-inbound", ()), ("reject-inbound", ("--text", "Wrong bot decline"))):
            with self.subTest(action=action), legacy_review_api() as calls:
                code, _, _ = cli(self.homes["owner"], "review", action, "other", mid, *options)
                self.assertEqual(2, code)
                self.assertFalse(any(c.args[1] == "POST" for c in calls.call_args_list))
        self.assertEqual([mid], [x["job_id"] for x in self.y("owner", "review", "list", "alice")["inbound"]])


class LegacySealedReviewTests(E2ECase):
    def test_decline_note_uses_legacy_sender_key_and_stays_encrypted(self):
        self.bot_with_host(files=False)
        self.y("caller", "connect", "%s/@%s" % (self.gw.url, self.handle), "--name", "Sealed caller")
        mid = self.y("caller", "chat", self.handle, "Synthetic sealed request")["message_id"]
        with legacy_review_api():
            result = self.y("owner", "review", "reject-inbound", self.handle, mid,
                            "--text", "Synthetic sealed decline")
        self.assertEqual("declined", result["state"])
        self.assert_opaque(self.stored(mid), "Synthetic sealed request", "Synthetic sealed decline")
        self.assertEqual("Synthetic sealed decline", self.y("caller", "status", mid)["result"]["text"])


class MessageContextFailureTests(unittest.TestCase):
    def test_only_missing_metadata_endpoint_can_fall_back(self):
        for status, code in ((401, "unauthorized"), (403, "forbidden"), (429, "rate_limited"),
                             (500, "internal_error"), (404, "response_too_large"), (None, "unreachable")):
            with self.subTest(status=status, code=code), redirect_stdout(io.StringIO()) as output, \
                    patch.object(P.C, "api_call", side_effect=P.C.ApiError(status, code)) as calls:
                with self.assertRaises(SystemExit) as raised:
                    P.owner_message_context("http://127.0.0.1", "synthetic-owner", "bot_" + "a" * 16,
                                            "msg_" + "b" * 16)
                self.assertEqual(2, raised.exception.code)
                self.assertEqual(1, calls.call_count)
                self.assertIn(code, output.getvalue())


if __name__ == "__main__":
    unittest.main()
