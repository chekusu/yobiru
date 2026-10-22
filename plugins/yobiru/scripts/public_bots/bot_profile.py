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
import struct
import unicodedata
import urllib.parse

LANGS = ("zh", "en", "ja")
HTML_LANG = {"zh": "zh-CN", "en": "en", "ja": "ja"}
SITE = "https://yobiru.com"
AVATAR_MAX_BYTES = 65536                    # decoded; its base64 JSON body fits the attachment body limit
AVATAR_MIN_PX, AVATAR_MAX_PX = 32, 2048
MAX_LINKS, LINK_LABEL_MAX, LINK_URL_MAX = 5, 40, 300
REPO = "https://github.com/chekusu/yobiru"


# The default picture: the orange spirit of the yobiru logo on its dark ground (192 px PNG, inlined so the page
# needs no extra request).
DEFAULT_AVATAR = ("data:image/png;base64,"
                  "iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAMAAABlApw1AAAAwFBMVEUwMDAAAAD9WiErLi87OztVVVX+YCQTExL8WR4wMDCOQynTUiVsPCtONS0s"
                  "LCwwMDCtSSctLS0wMDAwMDAwMDAwMDAuLi4vLy8vLy9/f38uLi7ETiUEDA8vLy9RJRYpGROSORq1Rh3RTB1mLBncUR5+QCqkPxoAAAAAAAAAAAAA"
                  "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADe/D69AAAAQHRSTlP+AP/8CQP/"
                  "//+u/////xNP/yx40SuLUYvGAm7//7H///////////8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAC27+NQAACUVJREFUeNrdXYmWojoQLSEgOwjq"
                  "uLTdPdv7/098JICiiFRShcDknJ4zchTq5taSVEIFVlxNCKf6zyZNT37ZbLg2W34+pemm+oYjBNtjgeUuTiWQ2Ob5oYAXrTjk+bb+sjMXALU86a7V"
                  "53bZ2oLffbb9XSpaP50UgOpHZ7vzG0FhoDXf8Hdb58bdVADk05PUL+pOBmSrv1r4aUKmgQDAUdJfkD3fw8RFYXAmACCfud0VRsLfgSh2WwoEMNYd"
                  "kdKkb2OQNi3eCKDsrmSnHg8MTd1klxiyAEa9n+QMnf9AQ54YsQAGvS9yrs6/pyEXBiyAmfIwi99AMFAk0NWedBzxGwiprh6BXvfvi9HEryEUez0S"
                  "tBiQ2jOi+DUEqUcjACh7JS9Gl78iIdcgAc+A8N8gfg3BF/wMpG8Sv4aQsgIo+dy9Uf7KEpBqBCjnKQp4eysEyqEiABxX+7d2/5WEffloFgbyCeRX"
                  "j8wZGCj10IfJmj9sCDCk/lt/iu5vSPC3Q4YAA/JvYDr51aM3AwhgzvJjEMBr/ZlYfvX411oEc+5/DAcwc/kHEcCc9QejRdDr/zczkb/mwNEDcJyR"
                  "/DWCow4AZyVmJL8SRfRwAD0E+HOSX8XkHgrguQHPTP4KgcACEKvT3OSXCE5PEcAzA0jnJ79EkD4zg2cAEpwB29fW/TSOIScoAM6qAMRCkQ1xFAVZ"
                  "lp3Dqp2zLAiiKO4ukLEhKJ5QAF35fwzKb9txkIVW01zXrf+x5F+YBSWMEUDY8KOLAJ4YwOCNoswaamH2dwwQXTOAJwYwJH9gDTfJR5hFMTeGZAjA"
                  "cARAyV+BKP8UBtZo8BKAMxwB7MjSaNIysohvMUdGA+cVADHsfzLL0oVgBXw02OIFAIHwQGDpN9dSNHB5ItEHAOWBYqXaBhjCiImFe090r0KYFGho"
                  "eZZZC3lYKPpUCDcGsjPPFAAPCw9jojYDAvX72JgB2bKYgQTxlAGxynFjuMgyM4OahYCqRzbkLTu+ATgK7A0CAgDL8hj0SBy7ALAEvIrFWN3KaCTc"
                  "UXBjING4Q6TGOsZq5IZUS0g6DKBiWMuSz1gEnvfUawUUNWpHM9ByQa0ZAXpI542iRo8qpGEBL50R3sOS1KhlBQ0Dif6mtzikGEKJPqJoUXLHgFjt"
                  "9Rm1IbMsEoLAfLcd7GsKGgYORrcJSAAoCODQZuAoU+mTIMjMEWyrXCPo+1Dz+dmTyY4pgqsnrVQoMd5KQENgmSOAIrmqkJEJMyGwTBE0ZgxqIkBJ"
                  "RhPHdpahHcgEhdOoUEIbW2U0AKa+qAoFIDWImI22afHAMotocmYmFABndaEOb0MiApNRhQ0XqUOgPY57Ps2cAEE1ogO1oEqdIdECWtnORsTLxVcg"
                  "RLH7ZMvbDbmKZSUDyYEOgK5EkcFg8pBIBohOlMsTmZiBdKRACsOvKNDMfxlEZBWMgccElB173nuVSBkBOKsDD4CYCsDSlaM0gpUD9ChwpYAov8mg"
                  "SHqhDReA+OW8HkOPvh1vSgA517K8fXYN0it34Uxbh3JWAJH15mBQATgAlw7By17GWEioP7VnBCDXPqw3jyhKABvgA0DXIW1XuoE9I4CYOLk0oGDP"
                  "yQDYoUunQJcBzs1Z5NmxriOyS/EvnAAYjCDUc3wX4NzeR58VaFJgl+Lz7k8M6QCyKQHYZwYKNEZENnP/M8zLdD0p92vNgctgxlO+8zIAwOPWIXYA"
                  "kftyxRU3Ow7mCgA7vT9PCYC0djy9DrFEMou0/joLAMGsAYwwN54dA9MZwRQA7Pd7oeF4gDcC5tEQEwB0opd/NBqxaBA6lPEDCHgAhBoKdJkhALQV"
                  "yynladkATqxpFZ4JjdZggjWxNQWAPTMDZyYAf/EMMCZ3ZWqOCQA2kvFmp3nSKtoAck439F4AzCs0BgA8DgCsVqwpv9pX7RFUSC7yiekA9A/rsADE"
                  "im2h28wGvB4tQsWBaqGba6sBrw0gAfxQeyX2rAAM1vmevGIQ4wDIzR5M221IbrSLANenarsNz4Yn5qFEiHOi1YYnTiPgGsxlaBNg2vTHml7HetFm"
                  "09+KMRL0Aui/7BFG0/XeafLG18HstGv9+uO6Xvf1gbAnlmGc0G3jq+ArhNGT2Pr+Wq8/f7qPqH7Ly39cw6TEbesxqyMNn3To58d6vf74eERQil9e"
                  "/uqu7iNXOK6bv4nb74dm9e5/Uv6yfd1f/l1f/nQNNei2/Z4xGD/LbH2u6/bfXfBqrq7DTlYIHYYZXkHp3LYL4KsW9OO7ffXXFUBnpxpOg1qvoHDG"
                  "ssdQVjqZzwEAHSMAGxvFyK9hoXTou5H0T9vA3RrXx6cZAXevYZm+CIcbz7m1F/p27zW98kJdDcJl5Q70VxGxFJQOvxT/67tjq5KDz/AxwGU6Jkx5"
                  "GVRnSO39/NUdNLju+ad8n9QzIeDhZVCD13HHWiNAjuM6r+NyjugIySH0yoCgvZI+HgUxfiZAKAow3ioBNp2SkMoyIOzYHXEm1leW4choBfGIU2Fl"
                  "AUdCaZIRzSDWJ0C/OAzeDNxx5Ie+4jC8JQq1X9J18Qt7veV5cAWSxnFFHr7MRG+BJFSJqrHsQKPQx4sSVbyJXjtGhmRPo9TKyyJhqDJtWtOzrFTt"
                  "4b0psuIQ/qbCoRXK01QjTLpao+bTUKE8TKlCTRKCcMAdZRpF9AZLFaKKRbJCyDRrAA4Wi+T1RDWEKKs1ybtfGtOvJDlcrhNVMFUbQomhrrDqytqL"
                  "cj0j1JceVTAVWbLWAENV4zYIsvIvig2qqSJL1uKLBpuAIJQWRhcNXn7Z5sUXzl5+6fLlF49ffvn+xR+gsPwjLJZ/iMjyj3FZ/kE6yz/KaPmHSS3/"
                  "OK/lH6i2/CPtln+o4D9xrOPSD9Zc/tGmyz9c9p843nf5Bywv/Yjrf+KQ8aUf816RIPVoDAjyllJ7nNWYANT9Rc4PQd4uF9riGwBQFCd5lXPmy16X"
                  "4ie62mMK4KZIXIf7mCkPAYDqKpEWDDSo3xepMOp9AoCqu7Y7GoZK+t12Zdj7JADVM5P0chNFX3iAS5pQxCcBqDSpxOAXzRqMxnKN7HtfSS9IItAA"
                  "lE+Xnedsdz4gmWi+4e+26qeCKAAVwLUHRbrz7bvVpMfVpdsHf5eK1k+nBnDrR7HN88PLyVtxyPOtuHE3FwBKoEaiTZqe/LK1KLDl51Oabhq8gu2x"
                  "/wOmeJx9yPPmmwAAAABJRU5ErkJggg==")


