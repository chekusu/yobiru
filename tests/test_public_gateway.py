"""Adversarial tests of the public bot gateway over real HTTP.

Each class runs real bot_gateway.py subprocesses with synthetic homes on ephemeral loopback ports.
Expected failures are asserted together with their durable effect (state, cancellation, budget),
not only the status code.
"""
import base64
import http.client
import json
import os
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zlib
from pathlib import Path

from support.pkg import snapshot
from support.public_harness import C, PUBLIC, Gateway, plain_env, wait_until

FAST = {"min_deadline_seconds": 1, "read_timeout_seconds": 3}


def raw(gw, method, path, body=b"", headers=None, read=True):
    """Exact bytes on the wire, for bodies the normal client would never produce."""
    conn = http.client.HTTPConnection("127.0.0.1", gw.port, timeout=10)
    try:
        conn.putrequest(method, path, skip_accept_encoding=True)
        for k, v in (headers or {}).items():
            conn.putheader(k, v)
        conn.endheaders()
        if body:
            conn.send(body)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, (json.loads(data) if data else {})
    finally:
        conn.close()


class GatewayCase(unittest.TestCase):
    LIMITS = FAST

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="yobiru gateway test ")
        cls.root = Path(cls._tmp.name)
        cls.gw = Gateway(cls.root, limits=cls.LIMITS).start()

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls._tmp.cleanup()

    def handle(self, tag=""):
        return ("t%s%s" % (self.id().rsplit(".", 1)[-1][-20:], tag)).replace("_", "-").lower()[:32]

    def published(self, tag="", **kw):
        return self.gw.bot(self.handle(tag), **kw)

    def friend(self, bot_id, caps, name="Synthetic friend"):
        actor_id, token = self.gw.actor(name)
        self.gw.conversation(bot_id, token)          # first contact as a visitor
        status, obj = self.gw.contact(bot_id, actor_id, "approve", caps)
        self.assertEqual(200, status, obj)
        return actor_id, token

    def message(self, mid, token):
        return self.gw.call("GET", "/v1/messages/%s" % mid, None, token)


class AdministrationTests(GatewayCase):
    def test_bootstrap_is_local_private_and_never_overwrites(self):
        home = self.gw.home
        self.assertEqual(0o700, stat.S_IMODE(home.stat().st_mode))
        for name in ("gateway.json", "gateway.sqlite3", "owner.token"):
            self.assertEqual(0o600, stat.S_IMODE((home / name).stat().st_mode), name)
        before = snapshot(home)
        r = subprocess.run([sys.executable, str(PUBLIC / "bot_gateway.py"), "--gateway-home", str(home), "init"],
                           capture_output=True, text=True, env=plain_env(self.root))
        self.assertEqual(1, r.returncode)
        self.assertIn("already initialized", r.stderr)
        self.assertEqual({k: v for k, v in before.items() if not k.startswith("gateway.sqlite3")},
                         {k: v for k, v in snapshot(home).items() if not k.startswith("gateway.sqlite3")})
        # there is no HTTP way to create an owner or read secrets
        for method, path in (("POST", "/v1/owners"), ("POST", "/v1/owner"), ("GET", "/v1/owners"),
                             ("POST", "/v1/admin/init"), ("GET", "/v1/owner/token")):
            status, obj = self.gw.call(method, path, {} if method == "POST" else None)
            self.assertIn(status, (401, 404, 405), (method, path))
            self.assertNotIn("token", json.dumps(obj))
        self.assertEqual(401, self.gw.call("GET", "/v1/owner")[0])

    def test_credential_realms_never_substitute_for_each_other(self):
        bot_id = self.published()
        actor_id, actor = self.gw.actor()
        worker = self.gw.worker(bot_id)
        verifier = self.gw.ok("POST", "/v1/owner/bots/%s/verifiers" % bot_id, {}, self.gw.owner_token)["token"]
        owner = self.gw.owner_token
        forged = C.TOKEN_PREFIX["owner"] + "_" + "A" * 43
        endpoints = {
            "owner": [("GET", "/v1/owner/bots", None), ("POST", "/v1/owner/bots/%s/publish" % bot_id, {}),
                      ("POST", "/v1/owner/bots/%s/workers" % bot_id, {})],
            "worker": [("POST", "/v1/worker/claim", {}), ("GET", "/v1/worker", None)],
            "verifier": [("POST", "/v1/capability/redeem", {"ticket": C.new_token("ticket")})],
            "actor": [("GET", "/v1/actor", None), ("POST", "/v1/bots/%s/conversations" % bot_id, {"op_key": "x" * 12})]}
        tokens = {"owner": owner, "worker": worker, "verifier": verifier, "actor": actor}
        for realm, calls in endpoints.items():
            for other, token in list(tokens.items()) + [("none", None), ("forged", forged), ("garbage", "Bearer")]:
                if other == realm:
                    continue
                for method, path, body in calls:
                    if other == "garbage":
                        status = raw(self.gw, method, path, headers={"Authorization": "Bearer not-a-token",
                                                                       "Content-Length": "0"})[0]
                    else:
                        status = self.gw.call(method, path, body, token)[0]
                    self.assertEqual(401, status, "%s credential on %s %s" % (other, method, path))
        # the worker never needs, and cannot use, the owner credential: a connector is a bot-scoped worker
        self.assertEqual(401, self.gw.call("POST", "/v1/worker/claim", {}, owner)[0])
        self.assertEqual(bot_id, self.gw.ok("GET", "/v1/worker", None, worker)["bot_id"])

    def test_another_owner_cannot_see_or_manage_a_bot(self):
        bot_id = self.published()
        token_file = self.root / ("second-owner-%s.token" % self.id()[-6:])
        r = subprocess.run([sys.executable, str(PUBLIC / "bot_gateway.py"), "--gateway-home", str(self.gw.home),
                            "owner", "add", "--label", "Other owner", "--token-file", str(token_file)],
                           capture_output=True, text=True, env=plain_env(self.root))
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertNotIn(token_file.read_text().strip(), r.stdout + r.stderr, "credentials are never printed")
        other = token_file.read_text().strip()
        for method, path, body in (("GET", "/v1/owner/bots/%s" % bot_id, None),
                                   ("PATCH", "/v1/owner/bots/%s" % bot_id, {"display_name": "Hijacked"}),
                                   ("POST", "/v1/owner/bots/%s/unpublish" % bot_id, {}),
                                   ("GET", "/v1/owner/bots/%s/contacts" % bot_id, None),
                                   ("POST", "/v1/owner/bots/%s/workers" % bot_id, {"replace": True})):
            self.assertEqual(404, self.gw.call(method, path, body, other)[0], path)
        self.assertEqual([], self.gw.ok("GET", "/v1/owner/bots", None, other)["bots"])
        self.assertEqual("published", self.gw.ok("GET", "/v1/owner/bots/%s" % bot_id, None, self.gw.owner_token)["state"])

    def test_handles_are_bound_to_one_owner_and_renames_keep_the_bot_id(self):
        h = self.handle()
        bot_id = self.gw.bot(h)
        owner = self.gw.owner_token
        self.assertEqual((409, "handle_unavailable"),
                         (lambda s, o: (s, o["error"]))(*self.gw.call("POST", "/v1/owner/bots",
                                                                      {"handle": h, "display_name": "Dup"}, owner)))
        for bad, code in (("admin", 409), ("yobiru", 409), ("ab", 400), ("Bad Handle", 400), ("x--y", 400),
                          ("trailing-", 400), ("9lives", 400)):
            self.assertEqual(code, self.gw.call("POST", "/v1/owner/bots", {"handle": bad, "display_name": "x"},
                                                owner)[0], bad)
        renamed = self.gw.ok("PATCH", "/v1/owner/bots/%s" % bot_id, {"handle": "renamed-handle"}, owner)
        self.assertEqual(bot_id, renamed["bot_id"])
        self.assertEqual(404, self.gw.call("GET", "/v1/bots/@" + h)[0])
        self.assertEqual(bot_id, self.gw.ok("GET", "/v1/bots/@renamed-handle")["bot_id"])
        token_file = self.root / ("rival-%s.token" % self.id()[-6:])
        subprocess.run([sys.executable, str(PUBLIC / "bot_gateway.py"), "--gateway-home", str(self.gw.home), "owner",
                        "add", "--token-file", str(token_file)], check=True, capture_output=True, env=plain_env(self.root))
        rival = token_file.read_text().strip()
        self.assertEqual(409, self.gw.call("POST", "/v1/owner/bots", {"handle": h, "display_name": "Impostor"},
                                           rival)[0], "a released handle cannot be taken by another owner")

    def test_public_card_is_an_allowlist_and_unpublished_bots_do_not_exist(self):
        bot_id = self.published()
        card = self.gw.ok("GET", "/v1/bots/%s" % bot_id)
        self.assertEqual({"ok", "type", "card_version", "issuer", "bot_id", "handle", "address", "id_address",
                          "display_name", "description", "capabilities", "capabilities_note", "accepts_visitors",
                          "accepts_followers", "accepts_contact_requests", "chat", "e2e", "identity", "api",
                          "connect", "stats", "links", "avatar", "agents"},
                         set(card))
        self.assertEqual({"messages": 0, "followers": 0, "friends": 0}, card["stats"], "counts only, never who")
        self.assertTrue(card["address"].endswith("/@" + self.handle()))
        self.assertEqual(card, self.gw.ok("GET", "/@" + self.handle()), "the share link serves the same card")
        self.assertEqual(card, self.gw.ok("GET", "/@" + bot_id))
        draft = self.gw.bot(self.handle("d"), publish=False)
        hidden = self.gw.bot(self.handle("u"))
        self.gw.ok("POST", "/v1/owner/bots/%s/unpublish" % hidden, {}, self.gw.owner_token)
        missing = self.gw.call("GET", "/v1/bots/bot_aaaaaaaaaaaaaaaa")
        _, actor = self.gw.actor()
        for ref in (draft, hidden, "@" + self.handle("d"), "@" + self.handle("u")):
            self.assertEqual(missing, self.gw.call("GET", "/v1/bots/%s" % ref), ref)
            self.assertEqual(404, self.gw.call("POST", "/v1/bots/%s/conversations" % ref, {"op_key": "a" * 12},
                                               actor)[0])
            self.assertEqual(404, self.gw.call("POST", "/v1/bots/%s/contact-request" % ref, {}, actor)[0])

    def test_no_enumeration_and_no_private_receiver_routes(self):
        not_found = self.gw.call("GET", "/v1/nothing-here")
        self.assertEqual((404, {"ok": False, "error": "not_found"}), not_found)
        for method, path in (("GET", "/v1/bots"), ("GET", "/info"), ("POST", "/msg"), ("GET", "/health"),
                             ("GET", "/requests/0011223344556677"), ("GET", "/f/%s/x" % ("A" * 43)),
                             ("GET", "/v1/conversations"), ("GET", "/v1/messages"), ("GET", "/")):
            status, obj = self.gw.call(method, path, {} if method == "POST" else None)
            self.assertEqual(not_found, (status, obj), path)
        self.assertEqual(400, raw(self.gw, "GET", "/v1/health?debug=1")[0], "query strings fail closed")
        self.assertEqual(405, self.gw.call("DELETE", "/v1/health")[0])
        self.assertEqual((405, "method_not_allowed"), (lambda st, o: (st, o["error"]))(
            *self.gw.call("GET", "/v1/actors")), "callers cannot be listed")
        health = self.gw.ok("GET", "/v1/health")
        self.assertEqual({"ok", "service", "api"}, set(health))


