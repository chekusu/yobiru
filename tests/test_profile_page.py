"""The share link's pages: activity counts on the card, the page for people (icons, 16 languages, nothing addressed to
agents in what people see) and the plain page for agents that ask for HTML.

Real bot_gateway.py subprocesses with synthetic homes on ephemeral loopback ports; the native host is played by the
test through the binding credential.
"""
import base64
import html
import json
import re
import struct
import tempfile
import unittest
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

from support.public_harness import FriendsGateway

import bot_agents as A
import bot_gateway as G
import bot_links as L
import bot_profile as P
import bot_profile_i18n as I18N

LIMITS = {"min_deadline_seconds": 1, "read_timeout_seconds": 5, "messages_per_minute_per_actor": 1000,
          "messages_per_hour_per_actor": 5000, "follows_per_hour_per_bot": 1000, "new_followers_per_hour_per_bot": 1000,
          "new_conversations_per_hour_per_bot": 1000, "new_contacts_per_hour_per_bot": 1000}
CHROME = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"
CLAUDE = "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Claude-User/1.0; +Claude-User@anthropic.com)"
BROWSER_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"


def visible(page):
    """What a person can see: the body without comments, scripts and tags."""
    body = page.split("</head>", 1)[1]
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    body = re.sub(r"<script\b.*?</script>", "", body, flags=re.S)
    return html.unescape(re.sub(r"<[^>]+>", " ", body))


class ProfilePageCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="yobiru profile page ")
        cls.gw = FriendsGateway(Path(cls._tmp.name), limits=LIMITS).start()

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls._tmp.cleanup()

    def handle(self, tag):
        return ("p%s%s" % (self.id().rsplit(".", 1)[-1][-18:], tag)).replace("_", "-").lower()[:32]

    def page(self, path, user_agent=CHROME, accept=BROWSER_ACCEPT, lang="en"):
        status, hdrs, body = self.gw.raw("GET", path, headers={"Accept": accept, "User-Agent": user_agent,
                                                                "Accept-Language": lang})
        return status, hdrs, body.decode("utf-8")

    def stats(self, ref):
        return self.gw.ok("GET", "/@" + ref)["stats"]


class AgentCommentTests(unittest.TestCase):
    def card(self, gateway):
        address = gateway + "/@alice"
        return {"display_name": "Alice", "handle": "@alice", "bot_id": "bot_profile_comment",
                "address": address, "connect": "yobiru public connect " + address,
                "api": {"mcp": gateway + "/mcp"}}

    def comments(self, page):
        parser = HTMLParser()
        comments = []
        parser.handle_comment = comments.append
        parser.feed(page)
        parser.close()
        return comments

    def test_agent_comment_preserves_gateway_urls(self):
        for gateway in ("https://xn--bcher-kva.example", "https://my--gateway.example",
                        "https://bots.example/profile--gateway"):
            with self.subTest(gateway=gateway):
                card = self.card(gateway)
                page = P.render_profile(card, "en", "/@alice", "test-nonce").decode()
                comments = self.comments(page)
                self.assertEqual(1, len(comments))
                self.assertIn(card["connect"], comments[0])
                self.assertIn(card["address"] + ".json", comments[0])
                self.assertIn(card["api"]["mcp"], comments[0])

    def test_agent_comment_cannot_end_early_or_expose_markup(self):
        for delimiter in ("-->", "--!>", "<!--nested-->"):
            with self.subTest(delimiter=delimiter):
                card = self.card("https://bots.example")
                card["connect"] += delimiter + '<b>comment-breakout-marker</b>'
                page = P.render_profile(card, "en", "/@alice", "test-nonce").decode()
                comments = self.comments(page)
                self.assertEqual(1, len(comments))
                self.assertEqual(P.agent_note(card), html.unescape(comments[0]).strip())
                self.assertNotIn("comment-breakout-marker", visible(page))


