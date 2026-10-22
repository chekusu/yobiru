"""The human page behind a share link.

`/@handle` answers two audiences. Agents and the yobiru client ask for JSON (an `Accept` that does not prefer HTML,
`/@handle.json` or `?format=json`) and get the machine-readable card, unchanged. A browser asks for `text/html` and
gets a small profile page built only from that same public card: name, handle, description, what visitors may do,
how to connect, and a block addressed to agents that points at the JSON. Nothing beyond the card is ever shown.

Pure functions only (stdlib): the gateway decides which answer a request gets and sends the bytes.
"""
import html
import json
import secrets

LANGS = ("zh", "en", "ja")
HTML_LANG = {"zh": "zh-CN", "en": "en", "ja": "ja"}
SITE = "https://yobiru.com"
REPO = "https://github.com/chekusu/yobiru"


def _media_ranges(header):
    """[(type, q)] from an Accept header; malformed q values count as 1, q=0 entries are kept (they refuse)."""
    out = []
    for part in (header or "").split(","):
        bits = [b.strip() for b in part.split(";")]
        if not bits[0]:
            continue
        q = 1.0
        for b in bits[1:]:
            if b.lower().startswith("q="):
                try:
                    q = max(0.0, min(1.0, float(b[2:])))
                except ValueError:
                    q = 1.0
        out.append((bits[0].lower(), q))
    return out


def prefers_html(accept):
    """True only when the caller names HTML explicitly and ranks it above JSON. A missing Accept, `*/*` (curl,
    most HTTP libraries) or `application/json` (the yobiru client) keeps the JSON card; a tie goes to JSON."""
    html_q = json_q = 0.0
    for media, q in _media_ranges(accept):
        if media in ("text/html", "application/xhtml+xml"):
            html_q = max(html_q, q)
        elif media == "application/json":
            json_q = max(json_q, q)
    return html_q > 0 and html_q > json_q


def pick_lang(query_lang, accept_language):
    if query_lang in LANGS:
        return query_lang
    best, best_q = None, 0.0
    for tag, q in _media_ranges(accept_language):
        primary = tag.split("-", 1)[0]
        if primary in LANGS and q > best_q:
            best, best_q = primary, q
    return best or "en"


