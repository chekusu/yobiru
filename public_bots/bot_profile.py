"""The pages behind a share link.

`/@handle` answers three audiences. Agents and the yobiru client ask for JSON (an `Accept` that does not prefer HTML,
`/@handle.json` or `?format=json`) and get the machine-readable card, unchanged. A browser asks for `text/html` and
gets a small profile page for people, built only from that same public card: picture, name, activity counts, what a
visitor may do (as icons) and a sentence to hand one's own agent. Nothing addressed to agents is shown there; the page
carries it only where people never look (the alternate link, the embedded card, a comment). An AI agent or HTTP
library that asks for HTML (known `User-Agent` tokens) gets a plain English page written for agents instead.

Pure functions only (stdlib): the gateway decides which answer a request gets and sends the bytes.
"""
import html
import json
import re
import secrets
import struct
import unicodedata

import bot_agents as A
import bot_links as L
import bot_profile_i18n as I18N

LANGS = I18N.LANGS
SITE = "https://yobiru.com"
AVATAR_MAX_BYTES = 65536                    # decoded; its base64 JSON body fits the attachment body limit
AVATAR_MIN_PX, AVATAR_MAX_PX = 32, 2048
MAX_LINKS, LINK_LABEL_MAX, LINK_URL_MAX = 12, L.LABEL_MAX, L.URL_MAX
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

# The yobiru mark that yobiru.com shows beside its wordmark (public/brand/yobiru-mark.png there): the dark tile cut
# out of its cream square, 72 px for 24 px at 3x, inlined like the default picture.
BRAND_MARK = ("data:image/png;base64,"
              "iVBORw0KGgoAAAANSUhEUgAAAEgAAABICAMAAABiM0N1AAABgFBMVEUyMjEtLSz7+PL8WiEAAAA5OTgxMTAvLi1YWFaxSyZ4d3XZ19OJiIb/YCIbGxuP"
              "RCnq6ONVVVWpqKXKyMQ5OjnRUyVnZ2W3trI1NTRUNyyYl5QtLSxGRkUtLS0zMzNrPCt4QSwrKzA0NDLBwLwBCxEfKCri4dwtLS0uLi0vLy5LJRaCgX64"
              "USkXHiEgHx4gIB8yMjJVVQBhYF+NORu2RB3+XB/h39oAVQBVKlVISEhhLBd/f3+fQB2Bf3yjPxugn52jop/fUR8AAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
              "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
              "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABD7JVYAAAAgHRS"
              "TlP+/v//AK2Wl////////////wP//wn///9Q///S/0kx//8Scv////81caL////////aA///////AwYH/wL///////8AAAAAAAAAAAAAAAAAAAAAAAAA"
              "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAPE+yAwAAAQ1SURBVHjarZjpYto4EIBnLIcaH2AM5r5pmqTpvdvuvd19/5da"
              "SZbk0WFgQ+eHA471ee4RgriR0Sh+kfyh14G5NZkfe1zurpZe7zifmOUSxKnzO3iR3M3l8gY0ircvxEjUNp40oEk8h5tkLkjA9bmRI0ijGLhdcLNs4xHE"
              "k0+3gz5NuGnH2zkIxxgOCD9A8ACba14or1zO+Bt6Fzm7fl0zxur+cAedrCdYXVJnwbQUjPV3EEat4JKLcPiVtVIU9T64BLtA0iMor3XBqBT1Aq8F8dVJ"
              "Phtn2biawj30bRJjQ+wIiHczzyItWYo4dEDFO/TVD6jzXEZUKsS9R7oMwvU4cqRCGLrW9V2dXOMwKSNPUoQ+O+8nF4RfooB840+5IGbHztMnCsrUdzir"
              "tV+kg9DR7k0YtPxAElzL3rjYBgl/zcKcaMafYwGVMBw1nHZwRNzQczfbGQ+7oHEXKB+EQH00UbdA3QpFCV/geZsx4xlVmPpbl4eki0KgBToVrlxfdnDK"
              "dQdo3/ZODRIfOnIoeiMMC/moGLog+WGQd0SsSf8ASBUcOiCsQlZViYqMDfqq6w3b9cq2kK/HaRvXkEZDRA/Enzw5mGxK8+ze65MiIx1QMPoVWK9RUXv7"
              "u7y+VeEnIENyfJRTjHhKVu3Pr1//K681aW6tdxpQ7hSFXULYOOmnv+X1TzcfVUS8CqkGitI6u1GpKNSoZEXbbdFqSTSzM/Gf9PStzGZp8yoh+yLcRXRv"
              "0xVCvJ1+xERPpOyLdhatEj6RHJAKnfiWGoXsekk/aDsXNWu0qnfvdYzADT+xbTmwe246MPW0GPb5tmSP9/m4ao2mbURU21K3H/PJkEgxIN6/l8MvpzeB"
              "Nl6lUgleR1laDoFcK+6AdBKoVMoGfq/MUrNorXcGKQIENJIVIbv22LWsaUtVOk2SaT4rSQNuRhHamSsHfylBVXSFZOhlNhmRIuynwewakKwi2iEdUlZO"
              "r9KoBKqQA1JdFf/6HwphEATn5wD1ENl+O5lNR2Z2EZQgkJhZ4ae7/PyiYWht/HwQXBiWSv5pfOKOI2efJO6mZzlVUw5mttptBMgbzmwEmsozu0bT2n3L"
              "lBc79ziyxsiz6GU20k1TJ+m0RtvNBmR0RBJ/oer3UPqklhUEtNLTyPRNk0+YjAMz3ExC2h9xBa+0RcRxeiQgJkuTmuUpXyNZbBfIE/8p2t7zhxR+xPU0"
              "fX7mraidOUh36Aq0gcnKDG4rWZ2goFFG37Y8tJpA/AgK5Pzcac10HGsFS9165L/7f10BfSXdFQRWYFBWv4lDlq2ju/MNnI1UCPfQHLJsgq/2LLRstR7e"
              "iEMWceyzCaqss4m2wOBjm+bYRxxEHZ4AOsw/L+Ig6qAPouL4lzh+uHvZGUlvG8efncO67fyx13vFpdf8EX/NDXLH3D3OHw72YR2Xzy88PjTHjv8BaT87"
              "OV+JxJMAAAAASUVORK5CYII=")


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
    """An owner's link list: up to MAX_LINKS {"label", "url"} with plain https URLs, mailto: addresses or tel:
    numbers (bot_links.check_url). Returns the canonical list or raises ValueError with a short reason."""
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
        L.check_url(url)
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


