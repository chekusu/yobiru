"""The share link's pages: activity counts on the card, the page for people (icons, 16 languages, nothing addressed to
agents in what people see) and the plain page for agents that ask for HTML.

Real bot_gateway.py subprocesses with synthetic homes on ephemeral loopback ports; the native host is played by the
test through the binding credential.
"""
import html
import json
import re
import tempfile
import unittest
from pathlib import Path

from support.public_harness import FriendsGateway

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

    def test_icons_show_who_can_do_what(self):
        h = self.handle("p")
        bot_id, _ = self.gw.owned_bot(h)
        self.gw.policy(bot_id, offered=["faq", "leave_message", "calendar_slots", "chat"], visitor=["faq"],
                       follower=["faq", "leave_message"], friend=["faq", "leave_message", "calendar_slots", "chat"],
                       accept_followers=False)
        _, _, text = self.page("/@%s?lang=zh" % h)
        states = dict((act, state) for state, act in re.findall(r'class="act s-(\w+)" type="button" data-act="(\w+)"',
                                                               text))
        self.assertEqual({"chat": "friend", "leave_message": "follower", "follow": "off", "calendar_slots": "friend",
                          "files": "off"}, states)
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
        caps = ["leave_message", "calendar_slots", "files", "chat"]
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
            "offered": ["chat", "leave_message", "calendar_slots", "files"],
            "visitor": ["chat", "calendar_slots", "files"],
            "follower": ["leave_message", "calendar_slots"], "friend": ["files"],
        })
        for lang in I18N.LANGS:
            status, _, text = self.page("/@%s?lang=%s" % (h, lang))
            self.assertEqual(200, status, lang)
            self.assertIn('<html lang="%s" dir="%s">' % (P.html_lang(lang), "rtl" if lang == "ar" else "ltr"), text)
            self.assertIn(html.escape(I18N.T[lang]["stats"]["followers"]), text, lang)
            self.assertEqual(16, text.count('<link rel="alternate" hreflang='), lang)
            for cap, state in (("chat", "visitor"), ("leave_message", "follower_only"),
                               ("calendar_slots", "visitor_follower"), ("files", "visitor_friend")):
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


if __name__ == "__main__":
    unittest.main()