T = {
    "zh": {
        "title": "%s 的 Yobiru 主页",
        "bot": "Yobiru bot",
        "unverified": "身份未经实名核实",
        "owner_review": "每条回复都由主人确认后才发出",
        "agent_on": "主人的 agent 已接入",
        "agent_off": "主人的 agent 暂未接入，可以先留言",
        "how_title": "怎么联系 TA",
        "how_body": "把这个页面的链接发给你的 Grok Bot 或其他个人 agent，然后跟它说：",
        "how_prompt": "用 Yobiru 联系 %s，地址是 %s",
        "how_after": "它会替你建立联系、打招呼，之后的往来也由它帮你跟进。",
        "copy": "复制",
        "copied": "已复制",
        "links": "链接",
        "copy_link": "复制主页链接",
        "json_card": "机器可读名片（JSON）",
        "what_is": "什么是 Yobiru？",
        "get_own": "给自己的 agent 也开一个主页",
        "can_title": "TA 开放了什么",
        "levels": {"visitor": "访客", "follower": "关注者", "friend": "朋友"},
        "caps": {"faq": "常见问题", "leave_message": "留言", "calendar_slots": "约时间", "chat": "聊天",
                 "files": "文件"},
        "none": "暂不开放",
        "accepts": {"visitors": "欢迎访客", "followers": "可以关注", "contact": "接受联系请求"},
        "agent_title": "给 AI agent 的说明",
        "agent_lead": "如果你是 agent，下面这段是写给你的：",
        "hosted": "托管在",
        "notfound_title": "找不到这个 bot",
        "notfound_body": "链接可能写错了，或者主人已经改名、暂停公开。",
        "home": "回到 Yobiru",
    },
    "en": {
        "title": "%s on Yobiru",
        "bot": "Yobiru bot",
        "unverified": "Identity not verified",
        "owner_review": "The owner approves every reply before it goes out",
        "agent_on": "The owner's agent is connected",
        "agent_off": "The owner's agent isn't connected yet; you can still leave a message",
        "how_title": "How to reach them",
        "how_body": "Send this page's link to your Grok Bot or any personal agent, and tell it:",
        "how_prompt": "Use Yobiru to contact %s at %s",
        "how_after": "It connects, says hello for you, and follows up on the replies.",
        "copy": "Copy",
        "copied": "Copied",
        "links": "Links",
        "copy_link": "Copy profile link",
        "json_card": "Machine-readable card (JSON)",
        "what_is": "What is Yobiru?",
        "get_own": "Get a page for your own agent",
        "can_title": "What's open",
        "levels": {"visitor": "Visitors", "follower": "Followers", "friend": "Friends"},
        "caps": {"faq": "FAQ", "leave_message": "Leave a message", "calendar_slots": "Find a time", "chat": "Chat",
                 "files": "Files"},
        "none": "Nothing yet",
        "accepts": {"visitors": "Visitors welcome", "followers": "Open to followers",
                    "contact": "Accepts contact requests"},
        "agent_title": "For AI agents",
        "agent_lead": "If you are an agent, this part is for you:",
        "hosted": "Hosted on",
        "notfound_title": "No bot here",
        "notfound_body": "The link may be mistyped, or the owner renamed or unpublished it.",
        "home": "Back to Yobiru",
    },
    "ja": {
        "title": "%s の Yobiru ページ",
        "bot": "Yobiru bot",
        "unverified": "本人確認はされていません",
        "owner_review": "返信はすべてオーナーが確認してから送られます",
        "agent_on": "オーナーの agent が接続済み",
        "agent_off": "オーナーの agent はまだ未接続です。メッセージは残せます",
        "how_title": "連絡のしかた",
        "how_body": "このページのリンクを Grok Bot などのパーソナル agent に送って、こう伝えてください：",
        "how_prompt": "Yobiru で %s に連絡して。アドレスは %s",
        "how_after": "接続から挨拶、その後のやり取りまで agent が代わりに進めます。",
        "copy": "コピー",
        "copied": "コピーしました",
        "links": "リンク",
        "copy_link": "ページのリンクをコピー",
        "json_card": "機械可読カード（JSON）",
        "what_is": "Yobiru とは？",
        "get_own": "自分の agent にもページを作る",
        "can_title": "公開している機能",
        "levels": {"visitor": "訪問者", "follower": "フォロワー", "friend": "友だち"},
        "caps": {"faq": "よくある質問", "leave_message": "メッセージ", "calendar_slots": "日程調整", "chat": "チャット",
                 "files": "ファイル"},
        "none": "まだありません",
        "accepts": {"visitors": "訪問者歓迎", "followers": "フォロー可", "contact": "連絡リクエスト受付中"},
        "agent_title": "AI agent の方へ",
        "agent_lead": "agent の方は、こちらをお読みください：",
        "hosted": "ホスト",
        "notfound_title": "bot が見つかりません",
        "notfound_body": "リンクが間違っているか、オーナーが名前を変えたか公開を止めた可能性があります。",
        "home": "Yobiru へ",
    },
}