# AI assistants, their crawlers and plain HTTP libraries. Only consulted when the caller asked for HTML: they then get
# the agent page instead of the one for people. Search engines and link previews are not listed (they get the
# people's page, whose title, description and picture are what they show).
AGENT_UA = re.compile(
    r"\b(?:ChatGPT-User|GPTBot|OAI-SearchBot|Claude-User|Claude-SearchBot|ClaudeBot|Claude-Web|anthropic-ai|"
    r"Perplexity-User|PerplexityBot|Grok|xAI|MistralAI-User|DuckAssistBot|YouBot|cohere-ai|Meta-ExternalAgent|"
    r"Meta-ExternalFetcher|Amazonbot|Bytespider|CCBot|Gemini|yobiru|python-|aiohttp|httpx|curl/|Wget/|node-fetch|"
    r"undici|axios/|Go-http-client|okhttp|HTTPie|libwww-perl|Deno/|Bun/)", re.I)


def is_agent(user_agent):
    return bool(user_agent) and AGENT_UA.search(user_agent) is not None


def html_lang(lang):
    return I18N.HTML_LANG.get(lang, lang)


# ---------------------------------------------------------------- icons (24 px line icons, drawn by the page's CSS)
_ICONS = {
    "chat": '<path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z"/>',
    "mail": '<rect width="20" height="16" x="2" y="4" rx="2"/>'
                     '<path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/>',
    "follow": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/>'
              '<path d="M19 8v6M22 11h-6"/>',
    "calendar_slots": '<rect width="18" height="18" x="3" y="4" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
    "files": '<path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1'
             '-2.83-2.83l8.49-8.48"/>',
    "share": '<path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"/><path d="m16 6-4-4-4 4"/><path d="M12 2v13"/>',
    "copy": '<rect width="14" height="14" x="8" y="8" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 '
            '2 .9 2 2"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "globe": '<circle cx="12" cy="12" r="10"/><path d="M2 12h20"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 '
             '1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>',
    "down": '<path d="m6 9 6 6 6-6"/>',
    "out": '<path d="M7 7h10v10"/><path d="M7 17 17 7"/>',
    "phone": '<path d="M13.83 16.57a1 1 0 0 0 1.21-.3l.36-.47a2 2 0 0 1 1.6-.8h3a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2A18 18 '
             '0 0 1 2 4a2 2 0 0 1 2-2h3a2 2 0 0 1 2 2v3a2 2 0 0 1-.8 1.6l-.47.35a1 1 0 0 0-.29 1.23 14 14 0 0 0 6.39 '
             '6.39"/>',
    "lock": '<rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    "user": '<circle cx="12" cy="8" r="5"/><path d="M20 21a8 8 0 0 0-16 0"/>',
    "off": '<circle cx="12" cy="12" r="10"/><path d="m4.9 4.9 14.2 14.2"/>',
    "spark": '<path d="M12 2.5l2.2 6.3 6.3 2.2-6.3 2.2-2.2 6.3-2.2-6.3-6.3-2.2 6.3-2.2z"/>',
    "braces": '<path d="M8 3H7a2 2 0 0 0-2 2v5a2 2 0 0 1-2 2 2 2 0 0 1 2 2v5c0 1.1.9 2 2 2h1"/>'
              '<path d="M16 21h1a2 2 0 0 0 2-2v-5c0-1.1.9-2 2-2a2 2 0 0 1-2-2V5a2 2 0 0 0-2-2h-1"/>',
}


def _icon(name, cls="i"):
    return '<svg class="%s" viewBox="0 0 24 24" aria-hidden="true">%s</svg>' % (cls, _ICONS[name])


_GENERIC_MARKS = {"email": "mail", "phone": "phone"}


def _display_links(card):
    """Only make links clickable when they pass today's rules, including records saved before an upgrade."""
    for link in card.get("links") or []:
        try:
            L.check_url(link["url"])
        except ValueError:
            continue
        yield link


def _display_agents(card):
    """The ids of the agents the owner shows, in order; ones this version has no mark for are left out."""
    return [a["id"] for a in card.get("agents") or [] if isinstance(a, dict) and a.get("id") in A.NAMES]