class StatsTests(ProfilePageCase):
    def test_counts_messages_followers_and_friends(self):
        a_id, a_src = self.gw.owned_bot(self.handle("a"))
        b_id, b_src = self.gw.owned_bot(self.handle("b"))
        c_id, c_src = self.gw.owned_bot(self.handle("c"))
        wtoken, _ = self.gw.native(a_id)
        self.gw.follow(b_src, a_id)
        self.gw.follow(a_src, b_id)                       # a and b are friends
        self.gw.follow(c_src, a_id)                       # c only follows a
        self.assertEqual({"messages": 0, "followers": 2, "friends": 1}, self.stats(a_id))
        self.assertEqual({"messages": 0, "followers": 1, "friends": 1}, self.stats(b_id))
        self.assertEqual({"messages": 0, "followers": 0, "friends": 0}, self.stats(c_id))

        cid = self.gw.conversation(a_id, c_src)
        mid = self.gw.chat(cid, c_src, "a question")      # c sent one
        self.assertEqual(1, self.stats(c_id)["messages"])
        self.assertEqual(0, self.stats(a_id)["messages"], "receiving is not sending; a draft is not an answer")
        self.gw.answer(a_id, wtoken, mid, "an answer")    # a answered it
        self.gw.ok("POST", "/v1/owner/conversations/%s/messages" % cid, {"op_key": "owner-note-1", "text": "hi"},
                   self.gw.owner_token)                   # and a's owner wrote in a's conversation
        status, _ = self.gw.send(cid, c_src, kind="notice", text="thanks")
        self.assertEqual(202, status)
        self.assertEqual(2, self.stats(a_id)["messages"])
        self.assertEqual(2, self.stats(c_id)["messages"])
        self.assertEqual(0, self.stats(b_id)["messages"])

        self.gw.sql("DELETE FROM messages")               # retention: the counts stay
        self.assertEqual({"messages": 2, "followers": 2, "friends": 1}, self.stats(a_id))
        self.gw.unfollow(b_src, a_id)
        self.assertEqual({"messages": 2, "followers": 1, "friends": 0}, self.stats(a_id))

        _, _, text = self.page("/@%s?lang=en" % self.handle("a"))
        self.assertIn("<li><b>2</b><span>Messages</span></li><li><b>1</b><span>Followers</span></li>"
                      "<li><b>0</b><span>Friends</span></li>", text)

    def test_upgrade_backfills_once_from_stored_messages(self):
        a_id, _ = self.gw.owned_bot(self.handle("a"))
        c_id, c_src = self.gw.owned_bot(self.handle("c"))
        cid = self.gw.conversation(a_id, c_src)
        for n in range(3):
            self.assertEqual(202, self.gw.send(cid, c_src, kind="notice", text="n%d" % n)[0])
        before = self.stats(c_id)["messages"]
        self.gw.stop()
        for name in ("bot_stats_sent", "bot_stats_owner", "bot_stats_answer"):
            self.gw.sql("DROP TRIGGER %s" % name)
        self.gw.sql("DROP TABLE bot_stats")               # a database from before this version
        self.gw.start()
        self.assertEqual(before, self.stats(c_id)["messages"])
        self.assertEqual(3, before)
        self.gw.stop()
        self.gw.start()                                   # a restart neither recounts nor drops the triggers
        self.assertEqual(3, self.stats(c_id)["messages"])
        self.assertEqual(202, self.gw.send(cid, c_src, kind="notice", text="later")[0])
        self.assertEqual(4, self.stats(c_id)["messages"])