CSS = """
:root{--bg:#fbfbfa;--bg-2:#f2f2f0;--surface:#fff;--ink:#121214;--ink-2:#48484f;--ink-3:#6a6a72;
--line:rgba(18,18,20,.1);--line-2:rgba(18,18,20,.16);--accent:#ff5b1f;--accent-ink:#c2410c;
--mono:'IBM Plex Mono',ui-monospace,'SFMono-Regular',Menlo,Consolas,monospace;
--sans:'Instrument Sans','Helvetica Neue',Arial,sans-serif;color-scheme:light}
:lang(zh-CN){--sans:'Instrument Sans','Helvetica Neue',Arial,'PingFang SC','Hiragino Sans GB','Noto Sans SC',
'Microsoft YaHei',sans-serif}
:lang(ja){--sans:'Instrument Sans','Helvetica Neue',Arial,'Hiragino Sans','Hiragino Kaku Gothic ProN','Noto Sans JP',
'Yu Gothic UI',Meiryo,sans-serif}
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);font-size:16px;line-height:1.55;
font-weight:420;-webkit-font-smoothing:antialiased}
a{color:inherit}
.wrap{max-width:560px;margin:0 auto;padding:20px 16px 48px}
.top{display:flex;align-items:center;justify-content:space-between;margin-bottom:28px}
.wordmark{display:inline-flex;align-items:baseline;gap:.06em;font-size:22px;font-weight:640;letter-spacing:-.045em;
line-height:1;text-decoration:none}
.wordmark i{width:.22em;height:.22em;border-radius:50%;background:var(--accent);display:inline-block}
.lang{display:flex;gap:2px;font-size:13px}
.lang a{padding:5px 9px;border-radius:999px;text-decoration:none;color:var(--ink-3)}
.lang a[aria-current]{background:var(--surface);color:var(--ink);box-shadow:0 0 0 1px var(--line)}
.card{background:var(--surface);border:1px solid var(--line);border-radius:24px;padding:24px;margin-bottom:14px}
.hero{text-align:center;padding:32px 24px 26px}
.avatar{width:84px;height:84px;border-radius:50%;margin:0 auto 14px;display:grid;place-items:center;
font-size:34px;font-weight:600;color:#fff;background:radial-gradient(circle at 30% 25%,#ff8a57,var(--accent) 55%,#d9400c)}
h1{font-size:26px;line-height:1.2;letter-spacing:-.02em;font-weight:640;margin:0}
.handle{font-family:var(--mono);font-size:14px;color:var(--ink-3);margin-top:4px}
.desc{margin:14px auto 0;max-width:420px;color:var(--ink-2);white-space:pre-line;overflow-wrap:anywhere}
.chips{display:flex;flex-wrap:wrap;justify-content:center;gap:6px;margin-top:16px}
.chip{font-size:12.5px;padding:4px 10px;border-radius:999px;background:var(--bg-2);color:var(--ink-2)}
.chip.on::before{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--accent);
margin-right:6px;vertical-align:1px}
.links{display:grid;gap:10px;margin:0 0 14px}
.btn{display:flex;align-items:center;justify-content:center;gap:8px;min-height:52px;padding:12px 18px;
border-radius:16px;background:var(--surface);border:1px solid var(--line-2);text-decoration:none;font-weight:560;
font:inherit;font-weight:560;color:var(--ink);cursor:pointer;width:100%;transition:transform .15s,box-shadow .15s}
.btn:hover{transform:translateY(-1px);box-shadow:0 6px 18px rgba(18,18,20,.06)}
.btn.primary{background:var(--ink);color:#fff;border-color:var(--ink)}
h2{font-size:15px;font-weight:620;margin:0 0 10px;letter-spacing:-.01em}
p{margin:0 0 10px;color:var(--ink-2)}
.prompt{display:flex;gap:10px;align-items:flex-start;background:var(--bg-2);border-radius:14px;padding:12px 14px;
margin:4px 0 10px}
.prompt span{flex:1;font-size:15px;overflow-wrap:anywhere;color:var(--ink)}
.mini{flex:none;font:inherit;font-size:12.5px;padding:4px 10px;border-radius:999px;border:1px solid var(--line-2);
background:var(--surface);cursor:pointer;color:var(--ink-2)}
.levels{display:grid;gap:8px;margin:0;padding:0;list-style:none}
.levels li{display:flex;gap:12px;align-items:baseline;justify-content:space-between;border-top:1px solid var(--line);
padding-top:8px}
.levels li:first-child{border-top:0;padding-top:0}
.levels b{font-weight:560;font-size:14px;flex:none}
.levels span{font-size:14px;color:var(--ink-2);text-align:right}
.agent{background:transparent;border-style:dashed}
.agent pre{margin:8px 0 0;font-family:var(--mono);font-size:12.5px;line-height:1.6;white-space:pre-wrap;
overflow-wrap:anywhere;color:var(--ink-2)}
.agent a{color:var(--accent-ink)}
footer{text-align:center;font-size:12.5px;color:var(--ink-3);margin-top:22px}
footer a{color:var(--ink-3)}
@media (prefers-reduced-motion:reduce){.btn{transition:none}}
"""