def _agent_mark(aid, cls="b"):
    view_box, rule, paths, faint = A.mark(aid)
    return '<svg class="%s" viewBox="%s" fill-rule="%s" aria-hidden="true">%s</svg>' % (cls, view_box, rule, "".join(
        '<path d="%s"%s/>' % (d, ' fill-opacity=".5"' if i in faint else "") for i, d in enumerate(paths)))


def _agents(ids, t):
    """The agents the owner shows, each with its product mark."""
    return ('<section class="card agents" id="agents" aria-labelledby="agents-h"><h2 id="agents-h">%s</h2>'
            '<ul class="chips">%s</ul></section>' % (_e(t["agents"]), "".join(
                '<li data-agent="%s"><span class="am">%s</span><bdi>%s</bdi></li>' % (aid, _agent_mark(aid),
                                                                                       _e(A.NAMES[aid]))
                for aid in ids)))


def _link_row(link):
    """One link button: the mark of the service its URL points to (never chosen by the label), the label and, below
    it, where the link goes when that says more than the label."""
    url, label = link["url"], link["label"]
    platform = L.platform_of(url)
    brand = L.mark(platform)
    icon = ('<svg class="b" viewBox="%s" aria-hidden="true"><path d="%s"/></svg>' % brand if brand
            else _icon(_GENERIC_MARKS.get(platform, "globe")))
    to = L.shown(url)
    sub = '<bdi class="to" dir="ltr">%s</bdi>' % _e(to) if to.lower() != label.lower() else ""
    return ('<a class="row" href="%s" rel="noopener nofollow ugc me" data-platform="%s"><span class="mk">%s</span>'
            '<span class="tx"><bdi class="lb">%s</bdi>%s</span>%s</a>'
            % (_e(url), platform, icon, _e(label), sub, _icon("out", "i go")))


