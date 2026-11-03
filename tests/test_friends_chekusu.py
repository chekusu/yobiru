"""Sign-in with the Chekusu account: `enroll-bot --chekusu`.

Chekusu (login.chekusu.com/partner/yobiru) is an email partner like any other: once the owner signs in there, it
signs attestations for the credentials this client generated. The first run prints a link and a code, a later
run collects the attestations and enrolls; an address that already owns bots here joins that owner instead.

A real bot_gateway.py subprocess on loopback and a fake Chekusu partner in this process.
"""
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from support.public_harness import C, FriendsGateway, cli

import bot_gateway as G  # noqa: E402  (on sys.path through the harness)

LIMITS = {"enroll_per_hour_per_ip": 1000, "enroll_per_hour": 1000, "bot_enroll_per_hour": 1000,
          "bot_enroll_per_hour_per_ip": 1000, "email_codes_per_hour_per_ip": 1000,
          "email_codes_per_hour_per_address": 1000}
SECRET = "chekusu-test-secret-0123456789abcdef0123456789"


class FakeChekusu:
    """The partner's two client routes. `sign_in(id, email)` stands in for the owner signing in."""

    def __init__(self, audience):
        self.audience, self.requests, self.n = audience, {}, 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                path = self.path[len("/partner/yobiru"):]
                if path == "/requests":
                    assert body["aud"] == fake.audience, body
                    fake.n += 1
                    rid = "req%019d" % fake.n
                    fake.requests[rid] = {"credentials": body["credentials"], "poll": "poll-%d" % fake.n,
                                          "lang": body.get("lang"), "done": None}
                    return self.reply(201, {"ok": True, "id": rid, "poll_secret": "poll-%d" % fake.n,
                                            "code": "ABCD-%04d" % fake.n,
                                            "sign_in_url": "https://login.chekusu.test/partner/yobiru/go/" + rid,
                                            "expires_at": "2099-01-01T00:00:00Z", "interval": 3})
                rid = path.split("/")[2]
                req = fake.requests.get(rid)
                if req is None or body.get("poll_secret") != req["poll"]:
                    return self.reply(404, {"ok": False, "error": "expired"})
                if req["done"] is None:
                    return self.reply(200, {"ok": True, "state": "pending"})
                del fake.requests[rid]
                return self.reply(200, {"ok": True, "state": "done", "attestations": req["done"]})

            def reply(self, status, obj):
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d/partner/yobiru" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def sign_in(self, rid, email):
        exp = int(time.time()) + 600
        req = self.requests[rid]
        req["done"] = {h: {"partner": "chekusu", "email": email, "exp": exp,
                           "sig": G.partner_signature(SECRET, self.audience, "chekusu", email, h, exp)}
                       for h in req["credentials"]}

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class ChekusuSignInTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="yobiru chekusu ")
        cls.gw = FriendsGateway(Path(cls._tmp.name), limits=LIMITS)
        secret = cls.gw.home / "chekusu.secret"
        secret.write_text(SECRET + "\n")
        os.chmod(str(secret), 0o600)
        cfg = json.loads((cls.gw.home / "gateway.json").read_text())
        cfg["enrollment"] = {"visitors": True, "bots": True, "self_enrolled_publish": True, "email_verification": True}
        cfg["partners"] = {"chekusu": {"secret_file": "chekusu.secret"}}
        (cls.gw.home / "gateway.json").write_text(json.dumps(cfg))
        os.chmod(str(cls.gw.home / "gateway.json"), 0o600)
        cls.gw.start()
        cls.chekusu = FakeChekusu(cls.gw.url)
        cls._env = os.environ.get("YOBIRU_CHEKUSU_PARTNER_URL")
        os.environ["YOBIRU_CHEKUSU_PARTNER_URL"] = cls.chekusu.url

    @classmethod
    def tearDownClass(cls):
        if cls._env is None:
            os.environ.pop("YOBIRU_CHEKUSU_PARTNER_URL", None)
        else:
            os.environ["YOBIRU_CHEKUSU_PARTNER_URL"] = cls._env
        cls.chekusu.stop()
        cls.gw.stop()
        cls._tmp.cleanup()

    def enroll(self, home, name):
        return cli(home, "enroll-bot", self.gw.url, "--name", name, "--chekusu", "--lang", "ja")

    def test_a_new_owner_signs_in_then_enrolls(self):
        home = Path(self._tmp.name) / "new-owner"
        code, first, err = self.enroll(home, "Sato")
        self.assertEqual((0, "sign_in_required"), (code, first.get("state")), (first, err))
        self.assertTrue(first["sign_in_url"].startswith("https://login.chekusu.test/"))
        self.assertIn(first["code"], first["next"])
        rid = first["sign_in_url"].rsplit("/", 1)[1]
        self.assertEqual("ja", self.chekusu.requests[rid]["lang"])
        self.assertEqual(2, len(self.chekusu.requests[rid]["credentials"]), "owner and join credentials")

        code, waiting, err = self.enroll(home, "Sato")
        self.assertEqual((0, "sign_in_required"), (code, waiting.get("state")), (waiting, err))
        self.assertEqual(first["code"], waiting["code"], "the same request until it is done")
        self.assertIn("not signed in yet", waiting["next"])

        self.chekusu.sign_in(rid, "sato@example.com")
        code, done, err = self.enroll(home, "Sato")
        self.assertEqual((0, True, True), (code, done.get("ok"), done.get("email_verified")), (done, err))
        entry = json.loads((home / "public" / "credentials.json").read_text())["gateways"][self.gw.url]
        self.assertTrue(entry["owner"]["token"])
        for leftover in ("pending_bot", "pending_join", "chekusu_request"):
            self.assertNotIn(leftover, entry)

    def test_an_address_that_owns_bots_joins_that_owner(self):
        first_home, second_home = Path(self._tmp.name) / "laptop", Path(self._tmp.name) / "desktop"
        for home, name in ((first_home, "Laptop"), (second_home, "Desktop")):
            code, ask, err = self.enroll(home, name)
            self.assertEqual(0, code, err)
            self.chekusu.sign_in(ask["sign_in_url"].rsplit("/", 1)[1], "kim@example.com")
            code, done, err = self.enroll(home, name)
            self.assertEqual((0, True), (code, done.get("ok")), (done, err))
        self.assertTrue(done.get("joined_existing_owner"), done)
        self.assertEqual("manager", done.get("credential"))

    def test_chekusu_does_not_mix_with_codes(self):
        code, _, err = cli(Path(self._tmp.name) / "mixed", "enroll-bot", self.gw.url, "--name", "X", "--chekusu",
                           "--email", "x@example.com")
        self.assertEqual(2, code)
        self.assertIn("--chekusu replaces", err)


if __name__ == "__main__":
    unittest.main()