def page(gw, path, headers=None):
    """(status, headers, body bytes) of a GET, for answers that may be HTML."""
    conn = http.client.HTTPConnection("127.0.0.1", gw.port, timeout=10)
    try:
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


def png(w, h, extra=b""):
    """A tiny valid RGB PNG, with optional extra chunks before the image data."""
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    rows = b"".join(b"\x00" + b"\xff\x5b\x1f" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + extra +
            chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


BROWSER = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
           "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}


class ProfilePageTests(GatewayCase):
    def test_agents_keep_the_json_card_and_browsers_get_a_page(self):
        bot_id = self.published()
        h = self.handle()
        self.gw.ok("PATCH", "/v1/owner/bots/%s" % bot_id,
                   {"description": "Hi <script>alert(1)</script> & welcome"}, self.gw.owner_token)
        card = self.gw.ok("GET", "/@" + h)
        for headers in ({}, {"Accept": "*/*"}, {"Accept": "application/json"},
                        {"Accept": "application/json, text/html"}, {"Accept": "text/html;q=0.5, application/json"},
                        dict(BROWSER, Accept="text/html;q=0")):
            status, hdrs, body = page(self.gw, "/@" + h, headers)
            self.assertEqual(200, status, headers)
            self.assertTrue(hdrs["content-type"].startswith("application/json"), headers)
            self.assertEqual(card, json.loads(body), headers)
            self.assertEqual("Accept", hdrs.get("vary"))
        for path in ("/@%s.json" % h, "/@%s?format=json" % h, "/@%s.json" % bot_id):
            status, hdrs, body = page(self.gw, path, BROWSER)
            self.assertEqual((200, card), (status, json.loads(body)), path)

        status, hdrs, body = page(self.gw, "/@" + h, BROWSER)
        self.assertEqual(200, status)
        self.assertTrue(hdrs["content-type"].startswith("text/html"))
        text = body.decode("utf-8")
        self.assertIn('<html lang="zh-CN" dir="ltr">', text)
        self.assertIn("Synthetic " + h, text)
        self.assertIn('rel="alternate" type="application/json" href="%s.json"' % card["address"], text)
        self.assertIn("%s.json" % card["address"], hdrs["link"])
        comment = text.split("<!--", 1)[1].split("-->", 1)[0]
        self.assertIn("yobiru public connect " + card["address"], comment, "agents find the steps in a comment")
        self.assertNotIn("yobiru public connect", text.split("</head>", 1)[1].replace(comment, ""))
        self.assertNotIn("<script>alert", text, "the description is escaped")
        self.assertIn("Hi &lt;script&gt;alert(1)&lt;/script&gt; &amp; welcome", text)
        embedded = text.split('<script type="application/json" id="yobiru-card">', 1)[1].split("</script>", 1)[0]
        self.assertEqual({k: v for k, v in card.items() if k != "ok"}, json.loads(embedded))
        nonce = hdrs["content-security-policy"].split("'nonce-", 1)[1].split("'", 1)[0]
        self.assertEqual(2, text.count('nonce="%s"' % nonce), "only the page's own style and script run")
        self.assertIn("frame-ancestors 'none'", hdrs["content-security-policy"])

        self.assertIn('<html lang="ja" dir="ltr">', page(self.gw, "/@%s?lang=ja" % h, BROWSER)[2].decode("utf-8"))
        self.assertIn('<html lang="en" dir="ltr">', page(self.gw, "/@" + h, {"Accept": "text/html"})[2].decode())
        self.assertIn('<html lang="en" dir="ltr">', page(self.gw, "/@%s?format=html&lang=en" % h)[2].decode())
        self.assertTrue(page(self.gw, "/@" + bot_id, BROWSER)[1]["content-type"].startswith("text/html"))

    def test_missing_bots_and_odd_queries(self):
        status, hdrs, body = page(self.gw, "/@nobody-here", BROWSER)
        self.assertEqual(404, status)
        self.assertTrue(hdrs["content-type"].startswith("text/html"))
        self.assertIn("noindex", body.decode("utf-8"))
        status, hdrs, body = page(self.gw, "/@nobody-here")
        self.assertEqual((404, "not_found"), (status, json.loads(body)["error"]))
        h = self.handle()
        self.published()
        for q in ("?x=1", "?format=xml", "?lang=zh&lang=en", "?format"):
            status, _, body = page(self.gw, "/@" + h + q, BROWSER)
            self.assertEqual((400, "query_not_supported"), (status, json.loads(body)["error"]), q)
        self.assertEqual(400, page(self.gw, "/v1/health?format=json")[0], "other routes still refuse queries")

    def test_owner_links_and_avatar(self):
        bot_id, h, owner = self.published(), self.handle(), self.gw.owner_token

        card = self.gw.ok("GET", "/@" + h)
        self.assertEqual(([], None), (card["links"], card["avatar"]))
        links = [{"label": "Site", "url": "https://example.com/me"}, {"label": "GitHub", "url": "https://github.com/x"}]
        self.assertEqual(links, self.gw.ok("PATCH", "/v1/owner/bots/%s" % bot_id, {"links": links}, owner)["links"])
        for bad in ([{"label": "x", "url": "http://example.com"}], [{"label": "x", "url": "javascript:alert(1)"}],
                    [{"label": "x", "url": "https://user:pw@example.com"}], [{"label": "", "url": "https://a.example"}],
                    [{"label": "x", "url": "https://a.example", "more": 1}], [links[0]] * 13, "https://a.example",
                    [{"label": "x", "url": "mailto:a@example.com?subject=hi"}], [{"label": "x", "url": "tel:12"}],
                    [{"label": "x", "url": "mailto:a@example.com,b@example.com"}]):
            status, obj = self.gw.call("PATCH", "/v1/owner/bots/%s" % bot_id, {"links": bad}, owner)
            self.assertEqual((400, "invalid_links"), (status, obj.get("error")), bad)
        self.assertEqual(links, self.gw.ok("GET", "/@" + h)["links"], "a refused edit changes nothing")

        image = png(64, 64, extra=struct.pack(">I", 14) + b"tEXt" + b"GPS\x0035.6,139.7" + b"\x00\x00\x00\x00")
        obj = self.gw.ok("PUT", "/v1/owner/bots/%s/avatar" % bot_id,
                         {"data_base64": base64.b64encode(image).decode()}, owner)
        self.assertEqual("image/png", obj["avatar_type"])
        card = self.gw.ok("GET", "/@" + h)
        self.assertTrue(card["avatar"].startswith("%s/@%s/avatar/" % (self.gw.url, bot_id)), card["avatar"])
        path = card["avatar"][len(self.gw.url):]
        status, hdrs, body = page(self.gw, path)
        self.assertEqual((200, "image/png"), (status, hdrs["content-type"]))
        self.assertNotIn(b"GPS", body, "text chunks are removed")
        self.assertIn("sandbox", hdrs["content-security-policy"])
        self.assertEqual(200, page(self.gw, "/@%s/avatar" % h)[0])
        self.assertEqual(404, page(self.gw, "/@%s/avatar/%s" % (bot_id, "0" * 16))[0])
        text = page(self.gw, "/@" + h, BROWSER)[2].decode("utf-8")
        self.assertIn('<img class="avatar" src="%s"' % card["avatar"], text)
        self.assertIn('href="https://github.com/x" rel="noopener nofollow ugc me" data-platform="github">', text)
        self.assertIn('<bdi class="lb">GitHub</bdi>', text)

        for bad in (b"<svg xmlns='http://www.w3.org/2000/svg'/>", b"GIF89a" + b"\x00" * 40, png(8, 8),
                    png(64, 64) + b"\x00" * 70000, b"\x89PNG\r\n\x1a\n"):
            status, obj = self.gw.call("PUT", "/v1/owner/bots/%s/avatar" % bot_id,
                                       {"data_base64": base64.b64encode(bad).decode()}, owner)
            self.assertIn(status, (400, 413), bad[:10])
        self.assertEqual(card["avatar"], self.gw.ok("GET", "/@" + h)["avatar"])
        _, stranger = self.gw.actor()
        self.assertEqual(401, self.gw.call("PUT", "/v1/owner/bots/%s/avatar" % bot_id,
                                           {"data_base64": base64.b64encode(image).decode()}, stranger)[0])
        self.gw.ok("DELETE", "/v1/owner/bots/%s/avatar" % bot_id, None, owner)
        self.assertIsNone(self.gw.ok("GET", "/@" + h)["avatar"])
        self.assertEqual(404, page(self.gw, path)[0])
        self.gw.ok("POST", "/v1/owner/bots/%s/unpublish" % bot_id, {}, owner)
        self.gw.ok("PUT", "/v1/owner/bots/%s/avatar" % bot_id, {"data_base64": base64.b64encode(image).decode()}, owner)
        self.assertEqual(404, page(self.gw, "/@%s/avatar" % bot_id)[0], "an unpublished bot shows no avatar")

    def test_cli_sets_links_and_avatar(self):
        from support.public_harness import cli
        h = self.handle()
        self.published()
        home = self.root / ("cli-%s" % h)
        img = self.root / ("%s.png" % h)
        img.write_bytes(png(120, 120))
        self.assertEqual(0, cli(home, "owner", "login", self.gw.url, "--token-file",
                                str(self.gw.home / "owner.token"))[0])
        code, out, err = cli(home, "bot", "edit", "@" + h, "--link", "Site=https://example.com",
                             "--link", "X=https://x.com/me")
        self.assertEqual(0, code, err)
        self.assertEqual(["Site", "X"], [l["label"] for l in out["links"]])
        code, out, err = cli(home, "bot", "avatar", "@" + h, str(img))
        self.assertEqual((0, "image/png"), (code, out.get("avatar_type")), err)
        self.assertTrue(self.gw.ok("GET", "/@" + h)["avatar"])
        self.assertEqual(0, cli(home, "bot", "edit", "@" + h, "--clear-links")[0])
        self.assertEqual(0, cli(home, "bot", "avatar", "@" + h, "--remove")[0])
        card = self.gw.ok("GET", "/@" + h)
        self.assertEqual(([], None), (card["links"], card["avatar"]))
        self.assertNotEqual(0, cli(home, "bot", "edit", "@" + h, "--link", "nope")[0])
        code, out, err = cli(home, "bot", "edit", "@" + h, "--link", "https://www.youtube.com/watch?v=abc",
                             "--link", "Mail=mailto:me@example.com")
        self.assertEqual(0, code, err)
        self.assertEqual([("YouTube", "https://www.youtube.com/watch?v=abc"), ("Mail", "mailto:me@example.com")],
                         [(l["label"], l["url"]) for l in out["links"]], "a bare URL keeps its query")

    def test_cli_keeps_equals_in_bare_web_destinations(self):
        from support.public_harness import cli
        h = self.handle()
        self.published()
        home = self.root / ("cli-%s" % h)
        self.assertEqual(0, cli(home, "owner", "login", self.gw.url, "--token-file",
                                str(self.gw.home / "owner.token"))[0])
        for value, expected in (("example.com/page?ref=friend.example", "https://example.com/page?ref=friend.example"),
                                ("www.youtube.com/watch?v=abc", "https://www.youtube.com/watch?v=abc"),
                                ("example.com/path=friend.example", "https://example.com/path=friend.example"),
                                ("example.com?ref=friend.example", "https://example.com?ref=friend.example"),
                                ("example.com#ref=friend.example", "https://example.com#ref=friend.example"),
                                ("example.com/page?ref=https://friend.example",
                                 "https://example.com/page?ref=https://friend.example"),
                                ("https://example.com/page?email=alice@example.com",
                                 "https://example.com/page?email=alice@example.com"),
                                ("Profile=https://example.com/page?ref=friend.example",
                                 "https://example.com/page?ref=friend.example"),
                                ("Profile=example.com/page?ref=friend.example",
                                 "https://example.com/page?ref=friend.example")):
            with self.subTest(value=value):
                code, out, err = cli(home, "bot", "edit", "@" + h, "--link", value)
                self.assertEqual(0, code, err)
                self.assertEqual([expected], [link["url"] for link in out["links"]])
                self.assertEqual(out["links"], self.gw.ok("GET", "/@" + h)["links"])
        links = self.gw.ok("GET", "/@" + h)["links"]
        code, _, _ = cli(home, "bot", "edit", "@" + h, "--link", "example.com/invalid path=friend.example")
        self.assertNotEqual(0, code)
        self.assertEqual(links, self.gw.ok("GET", "/@" + h)["links"], "an invalid bare URL is not reinterpreted")

    def test_cli_preserves_url_shaped_link_labels(self):
        from support.public_harness import cli
        h = self.handle()
        self.published()
        home = self.root / ("cli-%s" % h)
        self.assertEqual(0, cli(home, "owner", "login", self.gw.url, "--token-file",
                                str(self.gw.home / "owner.token"))[0])
        for label, url in (("docs.example.com/guide", "https://docs.example.com/new-guide"),
                           ("docs.example.com", "https://docs.example.com/new-guide?ref=profile"),
                           ("docs.example.com:8443/guide", "https://docs.example.com/new-guide"),
                           ("docs.example.com/mail", "mailto:me@example.com"),
                           ("docs.example.com/call", "tel:+1-202-555-0123")):
            with self.subTest(label=label, url=url):
                code, out, err = cli(home, "bot", "edit", "@" + h, "--link", label + "=" + url)
                self.assertEqual(0, code, err)
                expected = [{"label": label, "url": url}]
                self.assertEqual(expected, out["links"])
                self.assertEqual(expected, self.gw.ok("GET", "/@" + h)["links"])

    def test_cli_rejects_bare_ports_without_retargeting_links(self):
        from support.public_harness import cli
        h = self.handle()
        bot_id = self.published()
        home = self.root / ("cli-%s" % h)
        self.assertEqual(0, cli(home, "owner", "login", self.gw.url, "--token-file",
                                str(self.gw.home / "owner.token"))[0])
        links = [{"label": "Website", "url": "https://example.com/profile"}]
        self.gw.ok("PATCH", "/v1/owner/bots/%s" % bot_id, {"links": links}, self.gw.owner_token)
        for value in ("example.com:8443/path?next=friend.example", "example.com:8443/path=friend.example",
                      "example.com:8443?next=friend.example", "example.com:8443#next=friend.example",
                      "example.com:8443/path?next=https://friend.example"):
            for args in (("link", "add", "@" + h, value), ("edit", "@" + h, "--link", value)):
                with self.subTest(value=value, command=args[0]):
                    code, _, _ = cli(home, "bot", *args)
                    self.assertNotEqual(0, code, "an unsupported bare port must not select a new destination")
                    self.assertEqual(links, self.gw.ok("GET", "/@" + h)["links"])
        url = "https://example.com:8443/path?next=friend.example"
        code, out, err = cli(home, "bot", "edit", "@" + h, "--link", url)
        self.assertEqual(0, code, err)
        self.assertEqual([url], [item["url"] for item in out["links"]])
        self.assertEqual(out["links"], self.gw.ok("GET", "/@" + h)["links"])

    def test_cli_dotted_numbers_require_schemes(self):
        from support.public_harness import cli
        h = self.handle()
        bot_id = self.published()
        home = self.root / ("cli-%s" % h)
        self.assertEqual(0, cli(home, "owner", "login", self.gw.url, "--token-file",
                                str(self.gw.home / "owner.token"))[0])
        links = [{"label": "Website", "url": "https://example.com/profile"}]
        self.gw.ok("PATCH", "/v1/owner/bots/%s" % bot_id, {"links": links}, self.gw.owner_token)
        for value in ("202.555.0123", "192.168.0.1"):
            for args in (("link", "add", "@" + h, value), ("edit", "@" + h, "--link", value)):
                with self.subTest(value=value, command=args[0]):
                    code, _, err = cli(home, "bot", *args)
                    self.assertNotEqual(0, code)
                    self.assertIn("ambiguous", err)
                    self.assertIn("tel:", err)
                    self.assertIn("https://", err)
                    self.assertEqual(links, self.gw.ok("GET", "/@" + h)["links"])
        for args in (("link", "add", "@" + h, "tel:202.555.0123"),
                     ("edit", "@" + h, "--link", "tel:202.555.0123")):
            with self.subTest(command=args[0]):
                code, out, err = cli(home, "bot", *args)
                self.assertEqual(0, code, err)
                self.assertEqual("tel:202-555-0123", out["links"][-1]["url"])
                self.assertEqual("tel:202-555-0123", self.gw.ok("GET", "/@" + h)["links"][-1]["url"])

    def test_cli_requires_unambiguous_equals_in_email_links(self):
        from support.public_harness import cli
        h = self.handle()
        self.published()
        home = self.root / ("cli-%s" % h)
        self.assertEqual(0, cli(home, "owner", "login", self.gw.url, "--token-file",
                                str(self.gw.home / "owner.token"))[0])
        code, out, err = cli(home, "bot", "edit", "@" + h, "--link", "mailto:alice=work@example.com",
                             "--link", "Mail=mailto:me@example.com", "--link", "mailto:alice.smith/notes@example.com")
        self.assertEqual(0, code, err)
        links = [{"label": "alice=work@example.com", "url": "mailto:alice=work@example.com"},
                 {"label": "Mail", "url": "mailto:me@example.com"},
                 {"label": "alice.smith/notes@example.com", "url": "mailto:alice.smith/notes@example.com"}]
        self.assertEqual(links, out["links"])
        self.assertEqual(links, self.gw.ok("GET", "/@" + h)["links"])
        for value in ("alice=work@example.com", "Mail=me@example.com", "alice.smith/notes@example.com",
                      "example.com/page?email=alice@example.com"):
            with self.subTest(value=value):
                code, _, err = cli(home, "bot", "edit", "@" + h, "--link", value)
                self.assertNotEqual(0, code, "ambiguous email links must not change the destination")
                self.assertIn("ambiguous", err)
                self.assertIn("mailto:", err)
                self.assertEqual(links, self.gw.ok("GET", "/@" + h)["links"])

    def test_cli_generated_link_labels_fit_gateway_limit(self):
        from support.public_harness import cli
        h = self.handle()
        self.published()
        home = self.root / ("cli-%s" % h)
        self.assertEqual(0, cli(home, "owner", "login", self.gw.url, "--token-file",
                                str(self.gw.home / "owner.token"))[0])
        email40, email41 = "a" * 28 + "@example.com", "a" * 29 + "@example.com"
        host40, host41 = "a" * 28 + ".example.com", "a" * 29 + ".example.com"
        for value, url, label in ((email40, "mailto:" + email40, email40),
                                  (email41, "mailto:" + email41, "Email"),
                                  (host40 + "/profile", "https://" + host40 + "/profile", host40),
                                  (host41 + "/profile", "https://" + host41 + "/profile", "Website")):
            for args in (("link", "add", "@" + h, value), ("edit", "@" + h, "--link", value)):
                with self.subTest(value=value, command=args[0]):
                    code, out, err = cli(home, "bot", *args)
                    self.assertEqual(0, code, err)
                    links = [{"label": item["label"], "url": item["url"]} for item in out["links"]]
                    self.assertEqual({"label": label, "url": url}, links[-1])
                    self.assertEqual(links, self.gw.ok("GET", "/@" + h)["links"])

    def test_cli_explicit_link_labels_keep_gateway_limit(self):
        from support.public_harness import cli
        h = self.handle()
        self.published()
        home = self.root / ("cli-%s" % h)
        self.assertEqual(0, cli(home, "owner", "login", self.gw.url, "--token-file",
                                str(self.gw.home / "owner.token"))[0])
        address = "a" * 29 + "@example.com"
        for length in (40, 41):
            label = "L" * length
            for args in (("link", "add", "@" + h, address, "--label", label),
                         ("edit", "@" + h, "--link", label + "=mailto:" + address)):
                with self.subTest(length=length, command=args[0]):
                    code, out, err = cli(home, "bot", *args)
                    if length == 40:
                        self.assertEqual(0, code, err)
                        self.assertEqual(label, out["links"][0]["label"])
                    else:
                        self.assertNotEqual(0, code, "explicit labels must not be silently shortened")
                        self.assertEqual("invalid_links", out["error"])
                    self.assertEqual([{"label": "L" * 40, "url": "mailto:" + address}],
                                     self.gw.ok("GET", "/@" + h)["links"])

    def test_cli_rejects_tel_parameters_without_changing_links(self):
        from support.public_harness import cli
        h = self.handle()
        bot_id = self.published()
        home = self.root / ("cli-%s" % h)
        self.assertEqual(0, cli(home, "owner", "login", self.gw.url, "--token-file",
                                str(self.gw.home / "owner.token"))[0])
        links = [{"label": "Website", "url": "https://example.com/profile"}]
        self.gw.ok("PATCH", "/v1/owner/bots/%s" % bot_id, {"links": links}, self.gw.owner_token)
        for value in ("tel:+1-555-123-4567;ext=89", "tel:5550123;phone-context=+1-212"):
            for args in (("link", "add", "@" + h, value), ("edit", "@" + h, "--link", value)):
                with self.subTest(value=value, command=args[0]):
                    code, _, err = cli(home, "bot", *args)
                    self.assertNotEqual(0, code, "unsupported tel parameters must not become subscriber digits")
                    self.assertIn("tel:", err)
                    self.assertEqual(links, self.gw.ok("GET", "/@" + h)["links"])
        value = "tel:+1 (555) 123-4567"
        for args in (("link", "add", "@" + h, value), ("edit", "@" + h, "--link", value)):
            code, out, err = cli(home, "bot", *args)
            self.assertEqual(0, code, err)
            self.assertEqual("tel:+1-555-123-4567", out["links"][-1]["url"])
            self.assertEqual("tel:+1-555-123-4567", self.gw.ok("GET", "/@" + h)["links"][-1]["url"])

    def test_cli_changes_one_link_at_a_time(self):
        from support.public_harness import cli
        h = self.handle()
        self.published()
        home = self.root / ("cli-%s" % h)
        self.assertEqual(0, cli(home, "owner", "login", self.gw.url, "--token-file",
                                str(self.gw.home / "owner.token"))[0])
        seen = lambda out: [(l["n"], l["label"], l["url"], l["platform"]) for l in out["links"]]  # noqa: E731
        for args in (("x.com/me",), ("https://www.instagram.com/me", "--label", "IG"), ("+81 90 1234 5678",),
                     ("me@example.com", "--at", "1")):
            code, out, err = cli(home, "bot", "link", "add", "@" + h, *args)
            self.assertEqual(0, code, err)
        self.assertEqual([(1, "me@example.com", "mailto:me@example.com", "email"),
                          (2, "X", "https://x.com/me", "x"), (3, "IG", "https://www.instagram.com/me", "instagram"),
                          (4, "+81-90-1234-5678", "tel:+81-90-1234-5678", "phone")], seen(out))
        code, out, err = cli(home, "bot", "link", "add", "@" + h, "https://x.com/me", "--label", "X (Twitter)")
        self.assertEqual((0, ["me@example.com", "X (Twitter)", "IG", "+81-90-1234-5678"]),
                         (code, [l["label"] for l in out["links"]]), "the same URL again changes it, not a copy")
        code, out, err = cli(home, "bot", "link", "move", "@" + h, "instagram", "1")
        self.assertEqual((0, "IG"), (code, out["links"][0]["label"]), err)
        code, out, err = cli(home, "bot", "link", "remove", "@" + h, "twitter")
        self.assertEqual(0, code, err)
        code, out, err = cli(home, "bot", "link", "remove", "@" + h, "3")
        self.assertEqual(0, code, err)
        code, out, err = cli(home, "bot", "link", "list", "@" + h)
        self.assertEqual((0, ["https://www.instagram.com/me", "mailto:me@example.com"]),
                         (code, [l["url"] for l in out["links"]]), err)
        self.assertEqual(["https://www.instagram.com/me", "mailto:me@example.com"],
                         [l["url"] for l in self.gw.ok("GET", "/@" + h)["links"]], "the public card changed")
        cli(home, "bot", "link", "add", "@" + h, "https://instagram.com/second", "--label", "IG 2")
        code, _, err = cli(home, "bot", "link", "remove", "@" + h, "instagram")
        self.assertNotEqual(0, code)
        self.assertIn("matches 2 links", err)
        for bad in (("add", "@" + h, "javascript:alert(1)"), ("add", "@" + h, "@me"), ("remove", "@" + h, "tiktok")):
            self.assertNotEqual(0, cli(home, "bot", "link", *bad)[0], bad)

    def test_cli_sets_the_agents_the_page_shows(self):
        from support.public_harness import cli
        h = self.handle()
        self.published()
        home = self.root / ("cli-%s" % h)
        self.assertEqual(0, cli(home, "owner", "login", self.gw.url, "--token-file",
                                str(self.gw.home / "owner.token"))[0])
        code, out, err = cli(home, "bot", "edit", "@" + h, "--agents", "Grok Bot, claude code，Codex、grok")
        self.assertEqual((0, ["grok", "claude_code", "codex"]), (code, [a["id"] for a in out["agents"]]), err)
        self.assertEqual(["Grok Bot", "Claude Code", "Codex"], [a["name"] for a in self.gw.ok("GET", "/@" + h)["agents"]])
        code, _, err = cli(home, "bot", "edit", "@" + h, "--agents", "Grok, Skynet")
        self.assertNotEqual(0, code)
        self.assertIn("unknown agent 'Skynet'", err)
        self.assertEqual(3, len(self.gw.ok("GET", "/@" + h)["agents"]), "a refused edit changes nothing")
        code, out, err = cli(home, "bot", "edit", "@" + h, "--agents", "")
        self.assertEqual((0, []), (code, out["agents"]), err)

    def test_share_links_from_the_page_still_parse(self):
        for link in ("https://gw.example/@guoyu.json", "https://gw.example/@guoyu?lang=zh",
                     "https://gw.example/@guoyu#for-agents"):
            self.assertEqual(("https://gw.example", "@guoyu"), C.parse_address(link), link)
        self.assertEqual(("https://gw.example", "bot_aaaaaaaaaaaaaaaa"),
                         C.parse_address("https://gw.example/@bot_aaaaaaaaaaaaaaaa.json"))


class IsolationTests(GatewayCase):
    def test_contacts_cannot_reach_each_others_conversations(self):
        bot_id = self.published()
        a_id, a = self.gw.actor("A")
        b_id, b = self.gw.actor("B")
        cid_a = self.gw.conversation(bot_id, a, op_key="shared-op-key-1")
        cid_b = self.gw.conversation(bot_id, b, op_key="shared-op-key-1")
        self.assertNotEqual(cid_a, cid_b, "op keys are scoped to the authenticated caller")
        status, sent = self.gw.send(cid_a, a, text="A's private question", op_key="same-op-key-22")
        self.assertEqual(202, status)
        mid = sent["message_id"]
        for method, path, body in (("GET", "/v1/conversations/%s" % cid_a, None),
                                   ("POST", "/v1/conversations/%s/messages" % cid_a,
                                    {"op_key": "b-intrusion-1", "kind": "faq", "text": "x"}),
                                   ("GET", "/v1/messages/%s" % mid, None),
                                   ("GET", "/v1/conversations/%s/operations/same-op-key-22" % cid_a, None)):
            status, obj = self.gw.call(method, path, body, b)
            self.assertEqual((404, "not_found"), (status, obj.get("error")), path)
            self.assertNotIn("private question", json.dumps(obj))
        status, own = self.gw.send(cid_b, b, text="B's own", op_key="same-op-key-22")
        self.assertEqual((202, False), (status, own["duplicate"]), "same op key, different caller: a new message")
        self.assertNotEqual(mid, own["message_id"])
        history = self.gw.ok("GET", "/v1/conversations/%s" % cid_b, None, b)
        self.assertEqual(["B's own"], [m["content"]["text"] for m in history["messages"]])

    def test_a_grant_is_per_bot(self):
        x, y = self.published("x"), self.published("y")
        actor_id, token = self.friend(x, ["faq", "calendar_slots"])
        cid_y = self.gw.conversation(y, token)
        status, obj = self.gw.send(cid_y, token, kind="slots", text=None, query={"duration_minutes": 30})
        self.assertEqual((403, "capability_not_granted"), (status, obj["error"]))
        rel = self.gw.ok("GET", "/v1/bots/%s/contact" % y, None, token)
        self.assertEqual(("visitor", ["faq", "chat"]), (rel["relationship"], rel["capabilities"]))
        self.assertEqual([], rel["explicit_grants"])

    def test_two_friends_receive_exactly_their_own_capabilities(self):
        bot_id = self.published()
        a_id, a = self.friend(bot_id, ["faq", "calendar_slots"], "Friend A")
        b_id, b = self.friend(bot_id, ["faq"], "Friend B")
        # no visitor preset: what each contact gets is exactly the owner's explicit grant
        self.gw.ok("PATCH", "/v1/owner/bots/%s" % bot_id, {"policy": {"visitor": []}}, self.gw.owner_token)
        self.assertEqual(["faq"], self.gw.ok("GET", "/v1/bots/%s/contact" % bot_id, None, b)["capabilities"])
        cid_b = self.gw.conversation(bot_id, b)
        for kind, extra in (("slots", {"query": {"duration_minutes": 30}, "text": None}), ("chat", {"text": "hi"})):
            status, obj = self.gw.send(cid_b, b, kind=kind, **extra)
            self.assertEqual((403, "capability_not_granted"), (status, obj["error"]), kind)
        self.assertEqual(202, self.gw.send(cid_b, b, kind="faq", text="hours?")[0])
        cid_a = self.gw.conversation(bot_id, a)
        self.assertEqual(202, self.gw.send(cid_a, a, kind="slots", text=None, query={"duration_minutes": 30})[0])
        # explicit, bounded, fail-closed grants
        for caps, code in ((["shell"], "unknown_capability"), (None, "missing_capabilities"),
                           (["faq", "faq"], "missing_capabilities"), ("faq", "missing_capabilities")):
            status, obj = self.gw.contact(bot_id, b_id, "approve", caps)
            self.assertEqual((400, code), (status, obj["error"]), caps)
        self.gw.ok("PATCH", "/v1/owner/bots/%s" % bot_id, {"policy": {"offered": ["faq"]}}, self.gw.owner_token)
        status, obj = self.gw.contact(bot_id, b_id, "grant", ["faq", "calendar_slots"])
        self.assertEqual((400, "capability_not_offered"), (status, obj["error"]))
        empty = self.gw.contact(bot_id, b_id, "grant", [])[1]
        self.assertEqual(([], []), (empty["granted"], empty["effective_capabilities"]))
        self.assertEqual(403, self.gw.send(cid_b, b, kind="faq", text="still?")[0])

    def test_identity_comes_from_the_credential_not_from_claims(self):
        bot_id = self.published()
        a_id, a = self.gw.actor("A")
        b_id, b = self.gw.actor("B")
        cid = self.gw.conversation(bot_id, b)
        for field, value in (("actor_id", a_id), ("from", "a.example.ts.net"), ("from_agent", "owner"),
                             ("author", "owner"), ("level", "friend"), ("capabilities", ["calendar_slots"]),
                             ("callback_url", "http://127.0.0.1:1/x"), ("webhook", "https://example.com/h")):
            status, obj = self.gw.send(cid, b, text="spoof", **{field: value})
            self.assertEqual((400, "unknown_field"), (status, obj["error"]), field)
        body = json.dumps({"op_key": "header-spoof-1", "kind": "faq", "text": "hi"}).encode()
        status, obj = raw(self.gw, "POST", "/v1/conversations/%s/messages" % cid, body,
                          {"Authorization": "Bearer " + b, "Content-Type": "application/json",
                           "Content-Length": str(len(body)), "X-Actor-Id": a_id, "X-Forwarded-User": "owner"})
        self.assertEqual(202, status)
        view = self.gw.owner_view(cid)
        self.assertEqual({b_id}, {m["actor_id"] for m in view["messages"]})
        self.assertEqual({"untrusted_external"}, {m["content_trust"] for m in view["messages"]})


class GrantChangeTests(GatewayCase):
    def completed_slots(self, bot_id, token, worker):
        cid = self.gw.conversation(bot_id, token)
        status, sent = self.gw.send(cid, token, kind="slots", text=None, query={"duration_minutes": 30})
        self.assertEqual(202, status, sent)
        item = self.gw.claim(worker)[0]
        self.assertEqual(200, self.gw.start_work(worker, item)[0])
        reply = {"text": "suggestions", "slots": [{"start": "2030-01-07T09:00:00Z", "end": "2030-01-07T09:30:00Z"}]}
        self.assertEqual(200, self.gw.finish(worker, item, reply=reply)[0])
        return cid, sent["message_id"]

    def assert_withheld(self, token, cid, mid, reason):
        status, msg = self.message(mid, token)
        self.assertEqual(200, status)
        self.assertNotIn("result", msg)
        self.assertEqual(reason, msg["result_withheld"])
        history = self.gw.ok("GET", "/v1/conversations/%s" % cid, None, token)
        self.assertNotIn("2030-01-07", json.dumps(history))

    def test_revoke_cancels_queued_and_inflight_work_and_closes_reads(self):
        bot_id = self.published()
        actor_id, token = self.friend(bot_id, ["faq", "calendar_slots"])
        worker = self.gw.worker(bot_id)
        verifier = self.gw.ok("POST", "/v1/owner/bots/%s/verifiers" % bot_id, {}, self.gw.owner_token)["token"]
        cid, done_mid = self.completed_slots(bot_id, token, worker)
        _, inflight = self.gw.send(cid, token, kind="slots", text=None, query={"duration_minutes": 30})
        item = self.gw.claim(worker)[0]
        ticket = self.gw.start_work(worker, item)[1]["capability_ticket"]
        _, queued = self.gw.send(cid, token, kind="faq", text="queued question")
        self.assertEqual(200, self.gw.call("POST", "/v1/capability/redeem", {"ticket": ticket}, verifier)[0])

        self.assertEqual(200, self.gw.contact(bot_id, actor_id, "revoke")[0])
        self.assertEqual(403, self.gw.call("POST", "/v1/capability/redeem", {"ticket": ticket}, verifier)[0])
        status, obj = self.gw.finish(worker, item, reply={"text": "late", "slots": []})
        self.assertEqual((409, "already_final"), (status, obj["error"]))
        view = {m["message_id"]: m for m in self.gw.owner_view(cid)["messages"]}
        self.assertEqual(("cancelled", "contact_revoked"), (view[inflight["message_id"]]["state"],
                                                            view[inflight["message_id"]]["code"]))
        self.assertNotIn("result", view[inflight["message_id"]], "no late result was stored")
        self.assertEqual("cancelled", view[queued["message_id"]]["state"])
        for path in ("/v1/conversations/%s" % cid, "/v1/messages/%s" % done_mid,
                     "/v1/conversations/%s/operations/%s" % (cid, "x" * 10)):
            self.assertEqual(404, self.gw.call("GET", path, None, token)[0], path)
        self.assertEqual("revoked", self.gw.ok("GET", "/v1/bots/%s/contact" % bot_id, None, token)["status"])
        self.assertEqual((403, "contact_revoked"), (lambda s, o: (s, o["error"]))(*self.gw.send(cid, token)))

        # re-approval is a new generation: nothing earlier is revived or readable again
        approved = self.gw.contact(bot_id, actor_id, "approve", ["faq"])[1]
        self.assertGreater(approved["generation"], item["relationship"]["generation"])
        self.assertEqual([], self.gw.claim(worker))
        self.assertEqual(404, self.gw.call("GET", "/v1/conversations/%s" % cid, None, token)[0])
        self.assertEqual((409, "conversation_closed"), (lambda s, o: (s, o["error"]))(*self.gw.send(cid, token)))
        fresh = self.gw.conversation(bot_id, token)
        self.assertEqual(202, self.gw.send(fresh, token, text="new start")[0])

    def test_narrowing_withholds_old_results_including_unfetched_ones(self):
        bot_id = self.published()
        worker = self.gw.worker(bot_id)
        owner = self.gw.owner_token
        # 1. per-contact scope removal
        a_id, a = self.friend(bot_id, ["faq", "calendar_slots"], "A")
        cid, mid = self.completed_slots(bot_id, a, worker)
        self.assertIn("result", self.message(mid, a)[1])
        self.gw.contact(bot_id, a_id, "grant", ["faq"])
        self.assert_withheld(a, cid, mid, "grant_changed")
        self.gw.contact(bot_id, a_id, "grant", ["faq", "calendar_slots"])
        self.assert_withheld(a, cid, mid, "grant_changed")          # regrant does not release the old result
        # 2. demotion of a friend whose result was never fetched
        b_id, b = self.friend(bot_id, ["calendar_slots"], "B")
        cid_b, mid_b = self.completed_slots(bot_id, b, worker)
        self.gw.contact(bot_id, b_id, "demote")
        self.assert_withheld(b, cid_b, mid_b, "grant_changed")
        self.gw.contact(bot_id, b_id, "approve", ["calendar_slots"])
        self.assert_withheld(b, cid_b, mid_b, "grant_changed")
        # 3. bot policy narrowing strips the capability from stored grants; widening never regrants
        c_id, c = self.friend(bot_id, ["faq", "calendar_slots"], "C")
        cid_c, mid_c = self.completed_slots(bot_id, c, worker)
        _, queued = self.gw.send(cid_c, c, kind="slots", text=None, query={"duration_minutes": 30})
        self.gw.ok("PATCH", "/v1/owner/bots/%s" % bot_id, {"policy": {"offered": ["faq", "chat"]}},
                   owner)
        self.assert_withheld(c, cid_c, mid_c, "grant_changed")
        state = {m["message_id"]: m for m in self.gw.owner_view(cid_c)["messages"]}[queued["message_id"]]
        self.assertEqual(("cancelled", "policy_changed"), (state["state"], state["code"]))
        self.gw.ok("PATCH", "/v1/owner/bots/%s" % bot_id,
                   {"policy": {"offered": ["faq", "calendar_slots", "chat"]}}, owner)
        contacts = {x["actor_id"]: x for x in self.gw.ok("GET", "/v1/owner/bots/%s/contacts" % bot_id, None, owner)["contacts"]}
        self.assertEqual(["faq"], contacts[c_id]["granted"], "re-offering regrants nobody")
        self.assertNotIn("calendar_slots", contacts[c_id]["effective_capabilities"])
        self.assert_withheld(c, cid_c, mid_c, "grant_changed")
        # the owner still sees what happened
        self.assertIn("result", {m["message_id"]: m for m in self.gw.owner_view(cid_c)["messages"]}[mid_c])

    def test_mute_stops_processing_but_keeps_history(self):
        bot_id = self.published()
        actor_id, token = self.friend(bot_id, ["faq"])
        worker = self.gw.worker(bot_id)
        cid = self.gw.conversation(bot_id, token)
        _, answered = self.gw.send(cid, token, text="first")
        item = self.gw.claim(worker)[0]
        self.gw.start_work(worker, item)
        self.gw.finish(worker, item)
        _, queued = self.gw.send(cid, token, text="second")
        self.gw.contact(bot_id, actor_id, "mute")
        self.assertEqual((403, "contact_muted"), (lambda s, o: (s, o["error"]))(*self.gw.send(cid, token)))
        self.assertEqual([], self.gw.claim(worker))
        msgs = {m["message_id"]: m for m in self.gw.ok("GET", "/v1/conversations/%s" % cid, None, token)["messages"]}
        self.assertEqual(("cancelled", "contact_muted"), (msgs[queued["message_id"]]["state"],
                                                          msgs[queued["message_id"]]["code"]))
        self.assertIn("result", msgs[answered["message_id"]], "mute is not revoke: history stays readable")
        self.gw.contact(bot_id, actor_id, "unmute")
        self.assertEqual("cancelled", self.message(queued["message_id"], token)[1]["state"], "never revived")
        self.assertEqual(202, self.gw.send(cid, token, text="third")[0])

    def test_unpublish_cancels_everything_and_republish_revives_nothing(self):
        bot_id = self.published()
        actor_id, token = self.friend(bot_id, ["faq"])
        worker = self.gw.worker(bot_id)
        cid = self.gw.conversation(bot_id, token)
        _, one = self.gw.send(cid, token, text="one")
        _, two = self.gw.send(cid, token, text="two")
        item = self.gw.claim(worker)[0]
        self.gw.start_work(worker, item)
        self.gw.ok("POST", "/v1/owner/bots/%s/unpublish" % bot_id, {}, self.gw.owner_token)
        self.assertEqual(404, self.gw.call("GET", "/v1/conversations/%s" % cid, None, token)[0])
        self.assertEqual(409, self.gw.finish(worker, item)[0])
        self.assertEqual({"items": [], "bot_published": False, "ok": True},
                         self.gw.ok("POST", "/v1/worker/claim", {}, worker))
        self.gw.ok("POST", "/v1/owner/bots/%s/publish" % bot_id, {}, self.gw.owner_token)
        self.assertEqual([], self.gw.claim(worker))
        states = {m["message_id"]: (m["state"], m["code"]) for m in self.gw.owner_view(cid)["messages"]}
        self.assertEqual({one["message_id"]: ("cancelled", "bot_unpublished"),
                          two["message_id"]: ("cancelled", "bot_unpublished")}, states)


class RetentionAndDecisionTests(GatewayCase):
    def test_reject_is_a_decision_with_a_cooldown_not_a_block(self):
        bot_id = self.published()
        actor_id, token = self.gw.actor("Asker")
        self.assertEqual(202, self.gw.call("POST", "/v1/bots/%s/contact-request" % bot_id, {"note": "hi"}, token)[0])
        self.assertEqual("rejected", self.gw.contact(bot_id, actor_id, "reject")[1]["request"])
        self.assertEqual(409, self.gw.contact(bot_id, actor_id, "reject")[0], "nothing pending to reject")
        status, obj = self.gw.call("POST", "/v1/bots/%s/contact-request" % bot_id, {}, token)
        self.assertEqual((429, "request_cooldown"), (status, obj["error"]))
        rel = self.gw.ok("GET", "/v1/bots/%s/contact" % bot_id, None, token)
        self.assertEqual(("visitor", "rejected"), (rel["relationship"], rel["request"]))
        cid = self.gw.conversation(bot_id, token)
        self.assertEqual(202, self.gw.send(cid, token, text="still a visitor")[0])

    def test_owner_deletion_and_retention_remove_stored_bodies(self):
        import sqlite3
        bot_id = self.published()
        actor_id, token = self.gw.actor()
        cid = self.gw.conversation(bot_id, token)
        secret = "DELETE-ME-%s" % C.new_token("ticket")[4:16]
        self.assertEqual(202, self.gw.send(cid, token, text=secret)[0])
        self.gw.ok("DELETE", "/v1/owner/conversations/%s" % cid, None, self.gw.owner_token)
        self.assertEqual(404, self.gw.call("GET", "/v1/conversations/%s" % cid, None, token)[0])
        self.assertEqual(404, self.gw.call("GET", "/v1/owner/conversations/%s" % cid, None, self.gw.owner_token)[0])
        self.assertEqual(409, self.gw.call("POST", "/v1/owner/conversations/%s/messages" % cid,
                                           {"op_key": "reply-after-delete", "text": "x"}, self.gw.owner_token)[0])
        db_path = self.gw.home / "gateway.sqlite3"

        def sql(statement, args=()):
            db = sqlite3.connect(str(db_path), timeout=15)   # always closed: a stray WAL connection skews the test
            try:
                db.execute(statement, args)
                db.commit()
            finally:
                db.close()
        sql("PRAGMA wal_checkpoint(TRUNCATE)")
        self.assertNotIn(secret.encode(), db_path.read_bytes(), "secure_delete overwrote the deleted body")
        # retention: finished messages older than retention_days are purged by the running gateway
        cid2 = self.gw.conversation(bot_id, token)
        old = "RETENTION-%s" % C.new_token("ticket")[4:16]
        _, sent = self.gw.send(cid2, token, text=old)
        worker = self.gw.worker(bot_id)
        item = self.gw.claim(worker)[0]
        self.gw.start_work(worker, item)
        self.gw.finish(worker, item)
        sql("UPDATE messages SET finished_ts=finished_ts-31*86400 WHERE id=?", (sent["message_id"],))
        self.assertTrue(wait_until(lambda: self.message(sent["message_id"], token)[0] == 404, 6))
        sql("PRAGMA wal_checkpoint(TRUNCATE)")
        self.assertNotIn(old.encode(), db_path.read_bytes())
        self.assertEqual(200, self.gw.call("GET", "/v1/conversations/%s" % cid2, None, token)[0],
                         "conversation metadata is kept; only bodies and results are purged")


class LeaseTests(GatewayCase):
    def queued(self, n=1, caps=("faq",)):
        bot_id = self.published()
        actor_id, token = self.friend(bot_id, list(caps))
        cid = self.gw.conversation(bot_id, token)
        mids = [self.gw.send(cid, token, text="q%d" % i)[1]["message_id"] for i in range(n)]
        return bot_id, token, cid, mids

    def test_concurrent_claims_never_share_a_message(self):
        bot_id, token, cid, mids = self.queued(n=8)
        workers = [self.gw.worker(bot_id) for _ in range(3)]
        got, lock = [], threading.Lock()

        def drain(w):
            while True:
                items = self.gw.claim(w, limit=2)
                if not items:
                    return
                with lock:
                    got.extend(i["message_id"] for i in items)
        threads = [threading.Thread(target=drain, args=(w,)) for w in workers]
        [t.start() for t in threads]
        [t.join(30) for t in threads]
        self.assertEqual(sorted(mids), sorted(got), "each message claimed exactly once")

    def test_expired_or_stale_leases_are_fenced(self):
        bot_id, token, cid, (mid,) = self.queued()
        worker = self.gw.worker(bot_id)
        first = self.gw.claim(worker, lease_seconds=1)[0]
        time.sleep(1.3)
        status, obj = self.gw.start_work(worker, first)
        self.assertEqual((409, "fenced", "lease_expired"), (status, obj["error"], obj["detail"]))
        second = self.gw.claim(worker, lease_seconds=30)[0]
        self.assertEqual((mid, first["lease"]["fence"] + 1), (second["message_id"], second["lease"]["fence"]))
        self.assertEqual(409, self.gw.start_work(worker, first)[0], "the earlier claim token is dead")
        wrong = dict(second, lease=dict(second["lease"], lease_id="x" * 43))
        self.assertEqual("lease_mismatch", self.gw.start_work(worker, wrong)[1]["detail"])
        stale = dict(second, lease=dict(second["lease"], fence=second["lease"]["fence"] - 1))
        self.assertEqual("stale_fence", self.gw.start_work(worker, stale)[1]["detail"])
        self.assertEqual(409, self.gw.finish(worker, second)[0], "finish before start")
        self.assertEqual(200, self.gw.start_work(worker, second)[0])
        self.assertEqual(200, self.gw.finish(worker, second)[0])
        self.assertEqual(True, self.gw.finish(worker, second)[1]["duplicate"])
        status, obj = self.gw.finish(worker, second, reply={"text": "different"})
        self.assertEqual((409, "already_final"), (status, obj["error"]))
        self.assertEqual("synthetic answer", self.message(mid, token)[1]["result"]["text"])

    def test_slow_body_cannot_finish_after_the_lease_expired(self):
        bot_id, token, cid, (mid,) = self.queued()
        worker = self.gw.worker(bot_id)
        item = self.gw.claim(worker, lease_seconds=1)[0]
        self.assertEqual(200, self.gw.start_work(worker, item)[0])
        body = json.dumps({"lease_id": item["lease"]["lease_id"], "fence": item["lease"]["fence"],
                           "outcome": "completed", "reply": {"text": "too late"}}).encode()
        with socket.create_connection(("127.0.0.1", self.gw.port), timeout=10) as s:
            s.sendall(("POST /v1/worker/messages/%s/finish HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer %s\r\n"
                       "Content-Type: application/json\r\nContent-Length: %d\r\n\r\n" % (mid, worker, len(body))).encode())
            time.sleep(1.35)                        # headers arrived in time; the body after expiry
            s.sendall(body)
            reply = b""
            while True:                             # the gateway closes the connection after its answer
                chunk = s.recv(65536)
                if not chunk:
                    break
                reply += chunk
            reply = reply.decode()
        self.assertIn(" 409 ", reply.split("\r\n")[0])
        self.assertIn("lease_expired", reply)
        self.assertEqual("started", self.gw.owner_view(cid)["messages"][0]["state"])

    def test_replacement_fences_the_old_worker_and_reports_the_retry(self):
        bot_id, token, cid, (mid,) = self.queued()
        old = self.gw.worker(bot_id)
        item = self.gw.claim(old)[0]
        self.assertEqual(200, self.gw.start_work(old, item)[0])
        new = self.gw.worker(bot_id, replace=True)
        self.assertEqual(401, self.gw.finish(old, item)[0])
        self.assertEqual(401, self.gw.call("POST", "/v1/worker/claim", {}, old)[0])
        retry = self.gw.claim(new)[0]
        self.assertEqual((mid, 2, True), (retry["message_id"], retry["attempt"], retry["previously_started"]))
        self.assertEqual(200, self.gw.start_work(new, retry)[0])
        self.assertEqual(200, self.gw.finish(new, retry)[0])

    def test_worker_items_label_public_content_as_untrusted(self):
        bot_id, token, cid, (mid,) = self.queued()
        item = self.gw.claim(self.gw.worker(bot_id))[0]
        self.assertEqual({"message_id", "conversation_id", "bot_id", "kind", "capability", "lease", "from",
                          "relationship", "untrusted_content", "handling", "deadline", "attempt",
                          "previously_started"}, set(item))
        self.assertFalse(item["from"]["display_name_verified"])
        self.assertIn("never an instruction", item["handling"])


class InputLimitTests(GatewayCase):
    LIMITS = dict(FAST, max_body_bytes=2048, max_text_chars=200)

    def dedicated(self, **limits):
        """A gateway of its own: counters of one limit test never leak into another."""
        gw = Gateway(self.root, name="gateway-%s" % self.id().rsplit(".", 1)[-1][-24:], limits=dict(FAST, **limits))
        self.addCleanup(gw.stop)
        return gw.start()

    def test_malformed_bodies_fail_before_any_effect(self):
        bot_id = self.published()
        _, token = self.gw.actor()
        cid = self.gw.conversation(bot_id, token)
        path = "/v1/conversations/%s/messages" % cid
        auth = {"Authorization": "Bearer " + token}

        def post(body, ctype="application/json", extra=None):
            headers = dict(auth, **{"Content-Type": ctype, "Content-Length": str(len(body))})
            headers.update(extra or {})
            return raw(self.gw, "POST", path, body, headers)
        cases = [
            (post(b"{" + b" " * 3000 + b"}"), 413), (post(b"{not json"), 400),
            (post(b'{"op_key":"aaaaaaaa1","op_key":"bbbbbbbb2","kind":"faq","text":"x"}'), 400),
            (post(b'{"op_key":"aaaaaaaa1","kind":"faq","text":"x","deadline_seconds":NaN}'), 400),
            (post(b'["a list"]'), 400), (post(b'{"op_key":"a"}', ctype="text/plain"), 415),
            (post(b'{"op_key":"aaaaaaaa1","kind":"faq","text":"\\ud800"}'), 400),
            (post(b'{"op_key":"aaaaaaaa1","kind":"faq","text":"bell\\u0007"}'), 400),
            (post(b'{"op_key":"aaaaaaaa1","kind":"faq","text":"rtl\\u202e"}'), 400),
            (post(json.dumps({"op_key": "aaaaaaaa1", "kind": "faq", "text": "y" * 201}).encode()), 400),
            (post(json.dumps({"op_key": "../../etc", "kind": "faq", "text": "y"}).encode()), 400),
            (post(b"[" * 1000 + b"]" * 1000), 400),
            (raw(self.gw, "POST", path, b"", dict(auth, **{"Content-Type": "application/json"})), 411),
            (raw(self.gw, "POST", path, b"", dict(auth, **{"Content-Type": "application/json",
                                                           "Transfer-Encoding": "chunked"})), 411)]
        for (status, obj), want in cases:
            self.assertEqual(want, status, obj)
            self.assertIs(False, obj.get("ok"))
        self.assertEqual([], self.gw.owner_view(cid)["messages"], "nothing was stored")
        self.assertEqual(401, post(b"{" + b" " * 3000 + b"}", extra={"Authorization": "Bearer x"})[0],
                         "authentication is checked before an oversized body is read")

    def test_trickling_clients_are_cut_off_and_concurrency_is_capped(self):
        gw = self.dedicated(read_timeout_seconds=2, max_concurrent_requests=4)
        started, closed = time.monotonic(), False
        with socket.create_connection(("127.0.0.1", gw.port), timeout=10) as s:
            s.sendall(b"POST /v1/actors HTTP/1.1\r\nHost: x\r\n")
            while time.monotonic() - started < 8 and not closed:
                try:
                    s.sendall(b"X-Slow: 1\r\n")           # every header line arrives well within a read timeout
                    closed = select_ready(s, 0.5) and s.recv(1024) == b""
                except OSError:
                    closed = True
        self.assertTrue(closed)
        self.assertLess(time.monotonic() - started, 4.5, "the total request budget, not a per-read timeout")
        self.assertTrue(wait_until(lambda: gw.call("GET", "/v1/health")[0] == 200, 6), "the slot was released")
        held = [socket.create_connection(("127.0.0.1", gw.port), timeout=5) for _ in range(4)]
        try:
            time.sleep(0.3)
            status, obj = gw.call("GET", "/v1/health")
            self.assertEqual((503, "busy"), (status, obj["error"]))
        finally:
            for s in held:
                s.close()
        self.assertTrue(wait_until(lambda: gw.call("GET", "/v1/health")[0] == 200, 6))

    def test_deadlines_expire_queued_work_without_execution(self):
        bot_id = self.published()
        _, token = self.gw.actor()
        cid = self.gw.conversation(bot_id, token)
        for bad in (0, 604801, "60", 1.5):
            self.assertEqual(400, self.gw.send(cid, token, deadline_seconds=bad)[0], bad)
        status, sent = self.gw.send(cid, token, text="short", deadline_seconds=1)
        self.assertEqual(202, status)
        worker = self.gw.worker(bot_id)
        msg = wait_until(lambda: (lambda m: m if m["state"] == "expired" else None)(
            self.message(sent["message_id"], token)[1]), 5)
        self.assertEqual(("expired", "deadline_before_start"), (msg["state"], msg["code"]))
        self.assertEqual([], self.gw.claim(worker))

    def test_caller_rate_and_conversation_limits(self):
        gw = self.dedicated(messages_per_minute_per_actor=3, conversations_per_contact=2, turns_per_conversation=5)
        bot_id = gw.bot("limited")
        _, token = gw.actor()
        if time.time() % 60 > 50:
            time.sleep(61 - time.time() % 60)          # keep the four sends inside one rate window
        cid = gw.conversation(bot_id, token)
        self.assertEqual([202, 202, 202, 429], [gw.send(cid, token, text="m%d" % i)[0] for i in range(4)])
        gw.conversation(bot_id, token)
        status, obj = gw.call("POST", "/v1/bots/%s/conversations" % bot_id, {"op_key": "third-conv-1"}, token)
        self.assertEqual((429, "too_many_conversations"), (status, obj["error"]))

    def test_new_identities_cannot_bypass_bot_and_gateway_admission(self):
        if time.time() % 3600 > 3590:
            time.sleep(3601 - time.time() % 3600)        # keep the hourly windows from resetting mid-test
        gw = self.dedicated(new_contacts_per_hour_per_bot=2, enroll_per_hour=4, enroll_per_hour_per_ip=100)
        bot_id = gw.bot("admission")
        tokens = [gw.actor("Sybil %d" % i)[1] for i in range(3)]
        gw.conversation(bot_id, tokens[0])
        gw.conversation(bot_id, tokens[1])
        status, obj = gw.call("POST", "/v1/bots/%s/conversations" % bot_id, {"op_key": "sybil-conv-1"}, tokens[2])
        self.assertEqual((429, "rate_limited"), (status, obj["error"]), "a fresh identity is still a new contact")
        status, obj = gw.call("POST", "/v1/bots/%s/contact-request" % bot_id, {}, tokens[2])
        self.assertEqual((429, "rate_limited"), (status, obj["error"]))
        self.assertEqual([201, 429], [gw.call("POST", "/v1/actors", {"display_name": "x%d" % i})[0] for i in range(2)])
        self.assertEqual(3 + 1, json.loads(subprocess.run(
            [sys.executable, str(PUBLIC / "bot_gateway.py"), "--gateway-home", str(gw.home), "status", "--json"],
            capture_output=True, text=True, env=plain_env(self.root)).stdout)["actors"])

    def test_pending_work_is_bounded_per_contact_and_per_bot(self):
        gw = self.dedicated(pending_per_contact=2, pending_per_bot=3, messages_per_minute_per_actor=50)
        bot_id = gw.bot("bounded")
        tokens = [gw.actor("Caller %d" % i)[1] for i in range(2)]
        convs = [gw.conversation(bot_id, t) for t in tokens]
        first = [gw.send(convs[0], tokens[0], text="a%d" % i)[1].get("error") for i in range(3)]
        self.assertEqual([None, None, "too_many_pending"], first)
        second = [gw.send(convs[1], tokens[1], text="b%d" % i)[1].get("error") for i in range(2)]
        self.assertEqual([None, "mailbox_full"], second)
        worker = gw.worker(bot_id)
        item = gw.claim(worker)[0]
        gw.start_work(worker, item)
        gw.finish(worker, item)
        self.assertEqual(202, gw.send(convs[1], tokens[1], text="after drain")[0])

    def test_unknown_kinds_scopes_and_callbacks_fail_closed(self):
        owner = self.gw.owner_token
        bot_id = self.published()
        _, token = self.gw.actor()
        cid = self.gw.conversation(bot_id, token)
        for kind in ("exec", "shell", "agent", "attachment", "meeting_write", ""):
            status, obj = self.gw.send(cid, token, kind=kind)
            self.assertEqual((400, "unknown_kind"), (status, obj["error"]), kind)
        for body in ({"handle": "callback-bot", "display_name": "x", "avatar_url": "http://127.0.0.1/a.png"},
                     {"handle": "callback-bot", "display_name": "x", "webhook": "http://169.254.169.254/"},
                     {"handle": "callback-bot", "display_name": "x", "policy": {"friend": {"capabilities": ["shell"]}}},
                     {"handle": "callback-bot", "display_name": "x", "policy": {"admin": True}}):
            self.assertEqual(400, self.gw.call("POST", "/v1/owner/bots", body, owner)[0], body)
        actor_id = self.gw.owner_view(cid)["actor_id"]
        self.assertEqual(400, self.gw.contact(bot_id, actor_id, "admin")[0])
        self.assertEqual(400, self.gw.contact(bot_id, actor_id, "mute", ["faq"])[0])
        worker = self.gw.worker(bot_id)
        self.assertEqual(400, self.gw.call("POST", "/v1/worker/claim", {"scope": "all"}, worker)[0])
        self.assertEqual(400, self.gw.call("POST", "/v1/worker/claim", {"max": 1000}, worker)[0])
        self.gw.send(cid, token, text="hello")
        item = self.gw.claim(worker)[0]
        self.gw.start_work(worker, item)
        for outcome, code, reply in (("failed", "shell_error", None), ("completed", "no_answer", {"text": "x"}),
                                     ("completed", None, {"text": "x", "slots": []}),
                                     ("completed", None, {"text": "x", "url": "http://127.0.0.1/"}),
                                     ("escalated", None, {"text": "x"})):
            self.assertEqual(400, self.gw.finish(worker, item, outcome=outcome, code=code, reply=reply)[0],
                             (outcome, code, reply))
        self.assertEqual("started", self.gw.owner_view(cid)["messages"][0]["state"])


class DurabilityTests(GatewayCase):
    def test_receipt_follows_commit_and_claims_survive_hard_restarts(self):
        bot_id = self.published()
        _, token = self.gw.actor()
        cid = self.gw.conversation(bot_id, token)
        status, sent = self.gw.send(cid, token, text="durable", op_key="durable-op-1")
        self.assertEqual(202, status)
        worker = self.gw.worker(bot_id)
        self.gw.restart(hard=True)                  # SIGKILL right after the receipt
        self.assertEqual("queued", self.message(sent["message_id"], token)[1]["state"])
        status, again = self.gw.send(cid, token, text="durable", op_key="durable-op-1")
        self.assertEqual((200, sent["message_id"], True), (status, again["message_id"], again["duplicate"]))
        status, obj = self.gw.send(cid, token, text="changed", op_key="durable-op-1")
        self.assertEqual((409, "op_key_conflict"), (status, obj["error"]))
        item = self.gw.claim(worker, lease_seconds=60)[0]
        self.assertEqual(200, self.gw.start_work(worker, item)[0])
        self.gw.restart(hard=True)                  # claimed and started, not finished
        self.assertEqual("started", self.message(sent["message_id"], token)[1]["state"])
        self.assertEqual(200, self.gw.finish(worker, item)[0])
        self.gw.restart()
        self.assertEqual("completed", self.message(sent["message_id"], token)[1]["state"])
        self.assertEqual(1, len(self.gw.owner_view(cid)["messages"]), "retries never created a second message")


def select_ready(sock, timeout):
    import select
    return bool(select.select([sock], [], [], timeout)[0])


if __name__ == "__main__":
    unittest.main()