CSS = """
:root{--bg:#f6f6f4;--surface:#fff;--ink:#121214;--ink-2:#48484f;--ink-3:#76767e;--line:rgba(18,18,20,.08);
--line-2:rgba(18,18,20,.16);--accent:#ff5b1f;--accent-ink:#c2410c;--accent-soft:#fff0e8;--muted:#efefec;
--shadow:0 1px 2px rgba(18,18,20,.04),0 10px 28px rgba(18,18,20,.05);
--mono:ui-monospace,'SFMono-Regular',Menlo,Consolas,monospace;
--sans:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,'Helvetica Neue',Arial,sans-serif;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#0e0e10;--surface:#18181b;--ink:#f4f4f5;--ink-2:#c6c6cd;--ink-3:#8f8f99;
--line:rgba(255,255,255,.08);--line-2:rgba(255,255,255,.16);--accent-ink:#ff8a5c;--accent-soft:rgba(255,91,31,.15);
--muted:#232327;--shadow:none;color-scheme:dark}}
:lang(zh-CN){--sans:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Hiragino Sans GB','Noto Sans SC',
'Microsoft YaHei',sans-serif}
:lang(ja){--sans:-apple-system,BlinkMacSystemFont,'Segoe UI','Hiragino Sans','Hiragino Kaku Gothic ProN',
'Noto Sans JP','Yu Gothic UI',Meiryo,sans-serif}
:lang(ko){--sans:-apple-system,BlinkMacSystemFont,'Segoe UI','Apple SD Gothic Neo','Noto Sans KR','Malgun Gothic',
sans-serif}
:lang(ar){--sans:-apple-system,BlinkMacSystemFont,'SF Arabic','Geeza Pro','Noto Sans Arabic','Segoe UI',Tahoma,
sans-serif}
:lang(hi){--sans:-apple-system,BlinkMacSystemFont,'Kohinoor Devanagari','Noto Sans Devanagari','Nirmala UI',sans-serif}
:lang(bn){--sans:-apple-system,BlinkMacSystemFont,'Kohinoor Bangla','Noto Sans Bengali','Nirmala UI',sans-serif}
*,*::before,*::after{box-sizing:border-box}
[hidden]{display:none!important}
html{-webkit-text-size-adjust:100%;text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);font-size:15px;line-height:1.55;
-webkit-font-smoothing:antialiased}
[lang]{font-family:var(--sans)}
a{color:inherit}
button{font:inherit}
.i{width:20px;height:20px;flex:none;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;
stroke-linejoin:round}
.wrap{max-width:480px;margin:0 auto;padding:12px 16px 40px}
.top{display:flex;align-items:center;justify-content:space-between;height:52px;margin-bottom:8px}
.wordmark{display:inline-flex;align-items:center;gap:8px;font-size:22px;font-weight:650;letter-spacing:-.045em;
line-height:1;text-decoration:none;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif}
.wordmark img{width:24px;height:24px;flex:none}
.lang{position:relative}
.lang summary{list-style:none;display:flex;align-items:center;gap:6px;height:34px;padding:0 10px;border-radius:999px;
background:var(--surface);box-shadow:0 0 0 1px var(--line);font-size:13px;color:var(--ink-2);cursor:pointer;
user-select:none}
.lang summary::-webkit-details-marker{display:none}
.lang summary .i{width:16px;height:16px}
.lang ul{position:absolute;inset-inline-end:0;top:42px;z-index:10;margin:0;padding:6px;list-style:none;
background:var(--surface);border-radius:16px;box-shadow:0 0 0 1px var(--line),0 16px 40px rgba(0,0,0,.14);
display:grid;grid-template-columns:1fr 1fr;gap:2px;width:300px;max-width:calc(100vw - 32px)}
.lang a{display:block;padding:8px 10px;border-radius:10px;text-decoration:none;font-size:14px;white-space:nowrap;
overflow:hidden;text-overflow:ellipsis}
.lang a:hover{background:var(--muted)}
.lang a[aria-current]{background:var(--accent-soft);color:var(--accent-ink);font-weight:600}
.card{position:relative;background:var(--surface);border-radius:24px;box-shadow:0 0 0 1px var(--line),var(--shadow);
padding:20px;margin-bottom:12px}
.hero{text-align:center;padding:30px 18px 12px}
.iconbtn{display:inline-grid;place-items:center;width:38px;height:38px;flex:none;border-radius:50%;border:0;
background:transparent;color:var(--ink-2);cursor:pointer}
.iconbtn:hover{background:var(--muted);color:var(--ink)}
.iconbtn .ok{display:none}
.iconbtn.done{color:var(--accent-ink)}
.iconbtn.done .ok{display:block}
.iconbtn.done .cp{display:none}
.share{position:absolute;top:12px;inset-inline-end:12px}
.av{position:relative;width:96px;height:96px;margin:0 auto 14px}
.avatar{display:block;width:96px;height:96px;border-radius:50%;object-fit:cover;background:var(--muted);
box-shadow:0 0 0 1px var(--line)}
.av .on{position:absolute;inset-inline-end:-1px;bottom:1px;width:28px;height:28px;border-radius:50%;
background:var(--accent);color:#fff;display:grid;place-items:center;box-shadow:0 0 0 3px var(--surface)}
.av .on .i{width:15px;height:15px;fill:currentColor;stroke:none}
.av .on.ag{background:var(--surface);color:var(--ink);text-decoration:none;
box-shadow:inset 0 0 0 1px var(--line-2),0 0 0 3px var(--surface)}
.av .on .b{width:16px;height:16px;fill:currentColor}
h1{font-size:24px;line-height:1.2;letter-spacing:-.02em;font-weight:680;margin:0;overflow-wrap:anywhere}
.handle{font-family:var(--mono);font-size:13.5px;color:var(--ink-3);margin-top:4px}
.desc{margin:10px auto 0;max-width:400px;color:var(--ink-2);white-space:pre-line;overflow-wrap:anywhere}
.stats{display:grid;grid-template-columns:repeat(3,1fr);margin:20px 0 0;padding:0;list-style:none}
.stats li{display:flex;flex-direction:column;align-items:center;gap:1px;padding:0 4px}
.stats li+li{border-inline-start:1px solid var(--line)}
.stats b{font-size:21px;line-height:1.25;font-weight:680;letter-spacing:-.01em;font-variant-numeric:tabular-nums}
.stats span{font-size:12.5px;color:var(--ink-3)}
.acts{display:flex;justify-content:center;gap:2px;margin:20px -6px 0;padding:18px 0 0;list-style:none;
border-top:1px solid var(--line)}
.acts li{flex:1 1 0;min-width:0;max-width:78px}
.act{display:flex;flex-direction:column;align-items:center;gap:7px;width:100%;padding:0;border:0;background:none;
color:var(--ink-2);cursor:pointer;-webkit-tap-highlight-color:transparent}
.act .ic{position:relative;display:grid;place-items:center;width:46px;height:46px;border-radius:50%;
background:var(--accent-soft);color:var(--accent-ink);transition:box-shadow .15s,transform .15s}
.act:hover .ic,.act[aria-pressed=true] .ic{box-shadow:0 0 0 2px var(--accent);transform:translateY(-1px)}
.act:focus-visible{outline:none}
.act:focus-visible .ic{box-shadow:0 0 0 2px var(--ink)}
.act .lb{font-size:12px;line-height:1.25;max-width:100%;overflow-wrap:anywhere}
.act.s-off .ic{background:var(--muted);color:var(--ink-3)}
.act.s-off .ic::after{content:"";position:absolute;width:30px;height:2px;border-radius:2px;background:currentColor;
transform:rotate(-45deg);box-shadow:0 0 0 1.5px var(--muted)}
.act.s-off .lb{color:var(--ink-3)}
.act.s-off:hover .ic,.act.s-off[aria-pressed=true] .ic{box-shadow:0 0 0 2px var(--line-2)}
.badge{position:absolute;top:-3px;inset-inline-end:-3px;width:20px;height:20px;border-radius:50%;
background:var(--surface);color:var(--ink-2);display:grid;place-items:center;box-shadow:0 0 0 1px var(--line-2)}
.badge .i{width:11px;height:11px;stroke-width:2.4}
.note{min-height:38px;margin-top:14px;font-size:12.5px;line-height:1.5;color:var(--ink-3)}
.legend{display:flex;flex-wrap:wrap;justify-content:center;gap:4px 14px;margin:0}
.legend span{display:inline-flex;align-items:center;gap:5px}
.legend .i{width:13px;height:13px;stroke-width:2.2}
.legend .dot{width:8px;height:8px;border-radius:50%;background:var(--accent)}
.n{margin:0;color:var(--ink-2);font-size:13px}
.n b{color:var(--ink);font-weight:640}
.n span{display:block;color:var(--ink-3);font-size:12.5px;margin-top:2px}
.links{display:grid;grid-template-columns:minmax(0,1fr);gap:8px;margin:0 0 12px}
.row{display:flex;align-items:center;gap:12px;min-height:60px;padding:10px 16px;padding-inline-start:10px;
border-radius:18px;background:var(--surface);box-shadow:0 0 0 1px var(--line),var(--shadow);text-decoration:none;
transition:box-shadow .15s,transform .15s}
.row:hover{box-shadow:0 0 0 1px var(--line-2),var(--shadow);transform:translateY(-1px)}
.row:focus-visible{outline:none;box-shadow:0 0 0 2px var(--ink)}
.mk{display:grid;place-items:center;width:40px;height:40px;flex:none;border-radius:12px;background:var(--muted);
color:var(--ink)}
.mk .b{width:20px;height:20px;fill:currentColor}
.mk .i{width:20px;height:20px;stroke-width:1.7}
.tx{display:flex;flex-direction:column;align-items:flex-start;flex:1;min-width:0}
.tx>*{display:block;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lb{font-weight:600;line-height:1.35}
.to{font-size:12.5px;line-height:1.35;color:var(--ink-3)}
.row .go{color:var(--ink-3);width:18px;height:18px}
[dir=rtl] .row .go{transform:scaleX(-1)}
.agents h2{font-size:13px;line-height:1.3;font-weight:600;color:var(--ink-3);margin:0 0 12px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:0;padding:0;list-style:none}
.chips li{display:inline-flex;align-items:center;gap:8px;min-width:0;max-width:100%;height:40px;padding:0;
padding-inline:5px 14px;border-radius:999px;background:var(--muted);font-weight:600;font-size:14px}
.chips bdi{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.am{display:grid;place-items:center;width:30px;height:30px;flex:none;border-radius:50%;background:var(--surface);
color:var(--ink)}
.am .b{width:17px;height:17px;fill:currentColor}
.connect h2{display:flex;align-items:center;gap:8px;font-size:15px;line-height:1.3;font-weight:650;margin:0 0 4px}
.connect h2 .i{color:var(--accent);fill:currentColor;stroke:none;width:16px;height:16px}
.connect p{margin:0 0 12px;color:var(--ink-3);font-size:13.5px}
.prompt{display:flex;align-items:center;gap:6px;background:var(--muted);border-radius:14px;padding:10px 6px;
padding-inline-start:14px}
.prompt span{flex:1;min-width:0;font-size:14.5px;overflow-wrap:anywhere;color:var(--ink)}
footer{text-align:center;margin-top:24px;font-size:13px;color:var(--ink-3)}
footer nav{display:flex;flex-wrap:wrap;justify-content:center;gap:6px 18px;margin-bottom:12px}
footer nav a{color:var(--ink-2);text-decoration:none}
footer nav a:hover{text-decoration:underline}
.fine{display:flex;flex-wrap:wrap;align-items:center;justify-content:center;gap:2px 8px;margin:0;font-size:12px}
.fine a{color:var(--ink-3)}
.fine .json{display:inline-grid;place-items:center;width:26px;height:26px;border-radius:8px;text-decoration:none}
.fine .json:hover{background:var(--muted);color:var(--ink-2)}
.fine .json .i{width:14px;height:14px}
.toast{position:fixed;left:50%;bottom:24px;z-index:20;transform:translate(-50%,16px);opacity:0;background:var(--ink);
color:var(--bg);padding:8px 16px;border-radius:999px;font-size:13px;transition:opacity .2s,transform .2s;
pointer-events:none}
.toast.show{opacity:1;transform:translate(-50%,0)}
@media (min-width:600px){.wrap{padding-top:28px}}
@media (prefers-reduced-motion:reduce){.act .ic,.row,.toast{transition:none}.row:hover{transform:none}}
"""

