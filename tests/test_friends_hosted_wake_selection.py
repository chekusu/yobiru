"""Hosted wakes must reach admitted work behind a full page of message requests.

Real gateway and loopback receiver, using only synthetic messages and identities.
"""
import tempfile
import unittest
from pathlib import Path

from support.public_harness import FriendsGateway
from test_friends_hosted_wake import LIMITS, SECRET, Receiver


class HostedWakeSelectionTests(unittest.TestCase):
    def test_ready_work_is_not_hidden_by_200_older_review_requests(self):
        directory = tempfile.TemporaryDirectory(prefix="yobiru hosted selection ")
        self.addCleanup(directory.cleanup)
        gw = FriendsGateway(Path(directory.name), limits=LIMITS).start()
        self.addCleanup(gw.stop)
        receiver = Receiver()
        self.addCleanup(receiver.stop)
        bot = gw.bot("selection")
        gw.policy(bot, offered=["chat"], visitor=["chat"])

        # Review requests and admitted work have separate pending-per-bot pools.
        # Fill the review pool first, with no hosted route registered yet.
        for _ in range(20):
            _, caller = gw.actor()
            conversation = gw.conversation(bot, caller)
            for _ in range(10):
                status, sent = gw.send(conversation, caller, kind="chat", text="Synthetic review request")
                self.assertEqual(202, status, sent)

        actor, caller = gw.actor()
        self.assertEqual(200, gw.contact(bot, actor, "trust")[0])
        conversation = gw.conversation(bot, caller)
        status, ready = gw.send(conversation, caller, kind="chat", text="Synthetic admitted work")
        self.assertEqual(202, status, ready)

        _, worker = gw.native(bot)
        connector = gw.ok("POST", "/v1/owner/bots/%s/workers/%s/connector" % (bot, worker), {},
                          gw.owner_token)["token"]
        listed = gw.ok("GET", "/v1/connector/jobs", None, connector)["jobs"]
        self.assertEqual(200, len(listed))
        self.assertEqual({"review"}, {job["phase"] for job in listed})

        status, registered = gw.call("PUT", "/v1/owner/bots/%s/wake" % bot,
                                     {"url": receiver.url, "secret": SECRET, "wake_on_review": False},
                                     gw.owner_token)
        self.assertEqual(200, status, registered)
        requests = receiver.wait(1)
        self.assertEqual(1, len(requests), "review requests must not consume the admitted wake limit")
        body = requests[0]["body"]
        self.assertEqual((ready["message_id"], "ready", 1), (body["job_id"], body["phase"], body["jobs"]))
        states = gw.sql("SELECT wake_state, COUNT(*) AS n FROM messages WHERE bot_id=? GROUP BY wake_state",
                        (bot,))
        self.assertEqual(200, next(row["n"] for row in states if row["wake_state"] == "none"))


if __name__ == "__main__":
    unittest.main()
