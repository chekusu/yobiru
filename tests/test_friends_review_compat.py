"""Review commands remain usable across gateway-only and client-only upgrades."""
import base64
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from support.public_harness import legacy_gateway_prefix
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from test_friends_client import FriendsClientCase, TOKEN_PATTERN, cli
from test_friends_e2e import E2ECase
from support.public_harness import FriendsGateway, PUBLIC, plain_env
from fixtures.review_client_pr33_base import cmd_review as base_cmd_review

import bot_gateway as G

import bot_client as P

BASE_COMMIT = "0154a9375d9b7a2d3da70b8ebf074eeb12cf5cb1"


def legacy_review_api():
    """Exercise real gateway actions with the pre-PR metadata route and review response shape."""
    api_call = P.C.api_call

    def legacy(base, method, path, *args, **kwargs):
        if method == "GET" and re.fullmatch(r"/v1/owner/messages/msg_[a-z2-7]{16}", path):
            return 404, {"ok": False, "error": "not_found"}
        if method == "GET" and path.endswith("/reviews/full"):
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
            repeated = self.y("owner", "review", "reject-inbound", "alice", mid, "--text", "Synthetic decline")
        self.assertEqual("declined", result["state"])
        self.assertFalse(result["duplicate"])
        self.assertEqual(dict(result, duplicate=True), repeated)
        self.assertEqual("Synthetic decline", self.y("caller", "status", mid)["result"]["text"])
        contexts = list((self.homes["owner"] / "public" / "review-context").glob("*.json"))
        self.assertEqual(1, len(contexts))
        self.assertEqual(0, contexts[0].stat().st_mode & 0o077)
        saved = contexts[0].read_text()
        for text in ("Synthetic request", "Synthetic decline"):
            self.assertNotIn(text, saved)
        self.assertIsNone(TOKEN_PATTERN.search(saved))
        self.assert_no_credentials_printed()

    def test_decline_retries_after_successful_response_is_lost(self):
        _, mid = self.request()
        with legacy_review_api():
            api_call = P.C.api_call

            def lose_response(base, method, path, *args, **kwargs):
                result = api_call(base, method, path, *args, **kwargs)
                if method == "POST" and path == "/v1/owner/messages/%s/review" % mid:
                    self.assertEqual(200, result[0])
                    raise P.C.ApiError(None, "unreachable")
                return result

            with patch.object(P.C, "api_call", side_effect=lose_response):
                failed = self.y("owner", "review", "reject-inbound", "alice", mid,
                                "--text", "Synthetic decline", expect=2)
            self.assertEqual("unreachable", failed["error"])
            self.assertEqual([], self.y("owner", "review", "list", "alice")["inbound"])
            repeated = self.y("owner", "review", "reject-inbound", "alice", mid,
                              "--text", "Synthetic decline")
        self.assertTrue(repeated["duplicate"])
        self.assertEqual("Synthetic decline", self.y("caller", "status", mid)["result"]["text"])

    def test_reject_reply_retry_keeps_original_decline(self):
        bot, mid = self.request()
        with legacy_review_api():
            self.y("owner", "review", "approve-inbound", "alice", mid)
            self.y("host", "job", "claim", bot, mid)
            self.y("host", "job", "reply", bot, mid, "--text", "Synthetic draft")
            result = self.y("owner", "review", "reject-reply", "alice", mid, "--text", "Synthetic decline")
            self.assertEqual([], self.y("owner", "review", "list", "alice")["replies"])
            repeated = self.y("owner", "review", "reject-reply", "alice", mid, "--text", "Changed decline")
        self.assertFalse(result["duplicate"])
        self.assertEqual(dict(result, duplicate=True), repeated)
        self.assertEqual("Synthetic decline", self.y("caller", "status", mid)["result"]["text"])

    def test_fallback_does_not_apply_review_to_another_bot(self):
        _, mid = self.request()
        self.y("owner", "bot", "create", "--handle", "other", "--name", "Synthetic other bot")
        for state in ("pending", "declined"):
            for action, options in (("approve-inbound", ()), ("reject-inbound", ("--text", "Wrong bot decline"))):
                with self.subTest(action=action, state=state), legacy_review_api() as calls:
                    code, _, _ = cli(self.homes["owner"], "review", action, "other", mid, *options)
                    self.assertEqual(2, code)
                    self.assertFalse(any(c.args[1] == "POST" for c in calls.call_args_list))
            if state == "pending":
                self.assertEqual([mid], [x["job_id"] for x in self.y("owner", "review", "list", "alice")["inbound"]])
                with legacy_review_api():
                    self.y("owner", "review", "reject-inbound", "alice", mid, "--text", "Synthetic decline")