JS = """
(function(){var d=document,toast=d.querySelector('.toast');
function flash(t){if(!toast)return;toast.textContent=t;toast.classList.add('show');clearTimeout(flash.t);
flash.t=setTimeout(function(){toast.classList.remove('show')},1600)}
function copy(t,done){if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(t).then(done,
function(){})}}
d.querySelectorAll('[data-copy]').forEach(function(b){b.addEventListener('click',function(){
copy(b.getAttribute('data-copy'),function(){var l=b.getAttribute('aria-label');b.classList.add('done');
b.setAttribute('aria-label',b.getAttribute('data-done'));setTimeout(function(){b.classList.remove('done');
b.setAttribute('aria-label',l)},1600)})})});
var s=d.querySelector('[data-share]');if(s){s.addEventListener('click',function(){var u=s.getAttribute('data-share');
if(navigator.share){navigator.share({title:d.title,url:u}).catch(function(){})}
else{copy(u,function(){flash(s.getAttribute('data-done'))})}})}
var legend=d.querySelector('.legend'),cur=null;
function show(id){d.querySelectorAll('.n').forEach(function(n){n.hidden=n.getAttribute('data-for')!==id});
if(legend)legend.hidden=!!id}
d.querySelectorAll('.act').forEach(function(b){var id=b.getAttribute('data-act');
b.addEventListener('click',function(){if(cur===b){b.setAttribute('aria-pressed','false');cur=null;show(null)}
else{if(cur)cur.setAttribute('aria-pressed','false');cur=b;b.setAttribute('aria-pressed','true');show(id)}});
b.addEventListener('mouseenter',function(){if(!cur)show(id)});
b.addEventListener('mouseleave',function(){if(!cur)show(null)})});
var m=d.querySelector('details.lang');if(m){d.addEventListener('click',function(e){
if(m.open&&!m.contains(e.target))m.open=false})}})();
"""