class AudienceTests(ProfilePageCase):
    def test_people_and_agents_see_different_pages(self):
        h = self.handle("x")
        bot_id, _ = self.gw.owned_bot(h)
        card = self.gw.ok("GET", "/@" + h)

        status, hdrs, text = self.page("/@" + h)
        self.assertEqual(200, status)
        self.assertTrue(hdrs["content-type"].startswith("text/html"))
        self.assertIn("User-Agent", hdrs["vary"])
        seen = visible(text)
        for agent_only in ("yobiru public connect", bot_id, "/mcp", "AI agents", "bot_id"):
            self.assertNotIn(agent_only, seen, agent_only)
        self.assertIn('data-copy="Use Yobiru to contact %s at %s"' % (card["handle"], card["address"]), text)
        self.assertIn("Have your agent reach out", seen)
        self.assertIn('class="acts"', text)
        self.assertIn("yobiru public connect " + card["address"], text.split("<!--", 1)[1].split("-->", 1)[0])

        status, hdrs, agent = self.page("/@" + h, user_agent=CLAUDE)
        self.assertEqual(200, status)
        self.assertTrue(hdrs["content-type"].startswith("text/html"))
        self.assertIn("This version of the page is for AI agents", agent)
        self.assertIn("yobiru public connect " + card["address"], visible(agent))
        self.assertIn("followers: 0", agent)
        self.assertNotIn("<style", agent)
        self.assertNotIn('class="acts"', agent)
        self.assertIn('<script type="application/json" id="yobiru-card">', agent)

        self.assertIn('class="acts"', self.page("/@%s?format=html" % h, user_agent=CLAUDE)[2],
                      "an explicit format=html is the page for people")
        for ua in ("curl/8.4.0", "python-requests/2.32", CLAUDE):
            status, hdrs, body = self.page("/@" + h, user_agent=ua, accept="*/*")
            self.assertTrue(hdrs["content-type"].startswith("application/json"), ua)
            self.assertEqual(card, json.loads(body), ua)
        for ua in ("curl/8.4.0", "Mozilla/5.0 (compatible; GPTBot/1.2; +https://openai.com/gptbot)",
                   "Grok/1.0", "python-httpx/0.27", "node-fetch/1.0"):
            self.assertTrue(P.is_agent(ua), ua)
        for ua in (CHROME, "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like "
                   "Gecko) Version/18.0 Mobile/15E148 Safari/604.1", "Mozilla/5.0 (compatible; Googlebot/2.1)",
                   "Slackbot-LinkExpanding 1.0", "Twitterbot/1.0", None, ""):
            self.assertFalse(P.is_agent(ua), ua)

    def test_the_top_corner_shows_the_yobiru_mark(self):
        h = self.handle("x")
        self.gw.owned_bot(h)
        wordmark = ('<a class="wordmark" href="%s" dir="ltr" aria-label="Yobiru"><img src="%s" width="24" height="24" '
                    'alt=""><span>yobiru</span></a>' % (P.SITE, P.BRAND_MARK))
        status, hdrs, text = self.page("/@" + h)
        self.assertIn(wordmark, text)
        self.assertIn("img-src 'self' data:", hdrs["content-security-policy"])
        self.assertIn(wordmark, self.page("/@nobody-at-all")[2], "the page for a missing bot has it too")
        self.assertNotIn("<img", self.page("/@" + h, user_agent=CLAUDE)[2], "the agent page stays plain")

        png = base64.b64decode(P.BRAND_MARK.split(",", 1)[1])
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual((72, 72), struct.unpack(">II", png[16:24]), "24 px at 3x")
        self.assertLess(len(png), 4096)

    def test_icons_show_who_can_do_what(self):
        h = self.handle("p")
        bot_id, _ = self.gw.owned_bot(h)
        self.gw.policy(bot_id, offered=["faq", "calendar_slots", "chat"], visitor=["faq"],
                       follower=["faq", "chat"], friend=["faq", "calendar_slots", "chat"], accept_followers=False)
        _, _, text = self.page("/@%s?lang=zh" % h)
        states = dict((act, state) for state, act in re.findall(r'class="act s-(\w+)" type="button" data-act="(\w+)"',
                                                               text))
        self.assertEqual({"chat": "follower", "follow": "off", "calendar_slots": "friend", "files": "off"}, states,
                         "no separate leave-a-message button: a message is a chat (0.7.14)")
        self.assertIn("暂不接受关注", text)
        self.assertIn("关注之后可以用", text)
        self.assertNotIn('class="on"', text, "no agent badge without a binding")
        self.gw.native(bot_id)
        self.assertIn('class="on"', self.page("/@%s?lang=zh" % h)[2])

    def test_independent_presets_match_gateway_access(self):
        # Relationship presets replace each other; following (or becoming friends)
        # can remove a capability as well as grant it.
        cases = (
            ((), "off", "Not open right now"),
            (("visitor",), "visitor", "Only visitors (not following this bot)"),
            (("follower",), "follower_only", "Only one-way followers (not mutual friends)"),
            (("friend",), "friend", "For friends (mutual follows)"),
            (("visitor", "follower"), "visitor_follower",
             "Visitors and one-way followers only (not mutual friends)"),
            (("visitor", "friend"), "visitor_friend",
             "Visitors and mutual friends only (not one-way followers)"),
            (("follower", "friend"), "follower", "Open once you follow this bot"),
            (("visitor", "follower", "friend"), "all", "Open to everyone"),
        )
        caps = ["calendar_slots", "files", "chat"]
        for n, (allowed, state, explanation) in enumerate(cases):
            with self.subTest(allowed=allowed):
                h = self.handle("a%d" % n)
                bot_id, source = self.gw.owned_bot(h)
                caller_id, caller = self.gw.owned_bot(self.handle("b%d" % n))
                cid = self.gw.conversation(bot_id, caller)
                self.gw.policy(bot_id, offered=caps, **{
                    level: caps if level in allowed else [] for level in ("visitor", "follower", "friend")})
                for level in ("visitor", "follower", "friend"):
                    if level == "follower":
                        self.assertEqual(201, self.gw.follow(caller, bot_id)[0])
                    elif level == "friend":
                        self.assertEqual(201, self.gw.follow(source, caller_id)[0])
                    contact = self.gw.relation(caller, bot_id)
                    self.assertEqual(level, contact["relationship"])
                    self.assertEqual(set(caps) if level in allowed else set(), set(contact["capabilities"]))
                    status, result = self.gw.send(cid, caller, kind="chat", text="Synthetic permission check")
                    self.assertEqual(202 if level in allowed else 403, status, result)
                    if level not in allowed:
                        self.assertEqual("capability_not_granted", result["error"])

                status, _, page = self.page("/@" + h)
                self.assertEqual(200, status)
                for cap in caps:
                    button = re.search(r'<button[^>]+data-act="%s"[^>]+>' % cap, page).group(0)
                    self.assertIn('class="act s-%s"' % state, button)
                    self.assertIn('aria-label="%s: %s"' % (I18N.T["en"]["acts"][cap], explanation), button)
                    note = re.search(r'<p class="n" data-for="%s" hidden>(.*?)</p>' % cap, page).group(1)
                    self.assertIn(explanation, html.unescape(note))