class BaseGatewayDeclineTests(unittest.TestCase):
    def test_decline_retries_with_exact_base_gateway_across_client_processes(self):
        with tempfile.TemporaryDirectory(prefix="yobiru legacy decline ") as directory:
            root = Path(directory)
            legacy = root / "base-gateway"
            legacy.mkdir()
            for name in ("bot_gateway.py", "bot_common.py", "bot_e2e.py", "bot_mcp.py", "bot_blobs.py",
                         "bot_profile.py"):
                try:
                    source = subprocess.run(["git", "show", BASE_COMMIT + ":public_bots/" + name],
                                            cwd=PUBLIC.parent, capture_output=True, check=True).stdout
                except (FileNotFoundError, subprocess.CalledProcessError):
                    self.skipTest("the public PR base commit is unavailable (for example, a source archive)")
                (legacy / name).write_bytes(source)
            gw = FriendsGateway(root, prefix=legacy_gateway_prefix(legacy / "bot_gateway.py")).start()
            try:
                def client(home, *args):
                    result = subprocess.run([sys.executable, str(PUBLIC / "bot_client.py"),
                                             "--home", str(home), *args], env=plain_env(root),
                                            capture_output=True, text=True, timeout=30)
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    return json.loads(result.stdout)

                owner = root / "owner"
                client(owner, "owner", "login", gw.url, "--token-file", str(gw.home / "owner.token"))
                for sealed in (False, True):
                    with self.subTest(sealed=sealed):
                        handle = "sealed-decline" if sealed else "plain-decline"
                        if sealed:
                            bot = client(owner, "bot", "create", "--handle", handle,
                                         "--name", "Synthetic bot")["bot_id"]
                            client(owner, "bot", "publish", handle)
                            client(owner, "bot", "edit", handle, "--require-e2e", "on")
                        else:
                            bot = gw.bot(handle)
                        gw.native(bot)
                        caller = root / handle
                        client(caller, "connect", gw.url + "/@" + handle, "--name", "Synthetic caller")
                        mid = client(caller, "chat", handle, "Synthetic request")["message_id"]
                        args = ("review", "reject-inbound", handle, mid, "--text", "Synthetic decline")
                        first = client(owner, *args)
                        repeated = client(owner, *args)
                        self.assertFalse(first["duplicate"])
                        self.assertEqual(dict(first, duplicate=True), repeated)
                        self.assertEqual("Synthetic decline", client(caller, "status", mid)["result"]["text"])
            finally:
                gw.stop()