def _e(text):
    return html.escape("" if text is None else str(text), quote=True)


def _json_in_script(obj):
    """JSON safe inside <script type="application/json">: no '<', '>' or '&' can end the element."""
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


_GROUP = {"de": ".", "es": ".", "pt": ".", "it": ".", "id": ".", "tr": ".", "vi": ".", "fr": " ",
          "ru": " "}
_MYRIAD = {"zh": "万", "ja": "万", "ko": "만"}


def compact(n, lang):
    """A count as people read it: 1,234 / 12.3K / 1.2M, or 1.2万 in Chinese and Japanese (rounded down)."""
    n = max(0, int(n or 0))

    def one(x):
        s = "%.1f" % (int(x * 10) / 10.0)
        return s[:-2] if s.endswith(".0") else s
    if lang in _MYRIAD and n >= 10000:
        return one(n / 10000.0) + _MYRIAD[lang]
    if n >= 1000000:
        return one(n / 1000000.0) + "M"
    if n >= 10000:
        return one(n / 1000.0) + "K"
    return "{:,}".format(n).replace(",", _GROUP.get(lang, ","))


def _lang_menu(lang, path):
    t = I18N.T[lang]
    items = "".join('<li><a href="%s?lang=%s" hreflang="%s" lang="%s"%s>%s</a></li>' % (
        _e(path), code, html_lang(code), html_lang(code), ' aria-current="true"' if code == lang else "",
        _e(I18N.T[code]["name"])) for code in LANGS)
    return ('<details class="lang"><summary aria-label="%s">%s<span>%s</span>%s</summary><ul>%s</ul></details>'
            % (_e(t["language"]), _icon("globe"), _e(t["name"]), _icon("down"), items))


def _page(lang, title, description, head_extra, body, nonce, path, comment=""):
    t = I18N.T[lang]
    alternates = "".join('<link rel="alternate" hreflang="%s" href="%s?lang=%s">' % (html_lang(code), _e(path), code)
                         for code in LANGS)
    return ("<!doctype html>\n<html lang=\"%s\" dir=\"%s\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>%s</title><meta name=\"description\" content=\"%s\">"
            "<meta name=\"theme-color\" content=\"#f6f6f4\" media=\"(prefers-color-scheme: light)\">"
            "<meta name=\"theme-color\" content=\"#0e0e10\" media=\"(prefers-color-scheme: dark)\">%s%s"
            "<style nonce=\"%s\">%s</style></head><body>%s<div class=\"wrap\">"
            "<header class=\"top\"><a class=\"wordmark\" href=\"%s\" dir=\"ltr\" aria-label=\"Yobiru\">"
            "<img src=\"%s\" width=\"24\" height=\"24\" alt=\"\"><span>yobiru</span></a>%s</header>"
            "%s</div><div class=\"toast\" role=\"status\" aria-live=\"polite\"></div>"
            "<script nonce=\"%s\">%s</script></body></html>\n") % (
        html_lang(lang), "rtl" if lang in I18N.RTL else "ltr", _e(title), _e(description), alternates, head_extra,
        nonce, CSS, comment, SITE, BRAND_MARK, _lang_menu(lang, path), body, nonce, JS)


def agent_note(card):
    """Plain English for an agent that fetched a page instead of the card."""
    address, api = card["address"], card.get("api") or {}
    lines = [
        "This is a Yobiru bot (%s, bot_id %s): a personal agent's public address." % (card["handle"], card["bot_id"]),
        "Machine-readable card: %s.json (or GET %s with Accept: application/json)." % (address, address),
        "To connect on the user's behalf: install Yobiru (%s) and run" % REPO,
        "  %s" % card.get("connect", "yobiru public connect " + address),
        "then follow the card's `api` section; the yobiru client handles enrollment and messages.",
    ]
    if api.get("mcp"):
        lines.append("MCP (Streamable HTTP, needs a credential from this gateway): %s" % api["mcp"])
    lines.append("Contacts are saved by bot_id. Messages from senders this bot does not follow go to its owner's "
                 "message requests first. The identity is not a verified person.")
    return "\n".join(lines)


def _comment(text):
    """An HTML comment for agents that read the people's page: never shown, and it cannot end early."""
    # Escaping angle brackets prevents comment delimiters without changing URL
    # characters such as the double hyphen in punycode hosts and path prefixes.
    return "<!--\n%s\n-->" % text.replace("<", "&lt;").replace(">", "&gt;")


_ACCESS_STATES = {
    (): "off",
    ("visitor",): "visitor",
    ("follower",): "follower_only",
    ("friend",): "friend",
    ("visitor", "follower"): "visitor_follower",
    ("visitor", "friend"): "visitor_friend",
    ("follower", "friend"): "follower",
    ("visitor", "follower", "friend"): "all",
}


def _access_state(card, cap):
    """Describe the exact presets granting `cap`; relationship presets do not inherit from each other."""
    levels = card.get("capabilities") or {}
    allowed = tuple(level for level in ("visitor", "follower", "friend") if cap in (levels.get(level) or []))
    return _ACCESS_STATES[allowed]


ACTS = ("chat", "follow", "calendar_slots", "files")
_BADGE = {"visitor": "user", "follower_only": "user", "visitor_follower": "user", "visitor_friend": "user",
          "follower": "user", "friend": "lock"}