class LanguageTests(ProfilePageCase):
    def test_every_language_has_every_text(self):
        self.assertEqual(16, len(I18N.LANGS))
        en = I18N.T["en"]
        for lang in I18N.LANGS:
            t = I18N.T[lang]
            self.assertEqual(set(en), set(t), lang)
            for key, value in en.items():
                if isinstance(value, dict):
                    self.assertEqual(set(value), set(t[key]), (lang, key))
                else:
                    self.assertEqual(value.count("%s"), t[key].count("%s"), (lang, key))

    def test_pages_in_sixteen_languages(self):
        h = self.handle("l")
        self.gw.owned_bot(h, policy={
            "offered": ["chat", "calendar_slots", "files"],
            "visitor": ["calendar_slots", "files"],
            "follower": ["chat", "calendar_slots"], "friend": ["files"],
        })
        for lang in I18N.LANGS:
            status, _, text = self.page("/@%s?lang=%s" % (h, lang))
            self.assertEqual(200, status, lang)
            self.assertIn('<html lang="%s" dir="%s">' % (P.html_lang(lang), "rtl" if lang == "ar" else "ltr"), text)
            self.assertIn(html.escape(I18N.T[lang]["stats"]["followers"]), text, lang)
            self.assertEqual(16, text.count('<link rel="alternate" hreflang='), lang)
            for cap, state in (("chat", "follower_only"), ("calendar_slots", "visitor_follower"),
                               ("files", "visitor_friend")):
                label = "%s: %s" % (I18N.T[lang]["acts"][cap], I18N.T[lang]["st_long"][state])
                self.assertIn('aria-label="%s"' % html.escape(label, quote=True), text, (lang, cap))
                self.assertIn(html.escape(I18N.T[lang]["st"][state]), text, (lang, state))
        for header, lang in (("ko-KR,ko;q=0.9,en;q=0.5", "ko"), ("pt-BR,pt;q=0.9", "pt-BR"), ("sv-SE,sv", "en"),
                             ("zh-TW,zh;q=0.9", "zh-CN"), ("ar-EG", "ar")):
            self.assertIn('<html lang="%s"' % lang, self.page("/@" + h, lang=header)[2], header)
        status, _, text = self.page("/@nobody-at-all?lang=vi")
        self.assertEqual(404, status)
        self.assertIn(I18N.T["vi"]["notfound_title"], text)

    def test_compact_counts(self):
        self.assertEqual(["0", "999", "1,234", "12.3K", "1.2M"], [P.compact(n, "en") for n in (0, 999, 1234, 12345,
                                                                                           1234567)])
        self.assertEqual(["9,999", "1.2万", "123.4万"], [P.compact(n, "zh") for n in (9999, 12345, 1234567)])
        self.assertEqual(["1.234", "1.2만"], [P.compact(1234, "de"), P.compact(12345, "ko")])