JS = """
document.querySelectorAll('[data-copy]').forEach(function(b){b.addEventListener('click',function(){
var t=b.getAttribute('data-copy'),label=b.textContent;function done(){b.textContent=b.getAttribute('data-done');
setTimeout(function(){b.textContent=label},1600)}
if(navigator.clipboard){navigator.clipboard.writeText(t).then(done,function(){})}});});
"""


def _e(text):
    return html.escape("" if text is None else str(text), quote=True)


def _json_in_script(obj):
    """JSON safe inside <script type="application/json">: no '<', '>' or '&' can end the element."""
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _page(lang, title, description, head_extra, body, nonce, lang_links):
    t = T[lang]
    switch = "".join('<a href="%s" hreflang="%s"%s>%s</a>' % (_e(href), code, ' aria-current="true"' if code == lang
                                                               else "", label)
                     for code, label, href in lang_links)
    return ("<!doctype html>\n<html lang=\"%s\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>%s</title><meta name=\"description\" content=\"%s\">"
            "<meta name=\"theme-color\" content=\"#fbfbfa\">%s"
            "<style nonce=\"%s\">%s</style></head><body><div class=\"wrap\">"
            "<header class=\"top\"><a class=\"wordmark\" href=\"%s\">yobiru<i></i></a>"
            "<nav class=\"lang\" aria-label=\"Language\">%s</nav></header>%s"
            "<footer>%s <a href=\"%s\">Yobiru</a></footer></div>"
            "<script nonce=\"%s\">%s</script></body></html>\n") % (
        HTML_LANG[lang], _e(title), _e(description), head_extra, nonce, CSS, SITE, switch, body,
        _e(t["hosted"]), SITE, nonce, JS)


def _lang_links(path):
    return [(code, label, "%s?lang=%s" % (path, code)) for code, label in (("zh", "中文"), ("en", "EN"), ("ja", "日本語"))]


def agent_note(card):
    """Plain English for an agent that fetched the page instead of the card."""
    address, api = card["address"], card.get("api") or {}
    lines = [
        "This is the human-readable page of a Yobiru bot (%s, bot_id %s)." % (card["handle"], card["bot_id"]),
        "Machine-readable card: %s.json (or GET %s with Accept: application/json)." % (address, address),
        "To connect on the user's behalf: install Yobiru (%s) and run" % REPO,
        "  %s" % card.get("connect", "yobiru public connect " + address),
        "then follow the card's `api` section; the yobiru client handles enrollment and messages.",
    ]
    if api.get("mcp"):
        lines.append("MCP (Streamable HTTP, needs a credential from this gateway): %s" % api["mcp"])
    lines.append("Contacts are saved by bot_id; the owner approves every reply. The identity is not a verified person.")
    return "\n".join(lines)


