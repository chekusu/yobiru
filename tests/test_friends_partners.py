"""Partner email attestations and follow permits.

A partner the operator configured (gateway.json `partners`, a shared secret file) vouches that one of its users
controls an email address, so that user enrolls without a second code: the attestation names this gateway, the
address, the new owner credential's hash and an expiry at most 15 minutes ahead, and one address still names one
owner. A follow permit lets one named bot follow another that does not accept followers, so two people a partner
introduced become friends without publishing their bots to strangers.

Real bot_gateway.py subprocesses with synthetic homes on loopback.
"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from support.public_harness import C, FriendsGateway, cli

import bot_gateway as G  # noqa: E402  (on sys.path through the harness)

LIMITS = {"enroll_per_hour_per_ip": 1000, "enroll_per_hour": 1000, "bot_enroll_per_hour": 1000,
          "bot_enroll_per_hour_per_ip": 1000, "email_codes_per_hour_per_ip": 1000,
          "email_codes_per_hour_per_address": 1000}
SECRET = "pinku-test-secret-0123456789abcdef0123456789"


class PartnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="yobiru partners ")
        cls.gw = FriendsGateway(Path(cls._tmp.name), limits=LIMITS)
        secret = cls.gw.home / "pinku.secret"
        secret.write_text(SECRET + "\n")
        os.chmod(str(secret), 0o600)
        cfg = json.loads((cls.gw.home / "gateway.json").read_text())
        cfg["enrollment"] = {"visitors": True, "bots": True, "self_enrolled_publish": True, "email_verification": True}
        cfg["partners"] = {"pinku": {"secret_file": "pinku.secret"}}
        (cls.gw.home / "gateway.json").write_text(json.dumps(cfg))
        os.chmod(str(cls.gw.home / "gateway.json"), 0o600)
        cls.gw.start()
        cls.n = 0

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls._tmp.cleanup()

    def attest(self, email, owner, partner="pinku", secret=SECRET, exp=None, audience=None):
        exp = exp or int(time.time()) + 600
        return {"partner": partner, "email": email, "exp": exp,
                "sig": G.partner_signature(secret, audience or self.gw.url, partner, email, C.token_hash(owner), exp)}

    def enroll(self, attestation_for=None, **attest):
        PartnerTests.n += 1
        owner, source = C.new_token("owner"), C.new_token("source")
        body = {"display_name": "Synthetic %d" % self.n, "handle": "pt-%d" % self.n,
                "source_credential_sha256": C.token_hash(source)}
        if attestation_for is not None:
            body["email_attestation"] = self.attest(attestation_for, owner, **attest)
        status, obj = self.gw.call("POST", "/v1/enroll", body, owner)
        return status, obj, owner, source

    def test_the_gateway_advertises_its_partners(self):
        enrollment = self.gw.ok("GET", "/.well-known/yobiru")["enrollment"]
        self.assertEqual(["pinku"], enrollment["email_attestation_partners"])

    def test_an_attested_address_enrolls_without_a_code_once(self):
        status, obj, _, _ = self.enroll("Erin.Example+pinku@gmail.com")
        self.assertEqual((201, True), (status, obj["email_verified"]), obj)
        status, obj, _, _ = self.enroll("erinexample@googlemail.com")
        self.assertEqual((409, "email_in_use"), (status, obj["error"]), "one address still names one owner")
        stored = json.dumps(self.gw.sql("SELECT * FROM owners"))
        self.assertNotIn("erin", stored.lower(), "only the keyed hash is kept")

    def test_bad_attestations_are_refused(self):
        now = int(time.time())
        cases = [({"secret": "x" * 40}, "invalid_attestation"),
                 ({"partner": "other"}, "unknown_partner"),
                 ({"exp": now - 1}, "attestation_expired"),
                 ({"exp": now + G.PARTNER_ATTESTATION_SECONDS + 60}, "attestation_expired"),
                 ({"audience": "https://elsewhere.example"}, "invalid_attestation")]
        for i, (attest, error) in enumerate(cases):
            status, obj, _, _ = self.enroll("bad%d@example.com" % i, **attest)
            self.assertEqual((400, error), (status, obj["error"]), attest)
        # signed for another owner credential: useless to whoever intercepts it
        owner, source = C.new_token("owner"), C.new_token("source")
        body = {"display_name": "Thief", "source_credential_sha256": C.token_hash(source),
                "email_attestation": self.attest("victim@example.com", C.new_token("owner"))}
        status, obj = self.gw.call("POST", "/v1/enroll", body, owner)
        self.assertEqual((400, "invalid_attestation"), (status, obj["error"]))
        status, obj, _, _ = self.enroll()
        self.assertEqual((400, "email_required"), (status, obj["error"]), "codes are still required without one")

    def closed_bot(self, email):
        """A self-enrolled, published bot that accepts no followers and offers visitors nothing."""
        status, obj, owner, source = self.enroll(email)
        self.assertEqual(201, status, obj)
        bot = obj["bot_id"]
        self.gw.ok("POST", "/v1/owner/bots/%s/publish" % bot, {}, owner)
        self.gw.ok("PATCH", "/v1/owner/bots/%s" % bot,
                   {"policy": {"accept_followers": False, "visitor": [], "follower": []}}, owner)
        return bot, owner, source

    def test_permits_make_two_closed_bots_friends(self):
        a, _, a_source = self.closed_bot("a-permit@example.com")
        b, b_owner, b_source = self.closed_bot("b-permit@example.com")
        status, obj = self.gw.follow(a_source, b)
        self.assertEqual((403, "followers_closed"), (status, obj["error"]))
        permit = self.gw.ok("POST", "/v1/follow-permits", {"for": a}, b_source)
        self.assertEqual((b, a), (permit["target_bot_id"], permit["for_bot_id"]))
        stranger, _, stranger_source = self.closed_bot("c-permit@example.com")
        status, obj = self.gw.call("POST", "/v1/bots/%s/follow" % b, {"permit": permit["permit"]}, stranger_source)
        self.assertEqual((403, "invalid_permit"), (status, obj["error"]), "bound to the bot it names")
        status, obj = self.gw.call("POST", "/v1/bots/%s/follow" % b, {"permit": permit["permit"]}, a_source)
        self.assertEqual((201, True), (status, obj["following"]), obj)
        status, obj = self.gw.follow(b_source, a)
        self.assertEqual(201, status, "following back needs no permit")
        self.assertEqual("friend", self.gw.relation(a_source, b)["relationship"])
        # blocking still wins over a permit
        self.gw.ok("POST", "/v1/owner/bots/%s/contacts/%s" % (b, a), {"action": "block"}, b_owner)
        status, obj = self.gw.call("POST", "/v1/bots/%s/follow" % b, {"permit": permit["permit"]}, a_source)
        self.assertEqual((403, "contact_revoked"), (status, obj["error"]))

    def test_permits_expire_and_are_checked(self):
        a, _, a_source = self.closed_bot("a-exp@example.com")
        b, _, b_source = self.closed_bot("b-exp@example.com")
        for bad in ("fp1.%d.%s" % (int(time.time()) - 5, "0" * 64), "fp1.9999999999." + "0" * 64, "nonsense"):
            status, obj = self.gw.call("POST", "/v1/bots/%s/follow" % b, {"permit": bad}, a_source)
            self.assertEqual((403, "invalid_permit"), (status, obj["error"]), bad)
        status, obj = self.gw.call("POST", "/v1/follow-permits", {"for": a, "ttl_seconds": 10}, b_source)
        self.assertEqual((400, "invalid_ttl"), (status, obj["error"]))
        status, obj = self.gw.call("POST", "/v1/follow-permits", {"for": b}, b_source)
        self.assertEqual(404, status, "not for itself")

    def test_the_client_enrolls_with_an_attestation_and_follows_with_a_permit(self):
        homes = [Path(self._tmp.name) / name for name in ("client-a", "client-b")]
        bots = []
        for i, home in enumerate(homes):
            code, prep, err = cli(home, "enroll-bot", self.gw.url, "--name", "Client %d" % i, "--handle",
                                  "pt-client%d" % i, "--prepare")
            self.assertEqual(0, code, err)
            owner = json.loads((home / "public" / "credentials.json").read_text())["gateways"][self.gw.url][
                "pending_bot"]["owner_token"]
            self.assertEqual(C.token_hash(owner), prep["owner_credential_sha256"])
            exp = int(time.time()) + 600
            att = home / "attestation.json"
            email = "client%d@example.com" % i
            att.write_text(json.dumps({"partner": "pinku", "email": email, "exp": exp,
                                       "sig": G.partner_signature(SECRET, self.gw.url, "pinku", email,
                                                                  prep["owner_credential_sha256"], exp)}))
            code, done, err = cli(home, "enroll-bot", self.gw.url, "--name", "Client %d" % i,
                                  "--attestation-file", str(att))
            self.assertEqual((0, True), (code, done.get("email_verified")), (done, err))
            bots.append(done["bot_id"])
            self.gw.ok("POST", "/v1/owner/bots/%s/publish" % done["bot_id"], {}, owner)
            self.gw.ok("PATCH", "/v1/owner/bots/%s" % done["bot_id"], {"policy": {"accept_followers": False}}, owner)
        code, permit, err = cli(homes[1], "follow-permit", bots[0], "--gateway", self.gw.url)
        self.assertEqual(0, code, err)
        code, obj, err = cli(homes[0], "follow", bots[1], "--gateway", self.gw.url, "--permit", permit["permit"])
        self.assertEqual((0, True), (code, obj.get("following")), (obj, err))


if __name__ == "__main__":
    unittest.main()