class LinkTests(ProfilePageCase):
    def test_unsafe_link_edits_leave_public_links_unchanged(self):
        h = self.handle("u")
        bot_id, _ = self.gw.owned_bot(h)
        path = "/v1/owner/bots/%s" % bot_id
        links = [{"label": "Website", "url": "https://example.com/profile"}]
        self.gw.ok("PATCH", path, {"links": links}, self.gw.owner_token)
        for url in ("mailto:owner%40example.com?bcc=hidden@example.net",
                    "mailto:owner%40example.com%3Fbcc=hidden@example.net",
                    "mailto:owner%2Cother@example.com", "mailto:owner%0D%0ABcc=hidden@example.net",
                    "https://evil.example\\.instagram.com/login"):
            with self.subTest(url=url):
                status, obj = self.gw.call("PATCH", path, {"links": [{"label": "Contact", "url": url}]},
                                           self.gw.owner_token)
                self.assertEqual((400, "invalid_links"), (status, obj.get("error")))
                self.assertEqual(links, self.gw.ok("GET", "/@" + h)["links"])

    def test_unsafe_stored_links_are_not_clickable_after_upgrade(self):
        h = self.handle("u")
        bot_id, _ = self.gw.owned_bot(h)
        safe = {"label": "Website", "url": "https://example.com/profile"}
        self.gw.ok("PATCH", "/v1/owner/bots/%s" % bot_id, {"links": [safe]}, self.gw.owner_token)
        unsafe = [{"label": "Instagram", "url": "https://evil.example\\.instagram.com/login"},
                  {"label": "Mail", "url": "mailto:owner%40example.com?bcc=hidden@example.net"}]
        # Simulate links persisted by the older validator. Rendering must apply the current rules too.
        stored = [safe] + unsafe
        self.gw.sql("UPDATE bot_profiles SET links=? WHERE bot_id=?", (json.dumps(stored), bot_id))
        for user_agent in (CHROME, CLAUDE):
            with self.subTest(user_agent=user_agent):
                status, _, text = self.page("/@" + h, user_agent=user_agent)
                self.assertEqual(200, status)
                self.assertIn('href="%s"' % safe["url"], text)
                for link in unsafe:
                    self.assertNotIn('href="%s"' % html.escape(link["url"], quote=True), text)
                self.assertNotIn('data-platform="instagram"', text)
        self.assertEqual(stored, self.gw.ok("GET", "/@" + h)["links"], "rendering does not rewrite stored data")

    def test_each_link_shows_the_icon_of_where_it_goes(self):
        h = self.handle("k")
        bot_id, _ = self.gw.owned_bot(h)
        links = [{"label": "X", "url": "https://x.com/me"}, {"label": "小红书", "url": "https://xhslink.com/a1"},
                 {"label": "Music", "url": "https://music.youtube.com/channel/1"},
                 {"label": "Instagram", "url": "https://phish.example/login"},
                 {"label": "Mail", "url": "mailto:me@example.com"}, {"label": "Call", "url": "tel:+81-90-1234-5678"},
                 {"label": "me.example", "url": "https://me.example/"}]
        links += [{"label": "Post %d" % i, "url": "https://example.com/%d" % i} for i in range(12 - len(links))]
        self.assertEqual(links, self.gw.ok("PATCH", "/v1/owner/bots/%s" % bot_id, {"links": links},
                                           self.gw.owner_token)["links"])
        status, _, text = self.page("/@" + h)
        self.assertEqual(200, status)
        rows = re.findall(r'<a class="row" href="([^"]+)" rel="noopener nofollow ugc me" data-platform="([a-z_]+)">'
                          r'<span class="mk"><svg class="(b|i)"', text)
        self.assertEqual([("https://x.com/me", "x", "b"), ("https://xhslink.com/a1", "xiaohongshu", "b"),
                          ("https://music.youtube.com/channel/1", "youtubemusic", "b"),
                          ("https://phish.example/login", "website", "i"), ("mailto:me@example.com", "email", "i"),
                          ("tel:+81-90-1234-5678", "phone", "i"), ("https://me.example/", "website", "i")],
                         rows[:7], "the icon follows the URL, never the label")
        self.assertEqual(12, len(rows))
        self.assertIn(L._MARKS["x"], text)
        self.assertIn('<bdi class="to" dir="ltr">phish.example/login</bdi>', text, "where it really goes is shown")
        self.assertNotIn('<bdi class="to" dir="ltr">me.example</bdi>', text, "not when the label already says it")
        agent = self.page("/@" + h, user_agent=CLAUDE)[2]
        self.assertIn('<li>Instagram (Website): <a href="https://phish.example/login">', agent)
        self.assertIn('<li>X (X): <a href="https://x.com/me">', agent)

    def test_services_labels_and_input(self):
        ids = [p[0] for p in L.PLATFORMS]
        self.assertEqual(len(ids), len(set(ids)))
        hosts = [host for p in L.PLATFORMS for host in p[3].split()]
        self.assertEqual(len(hosts), len(set(hosts)), "each host names one service")
        for pid, label, mark, _ in L.PLATFORMS:
            self.assertTrue(label, pid)
            if mark:
                view_box, path = L.mark(pid)
                self.assertRegex(path, r"^[MmZzLlHhVvCcSsQqTtAa0-9.,\- ]+\Z", pid)
        for url, pid in (("https://www.x.com/a", "x"), ("https://twitter.com/a", "x"), ("https://m.youtube.com/a",
                         "youtube"), ("https://music.youtube.com/a", "youtubemusic"), ("https://v.douyin.com/a",
                         "douyin"), ("https://mp.weixin.qq.com/s/a", "wechat_official"), ("https://qq.com/", "website"),
                         ("https://mastodon.example/@a", "mastodon"), ("https://notx.com/a", "website"),
                         ("https://x.com.evil.example/", "website"), ("https://bots.yobiru.com/@a", "yobiru")):
            self.assertEqual(pid, L.platform_of(url), url)
        self.assertEqual(["X", "抖音", "me@example.com", "example.org"],
                         [L.default_label(u) for u in ("https://x.com/a", "https://douyin.com/a",
                                                       "mailto:me@example.com", "https://www.example.org/a")])
        self.assertEqual(["https://x.com/me", "https://a.example/b", "mailto:me@example.com", "tel:+1-555-123-4567",
                          "tel:090-1234-5678"],
                         [L.normalize(v) for v in ("x.com/me", "http://a.example/b", " me@example.com ",
                                                   "+1 (555) 123-4567", "090 1234 5678")])
        for bad in ("@me", "javascript:alert(1)", "https://localhost/", "mailto:me@example.com?cc=x@example.com",
                    "tel:+1", "data:text/html,hi", "https://a.example/" + "x" * 300):
            with self.assertRaises(ValueError, msg=bad):
                L.normalize(bad)
        self.assertEqual(["x", "xiaohongshu", "wechat_official", None],
                         [L.platform_named(n) for n in ("Twitter", "小红书", "公众号", "myspace")])