def render_profile(card, lang, path, nonce):
    """The profile page for one published bot, from its public card only."""
    t = T[lang]
    name, handle, address = card["display_name"] or card["handle"], card["handle"], card["address"]
    json_url = address + ".json"
    initial = (name.strip()[:1] or "@").upper()
    chat = card.get("chat") or {}
    chips = ['<span class="chip%s">%s</span>' % (" on" if chat.get("agent_connected") else "",
                                                 _e(t["agent_on"] if chat.get("agent_connected") else t["agent_off"]))]
    for key, flag in (("visitors", card.get("accepts_visitors")), ("followers", card.get("accepts_followers")),
                      ("contact", card.get("accepts_contact_requests"))):
        if flag:
            chips.append('<span class="chip">%s</span>' % _e(t["accepts"][key]))
    levels = []
    for level in ("visitor", "follower", "friend"):
        caps = [t["caps"].get(c, c) for c in (card.get("capabilities") or {}).get(level) or []]
        levels.append("<li><b>%s</b><span>%s</span></li>" % (_e(t["levels"][level]),
                                                             _e(" · ".join(caps) if caps else t["none"])))
    prompt = t["how_prompt"] % (handle, address)
    body = (
        '<main><section class="card hero"><div class="avatar" aria-hidden="true">%(initial)s</div>'
        '<h1>%(name)s</h1><div class="handle">%(handle)s</div>%(desc)s'
        '<div class="chips">%(chips)s</div></section>'
        '<nav class="links" aria-label="%(links)s">'
        '<button class="btn primary" type="button" data-copy="%(address)s" data-done="%(copied)s">%(copy_link)s</button>'
        '<a class="btn" href="%(json_url)s" type="application/json">%(json_card)s</a>'
        '<a class="btn" href="%(site)s">%(what_is)s</a>'
        '<a class="btn" href="%(site)s/#friends">%(get_own)s</a></nav>'
        '<section class="card"><h2>%(how_title)s</h2><p>%(how_body)s</p>'
        '<div class="prompt"><span>%(prompt)s</span><button class="mini" type="button" data-copy="%(prompt)s" '
        'data-done="%(copied)s">%(copy)s</button></div><p>%(how_after)s</p>'
        '<p>%(review)s · %(unverified)s</p></section>'
        '<section class="card"><h2>%(can_title)s</h2><ul class="levels">%(levels)s</ul></section>'
        '<section class="card agent" id="for-agents"><h2>%(agent_title)s</h2><p>%(agent_lead)s</p>'
        '<pre>%(note)s</pre></section></main>'
    ) % {"initial": _e(initial), "name": _e(name), "handle": _e(handle),
         "desc": '<p class="desc">%s</p>' % _e(card["description"]) if card.get("description") else "",
         "chips": "".join(chips), "links": _e(t["links"]), "address": _e(address), "copied": _e(t["copied"]),
         "copy_link": _e(t["copy_link"]), "json_url": _e(json_url), "json_card": _e(t["json_card"]), "site": SITE,
         "what_is": _e(t["what_is"]), "get_own": _e(t["get_own"]), "how_title": _e(t["how_title"]),
         "how_body": _e(t["how_body"]), "prompt": _e(prompt), "copy": _e(t["copy"]), "how_after": _e(t["how_after"]),
         "review": _e(t["owner_review"]), "unverified": _e(t["unverified"]), "can_title": _e(t["can_title"]),
         "levels": "".join(levels), "agent_title": _e(t["agent_title"]), "agent_lead": _e(t["agent_lead"]),
         "note": _linkify(agent_note(card), (json_url, REPO, (card.get("api") or {}).get("mcp")))}
    description = card.get("description") or "%s · %s" % (handle, t["bot"])
    head = ('<link rel="alternate" type="application/json" href="%(j)s" title="Yobiru bot card">'
            '<link rel="canonical" href="%(a)s">'
            '<meta property="og:type" content="profile"><meta property="og:title" content="%(n)s (%(h)s)">'
            '<meta property="og:description" content="%(d)s"><meta property="og:url" content="%(a)s">'
            '<meta name="yobiru:card" content="%(j)s">'
            '<script type="application/json" id="yobiru-card">%(card)s</script>') % {
        "j": _e(json_url), "a": _e(address), "n": _e(name), "h": _e(handle), "d": _e(description),
        "card": _json_in_script(card)}
    return _page(lang, t["title"] % name, description, head, body, nonce, _lang_links(path)).encode("utf-8")


def _linkify(text, urls):
    out = _e(text)
    for url in sorted({u for u in urls if u}, key=len, reverse=True):
        safe = _e(url)
        out = out.replace(safe, '<a href="%s">%s</a>' % (safe, safe), 1)
    return out


def render_not_found(lang, path, nonce):
    t = T[lang]
    body = ('<main><section class="card hero"><div class="avatar" aria-hidden="true">?</div><h1>%s</h1>'
            '<p class="desc">%s</p></section><nav class="links"><a class="btn primary" href="%s">%s</a></nav>'
            '</main>') % (_e(t["notfound_title"]), _e(t["notfound_body"]), SITE, _e(t["home"]))
    return _page(lang, t["notfound_title"], t["notfound_body"], '<meta name="robots" content="noindex">', body, nonce,
                 _lang_links(path)).encode("utf-8")


def new_nonce():
    return secrets.token_urlsafe(16)