def image_info(data):
    """(content type, width, height) of a PNG, JPEG or WebP read from its own bytes, never from a declared type.
    Anything else (SVG, GIF, HTML, truncated or oversized files) raises ValueError."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ValueError("empty image")
    if len(data) > AVATAR_MAX_BYTES:
        raise ValueError("larger than %d bytes" % AVATAR_MAX_BYTES)
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR" and len(data) >= 24:
        kind, (w, h) = "image/png", struct.unpack(">II", data[16:24])
    elif data[:3] == b"\xff\xd8\xff":
        kind, (w, h) = "image/jpeg", _jpeg_size(data)
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
        kind, (w, h) = "image/webp", _webp_size(data)
    else:
        raise ValueError("not a PNG, JPEG or WebP image")
    if not (AVATAR_MIN_PX <= w <= AVATAR_MAX_PX and AVATAR_MIN_PX <= h <= AVATAR_MAX_PX):
        raise ValueError("%dx%d: each side must be %d-%d pixels" % (w, h, AVATAR_MIN_PX, AVATAR_MAX_PX))
    return kind, w, h


def _jpeg_size(data):
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            raise ValueError("malformed JPEG")
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        if length < 2:
            raise ValueError("malformed JPEG")
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        i += 2 + length
    raise ValueError("malformed JPEG")


def _webp_size(data):
    chunk = data[12:16]
    if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
        w, h = struct.unpack("<HH", data[26:30])
        return w & 0x3FFF, h & 0x3FFF
    if chunk == b"VP8L" and data[20:21] == b"\x2f":
        b = data[21:25]
        return 1 + (((b[1] & 0x3F) << 8) | b[0]), 1 + (((b[3] & 0x0F) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
    if chunk == b"VP8X":
        return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
    raise ValueError("malformed WebP")


def strip_metadata(data, kind):
    """The image without EXIF, XMP, text or comment blocks (which can carry GPS positions, device names or
    editing history); pixel data is kept byte for byte. `kind` comes from image_info."""
    data = bytes(data)
    if kind == "image/jpeg":
        out, i = [data[:2]], 2
        while i + 4 <= len(data):
            marker = data[i + 1]
            if marker == 0xDA:                        # start of scan: the rest is image data
                out.append(data[i:])
                break
            length = struct.unpack(">H", data[i + 2:i + 4])[0]
            seg = data[i:i + 2 + length]
            if not (0xE1 <= marker <= 0xEF or marker == 0xFE):   # keep JFIF (APP0); drop APP1-15 and comments
                out.append(seg)
            i += 2 + length
        return b"".join(out)
    if kind == "image/png":
        out, i = [data[:8]], 8
        while i + 12 <= len(data):
            n = struct.unpack(">I", data[i:i + 4])[0]
            ctype = data[i + 4:i + 8]
            if ctype not in (b"eXIf", b"tEXt", b"iTXt", b"zTXt", b"tIME"):
                out.append(data[i:i + 12 + n])
            i += 12 + n
        return b"".join(out)
    if kind == "image/webp":
        out, i = [], 12
        while i + 8 <= len(data):
            n = struct.unpack("<I", data[i + 4:i + 8])[0]
            ctype, chunk = data[i:i + 4], data[i:i + 8 + n + (n & 1)]
            if ctype == b"VP8X":
                chunk = chunk[:8] + bytes([chunk[8] & ~0x0C & 0xFF]) + chunk[9:]   # clear the EXIF and XMP flags
            if ctype not in (b"EXIF", b"XMP "):
                out.append(chunk)
            i += 8 + n + (n & 1)
        body = b"WEBP" + b"".join(out)
        return b"RIFF" + struct.pack("<I", len(body)) + body
    raise ValueError("unsupported image type")


def clean_links(value):
    """An owner's link list: up to MAX_LINKS {"label", "url"} with plain https URLs. Returns the canonical list or
    raises ValueError with a short reason."""
    if not isinstance(value, list) or len(value) > MAX_LINKS:
        raise ValueError("links: a list of at most %d {label, url}" % MAX_LINKS)
    out = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"label", "url"}:
            raise ValueError("links: each item is exactly {label, url}")
        label, url = item["label"], item["url"]
        if not isinstance(label, str) or not label.strip() or len(label) > LINK_LABEL_MAX or \
                any(unicodedata.category(c) in ("Cc", "Cs", "Cf", "Zl", "Zp") for c in label):
            raise ValueError("links: label is 1-%d characters of plain text" % LINK_LABEL_MAX)
        if not isinstance(url, str) or len(url) > LINK_URL_MAX or any(c.isspace() or ord(c) < 33 or ord(c) > 126
                                                                      for c in url):
            raise ValueError("links: url is an https URL of at most %d ASCII characters" % LINK_URL_MAX)
        u = urllib.parse.urlsplit(url)
        if u.scheme != "https" or not u.hostname or "." not in u.hostname or u.username or u.password or \
                "@" in u.netloc:
            raise ValueError("links: url must be https://host/... with no credentials")
        out.append({"label": " ".join(unicodedata.normalize("NFC", label).split()), "url": url})
    return out


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
img.avatar{display:block;object-fit:cover;background:var(--bg-2);box-shadow:0 0 0 1px var(--line)}
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
    avatar = '<img class="avatar" src="%s" width="84" height="84" alt="">' % _e(card.get("avatar") or DEFAULT_AVATAR)
    own_links = "".join('<a class="btn" href="%s" rel="noopener nofollow ugc me">%s</a>' % (_e(l["url"]), _e(l["label"]))
                        for l in card.get("links") or [])
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
        '<main><section class="card hero">%(avatar)s'
        '<h1>%(name)s</h1><div class="handle">%(handle)s</div>%(desc)s'
        '<div class="chips">%(chips)s</div></section>'
        '<nav class="links" aria-label="%(links)s">%(own_links)s'
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
    ) % {"avatar": avatar, "own_links": own_links, "name": _e(name), "handle": _e(handle),
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
            '<meta name="yobiru:card" content="%(j)s">%(img)s'
            '<script type="application/json" id="yobiru-card">%(card)s</script>') % {
        "j": _e(json_url), "a": _e(address), "n": _e(name), "h": _e(handle), "d": _e(description),
        "card": _json_in_script(card),
        "img": '<meta property="og:image" content="%s">' % _e(card["avatar"]) if card.get("avatar") else ""}
    return _page(lang, t["title"] % name, description, head, body, nonce, _lang_links(path)).encode("utf-8")


def _linkify(text, urls):
    out = _e(text)
    for url in sorted({u for u in urls if u}, key=len, reverse=True):
        safe = _e(url)
        out = out.replace(safe, '<a href="%s">%s</a>' % (safe, safe), 1)
    return out


def render_not_found(lang, path, nonce):
    t = T[lang]
    body = ('<main><section class="card hero"><img class="avatar" src="%s" width="84" height="84" alt=""><h1>%s</h1>'
            '<p class="desc">%s</p></section><nav class="links"><a class="btn primary" href="%s">%s</a></nav>'
            '</main>') % (DEFAULT_AVATAR, _e(t["notfound_title"]), _e(t["notfound_body"]), SITE, _e(t["home"]))
    return _page(lang, t["notfound_title"], t["notfound_body"], '<meta name="robots" content="noindex">', body, nonce,
                 _lang_links(path)).encode("utf-8")


def new_nonce():
    return secrets.token_urlsafe(16)