def _acts(card, t):
    """The icon row and notes describing exactly which relationships allow each action."""
    connected = bool((card.get("chat") or {}).get("agent_connected"))
    items, notes, used = [], [], []
    for act in ACTS:
        if act == "follow":
            state = "all" if card.get("accepts_followers") else "off"
            text = t["follow_on"] if state == "all" else t["follow_off"]
            extra = t["follow_back"] if state == "all" else ""
        else:
            state = _access_state(card, act)
            text = t["st_long"][state]
            extra = ""
            if act == "chat" and state != "off":
                extra = t["chat_requests"] if connected else t["chat_waiting"]
        if state not in used:
            used.append(state)
        badge = '<i class="badge">%s</i>' % _icon(_BADGE[state]) if state in _BADGE else ""
        items.append('<li><button class="act s-%s" type="button" data-act="%s" aria-pressed="false" '
                     'aria-label="%s"><span class="ic">%s%s</span><span class="lb">%s</span></button></li>'
                     % (state, act, _e("%s: %s" % (t["acts"][act], text)), _icon(act), badge, _e(t["acts"][act])))
        notes.append('<p class="n" data-for="%s" hidden><b>%s</b> · %s%s</p>'
                     % (act, _e(t["acts"][act]), _e(text), "<span>%s</span>" % _e(extra) if extra else ""))
    marks = dict((state, _icon(icon)) for state, icon in _BADGE.items())
    marks.update({"all": '<i class="dot"></i>', "off": _icon("off")})
    legend = "".join("<span>%s%s</span>" % (marks[s], _e(t["st"][s]))
                     for s in ("all", "visitor", "visitor_follower", "visitor_friend", "follower", "follower_only",
                               "friend", "off") if s in used)
    return ('<ul class="acts">%s</ul><div class="note" aria-live="polite"><p class="legend">%s</p>%s</div>'
            % ("".join(items), legend, "".join(notes)))


def render_profile(card, lang, path, nonce):
    """The profile page for people, for one published bot, from its public card only."""
    t = I18N.T[lang]
    name, handle, address = card["display_name"] or card["handle"], card["handle"], card["address"]
    json_url = address + ".json"
    stats = card.get("stats") or {}
    connected = bool((card.get("chat") or {}).get("agent_connected"))
    agents = _display_agents(card)
    if agents:
        # The first agent the owner shows, on the picture; a tap goes to the list.
        said = t["agent_badge"] % A.NAMES[agents[0]]
        badge = '<a class="on ag" href="#agents" aria-label="%s" title="%s">%s</a>' % (
            _e(said), _e(said), _agent_mark(agents[0]))
    else:
        badge = ('<span class="on" role="img" aria-label="%s" title="%s">%s</span>' % (
            _e(t["agent_on"]), _e(t["agent_on"]), _icon("spark"))) if connected else ""
    own_links = "".join(_link_row(link) for link in _display_links(card))
    prompt = t["connect_prompt"] % (handle, address)
    body = (
        '<main><section class="card hero">'
        '<button class="iconbtn share" type="button" data-share="%(address)s" data-done="%(link_copied)s" '
        'aria-label="%(share)s" title="%(share)s">%(share_icon)s</button>'
        '<div class="av"><img class="avatar" src="%(avatar)s" width="96" height="96" alt="">%(badge)s</div>'
        '<h1 dir="auto">%(name)s</h1><div class="handle"><bdi>%(handle)s</bdi></div>%(desc)s'
        '<ul class="stats">%(stats)s</ul>%(acts)s</section>'
        '%(agents)s%(links)s'
        '<section class="card connect"><h2>%(spark)s%(connect_title)s</h2><p>%(connect_body)s</p>'
        '<div class="prompt"><span>%(shown)s</span>'
        '<button class="iconbtn" type="button" data-copy="%(prompt)s" data-done="%(copied)s" aria-label="%(copy)s" '
        'title="%(copy)s">%(copy_icon)s%(check_icon)s</button></div></section></main>'
        '<footer><nav><a href="%(site)s">%(what_is)s</a><a href="%(site)s/#friends">%(get_own)s</a></nav>'
        '<p class="fine"><span>%(unverified)s</span><span aria-hidden="true">·</span><span>%(hosted)s</span>'
        '<a class="json" href="%(json_url)s" type="application/json" aria-label="%(json_card)s" '
        'title="%(json_card)s">%(braces)s</a></p></footer>'
    ) % {"address": _e(address), "link_copied": _e(t["link_copied"]), "share": _e(t["share"]),
         "share_icon": _icon("share"), "avatar": _e(card.get("avatar") or DEFAULT_AVATAR), "badge": badge,
         "name": _e(name), "handle": _e(handle),
         "desc": '<p class="desc" dir="auto">%s</p>' % _e(card["description"]) if card.get("description") else "",
         "stats": "".join('<li><b>%s</b><span>%s</span></li>' % (_e(compact(stats.get(k), lang)), _e(t["stats"][k]))
                          for k in ("messages", "followers", "friends")),
         "acts": _acts(card, t), "agents": _agents(agents, t) if agents else "",
         "links": '<nav class="links" aria-label="%s">%s</nav>' % (_e(t["links"]), own_links) if own_links else "",
         "spark": _icon("spark"), "connect_title": _e(t["connect_title"]), "connect_body": _e(t["connect_body"]),
         "prompt": _e(prompt), "copied": _e(t["copied"]), "copy": _e(t["copy"]),
         "shown": _e(t["connect_prompt"]) % ("<bdi>%s</bdi>" % _e(handle), "<bdi>%s</bdi>" % _e(address)),
         "copy_icon": _icon("copy", "i cp"), "check_icon": _icon("check", "i ok"), "site": SITE,
         "what_is": _e(t["what_is"]), "get_own": _e(t["get_own"]), "unverified": _e(t["unverified"]),
         "hosted": _e(t["hosted"]).replace("%s", '<a href="%s">Yobiru</a>' % SITE), "json_url": _e(json_url),
         "json_card": _e(t["json_card"]), "braces": _icon("braces")}
    description = card.get("description") or "%s · Yobiru bot" % handle
    return _page(lang, t["title"] % name, description, _card_head(card, name, description), body, nonce,
                 path, _comment(agent_note(card))).encode("utf-8")


