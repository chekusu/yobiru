"""Owner approvals: ask the owner to sign an instruction with a passkey on their own device, then carry it.

Run as `yobiru approval ...` (see docs/APPROVALS.md). The owner opens a link to https://yobiru.com/approve, reads
exactly what they approve and signs it with a passkey (Face ID, fingerprint or the device PIN). The page and this
command meet at a drop on a Yobiru gateway, sealed with ChaCha20-Poly1305 under a key derived from the secret in the
link's fragment, which browsers never send: the gateway keeps only ciphertext and cannot read or change it unnoticed.

Nothing here can approve anything. Verification lives in tailmsg.py, which each receiver runs on its own against
the owner keys pinned in its data home; this module only builds requests for a signature, collects answers and pins
keys (the first one when the owner sets it up with this machine's agent, later ones only with a signed approval).
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import sys
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
APPROVE_PAGE = "https://yobiru.com/approve/"
DEFAULT_RELAY = "https://bots.yobiru.com"
RELAY_ENV = "YOBIRU_APPROVAL_RELAY"
PAGE_ENV = "YOBIRU_APPROVAL_PAGE"           # a test copy of the page; real keys are bound to https://yobiru.com
DROP_ID_DOMAIN = b"yobiru-drop-id\n"
DROP_KEY_DOMAIN = b"yobiru-drop-key\n"
DEFAULT_EXPIRES = 3600
POLL_SECONDS = 3
MODES = ("sign", "register", "add-key")
DROP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{32}\Z")


class Fail(Exception):
    pass


def _modules():
    sys.path.insert(0, str(HERE / "public_bots"))
    import bot_common
    import bot_e2e
    return bot_common, bot_e2e


def out(obj):
    print(json.dumps(obj, ensure_ascii=True, indent=2))


def parse_duration(text):
    m = re.fullmatch(r"\s*(\d{1,7})\s*([smhd]?)\s*", str(text))
    if not m:
        raise Fail("durations look like 90s, 30m, 1h or 2d")
    seconds = int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    if not 60 <= seconds <= 7 * 86400:
        raise Fail("an approval lasts from 1 minute to 7 days")
    return seconds


# ---------------------------------------------------------------- drops
def drop_parts(secret):
    """-> (drop id, AEAD key) for a link secret. The id is a hash, so the gateway learns nothing of the key."""
    did = _tm.b64u(hashlib.sha256(DROP_ID_DOMAIN + secret).digest())[:32]
    return did, hashlib.sha256(DROP_KEY_DOMAIN + secret).digest()


def seal(key, did, slot, obj):
    _, E = _modules()
    nonce = secrets.token_bytes(12)
    plain = json.dumps(obj, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return _tm.b64u(nonce + E.aead_encrypt(key, nonce, plain, ("yobiru-drop-v1:%s:%s" % (slot, did)).encode("ascii")))


def unseal(key, did, slot, text):
    _, E = _modules()
    raw = _tm.unb64u(text, 16384, "drop")
    try:
        plain = E.aead_decrypt(key, raw[:12], raw[12:], ("yobiru-drop-v1:%s:%s" % (slot, did)).encode("ascii"))
        obj = json.loads(plain.decode("utf-8"))
    except Exception:
        raise Fail("the drop's %s does not open with this link (wrong link, or it was tampered with)" % slot)
    if not isinstance(obj, dict) or obj.get("v") != 1:
        raise Fail("the drop's %s is not a Yobiru approval drop" % slot)
    return obj


def relay_url(arg):
    C, _ = _modules()
    try:
        return C.normalize_base_url(arg or os.environ.get(RELAY_ENV) or DEFAULT_RELAY)
    except ValueError as e:
        raise Fail("relay: %s" % e)


def link_for(relay, secret):
    host = urllib.parse.urlsplit(relay).hostname
    r = host if relay == "https://" + host else relay
    return (os.environ.get(PAGE_ENV) or APPROVE_PAGE) + "#" + urllib.parse.urlencode({"r": r, "s": _tm.b64u(secret)})


def parse_link(text):
    """-> (relay, secret) from an approval link."""
    frag = urllib.parse.urlsplit(text.strip()).fragment
    q = urllib.parse.parse_qs(frag)
    if set(q) != {"r", "s"} or len(q["r"]) != 1 or len(q["s"]) != 1:
        raise Fail("not an approval link")
    r = q["r"][0]
    relay = relay_url(r if "://" in r else "https://" + r)
    secret = _tm.unb64u(q["s"][0], 64, "link secret")
    if len(secret) != 32:
        raise Fail("not an approval link")
    return relay, secret


def drop_call(relay, method, path, data=None):
    C, _ = _modules()
    try:
        if data is None:
            status, obj = C.api_call(relay, method, path, timeout=20)
        else:
            status, kind, raw = C.post_raw(relay, path, data.encode("ascii"), {"Content-Type": "text/plain"}, timeout=20)
            obj = json.loads(raw.decode("utf-8")) if raw else {}
    except C.ApiError as e:
        raise Fail("relay %s: %s" % (relay, e))
    except ValueError:
        raise Fail("relay %s answered something unexpected" % relay)
    return status, obj


def pending_dir(home):
    d = Path(home) / "approvals" / "pending"
    d.mkdir(parents=True, exist_ok=True)
    for p in (d.parent, d):
        os.chmod(str(p), 0o700)
    return d


def save_pending(home, rec):
    _tm.write_private_json(pending_dir(home) / (rec["id"] + ".json"), rec)


def open_drop(home, relay, mode, ask, expires_at, purpose):
    secret = secrets.token_bytes(32)
    did, key = drop_parts(secret)
    status, obj = drop_call(relay, "POST", "/v1/approval-drops/%s/ask" % did, seal(key, did, "ask", ask))
    if status not in (200, 201) or obj.get("ok") is not True:
        raise Fail("the relay did not open the drop (HTTP %s %s)" % (status, obj.get("error", "")))
    link = link_for(relay, secret)
    rec = {"v": 1, "id": did, "mode": mode, "relay": relay, "secret": _tm.b64u(secret), "link": link,
           "created_at": _tm.iso(_tm.now_utc()), "expires_at": expires_at, "purpose": purpose,
           "intent": ask.get("intent")}
    save_pending(home, rec)
    return rec


def find_pending(home, ref):
    """A pending drop by link, id or unique id prefix; a link from elsewhere (another machine's blocked answer)
    is adopted on first use."""
    if "#" in ref:
        relay, secret = parse_link(ref)
        did, _ = drop_parts(secret)
        path = pending_dir(home) / (did + ".json")
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        return {"v": 1, "id": did, "mode": None, "relay": relay, "secret": _tm.b64u(secret), "link": ref.strip(),
                "created_at": _tm.iso(_tm.now_utc()), "expires_at": None, "purpose": {"adopted": True}, "intent": None}
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,32}", ref):
        raise Fail("give an approval link or the id `approval request` printed")
    hits = sorted(pending_dir(home).glob(ref + "*.json"))
    if len(hits) != 1:
        raise Fail("no unique pending approval matching %r" % ref)
    return json.loads(hits[0].read_text(encoding="utf-8"))


# ---------------------------------------------------------------- local identity and keys
def ctx_for(home):
    return _tm.Ctx(home)


def self_identity(home):
    """(host, node id) of this machine, as a receiver here checks an approval's recipient."""
    try:
        node = ctx_for(home).self_node()
    except Exception as e:
        raise Fail("this machine's Tailscale identity is unavailable (%s); owner keys belong to a receiver's "
                   "data home" % e)
    return node["node"], _tm.stable_peer_id(node.get("stable_id"))


def keys(home):
    try:
        return _tm.load_owner_keys(home)
    except _tm.ApprovalError as e:
        raise Fail(str(e))


def save_approval(home, blob, intent, digest, link=None):
    d = Path(home) / "approvals"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(str(d), 0o700)
    ref = digest[:16]
    _tm.write_private_json(d / (ref + ".json"), {"v": 1, "approval": blob, "intent": intent, "digest": digest,
                                                 "saved_at": _tm.iso(_tm.now_utc()), "link": link})
    return ref


def intent_summary(intent):
    s = {"type": intent["type"], "to": intent["to"], "expires_at": intent["expires_at"]}
    if intent["type"] == _tm.APPROVAL_TYPE:
        s.update(instruction=intent["instruction"], files=intent.get("files", []), uses=intent["uses"],
                 requester=intent.get("from"))
    elif intent["type"] == _tm.KEY_ADD_TYPE:
        s["key"] = _tm.owner_key_record(intent["key"])["fingerprint"]
    else:
        s["key_id"] = intent["key_id"]
    return s


def new_intent(kind, to, expires, extra):
    now = _tm.now_utc()
    intent = {"type": kind, "v": 1, "to": to, "nonce": _tm.b64u(secrets.token_bytes(16)),
              "issued_at": _tm.iso(now), "expires_at": _tm.iso(now + dt.timedelta(seconds=expires))}
    intent.update(extra)
    _tm.validate_intent(intent)
    return intent, _tm.json_canonical(intent).encode("utf-8")


def file_items(paths):
    items, names = [], set()
    for p in paths:
        path = Path(p).expanduser()
        if not path.is_file():
            raise Fail("no such file: %s" % p)
        name = _tm.safe_name(path.name)
        if name in names:
            raise Fail("two approved files are named %s" % name)
        names.add(name)
        items.append({"name": name, "sha256": _tm.sha256_file(path), "size": path.stat().st_size})
    return items


def peer_info(home, peer):
    """/info of a receiver: its name, StableID principal, how it sees this machine and its owner keys."""
    ctx = ctx_for(home)
    addr, agent = _tm.split_addr(peer)
    host, port = _tm.split_peer(addr, int(ctx.cfg["port"]))
    try:
        fqdn, ip = ctx.resolve(host)
        status, info = _tm.http_json(ip, port, "GET", "/info", host=fqdn, timeout=5, deadline=5, limit=64 * 1024)
    except Exception as e:
        raise Fail("could not reach %s for its identity and owner keys: %s" % (host, e))
    if status != 200 or not isinstance(info, dict) or info.get("node") != fqdn:
        raise Fail("%s did not answer /info as itself (HTTP %s)" % (fqdn, status))
    if _tm.OWNER_APPROVAL_CAPABILITY not in (info.get("capabilities") or []):
        raise Fail("%s runs a Yobiru receiver without owner approvals; upgrade it to 0.7.30 or later first" % fqdn)
    if agent is not None and _tm.agent_name_error(agent):
        raise Fail("bad agent %r" % agent)
    return fqdn, (_tm.norm_agent(agent) if agent else None), info


# ---------------------------------------------------------------- commands
def cmd_request(home, a):
    """Ask the owner to approve an instruction for one agent on one of their receivers."""
    expires, files = parse_duration(a.expires), file_items(a.file)
    fqdn, agent, info = peer_info(home, a.peer)
    to = {"host": fqdn}
    if agent:
        to["agent"] = agent
    if isinstance(info.get("node_id"), str) and _tm.stable_id_from_peer(info["node_id"]):
        to["node"] = info["node_id"]
    extra = {"instruction": a.instruction, "files": files, "uses": a.uses}
    caller = info.get("caller") if isinstance(info.get("caller"), dict) else {}
    if not a.any_requester:
        frm = {}
        if isinstance(caller.get("node"), str) and _tm.HOST_RE.fullmatch(caller["node"].rstrip(".").lower()):
            frm["host"] = caller["node"].rstrip(".").lower()
        if isinstance(caller.get("id"), str) and _tm.stable_id_from_peer(caller["id"]):
            frm["node"] = caller["id"]
        if frm:
            extra["from"] = frm
    intent, raw = new_intent(_tm.APPROVAL_TYPE, to, expires, extra)
    creds = [k["credential_id"] for k in info.get("owner_keys") or [] if isinstance(k, dict) and
             isinstance(k.get("credential_id"), str)]
    if not creds:
        raise Fail("%s trusts no owner key yet: ask its agent to run `yobiru approval setup` with the owner first" % fqdn)
    ask = {"v": 1, "mode": "sign", "intent": _tm.b64u(raw), "credentials": creds[:16],
           "keys": [{"fingerprint": k.get("fingerprint"), "label": k.get("label")} for k in info["owner_keys"][:16]]}
    rec = open_drop(home, relay_url(a.relay), "sign", ask, intent["expires_at"], {"request_to": a.peer})
    out({"ok": True, "state": "waiting_for_owner", "ref": rec["id"], "link": rec["link"],
         "approval": intent_summary(intent),
         "next": "give the owner this link; then `yobiru approval wait %s` and send the request with "
                 "`yobiru request send %s TEXT --approval ID` (the same files)" % (rec["id"], a.peer)})


def cmd_for(home, a):
    """On a receiver: ask the owner to approve a request that arrived here without a signature."""
    ctx = ctx_for(home)
    _, rec = _tm.load_msg(ctx, a.id)
    env, received = rec["envelope"], rec.get("received") or {}
    if "request" not in env:
        raise Fail("only a tracked request can be approved this way")
    host, node_id = self_identity(home)
    to = {"host": host}
    if node_id:
        to["node"] = node_id
    if env.get("agent") is not None and received.get("agent_status") != "default":
        to["agent"] = _tm.norm_agent(env["agent"])
    text = env.get("text") or ""
    if not text.strip() or len(text) > _tm.INSTRUCTION_MAX:
        raise Fail("the request's text must be 1 to %d characters to be approved as it is" % _tm.INSTRUCTION_MAX)
    items = env["request"].get("uploads", []) if _tm.is_pull_request(env) else env.get("fetch", [])
    files = [{"name": _tm.safe_name(f.get("name") or "file"), "sha256": f["sha256"], "size": f["size"]}
             for f in items]
    peer = received.get("peer") or {}
    frm = {}
    if _tm.stable_peer_id(peer.get("stable_id")):
        frm["node"] = _tm.stable_peer_id(peer["stable_id"])
    name = peer.get("dns_name") or peer.get("node")
    if isinstance(name, str) and not name.startswith("node:") and _tm.HOST_RE.fullmatch(name.rstrip(".").lower()):
        frm["host"] = name.rstrip(".").lower()
    extra = {"instruction": text, "files": files, "uses": a.uses}
    if frm:
        extra["from"] = frm
    intent, raw = new_intent(_tm.APPROVAL_TYPE, to, parse_duration(a.expires), extra)
    pinned = keys(home)
    if not pinned:
        raise Fail("this machine trusts no owner key yet: run `yobiru approval setup` with the owner first")
    ask = {"v": 1, "mode": "sign", "intent": _tm.b64u(raw), "credentials": [k["credential_id"] for k in pinned],
           "keys": [{"fingerprint": k["fingerprint"], "label": k["label"]} for k in pinned]}
    drop = open_drop(home, relay_url(a.relay), "sign", ask, intent["expires_at"], {"for_request": env["id"]})
    out({"ok": True, "state": "waiting_for_owner", "ref": drop["id"], "link": drop["link"],
         "approval": intent_summary(intent),
         "blocked_text": ("This needs my owner's signature. Owner: open %s to approve it. Sender: then run "
                          "`yobiru approval wait <that link>` and send the same request again with "
                          "`--approval ID` (same files)." % drop["link"])})


def setup_with_key(home, a, host, node_id, pinned):
    """Trust a passkey the owner already uses elsewhere, from its `yobiru-owner-key:` text (`approval keys` on a
    machine that trusts it). The first key is pinned as given, as `setup` would; another one needs the owner's
    signature with a key this machine already trusts."""
    try:
        key = _tm.owner_key_from_text(a.key)
    except _tm.ApprovalError as e:
        raise Fail(str(e))
    key = {f: key[f] for f in _tm.OWNER_KEY_FIELDS}
    if a.label:
        key["label"] = a.label[:80]
    if not pinned:
        try:
            added = _tm.owner_key_add(home, key, None, host, node_id)
        except _tm.ApprovalError as e:
            raise Fail(str(e))
        out({"ok": True, "state": "key_added", "key": added["key"], "keys": added["keys"],
             "note": "this machine now accepts requests carrying approvals signed with this passkey"})
        return 0
    rec = _tm.owner_key_record(key)
    if any(k["id"] == rec["id"] for k in pinned):
        out({"ok": True, "state": "key_already_trusted", "key": _tm.owner_key_view(rec)})
        return 0
    to = {"host": host}
    if node_id:
        to["node"] = node_id
    intent, raw = new_intent(_tm.KEY_ADD_TYPE, to, 3600, {"key": key})
    ask = {"v": 1, "mode": "sign", "intent": _tm.b64u(raw), "credentials": [k["credential_id"] for k in pinned],
           "keys": [{"fingerprint": k["fingerprint"], "label": k["label"]} for k in pinned]}
    drop = open_drop(home, relay_url(a.relay), "sign", ask, intent["expires_at"], {"add_key": rec["id"]})
    result = {"ok": True, "state": "waiting_for_owner", "ref": drop["id"], "link": drop["link"],
              "approval": intent_summary(intent),
              "next": "the owner confirms with a passkey this machine already trusts; then `yobiru approval wait %s`"
                      % drop["id"]}
    if a.wait:
        return cmd_wait(home, argparse.Namespace(ref=drop["id"], timeout=a.wait), prefix=result)
    out(result)


def cmd_setup(home, a):
    """Set up (or add) an owner key for this data home: the owner creates a passkey on their device."""
    host, node_id = self_identity(home)
    pinned = keys(home)
    if a.key:
        return setup_with_key(home, a, host, node_id, pinned)
    label = a.label or host.split(".")[0]
    mode = "add-key" if pinned else "register"
    ask = {"v": 1, "mode": mode, "host": host, "label": label[:80], "rp_id": _tm.OWNER_RP_ID}
    if node_id:
        ask["node"] = node_id
    if pinned:
        ask["credentials"] = [k["credential_id"] for k in pinned]
        ask["keys"] = [{"fingerprint": k["fingerprint"], "label": k["label"]} for k in pinned]
    expires = _tm.iso(_tm.now_utc() + dt.timedelta(days=1))
    rec = open_drop(home, relay_url(a.relay), mode, ask, expires, {"setup": host})
    result = {"ok": True, "state": "waiting_for_owner", "ref": rec["id"], "link": rec["link"], "mode": mode,
              "machine": host, "label": label,
              "next": "give the owner this link (they create a passkey with Face ID, fingerprint or PIN%s); then "
                      "`yobiru approval wait %s`" % (" and confirm with a key this machine already trusts"
                                                     if pinned else "", rec["id"])}
    if a.wait:
        return cmd_wait(home, argparse.Namespace(ref=rec["id"], timeout=a.wait), prefix=result)
    out(result)


def finish(home, rec, answer, ask):
    """Apply a drop's answer according to what was asked."""
    mode = ask.get("mode")
    if answer.get("declined") is True:
        return {"ok": True, "state": "declined", "ref": rec["id"], "detail": "the owner declined"}
    if mode == "sign":
        approval = answer.get("approval")
        try:
            blob, intent, raw = _tm.parse_approval(approval)
        except _tm.ApprovalError as e:
            raise Fail("the owner's answer is not a valid approval: %s" % e)
        if blob["intent"] != ask.get("intent"):
            raise Fail("the owner's answer signs different content than was asked")
        digest = hashlib.sha256(raw).hexdigest()
        result = {"ok": True, "state": "signed", "ref": rec["id"], "approval_id": save_approval(home, blob, intent, digest, rec.get("link")),
                  "approval": intent_summary(intent)}
        if intent["type"] == _tm.KEY_ADD_TYPE:
            host, node_id = self_identity(home)
            try:
                added = _tm.owner_key_add(home, intent["key"], blob, host, node_id)
            except _tm.ApprovalError as e:
                raise Fail(str(e))
            result.update(state="key_added" if added["added"] else "key_already_trusted", key=added["key"],
                          keys=added["keys"])
        elif intent["type"] == _tm.KEY_REMOVE_TYPE:
            host, node_id = self_identity(home)
            try:
                result["removed"] = _tm.owner_key_remove(home, intent["key_id"], blob, host, node_id)
            except _tm.ApprovalError as e:
                raise Fail(str(e))
            result["state"] = "key_removed"
        elif intent["type"] == _tm.APPROVAL_TYPE:
            result["next"] = "send the request with `--approval %s` (exactly the approved files)" % result["approval_id"]
        return result
    if mode in ("register", "add-key"):
        host, node_id = self_identity(home)
        if ask.get("host") != host:
            raise Fail("this setup link was made for %s, not this machine" % ask.get("host"))
        try:
            added = _tm.owner_key_add(home, answer.get("key"), answer.get("approval") if mode == "add-key" else None,
                                      host, node_id)
        except _tm.ApprovalError as e:
            raise Fail(str(e))
        return {"ok": True, "state": "key_added" if added["added"] else "key_already_trusted", "ref": rec["id"],
                "key": added["key"], "keys": added["keys"],
                "note": "this machine now accepts requests carrying approvals signed with this passkey"}
    raise Fail("unknown approval drop mode")


def cmd_wait(home, a, prefix=None):
    rec = find_pending(home, a.ref)
    relay, secret = rec["relay"], _tm.unb64u(rec["secret"], 64)
    did, key = drop_parts(secret)
    deadline = time.monotonic() + max(0, a.timeout)
    while True:
        status, obj = drop_call(relay, "GET", "/v1/approval-drops/%s" % did)
        if status == 404:
            raise Fail("the approval drop is gone (it lasts a day) or was never opened")
        if status != 200 or obj.get("ok") is not True:
            raise Fail("the relay answered HTTP %s %s" % (status, obj.get("error", "")))
        ask = unseal(key, did, "ask", obj["ask"])
        if ask.get("mode") not in MODES:
            raise Fail("unknown approval drop mode")
        if rec.get("mode") is None:          # adopted from a link: remember it
            rec.update(mode=ask["mode"], intent=ask.get("intent"))
            save_pending(home, rec)
        if obj.get("answer"):
            result = finish(home, rec, unseal(key, did, "answer", obj["answer"]), ask)
            try:
                (pending_dir(home) / (rec["id"] + ".json")).unlink()
            except OSError:
                pass
            out(dict(prefix or {}, **result))
            return 0
        if time.monotonic() >= deadline:
            out(dict(prefix or {}, ok=True, state="waiting_for_owner", ref=rec["id"], link=rec["link"],
                     detail="not answered yet; run `yobiru approval wait %s` again" % rec["id"]))
            return 3
        time.sleep(min(POLL_SECONDS, max(0.1, deadline - time.monotonic())))


def cmd_keys(home, a):
    pinned = keys(home)
    out({"ok": True, "keys": [dict(_tm.owner_key_view(k), share=_tm.owner_key_text(k)) for k in pinned],
         "note": ("no owner key yet: `yobiru approval setup` creates one with the owner" if not pinned else
                  "requests with approvals signed by these passkeys are accepted here")})


def cmd_remove(home, a):
    host, node_id = self_identity(home)
    pinned = keys(home)
    target = next((k for k in pinned if a.key in (k["id"], k["fingerprint"])), None)
    if target is None:
        raise Fail("no pinned owner key %s (see `yobiru approval keys`)" % a.key)
    to = {"host": host}
    if node_id:
        to["node"] = node_id
    intent, raw = new_intent(_tm.KEY_REMOVE_TYPE, to, 3600, {"key_id": target["id"]})
    ask = {"v": 1, "mode": "sign", "intent": _tm.b64u(raw), "credentials": [k["credential_id"] for k in pinned],
           "keys": [{"fingerprint": k["fingerprint"], "label": k["label"]} for k in pinned]}
    rec = open_drop(home, relay_url(a.relay), "sign", ask, intent["expires_at"], {"remove_key": target["id"]})
    out({"ok": True, "state": "waiting_for_owner", "ref": rec["id"], "link": rec["link"],
         "approval": intent_summary(intent), "next": "the owner signs; then `yobiru approval wait %s`" % rec["id"]})


def cmd_verify(home, a):
    """Check an approval here, as this machine's receiver would (signature, key, validity; not its uses)."""
    source = a.approval
    path = Path(home) / "approvals" / (source + ".json")
    if re.fullmatch(r"[0-9a-f]{8,64}", source) and path.is_file():
        source = json.loads(path.read_text(encoding="utf-8"))["approval"]
    elif Path(source).expanduser().is_file():
        source = Path(source).expanduser().read_text(encoding="utf-8")
    try:
        check = _tm.verify_approval(source, keys(home), kinds=(_tm.APPROVAL_TYPE, _tm.KEY_ADD_TYPE, _tm.KEY_REMOVE_TYPE))
    except _tm.ApprovalError as e:
        out({"ok": True, "verified": False, "reason": str(e)})
        return 1
    host, node_id = self_identity(home)
    reason = _tm.approval_audience_error(check["intent"], host, node_id, check["intent"]["to"].get("agent"),
                                         "known" if check["intent"]["to"].get("agent") else "default")
    out({"ok": True, "verified": reason is None, "reason": reason, "key": _tm.owner_key_view(check["key"]),
         "digest": check["digest"], "approval": intent_summary(check["intent"])})
    return 0 if reason is None else 1


def parser():
    ap = argparse.ArgumentParser(prog="yobiru approval", description=__doc__.split("\n\n")[0])
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("setup", help="set up the owner's passkey for this machine (or add another one)")
    p.add_argument("--label", help="name shown with the passkey (default: this machine's name)")
    p.add_argument("--relay", help="gateway that carries the sealed answer (default %s)" % DEFAULT_RELAY)
    p.add_argument("--wait", type=int, default=0, metavar="SECONDS", help="wait for the owner this long")
    p.add_argument("--key", metavar="TEXT", help="trust a passkey the owner already uses on another machine: its "
                                                 "yobiru-owner-key:... text from `yobiru approval keys` there")
    p = sp.add_parser("request", help="ask the owner to approve an instruction for an agent on another machine")
    p.add_argument("peer", help="receiver and agent, e.g. mac-studio/claude")
    p.add_argument("instruction", help="what the agent may do, in the owner's words")
    p.add_argument("--file", action="append", default=[], help="a file the request will carry (exactly these)")
    p.add_argument("--expires", default="1h", help="how long it holds: 30m, 1h (default), 2d; at most 7d")
    p.add_argument("--uses", type=int, default=1, help="requests it may authorize (default 1, at most 100)")
    p.add_argument("--any-requester", action="store_true", help="do not tie it to this machine as the sender")
    p.add_argument("--relay")
    p = sp.add_parser("for", help="on a receiver: ask the owner to approve a request that arrived unsigned")
    p.add_argument("id", help="the incoming request's message id")
    p.add_argument("--expires", default="1h")
    p.add_argument("--uses", type=int, default=1)
    p.add_argument("--relay")
    p = sp.add_parser("wait", help="wait for the owner's answer and save or apply it")
    p.add_argument("ref", help="the ref or link `approval request|for|setup|remove` printed")
    p.add_argument("--timeout", type=int, default=300, help="seconds to wait (default 300; 0 checks once)")
    p = sp.add_parser("get", help="check once for the owner's answer (same as wait --timeout 0)")
    p.add_argument("ref")
    sp.add_parser("keys", help="owner keys this machine trusts")
    p = sp.add_parser("remove", help="stop trusting an owner key (the owner signs the removal)")
    p.add_argument("key", help="fingerprint or id from `approval keys`")
    p.add_argument("--relay")
    p = sp.add_parser("verify", help="check an approval here as the receiver would")
    p.add_argument("approval", help="approval id, file or yobiru-approval:... text")
    return ap


_tm = None


def main(home, argv, launcher):
    global _tm
    _tm = launcher.core()
    a = parser().parse_args(argv)
    try:
        if a.cmd == "get":
            a = argparse.Namespace(cmd="wait", ref=a.ref, timeout=0)
        return {"setup": cmd_setup, "request": cmd_request, "for": cmd_for, "wait": cmd_wait, "keys": cmd_keys,
                "remove": cmd_remove, "verify": cmd_verify}[a.cmd](Path(home), a) or 0
    except (Fail, _tm.ApprovalError) as e:
        out({"ok": False, "error": str(e)})
        return 2
