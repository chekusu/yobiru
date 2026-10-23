"""Owner review actions resolve their message independently of the pending queue."""
from test_friends_client import FriendsClientCase, cli


class ReviewTargetTests(FriendsClientCase):
    def request(self):
        bot = self.owner_bot("alice")
        self.native("alice", bot)
        self.y("caller", "connect", self.gw.url + "/@alice", "--name", "Synthetic visitor")
        job = self.y("caller", "chat", "alice", "Synthetic request")["message_id"]
        return bot, job

    def test_accept_ignored_request_then_block_accepted_sender(self):
        bot, job = self.request()
        self.y("owner", "review", "ignore", "alice", job)
        self.assertEqual([], self.y("owner", "review", "list", "alice")["inbound"])
        accepted = self.y("owner", "review", "accept", "alice", job)
        self.assertTrue(accepted["inbound_preapproved"])
        self.y("host", "job", "claim", bot, job)
        self.y("host", "job", "read", bot, job)
        blocked = self.y("owner", "review", "block", "alice", job)
        self.assertEqual("revoked", blocked["status"])
        self.y("caller", "chat", "alice", "Blocked follow-up", expect=2)
        self.assert_no_credentials_printed()

    def test_block_ignored_request_without_another_message(self):
        _, job = self.request()
        self.y("owner", "review", "ignore", "alice", job)
        self.assertEqual("revoked", self.y("owner", "review", "block", "alice", job)["status"])
        self.y("caller", "chat", "alice", "Blocked follow-up", expect=2)

    def test_accept_block_and_ignore_check_selected_bot_before_changing_request(self):
        _, job = self.request()
        other = self.y("owner", "bot", "create", "--handle", "other", "--name", "Synthetic other bot")["bot_id"]
        for action in ("accept", "block", "ignore"):
            with self.subTest(action=action):
                code, _, error = cli(self.homes["owner"], "review", action, "other", job)
                self.assertEqual(2, code)
                self.assertIn(other, error)
        self.assertEqual([job], [x["job_id"] for x in self.y("owner", "review", "list", "alice")["inbound"]])
        self.y("caller", "chat", "alice", "Still allowed")

    def test_message_context_is_owner_only_and_contains_no_body(self):
        bot, job = self.request()
        path = "/v1/owner/messages/%s" % job
        context = self.gw.ok("GET", path, token=self.gw.owner_token)
        self.assertEqual((bot, job, "agent", "pending"),
                         (context["bot_id"], context["message_id"], context["route"], context["review"]["inbound"]))
        self.assertTrue(context["request_hash"])
        self.assertNotIn("content", context)
        self.assertNotIn("draft", context)
        _, stranger_owner, stranger_source = self.gw.enroll_bot("stranger")
        self.assertEqual(404, self.gw.call("GET", path, token=stranger_owner)[0])
        self.assertEqual(401, self.gw.call("GET", path, token=stranger_source)[0])