class AgentTests(ProfilePageCase):
    def test_agents_the_owner_shows_are_on_the_card_and_the_page(self):
        h = self.handle("g")
        bot_id, _ = self.gw.owned_bot(h)
        path = "/v1/owner/bots/%s" % bot_id
        self.assertEqual([], self.gw.ok("GET", "/@" + h)["agents"])
        self.gw.native(bot_id)
        _, _, text = self.page("/@%s?lang=zh" % h)
        self.assertIn('<span class="on" role="img" aria-label="主人的 agent 已接入"', text, "no agents shown: the spark")
        self.assertNotIn('id="agents"', text)

        owner = self.gw.ok("PATCH", path, {"agents": ["grok", "claude_code", "codex"]}, self.gw.owner_token)
        self.assertEqual(["grok", "claude_code", "codex"], [a["id"] for a in owner["agents"]])
        card = self.gw.ok("GET", "/@" + h)
        self.assertEqual([{"id": "grok", "name": "Grok Bot"}, {"id": "claude_code", "name": "Claude Code"},
                          {"id": "codex", "name": "Codex"}], card["agents"])
        _, _, text = self.page("/@%s?lang=zh" % h)
        view_box, _, (mark,), _ = A.mark("grok")
        badge = re.search(r'<a class="on ag" href="#agents" aria-label="([^"]+)" title="[^"]+"><svg class="b" '
                          r'viewBox="%s" fill-rule="evenodd" aria-hidden="true"><path d="([^"]+)"/>'
                          % re.escape(view_box), text)
        self.assertEqual(("已连接的 agent：Grok Bot", mark), badge.groups(), "the first agent is on the picture")
        self.assertEqual(["grok", "claude_code", "codex"], re.findall(r'<li data-agent="([a-z_]+)">', text))
        self.assertIn('<h2 id="agents-h">已连接的 agent</h2>', text)
        self.assertIn("Claude Code", visible(text))
        self.assertLess(text.index('id="agents"'), text.index('class="card connect"'))
        agent = self.page("/@" + h, user_agent=CLAUDE)[2]
        self.assertIn("agents the owner shows (the owner&#x27;s statement): Grok Bot, Claude Code, Codex", agent)

        self.gw.ok("PATCH", path, {"agents": []}, self.gw.owner_token)
        self.assertEqual([], self.gw.ok("GET", "/@" + h)["agents"])
        self.assertNotIn('id="agents"', self.page("/@" + h)[2])

    def test_agent_lists_are_checked(self):
        h = self.handle("c")
        bot_id, _ = self.gw.owned_bot(h)
        path = "/v1/owner/bots/%s" % bot_id
        self.gw.ok("PATCH", path, {"agents": ["muse"]}, self.gw.owner_token)
        for bad in ("grok", ["grok", "grok"], ["skynet"], [1], list(A.IDS[:A.MAX_AGENTS + 1])):
            with self.subTest(agents=bad):
                status, obj = self.gw.call("PATCH", path, {"agents": bad}, self.gw.owner_token)
                self.assertEqual((400, "invalid_agents"), (status, obj.get("error")))
        self.assertEqual([{"id": "muse", "name": "Muse"}], self.gw.ok("GET", "/@" + h)["agents"])
        # An id a later version added and this one does not know (after a rollback) is left out, never drawn.
        self.gw.sql("UPDATE bot_profiles SET agents=? WHERE bot_id=?", (json.dumps(["future", "muse"]), bot_id))
        self.assertEqual([{"id": "muse", "name": "Muse"}], self.gw.ok("GET", "/@" + h)["agents"])
        self.assertEqual(["muse"], re.findall(r'<li data-agent="([a-z_]+)">', self.page("/@" + h)[2]))

    def test_upgrade_adds_the_agents_column_once(self):
        h = self.handle("o")
        bot_id, _ = self.gw.owned_bot(h)
        links = [{"label": "X", "url": "https://x.com/me"}]
        self.gw.ok("PATCH", "/v1/owner/bots/%s" % bot_id, {"links": links}, self.gw.owner_token)
        self.gw.stop()
        self.gw.sql("ALTER TABLE bot_profiles DROP COLUMN agents")      # a database from before this version
        self.gw.start()
        self.gw.stop()
        self.gw.start()                                                 # a restart adds nothing twice
        card = self.gw.ok("GET", "/@" + h)
        self.assertEqual((links, []), (card["links"], card["agents"]))
        self.gw.ok("PATCH", "/v1/owner/bots/%s" % bot_id, {"agents": ["gemini"]}, self.gw.owner_token)
        self.assertEqual(["gemini"], [a["id"] for a in self.gw.ok("GET", "/@" + h)["agents"]])

    def test_catalog_names_and_marks(self):
        self.assertEqual(len(A.IDS), len(set(A.IDS)))
        self.assertEqual(set(A.IDS), set(A._MARKS))
        self.assertEqual(A.IDS + ("other",), G.HOST_KINDS, "a binding's host kind and a shown agent share names")
        for aid in A.IDS:
            view_box, rule, paths, faint = A.mark(aid)
            self.assertIn(view_box, ("0 0 24 24", "0 0 100 100", "-9.94 -9.94 248.41 248.41"),
                          aid)
            self.assertIn(rule, ("evenodd", "nonzero"), aid)
            self.assertTrue(paths, aid)
            for d in paths:
                self.assertRegex(d, r"^[MmZzLlHhVvCcSsQqTtAa0-9.,\- ]+\Z", aid)
            self.assertTrue(all(0 <= i < len(paths) for i in faint), aid)
        names = [A.named(n) for n in ("Grok", "grok bot", "Claude Code", "claude-code", "CLAUDE_CODE", "Meta Muse",
                                      "OpenAI Codex", "ChatGPT", "通义千问", "豆包", "clawdbot", "myspace", "")]
        self.assertEqual(["grok", "grok", "claude_code", "claude_code", "claude_code", "muse", "codex", "chatgpt",
                          "qwen", "doubao", "openclaw", None, None], names)
        owners = {}
        for aid, name, aliases in A.AGENTS:
            for n in [aid, name] + [x for x in aliases.split(",") if x.strip()]:
                owners.setdefault(A._key(n), set()).add(aid)
        self.assertEqual({}, {k: v for k, v in owners.items() if len(v) > 1}, "no name means two agents")
        for lang in I18N.LANGS:
            self.assertIn("%s", I18N.T[lang]["agent_badge"], lang)