def _card_head(card, name, description):
    """The machine-facing part of a page head: the JSON alternate, social preview tags and the embedded card."""
    json_url = card["address"] + ".json"
    return ('<link rel="alternate" type="application/json" href="%(j)s" title="Yobiru bot card">'
            '<link rel="canonical" href="%(a)s">'
            '<meta property="og:type" content="profile"><meta property="og:title" content="%(n)s (%(h)s)">'
            '<meta property="og:description" content="%(d)s"><meta property="og:url" content="%(a)s">'
            '<meta name="yobiru:card" content="%(j)s">%(img)s'
            '<script type="application/json" id="yobiru-card">%(card)s</script>') % {
        "j": _e(json_url), "a": _e(card["address"]), "n": _e(name), "h": _e(card["handle"]), "d": _e(description),
        "card": _json_in_script(card),
        "img": '<meta property="og:image" content="%s">' % _e(card["avatar"]) if card.get("avatar") else ""}


def render_agent_page(card, nonce=None):
    """The page for an AI agent or HTTP library that asked for HTML: plain English text, no styling, the card in
    full. `nonce` is unused (nothing runs here); it keeps the signature of the other pages."""
    name, handle = card["display_name"] or card["handle"], card["handle"]
    stats, levels = card.get("stats") or {}, card.get("capabilities") or {}
    description = card.get("description") or ""
    yes = lambda flag: "yes" if flag else "no"  # noqa: E731
    facts = [
        "messages: %d (messages this bot has sent on Yobiru, its answers included)" % int(stats.get("messages") or 0),
        "followers: %d" % int(stats.get("followers") or 0),
        "friends: %d (mutual follows)" % int(stats.get("friends") or 0),
    ] + ["%s: %s" % (level, ", ".join(levels.get(level) or []) or "nothing")
         for level in ("visitor", "follower", "friend")] + [
        "accepts followers: %s" % yes(card.get("accepts_followers")),
        "accepts contact requests: %s" % yes(card.get("accepts_contact_requests")),
        "owner's agent connected (binding configured): %s" % yes((card.get("chat") or {}).get("agent_connected")),
        "agents the owner shows (the owner's statement): %s" % (
            ", ".join(A.NAMES[aid] for aid in _display_agents(card)) or "none"),
    ]
    links = "".join('<li>%s (%s): <a href="%s">%s</a></li>' % (
        _e(link["label"]), _e(L.LABELS[L.platform_of(link["url"])]), _e(link["url"]), _e(link["url"]))
        for link in _display_links(card))
    body = ("<main><h1>%s (%s)</h1><p>A Yobiru bot. This version of the page is for AI agents; people who open the "
            "link in a browser see a profile page.</p>%s<h2>How to connect</h2><pre>%s</pre>"
            "<h2>Facts</h2><ul>%s</ul>%s<h2>Machine-readable card</h2><p><a href=\"%s\">%s</a></p><pre>%s</pre></main>"
            ) % (_e(name), _e(handle), "<p>%s</p>" % _e(description) if description else "",
                 _e(agent_note(card)), "".join("<li>%s</li>" % _e(f) for f in facts),
                 "<h2>The owner's links</h2><ul>%s</ul>" % links if links else "",
                 _e(card["address"] + ".json"), _e(card["address"] + ".json"),
                 _e(json.dumps(card, ensure_ascii=False, indent=2)))
    return ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>%s</title>"
            "<meta name=\"description\" content=\"%s\">%s</head><body>%s</body></html>\n" % (
                _e("%s (%s) - Yobiru bot" % (name, handle)), _e(description or "%s · Yobiru bot" % handle),
                _card_head(card, name, description or "%s · Yobiru bot" % handle), body)).encode("utf-8")


def render_not_found(lang, path, nonce):
    t = I18N.T[lang]
    body = ('<main><section class="card hero"><div class="av"><img class="avatar" src="%s" width="96" height="96" '
            'alt=""></div><h1>%s</h1><p class="desc">%s</p></section><nav class="links"><a class="row" href="%s">'
            '<span class="mk">%s</span><span class="tx"><span class="lb">%s</span></span>%s</a></nav></main>') % (
                DEFAULT_AVATAR, _e(t["notfound_title"]), _e(t["notfound_body"]), SITE, _icon("globe"), _e(t["home"]),
                _icon("out", "i go"))
    return _page(lang, t["notfound_title"], t["notfound_body"], '<meta name="robots" content="noindex">', body, nonce,
                 path).encode("utf-8")


def new_nonce():
    return secrets.token_urlsafe(16)