class BaseClientReviewTests(FriendsClientCase):
    """Old owner commands still see and act on every request after a gateway-only upgrade."""
    base_commit = BASE_COMMIT

    def large_requests(self):
        bot = self.owner_bot("legacy-review")
        self.gw.policy(bot, offered=["chat", "files"], visitor=["chat", "files"])
        _, caller = self.gw.actor()
        cid = self.gw.conversation(bot, caller)
        size = G.LIMIT_DEFAULTS["attachment_media_max_bytes"]
        raw = b"%PDF-1.7\n" + b"0" * (size - len(b"%PDF-1.7\n"))
        data = base64.b64encode(raw).decode("ascii")
        attachments = [{"name": name, "data": data} for name in ("first.pdf", "second.pdf")]
        mids = []
        for i in range(3):
            status, sent = self.gw.send(cid, caller, kind="chat", text="legacy request %d" % i,
                                       attachments=attachments)
            self.assertEqual(202, status, sent)
            mids.append(sent["message_id"])
        return bot, mids, size

    def check_legacy_commands(self, old_cli, mids, size):
        listed = old_cli("review", "list", "legacy-review")
        self.assertEqual(mids, [item["job_id"] for item in listed["inbound"]])
        self.assertNotIn("next", listed, "the original endpoint returns the entire metadata queue")
        for item in listed["inbound"]:
            self.assertNotIn("message", item)
            self.assertNotIn("display_name_untrusted", item)
            self.assertEqual({"count": 2, "bytes": 2 * size}, item["attachments_pending"])
        approved = old_cli("review", "approve-inbound", "legacy-review", mids[-1])
        self.assertEqual("approved", approved["inbound"])

    def test_legacy_commands_list_and_approve_beyond_first_full_body_page(self):
        bot, mids, size = self.large_requests()
        first_page = self.gw.ok("GET", "/v1/owner/bots/%s/reviews/full" % bot, None, self.gw.owner_token)
        self.assertEqual(mids[:2], [item["job_id"] for item in first_page["inbound"]])
        self.assertIsNotNone(first_page["next"])
        with patch.object(P, "cmd_review", base_cmd_review):
            self.check_legacy_commands(lambda *args: self.y("owner", *args), mids, size)
        self.assert_no_credentials_printed()

    def test_exact_base_client_lists_and_approves_beyond_first_full_body_page(self):
        # Also run the complete, unmodified base client and its base dependencies when Git history is present.
        legacy = self.root / "base-client"
        legacy.mkdir()
        for name in ("bot_client.py", "bot_common.py", "bot_e2e.py", "bot_mcp.py"):
            try:
                source = subprocess.run(["git", "show", self.base_commit + ":public_bots/" + name],
                                        cwd=PUBLIC.parent, capture_output=True, check=True).stdout
            except (FileNotFoundError, subprocess.CalledProcessError):
                self.skipTest("the public PR base commit is unavailable (for example, a source archive)")
            (legacy / name).write_bytes(source)
        _, mids, size = self.large_requests()

        def old_cli(*args):
            result = subprocess.run([sys.executable, str(legacy / "bot_client.py"),
                                     "--home", str(self.homes["owner"]), *args],
                                    env=plain_env(self.root), capture_output=True, text=True, timeout=30)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            return json.loads(result.stdout)

        self.check_legacy_commands(old_cli, mids, size)


class LegacySealedReviewTests(E2ECase):
    def test_decline_note_uses_legacy_sender_key_and_stays_encrypted(self):
        self.bot_with_host(files=False)
        self.y("caller", "connect", "%s/@%s" % (self.gw.url, self.handle), "--name", "Sealed caller")
        mid = self.y("caller", "chat", self.handle, "Synthetic sealed request")["message_id"]
        with legacy_review_api() as calls:
            result = self.y("owner", "review", "reject-inbound", self.handle, mid,
                            "--text", "Synthetic sealed decline")
            repeated = self.y("owner", "review", "reject-inbound", self.handle, mid,
                              "--text", "Synthetic sealed decline")
        self.assertEqual("declined", result["state"])
        self.assertEqual(dict(result, duplicate=True), repeated)
        declines = [c.args[3] for c in calls.call_args_list if c.args[1] == "POST"
                    and c.args[2] == "/v1/owner/messages/%s/review" % mid]
        self.assertEqual(2, len(declines))
        for body in declines:
            self.assertNotIn("text", body)
            self.assertIn("sealed", body)
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


class ReviewNegotiationFailureTests(unittest.TestCase):
    def test_only_missing_full_review_endpoint_can_fall_back(self):
        for status, code in ((401, "unauthorized"), (403, "forbidden"), (429, "rate_limited"),
                             (500, "internal_error"), (404, "response_too_large"), (None, "unreachable")):
            with self.subTest(status=status, code=code), redirect_stdout(io.StringIO()) as output, \
                    patch.object(P.C, "api_call", side_effect=P.C.ApiError(status, code)) as calls:
                with self.assertRaises(SystemExit) as raised:
                    P.owner_reviews("http://127.0.0.1", "synthetic-owner", "bot_" + "a" * 16)
                self.assertEqual(2, raised.exception.code)
                self.assertEqual(1, calls.call_count)
                self.assertIn(code, output.getvalue())


if __name__ == "__main__":
    unittest.main()