class LinkValidationTests(unittest.TestCase):
    def test_dotted_numbers_require_an_explicit_destination_type(self):
        for value in ("202.555.0123", "1.202.555.0123", "202-555.0123", "192.168.0.1"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "https://.*tel:"):
                    L.normalize(value)
        for value, expected in (("tel:202.555.0123", "tel:202-555-0123"),
                                ("TEL:1.202.555.0123", "tel:1-202-555-0123"),
                                ("tel:192.168.0.1", "tel:192-168-0-1"),
                                ("https://192.168.0.1", "https://192.168.0.1"),
                                ("http://192.168.0.1", "https://192.168.0.1"),
                                ("192.168.0.1/help", "https://192.168.0.1/help")):
            with self.subTest(value=value):
                self.assertEqual(expected, L.normalize(value))

    def test_tel_normalization_rejects_parameters_and_nonformatting_characters(self):
        for value in ("tel:+1-555-123-4567;ext=89", "tel:5550123;phone-context=+1-212",
                      "TEL:+1-555-123-4567;ext=89", "tel:+1-555-123-4567?ext=89",
                      "tel:+1-555-123-4567#89", "tel:+1-555-123-4567%2389",
                      "tel:+1-555-123-4567x89", "tel:+1-555-123-4567/89",
                      "tel:+1-555-123-4567,89", "tel:+1-555-123-4567*89",
                      "tel:+1-555-123-4567+89", "tel:++1-555-123-4567",
                      "tel:+1-555-123\t4567", "tel:+1-555-123\n4567",
                      "tel:+1-555-123-4567\u0668\u0669"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "tel:"):
                    L.normalize(value)

    def test_tel_normalization_preserves_formatted_subscriber_numbers(self):
        for value, expected in (("+1 (555) 123-4567", "tel:+1-555-123-4567"),
                                ("tel:+1 (555) 123-4567", "tel:+1-555-123-4567"),
                                ("TEL:+1.555.123.4567", "tel:+1-555-123-4567"),
                                ("tel:(555) 0123", "tel:555-0123"),
                                ("tel:090-1234-5678", "tel:090-1234-5678"),
                                ("tel:+15551234567", "tel:+15551234567")):
            with self.subTest(value=value):
                self.assertEqual(expected, L.normalize(value))
        for value in ("tel:12345", "tel:+123456789012345678901"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    L.normalize(value)

    def test_mailto_rejects_headers_and_decoded_recipient_delimiters(self):
        for url in ("mailto:owner%40example.com?bcc=hidden@example.net",
                    "mailto:owner%40example.com#hidden@example.net",
                    "mailto:owner@example.com?", "mailto:owner@example.com#",
                    "mailto:owner%2Chidden@example.net", "mailto:owner%3Bhidden@example.net",
                    "mailto:owner%40example.com%3Fbcc=hidden@example.net",
                    "mailto:owner%0D%0ABcc=hidden@example.net", "mailto:owner%00@example.net",
                    "mailto:owner%20name@example.net", "mailto:owner%FF@example.net",
                    "mailto:owner%2@example.net", "mailto:owner%GG@example.net"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    L.check_url(url)
                self.assertEqual("website", L.platform_of(url), "invalid old links must not get a service icon")

    def test_mailto_preserves_one_address_and_literal_local_characters(self):
        for url in ("mailto:owner@example.com", "mailto:alice=work@example.com",
                    "mailto:owner+notes@example.com", "mailto:owner%40example.com",
                    "mailto:question%3Fmark@example.com", "mailto:hash%23tag@example.com",
                    "mailto:100%25done@example.com"):
            with self.subTest(url=url):
                self.assertEqual(url, L.check_url(url))
                self.assertEqual("email", L.platform_of(url))
        for address, url in (("alice=work@example.com", "mailto:alice=work@example.com"),
                             ("first.last@example.com", "mailto:first.last@example.com"),
                             ("question?mark@example.com", "mailto:question%3Fmark@example.com"),
                             ("hash#tag@example.com", "mailto:hash%23tag@example.com"),
                             ("100%done@example.com", "mailto:100%25done@example.com")):
            with self.subTest(address=address):
                self.assertEqual(url, L.normalize(address))

    def test_backslash_in_authority_cannot_claim_a_service(self):
        # Browsers parse the backslash as a path separator and navigate to evil.example;
        # urllib.parse otherwise sees a hostname ending with .instagram.com / .x.com.
        for url in (r"https://evil.example\.instagram.com/login", r"https://evil.example\.x.com/me"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    L.check_url(url)
                with self.assertRaises(ValueError):
                    L.normalize(url)
                self.assertEqual("website", L.platform_of(url), "also protect links stored before upgrade")
        for url in ("https://www.instagram.com/me", "https://www.instagram.com/?next=https://other.example",
                    r"https://www.instagram.com/path\segment", "https://www.instagram.com/path%5Csegment"):
            with self.subTest(url=url):
                self.assertEqual(url, L.check_url(url))
                self.assertEqual("instagram", L.platform_of(url))

    def test_bare_url_keeps_query_and_fragment_destinations(self):
        for value in ("example.com/page?ref=https://friend.example", "example.com?ref=friend.example",
                      "example.com#ref=friend.example"):
            with self.subTest(value=value):
                self.assertEqual("https://" + value, L.normalize(value))

    def test_ambiguous_bare_email_or_url_requires_an_explicit_scheme(self):
        for value, mailto in (("alice.smith/notes@example.com", "mailto:alice.smith/notes@example.com"),
                              ("alice.smith?notes@example.com", "mailto:alice.smith%3Fnotes@example.com"),
                              ("alice.smith#notes@example.com", "mailto:alice.smith%23notes@example.com"),
                              ("example.com/page?email=alice@example.com",
                               "mailto:example.com/page%3Femail=alice@example.com")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "https://.*mailto:"):
                    L.normalize(value)
                self.assertEqual("https://" + value, L.normalize("https://" + value))
                self.assertEqual(mailto, L.normalize(mailto))
                self.assertEqual(value, urllib.parse.unquote(L.normalize(mailto)[7:]))


if __name__ == "__main__":
    unittest.main()
