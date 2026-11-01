"""An existing owner adds a bot from a new machine by email (join), with a credential for that one bot only.

A self-enrolled owner's address names one owner, so a second agent of the same person (Grok Bot, then Muse) cannot
enroll its own owner with it. Join: the new machine proves the address with the same email challenge (or a partner
attestation) and gets one new bot of that owner with a client-generated manager credential (ybm_) that administers
that bot only: contacts, reviews, wakes, bindings, its profile. It never sees the owner credential or the owner's
other bots, cannot create bots or manage manager credentials. The owner sees how the bot came, can revoke the
manager (and by default everything of that bot, which retires it from the quota), and is told by email.

Real bot_gateway.py subprocesses with synthetic homes on loopback; the relay runs in-process with a fake sender.
"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from support.public_harness import C, FriendsGateway, cli

import bot_gateway as G  # noqa: E402  (on sys.path through the harness)

LIMITS = {"min_deadline_seconds": 1, "read_timeout_seconds": 5, "enroll_per_hour_per_ip": 1000,
          "enroll_per_hour": 1000, "bot_enroll_per_hour": 1000, "bot_enroll_per_hour_per_ip": 1000,
          "email_codes_per_hour_per_ip": 1000, "email_codes_per_hour_per_address": 1000,
          "new_contacts_per_hour_per_bot": 1000, "new_conversations_per_hour_per_bot": 1000,
          "bots_joined_per_day_per_owner": 1000, "hosted_wake_private_hosts": 1}
SECRET = "pinku-test-secret-0123456789abcdef0123456789"
MAIL = {"host": "smtp.invalid", "port": 465, "security": "ssl", "username": None, "password": None,
        "from": "Yobiru <no-reply@yobiru.test>", "reply_to": None}


def gateway(root, limits=LIMITS, **enrollment):
    gw = FriendsGateway(Path(root), limits=limits)
    secret = gw.home / "pinku.secret"
    secret.write_text(SECRET + "\n")
    os.chmod(str(secret), 0o600)
    cfg = json.loads((gw.home / "gateway.json").read_text())
    cfg["enrollment"] = dict({"visitors": True, "bots": True, "self_enrolled_publish": True,
                              "email_verification": True}, **enrollment)
    cfg["partners"] = {"pinku": {"secret_file": "pinku.secret"}}
    (gw.home / "gateway.json").write_text(json.dumps(cfg))
    os.chmod(str(gw.home / "gateway.json"), 0o600)
    return gw.start()


class JoinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="yobiru join ")
        cls.gw = gateway(cls._tmp.name)
        cls.n = 0

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls._tmp.cleanup()

    # ---- helpers
    def address(self):
        JoinTests.n += 1
        return "person.%d+agents@example.com" % self.n

    def challenge(self, email, lang="zh"):
        obj = self.gw.ok("POST", "/v1/enroll/email", {"email": email, "lang": lang})
        code = self.gw.sql("SELECT code FROM mail_outbox WHERE challenge_id=?", (obj["challenge_id"],))[0]["code"]
        return {"id": obj["challenge_id"], "code": code}

    def owner(self, email):
        """A self-enrolled owner with its first bot. -> (owner token, bot id)."""
        owner, source = C.new_token("owner"), C.new_token("source")
        JoinTests.n += 1
        status, obj = self.gw.call("POST", "/v1/enroll", {"display_name": "First %d" % self.n,
                                                          "handle": "jo-first-%d" % self.n,
                                                          "source_credential_sha256": C.token_hash(source),
                                                          "email_challenge": self.challenge(email)}, owner)
        self.assertEqual(201, status, obj)
        return owner, obj["bot_id"]

    def join(self, challenge=None, manager=None, source=None, **extra):
        manager, source = manager or C.new_token("manager"), source or C.new_token("source")
        JoinTests.n += 1
        body = dict({"display_name": "Muse %d" % self.n, "source_credential_sha256": C.token_hash(source)}, **extra)
        if challenge is not None:
            body["email_challenge"] = challenge
        status, obj = self.gw.call("POST", "/v1/enroll/join", body, manager)
        return status, obj, manager, source

    def joined(self, email):
        """An owner plus a bot joined from a new machine. -> (owner, first bot, manager, joined bot)."""
        owner, first = self.owner(email)
        status, obj, manager, _ = self.join(self.challenge(email))
        self.assertEqual(201, status, obj)
        return owner, first, manager, obj["bot_id"]

    def notices(self):
        return self.gw.sql("SELECT * FROM mail_notices ORDER BY created_ts, rowid")

    # ---- enrollment
    def test_the_gateway_advertises_join(self):
        enrollment = self.gw.ok("GET", "/.well-known/yobiru")["enrollment"]
        self.assertEqual("POST /v1/enroll/join", enrollment["join_by_email"])

    def test_an_address_in_use_joins_with_the_same_code(self):
        email = self.address()
        owner, first = self.owner(email)
        challenge = self.challenge(email.replace("person.", "PERSON."))        # the same mailbox, spelled otherwise
        status, obj = self.gw.call("POST", "/v1/enroll", {"display_name": "Muse", "source_credential_sha256":
                                                          C.token_hash(C.new_token("source")),
                                                          "email_challenge": challenge}, C.new_token("owner"))
        self.assertEqual((409, "email_in_use", "POST /v1/enroll/join"), (status, obj["error"], obj["join"]))
        status, obj, manager, source = self.join(challenge)
        self.assertEqual(201, status, obj)
        mine = self.gw.ok("GET", "/v1/owner", None, owner)
        self.assertEqual(mine["owner_id"], obj["owner_id"])
        self.assertEqual((True, "manager", False), (obj["joined_existing_owner"], obj["credential"], obj["duplicate"]))
        text = json.dumps(obj)
        self.assertNotIn("ybo_", text, "the owner credential never goes to the new machine")
        self.assertNotIn(first, text, "nor anything about the owner's other bots")
        status, again, _, _ = self.join(challenge)
        self.assertEqual((400, "email_code_expired"), (status, again["error"]), "the code works once")
        status, retry, _, _ = self.join(challenge, manager=manager, source=source)
        self.assertEqual((200, True, obj["bot_id"]), (status, retry["duplicate"], retry["bot_id"]))
        status, conflict, _, _ = self.join(challenge, manager=manager)
        self.assertEqual((409, "enrollment_conflict"), (status, conflict["error"]))
        me = self.gw.ok("GET", "/v1/actor", None, source)
        self.assertEqual(obj["bot_id"], me["principal_id"], "the source credential speaks as the new bot")

    def test_an_unused_address_cannot_join_and_keeps_its_code(self):
        email = self.address()
        challenge = self.challenge(email)
        status, obj, _, _ = self.join(challenge)
        self.assertEqual((404, "no_owner_for_email"), (status, obj["error"]))
        status, obj = self.gw.call("POST", "/v1/enroll", {"display_name": "New", "source_credential_sha256":
                                                          C.token_hash(C.new_token("source")),
                                                          "email_challenge": challenge}, C.new_token("owner"))
        self.assertEqual(201, status, "the refused join left the code usable")

    def test_wrong_codes_count_and_spend_the_challenge(self):
        email = self.address()
        self.owner(email)
        challenge = self.challenge(email)
        wrong = dict(challenge, code="%06d" % ((int(challenge["code"]) + 3) % 10 ** 6))
        for left in (4, 3, 2, 1, 0):
            status, obj, _, _ = self.join(wrong)
            self.assertEqual((400, "email_code_invalid", left), (status, obj["error"], obj["attempts_left"]))
        status, obj, _, _ = self.join(challenge)
        self.assertEqual((400, "email_code_expired"), (status, obj["error"]))

    def test_a_given_address_must_be_the_one_the_code_went_to(self):
        email = self.address()
        self.owner(email)
        status, obj, _, _ = self.join(self.challenge(email), email="someone.else@example.com")
        self.assertEqual((400, "email_mismatch"), (status, obj["error"]))

    def test_the_bearer_is_a_manager_credential(self):
        email = self.address()
        self.owner(email)
        status, obj = self.gw.call("POST", "/v1/enroll/join", {"display_name": "x", "source_credential_sha256":
                                                               C.token_hash(C.new_token("source")),
                                                               "email_challenge": self.challenge(email)},
                                   C.new_token("owner"))
        self.assertEqual(401, status, obj)

    def test_a_partner_attestation_joins(self):
        email = self.address()
        self.owner(email)
        manager = C.new_token("manager")
        exp = int(time.time()) + 600
        att = {"partner": "pinku", "email": email, "exp": exp,
               "sig": G.partner_signature(SECRET, self.gw.url, "pinku", email, C.token_hash(manager), exp)}
        status, obj, _, _ = self.join(email_attestation=dict(att, sig="0" * 64))
        self.assertEqual((400, "invalid_attestation"), (status, obj["error"]), "bound to the manager credential")
        status, obj, _, _ = self.join(manager=manager, email_attestation=att)
        self.assertEqual(201, status, obj)
        self.assertEqual(email, self.notices()[-1]["address"])

    # ---- scope of the manager credential
    def test_a_manager_administers_its_bot_only(self):
        owner, first, manager, bot = self.joined(self.address())
        mine = self.gw.ok("GET", "/v1/owner", None, manager)
        self.assertEqual({"bot_id": bot, "credential": "manager"}, mine["scope"])
        self.assertEqual([bot], [b["bot_id"] for b in self.gw.ok("GET", "/v1/owner/bots", None, manager)["bots"]])
        directory = self.gw.ok("GET", "/v1/owner/directory", None, manager)
        self.assertEqual(([bot], []), ([b["bot_id"] for b in directory["bots"]], directory["reserved_handles"]))
        status, obj = self.gw.call("POST", "/v1/owner/bots", {"handle": "jo-more-%d" % self.n,
                                                              "display_name": "More"}, manager)
        self.assertEqual((403, "bot_scoped"), (status, obj["error"]))
        for method, path, body in (("GET", "/v1/owner/bots/%s/managers" % bot, None),
                                   ("POST", "/v1/owner/bots/%s/managers" % bot, {}),
                                   ("POST", "/v1/owner/bots/%s/managers/%s/revoke" % (bot, "0" * 16), {})):
            status, obj = self.gw.call(method, path, body, manager)
            self.assertEqual((403, "bot_scoped"), (status, obj["error"]), path)
        # Everything the Mokutori plugin does with it, on its own bot.
        self.gw.ok("PATCH", "/v1/owner/bots/%s" % bot, {"display_name": "Muse renamed"}, manager)
        self.gw.ok("POST", "/v1/owner/bots/%s/publish" % bot, {}, manager)
        wtoken, _ = self.gw.native(bot, owner=manager)
        self.gw.ok("PUT", "/v1/owner/bots/%s/wake" % bot, {"url": "http://127.0.0.1:9/wake", "secret": "s" * 16,
                                                           "test": False}, manager)
        self.gw.ok("GET", "/v1/owner/bots/%s/wake" % bot, None, manager)
        self.gw.ok("DELETE", "/v1/owner/bots/%s/wake" % bot, None, manager)
        source = self.gw.source_for(bot, owner=manager)
        actor, atoken = self.gw.actor()
        cid = self.gw.conversation(bot, atoken)
        mid = self.gw.chat(cid, atoken, "pair me")
        self.assertEqual(200, self.gw.admit(bot, mid, owner=manager)[0])
        status, obj = self.gw.contact(bot, actor, "grant", ["chat", "files"], owner=manager)
        self.assertEqual(200, status, obj)
        self.gw.ok("GET", "/v1/owner/messages/%s" % mid, None, manager)
        self.gw.ok("GET", "/v1/owner/conversations/%s" % cid, None, manager)
        decisions = self.gw.ok("GET", "/v1/owner/bots/%s/decisions" % bot, None, manager)["decisions"]
        cred = C.token_hash(manager)[:16]
        self.assertTrue(decisions and all(d["actor"]["credential_id"] == cred for d in decisions),
                        "the audit record names the manager credential")
        verified = self.gw.ok("POST", "/v1/decisions/verify", {"decision_id": decisions[0]["decision_id"]}, manager)
        self.assertEqual(bot, verified["decision"]["bot_id"])
        self.assertEqual(200, self.gw.call("GET", "/v1/actor", None, source)[0])
        self.assertTrue(self.gw.ok("GET", "/v1/worker", None, wtoken)["bot_id"] == bot)
        # The owner's other bot is out of reach: the same 404 as a bot that does not exist.
        for method, path, body in (
                ("GET", "/v1/owner/bots/%s" % first, None),
                ("PATCH", "/v1/owner/bots/%s" % first, {"display_name": "taken"}),
                ("POST", "/v1/owner/bots/%s/publish" % first, {}),
                ("POST", "/v1/owner/bots/%s/unpublish" % first, {}),
                ("GET", "/v1/owner/bots/%s/contacts" % first, None),
                ("POST", "/v1/owner/bots/%s/contacts/%s" % (first, actor), {"action": "block"}),
                ("GET", "/v1/owner/bots/%s/reviews/full" % first, None),
                ("GET", "/v1/owner/bots/%s/wake" % first, None),
                ("PUT", "/v1/owner/bots/%s/wake" % first, {"url": "http://127.0.0.1:9/w", "secret": "s" * 16}),
                ("POST", "/v1/owner/bots/%s/workers" % first, {"adapter": "native", "label": "steal"}),
                ("GET", "/v1/owner/bots/%s/workers" % first, None),
                ("POST", "/v1/owner/bots/%s/sources" % first, {}),
                ("GET", "/v1/owner/bots/%s/sources" % first, None),
                ("GET", "/v1/owner/bots/%s/decisions" % first, None),
                ("GET", "/v1/owner/bots/%s/conversations" % first, None),
                ("POST", "/v1/owner/bots/%s/verifiers" % first, {"label": "v"})):
            status, obj = self.gw.call(method, path, body, manager)
            self.assertEqual((404, "not_found"), (status, obj.get("error")), (method, path))
        # The owner's own credential still reaches both.
        self.assertEqual({first, bot}, {b["bot_id"] for b in self.gw.ok("GET", "/v1/owner/bots", None,
                                                                         owner)["bots"]})

    def test_a_manager_cannot_reach_another_bots_conversations_or_messages(self):
        owner, first, manager, bot = self.joined(self.address())
        self.gw.ok("POST", "/v1/owner/bots/%s/publish" % first, {}, owner)
        _, atoken = self.gw.actor()
        cid = self.gw.conversation(first, atoken)
        mid = self.gw.chat(cid, atoken, "hello first")
        for method, path, body in (("GET", "/v1/owner/conversations/%s" % cid, None),
                                   ("GET", "/v1/owner/messages/%s" % mid, None),
                                   ("POST", "/v1/owner/messages/%s/review" % mid, {"action": "reject_inbound"}),
                                   ("POST", "/v1/owner/conversations/%s/messages" % cid, {"text": "x",
                                                                                          "op_key": "op-12345678"}),
                                   ("DELETE", "/v1/owner/conversations/%s" % cid, None)):
            status, obj = self.gw.call(method, path, body, manager)
            self.assertEqual(404, status, (method, path, obj))
        self.assertEqual(200, self.gw.admit(first, mid, owner=owner)[0])
        decision = self.gw.ok("GET", "/v1/owner/bots/%s/decisions" % first, None, owner)["decisions"][0]
        for method, path, body in (("GET", "/v1/owner/decisions/%s" % decision["decision_id"], None),
                                   ("POST", "/v1/decisions/verify", {"decision_id": decision["decision_id"]})):
            status, obj = self.gw.call(method, path, body, manager)
            self.assertEqual(404, status, (path, obj))

    # ---- the owner's view and revocation
    def test_the_owner_sees_and_revokes_a_joined_bot(self):
        owner, first, manager, bot = self.joined(self.address())
        view = self.gw.ok("GET", "/v1/owner/bots/%s" % bot, None, owner)
        cred = C.token_hash(manager)[:16]
        self.assertEqual(("email_join", [cred]), (view["added_via"], [m["credential_id"] for m in view["managers"]]))
        self.assertIsNone(self.gw.ok("GET", "/v1/owner/bots/%s" % first, None, owner)["added_via"])
        self.gw.ok("POST", "/v1/owner/bots/%s/publish" % bot, {}, manager)
        wtoken, _ = self.gw.native(bot, owner=manager)
        source = self.gw.source_for(bot, owner=manager)
        listed = self.gw.ok("GET", "/v1/owner/bots/%s/managers" % bot, None, owner)["managers"]
        self.assertEqual([cred], [m["credential_id"] for m in listed])
        out = self.gw.ok("POST", "/v1/owner/bots/%s/managers/%s/revoke" % (bot, cred), {}, owner)
        self.assertEqual((True, "unpublished"), (out["everything"], out["bot"]["state"]))
        for token, path in ((manager, "/v1/owner"), (source, "/v1/actor"), (wtoken, "/v1/worker")):
            self.assertEqual(401, self.gw.call("GET", path, None, token)[0], path)
        again = self.gw.ok("POST", "/v1/owner/bots/%s/managers/%s/revoke" % (bot, cred), {}, owner)
        self.assertTrue(again["revoked"], "revoking is idempotent")
        self.assertEqual(200, self.gw.call("GET", "/v1/owner/bots/%s" % first, None, owner)[0])

    def test_revoking_everything_forgets_the_hosted_wake(self):
        owner, _, manager, bot = self.joined(self.address())
        self.gw.ok("PUT", "/v1/owner/bots/%s/wake" % bot, {"url": "http://127.0.0.1:9/wake", "secret": "s" * 16,
                                                           "test": False}, manager)
        cred = C.token_hash(manager)[:16]
        self.gw.ok("POST", "/v1/owner/bots/%s/managers/%s/revoke" % (bot, cred), {}, owner)
        self.assertEqual([], self.gw.sql("SELECT bot_id FROM hosted_wakes WHERE bot_id=?", (bot,)),
                         "the wake URL and secret set from that machine are gone")
        self.assertFalse(self.gw.ok("GET", "/v1/owner/bots/%s/wake" % bot, None, owner)["configured"])

    def test_a_manager_never_takes_a_handle_the_owner_kept(self):
        email = self.address()
        owner, first, manager, bot = self.joined(email)
        old = self.gw.ok("GET", "/v1/owner/bots/%s" % first, None, owner)["handle"].lstrip("@")
        self.gw.ok("PATCH", "/v1/owner/bots/%s" % first, {"handle": old + "-new"}, owner)   # the old one is kept
        status, obj, _, _ = self.join(self.challenge(email), handle=old)
        self.assertEqual((409, "handle_unavailable"), (status, obj["error"]), "join cannot take it")
        status, obj = self.gw.call("PATCH", "/v1/owner/bots/%s" % bot, {"handle": old}, manager)
        self.assertEqual((409, "handle_unavailable"), (status, obj["error"]), "nor can a rename by the manager")
        mine = self.gw.ok("GET", "/v1/owner/bots/%s" % bot, None, manager)["handle"].lstrip("@")
        self.gw.ok("PATCH", "/v1/owner/bots/%s" % bot, {"handle": mine, "display_name": "Same handle"}, manager)
        self.gw.ok("PATCH", "/v1/owner/bots/%s" % bot, {"handle": "jo-fresh-%d" % self.n}, manager)
        self.gw.ok("PATCH", "/v1/owner/bots/%s" % first, {"handle": old}, owner)    # the owner still can

    def test_revoking_only_the_manager_keeps_the_rest(self):
        owner, _, manager, bot = self.joined(self.address())
        source = self.gw.source_for(bot, owner=manager)
        cred = C.token_hash(manager)[:16]
        out = self.gw.ok("POST", "/v1/owner/bots/%s/managers/%s/revoke" % (bot, cred), {"everything": False}, owner)
        self.assertFalse(out["everything"])
        self.assertEqual(401, self.gw.call("GET", "/v1/owner", None, manager)[0])
        self.assertEqual(200, self.gw.call("GET", "/v1/actor", None, source)[0])

    def test_the_owner_issues_a_manager_for_an_existing_bot(self):
        owner, first = self.owner(self.address())
        manager = C.new_token("manager")
        obj = self.gw.ok("POST", "/v1/owner/bots/%s/managers" % first, {"credential_sha256": C.token_hash(manager),
                                                                        "label": "grok"}, owner)
        self.assertEqual((first, False), (obj["bot_id"], obj["duplicate"]))
        self.assertNotIn("token", obj, "the client kept the secret")
        self.assertEqual({"bot_id": first, "credential": "manager"},
                         self.gw.ok("GET", "/v1/owner", None, manager)["scope"])
        self.assertIsNone(self.gw.ok("GET", "/v1/owner/bots/%s" % first, None, owner)["added_via"])

    # ---- quota and rate limits
    def test_the_quota_counts_live_bots_and_retiring_frees_a_place(self):
        with tempfile.TemporaryDirectory(prefix="yobiru join quota ") as tmp:
            gw = gateway(tmp, limits=dict(LIMITS, bots_per_self_enrolled_owner=2))
            try:
                JoinTests.gw, saved = gw, JoinTests.gw
                try:
                    email = self.address()
                    owner, first, manager, bot = self.joined(email)
                    status, obj, _, _ = self.join(self.challenge(email))
                    self.assertEqual((409, "too_many_bots"), (status, obj["error"]))
                    status, obj = gw.call("POST", "/v1/owner/bots", {"handle": "jo-q-%d" % self.n,
                                                                     "display_name": "q"}, owner)
                    self.assertEqual((409, "too_many_bots"), (status, obj["error"]))
                    gw.ok("POST", "/v1/owner/bots/%s/managers/%s/revoke" % (bot, C.token_hash(manager)[:16]), {},
                          owner)
                    status, obj, _, _ = self.join(self.challenge(email))
                    self.assertEqual(201, status, "a retired bot no longer counts")
                finally:
                    JoinTests.gw = saved
            finally:
                gw.stop()

    def test_an_owner_adds_a_few_bots_a_day(self):
        with tempfile.TemporaryDirectory(prefix="yobiru join rate ") as tmp:
            gw = gateway(tmp, limits=dict(LIMITS, bots_joined_per_day_per_owner=1))
            try:
                JoinTests.gw, saved = gw, JoinTests.gw
                try:
                    email = self.address()
                    self.joined(email)
                    challenge = self.challenge(email)
                    status, obj, _, _ = self.join(challenge)
                    self.assertEqual((429, "rate_limited"), (status, obj["error"]))
                    self.assertIsNone(gw.sql("SELECT used_ts FROM email_challenges WHERE id=?",
                                             (challenge["id"],))[0]["used_ts"], "a refusal never spends the code")
                finally:
                    JoinTests.gw = saved
            finally:
                gw.stop()

    def test_a_partner_only_gateway_advertises_and_opens_join(self):
        with tempfile.TemporaryDirectory(prefix="yobiru join partner ") as tmp:
            gw = gateway(tmp, email_verification=False)
            try:
                self.assertEqual("POST /v1/enroll/join", gw.ok("GET", "/.well-known/yobiru")["enrollment"]
                                 ["join_by_email"])
                email, exp = "partner.only@example.com", int(time.time()) + 600

                def attestation(token):
                    return {"partner": "pinku", "email": email, "exp": exp,
                            "sig": G.partner_signature(SECRET, gw.url, "pinku", email, C.token_hash(token), exp)}
                owner = C.new_token("owner")
                gw.ok("POST", "/v1/enroll", {"display_name": "First", "source_credential_sha256":
                                             C.token_hash(C.new_token("source")),
                                             "email_attestation": attestation(owner)}, owner)
                manager = C.new_token("manager")
                status, obj = gw.call("POST", "/v1/enroll/join", {"display_name": "Second", "source_credential_sha256":
                                                                  C.token_hash(C.new_token("source")),
                                                                  "email_attestation": attestation(manager)}, manager)
                self.assertEqual(201, status, obj)
            finally:
                gw.stop()

    def test_join_can_be_turned_off(self):
        with tempfile.TemporaryDirectory(prefix="yobiru join off ") as tmp:
            gw = gateway(tmp, join_by_email=False)
            try:
                self.assertIsNone(gw.ok("GET", "/.well-known/yobiru")["enrollment"]["join_by_email"])
                status, obj = gw.call("POST", "/v1/enroll/join", {"display_name": "x", "source_credential_sha256":
                                                                  C.token_hash(C.new_token("source"))},
                                      C.new_token("manager"))
                self.assertEqual((404, "join_off"), (status, obj["error"]))
            finally:
                gw.stop()

    # ---- the owner is told by email
    def test_the_owner_is_told_and_the_address_is_forgotten(self):
        email = self.address()
        _, _, manager, bot = self.joined(email)
        notice = [n for n in self.notices() if json.loads(n["detail"])["bot_id"] == bot][0]
        self.assertEqual(("bot_added", email, "zh"), (notice["kind"], notice["address"], notice["lang"]))
        self.assertEqual([], self.gw.sql("SELECT * FROM challenge_addresses WHERE challenge_id IN (SELECT id FROM "
                                         "email_challenges WHERE used_ts IS NOT NULL)"),
                         "a used challenge keeps no address")
        stored = json.dumps(self.gw.sql("SELECT * FROM challenge_addresses"))
        self.assertNotIn("example.com", stored, "open challenges keep the address sealed only")
        store = G.Store(self.gw.home, G.load_config(self.gw.home))
        sent = []

        def fake(mail_cfg, messages):
            sent.extend(messages)
            return [None] * len(messages)
        while G.relay_once(store, MAIL, send=fake)["sent"]:    # the other tests' codes queue up here too
            pass
        mine = [m for m in sent if m["To"] == email and "新增" in m["Subject"]]
        self.assertEqual(1, len(mine))
        handle = self.gw.ok("GET", "/v1/owner/bots/%s" % bot, None, manager)["handle"]
        cred = C.token_hash(manager)[:16]
        for part in ("plain", "html"):
            content = mine[0].get_body((part,)).get_content()
            self.assertIn(handle, content)
            self.assertIn("revoke-manager %s %s" % (bot, cred), content)
        self.assertIn('src="cid:%s"' % G.MAIL_LOGO_CID, mine[0].get_body(("html",)).get_content())
        logo = [p for p in mine[0].walk() if p.get_content_type() == "image/png"]
        self.assertEqual([G.MAIL_LOGO_PNG], [p.get_content() for p in logo], "the logo travels inside the notice")
        row = self.gw.sql("SELECT * FROM mail_notices WHERE id=?", (notice["id"],))[0]
        self.assertEqual(None, row["address"])
        self.assertIsNotNone(row["sent_ts"])
        sent.clear()
        while G.relay_once(store, MAIL, send=fake)["sent"]:
            pass
        self.assertEqual([], [m for m in sent if m["To"] == email], "sent once")

    def test_a_notice_that_cannot_be_built_never_holds_back_codes(self):
        with tempfile.TemporaryDirectory(prefix="yobiru join relay ") as tmp:
            gw = gateway(tmp)
            try:
                now = int(time.time())
                gw.sql("INSERT INTO mail_notices(id, kind, address, lang, detail, created_ts, expires_ts) "
                       "VALUES(?,?,?,?,?,?,?)", ("eml_" + "b" * 16, "bot_added", "owner@example.com", "en", "{}", now,
                                                 now + 3600))
                gw.ok("POST", "/v1/enroll/email", {"email": "new.person@example.com", "lang": "en"})
                store, sent = G.Store(gw.home, G.load_config(gw.home)), []

                def fake(mail_cfg, messages):
                    sent.extend(messages)
                    return [None] * len(messages)
                self.assertEqual({"sent": 1, "failed": 1}, G.relay_once(store, MAIL, send=fake))
                self.assertEqual(["new.person@example.com"], [m["To"] for m in sent], "the code still went out")
                row = gw.sql("SELECT * FROM mail_notices")[0]
                self.assertEqual((None, "render: KeyError"), (row["sent_ts"], row["error"]))
            finally:
                gw.stop()

    def test_every_language_renders(self):
        for lang in G.MAIL_LANGS:
            row = {"kind": "bot_added", "lang": lang, "created_ts": 1767225600,
                   "detail": json.dumps({"bot_id": "bot_" + "a" * 16, "handle": "muse-scan", "display_name": "Muse",
                                         "credential_id": "0123456789abcdef"})}
            subject, text, page = G.notice_parts(row, "https://bots.yobiru.com")
            self.assertIn("@muse-scan", subject)
            self.assertIn("bots.yobiru.com", text)
            self.assertIn("0123456789abcdef", page)

    # ---- the client
    def test_the_client_joins_automatically_and_keeps_a_bot_credential(self):
        email = self.address()
        owner, first = self.owner(email)
        home = Path(self._tmp.name) / ("client-%d" % self.n)
        code, out, err = cli(home, "enroll-bot", self.gw.url, "--name", "Muse scan", "--email", email)
        self.assertEqual(0, code, (out, err))
        saved = (home / "public" / "credentials.json").read_text()
        self.assertNotIn("example.com", saved, "the client never stores the address")
        cid = json.loads(saved)["gateways"][self.gw.url]["email_challenge"]["id"]
        mail_code = self.gw.sql("SELECT code FROM mail_outbox WHERE challenge_id=?", (cid,))[0]["code"]
        code, out, err = cli(home, "enroll-bot", self.gw.url, "--name", "Muse scan", "--email-code", mail_code)
        self.assertEqual(0, code, (out, err))
        self.assertEqual((True, "manager"), (out["joined_existing_owner"], out["credential"]))
        bot = out["bot_id"]
        entry = json.loads((home / "public" / "credentials.json").read_text())["gateways"][self.gw.url]
        self.assertTrue(entry["owner"]["token"].startswith("ybm_"))
        self.assertEqual(bot, entry["owner"]["scope_bot_id"])
        self.assertNotIn("pending_join", entry)
        self.assertNotIn("pending_bot", entry)
        code, out, err = cli(home, "owner", "bots")
        self.assertEqual((0, [bot]), (code, [b["bot_id"] for b in out["bots"]]), err)
        code, out, err = cli(home, "bot", "edit", bot, "--name", "Muse renamed")
        self.assertEqual(0, code, (out, err))
        code, out, err = cli(home, "bot", "create", "--handle", "jo-cli-more", "--name", "More")
        self.assertNotEqual(0, code, "a bot credential creates no bots")
        # The owner, on its own device, sees the bot and revokes it.
        owner_home = Path(self._tmp.name) / ("owner-%d" % self.n)
        token_file = owner_home / "owner.token"
        owner_home.mkdir(parents=True)
        token_file.write_text(owner + "\n")
        os.chmod(str(token_file), 0o600)
        self.assertEqual(0, cli(owner_home, "owner", "login", self.gw.url, "--token-file", str(token_file))[0])
        code, out, err = cli(owner_home, "owner", "managers", bot)
        self.assertEqual(0, code, err)
        cred = out["managers"][0]["credential_id"]
        code, out, err = cli(owner_home, "owner", "revoke-manager", bot, cred)
        self.assertEqual((0, True), (code, out["everything"]), err)
        self.assertNotEqual(0, cli(home, "owner", "bots")[0], "the new machine lost its access")

    def test_the_owner_hands_a_machine_a_bot_credential(self):
        email = self.address()
        owner, first = self.owner(email)
        owner_home = Path(self._tmp.name) / ("owner-issue-%d" % self.n)
        owner_home.mkdir(parents=True)
        token_file = owner_home / "owner.token"
        token_file.write_text(owner + "\n")
        os.chmod(str(token_file), 0o600)
        self.assertEqual(0, cli(owner_home, "owner", "login", self.gw.url, "--token-file", str(token_file))[0])
        manager_file = owner_home / "grok.token"
        code, out, err = cli(owner_home, "owner", "issue-manager", first, "--token-file", str(manager_file),
                             "--label", "grok")
        self.assertEqual(0, code, (out, err))
        self.assertTrue(manager_file.read_text().startswith("ybm_"))
        machine = Path(self._tmp.name) / ("machine-%d" % self.n)
        code, out, err = cli(machine, "owner", "login", self.gw.url, "--token-file", str(manager_file))
        self.assertEqual(0, code, (out, err))
        code, out, err = cli(machine, "owner", "bots")
        self.assertEqual((0, [first]), (code, [b["bot_id"] for b in out["bots"]]), err)


if __name__ == "__main__":
    unittest.main()
