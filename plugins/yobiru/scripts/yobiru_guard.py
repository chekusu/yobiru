#!/usr/bin/env python3
"""Yobiru guard for Claude Code: the owner allows one blocked tool call by signing it with a passkey.

When Claude Code's auto mode refuses a tool call, the usual way through is for the owner to type an approval into
that very conversation. The guard offers a second way, made of three Claude Code hooks (`yobiru guard install`):

- PermissionDenied: auto mode refused a call. The guard remembers that exact call and tells the model it may retry.
- PreToolUse: for a remembered call (or one a --protect rule names), the guard opens a request on
  https://yobiru.com/approve and refuses the call with the link. The owner reads exactly that call there and signs it
  with a passkey (Face ID, fingerprint or the device PIN). When the call comes again, unchanged, the guard checks the
  signature here, against the owner keys written into the hook's own command, and allows it once.
- SessionStart: a short note to the model about how this works.

Nothing here can approve anything by itself. Only a WebAuthn signature by one of the owner's passkeys verifies (in
tailmsg.py, the same code a Yobiru receiver uses), over text that names this computer, the agent "claude-code" and
the exact call (its SHA-256 included), for one use within the approval's lifetime. The owner keys and this
computer's name live in the hook command inside the Claude Code settings file, which Claude Code itself protects,
not in a file an agent could rewrite unnoticed. A failure anywhere leaves the call to Claude Code's own permission
checks: the guard can only add a way through, never remove one.

The hook runs as `python3 yobiru_guard.py hook ...` (no launcher, so the check before every tool call stays quick);
the other commands run as `yobiru guard ...`.
"""
import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shlex
import socket
import sys
import time
import unicodedata
from pathlib import Path

sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
AGENT = "claude-code"
MARK = "yobiru_guard.py"            # how `guard install` recognizes its own hook entries
EVENTS = ("PreToolUse", "PermissionDenied", "SessionStart")
TIMEOUTS = {"PreToolUse": 30, "PermissionDenied": 15, "SessionStart": 10}
STDIN_MAX = 16 * 1024 * 1024
DENIAL_TTL = 3600                   # a call auto mode refused is remembered for an hour
DROP_TTL = 86400                    # the relay keeps a request for a day
USED_TTL = 8 * 86400                # one-use markers outlive the longest approval (7 days)
DEFAULT_EXPIRES = 3600
WAIT_DEFAULT = 600
POLL_SECONDS = 3
REASON_MAX = 200
TEXT_MAX = 4000                     # the instruction's limit in characters (tailmsg.INSTRUCTION_MAX)
TEXT_BYTES = 24000                  # and in encoded bytes, leaving the rest of the 32 KiB intent for its other fields
# Fields that do not change what a shell command does: a retry may change them without a new signature.
COSMETIC = {"Bash": ("description", "timeout", "run_in_background"),
            "PowerShell": ("description", "timeout", "run_in_background")}
SHELL_TOOLS = ("Bash", "PowerShell")
FILE_WRITE_TOOLS = ("Write",)
FILE_EDIT_TOOLS = ("Edit",)
DIGEST_RE = re.compile(r"^[0-9a-f]{64}\Z")
REF_RE = re.compile(r"^[0-9a-f]{8,64}\Z")
MANAGED_SETTINGS = {"darwin": "/Library/Application Support/ClaudeCode/managed-settings.json",
                    "linux": "/etc/claude-code/managed-settings.json"}

# What the owner reads on yobiru.com/approve, under the page's own "Approve this instruction?". Each text ends with
# the SHA-256 of the exact call, so equal text means the same call even where escapes or a cut leave doubt.
TEXT = {
    "en": {"Bash": "Claude Code runs this command once:", "PowerShell": "Claude Code runs this command once:",
           "Write": "Claude Code writes this file once:", "Edit": "Claude Code changes this file once:",
           "tool": "Claude Code uses {tool} once, with this input:", "folder": "Folder: {v}", "file": "File: {v}",
           "options": "Options: {v}", "old": "Replace:", "new": "With:", "every": "Every occurrence.",
           "cut": "[{n} more characters not shown]", "check": "Exact call: sha256 {v}"},
    "zh-CN": {"Bash": "Claude Code 执行一次这条命令：", "PowerShell": "Claude Code 执行一次这条命令：",
              "Write": "Claude Code 写入一次这个文件：", "Edit": "Claude Code 修改一次这个文件：",
              "tool": "Claude Code 调用一次 {tool}，参数如下：", "folder": "目录：{v}", "file": "文件：{v}",
              "options": "选项：{v}", "old": "把：", "new": "改成：", "every": "全部替换。",
              "cut": "［还有 {n} 个字符没显示］", "check": "调用校验：sha256 {v}"},
    "ja": {"Bash": "Claude Code がこのコマンドを1回実行します：", "PowerShell": "Claude Code がこのコマンドを1回実行します：",
           "Write": "Claude Code がこのファイルに1回書き込みます：", "Edit": "Claude Code がこのファイルを1回変更します：",
           "tool": "Claude Code が {tool} を次の入力で1回使います：", "folder": "フォルダ：{v}", "file": "ファイル：{v}",
           "options": "オプション：{v}", "old": "置換前：", "new": "置換後：", "every": "すべて置換します。",
           "cut": "［ほか {n} 文字は表示していません］", "check": "呼び出しの照合値：sha256 {v}"},
}
LANGS = tuple(TEXT)

NOTE = ("Yobiru guard is on for this computer. When auto mode blocks a tool call that your owner wants, retry the "
        "identical call once: the guard then refuses it with a yobiru.com/approve link. Send your owner that link, "
        "wait with the command the guard gives, and retry the call unchanged after they sign. They approve that one "
        "call with a passkey; signatures are checked here, so no one else, you included, can approve it.")


class Fail(Exception):
    pass


# ---------------------------------------------------------------- small helpers (no tailmsg.py needed)
def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def age(stamp):
    """Seconds since an ISO time; very large when it is missing or bad."""
    try:
        return (now() - dt.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)).total_seconds()
    except (TypeError, ValueError):
        return float("inf")


def parse_duration(text):
    m = re.fullmatch(r"\s*(\d{1,7})\s*([smhd]?)\s*", str(text))
    if not m:
        raise Fail("durations look like 30m, 1h or 2d")
    seconds = int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    if not 60 <= seconds <= 7 * 86400:
        raise Fail("an approval lasts from 1 minute to 7 days")
    return seconds


def guard_dir(home, *parts):
    d = Path(home) / "guard"
    for p in parts:
        d = d / p
    return d


def ensure_dir(path):
    """A directory of the guard's state, owner-only (the guard directory itself too)."""
    path.mkdir(parents=True, exist_ok=True)
    for p in [path] + ([path.parent] if path.parent.name == "guard" else []):
        try:
            os.chmod(str(p), 0o700)
        except OSError:
            pass
    return path


class CallLock(object):
    """One hook at a time per call (parallel identical calls must not open two requests). Best effort: no lock
    where fcntl is missing."""

    def __init__(self, home, digest):
        self.path, self.fd = ensure_dir(guard_dir(home, "calls")) / (".%s.lock" % digest), None

    def __enter__(self):
        try:
            import fcntl
        except ImportError:
            return self
        self.fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + 20
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() > deadline:
                    raise Fail("another check of this call is still running")
                time.sleep(0.1)

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)       # the file stays: removing it could let two holders lock different files


def write_json(path, obj):
    path = Path(path)
    ensure_dir(path.parent)
    tmp = path.with_name(".%s.%d.%s.tmp" % (path.name, os.getpid(), secrets.token_hex(4)))
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=True, indent=2)
            f.write("\n")
        os.replace(str(tmp), str(path))
    except BaseException:
        if os.path.lexists(str(tmp)):
            os.unlink(str(tmp))
        raise


def read_json(path):
    try:
        path = Path(path)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            return None
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else None
    except (OSError, ValueError):
        return None


def remove(path):
    try:
        os.unlink(str(path))
    except OSError:
        pass


def one_line(text, limit=REASON_MAX):
    if not isinstance(text, str):
        return None
    text = "".join(c if unicodedata.category(c)[0] not in "CZ" or c == " " else " " for c in text)
    return " ".join(text.split())[:limit] or None


# ---------------------------------------------------------------- the exact call
def binding(tool, tool_input, cwd):
    """What a signature covers: the tool, its whole input (for a shell, the command and any non-cosmetic option)
    and, for a shell, the folder it runs in."""
    inp = dict(tool_input) if isinstance(tool_input, dict) else {"input": tool_input}
    for k in COSMETIC.get(tool, ()):
        inp.pop(k, None)
    b = {"tool": tool if isinstance(tool, str) else "", "input": inp}
    if b["tool"] in SHELL_TOOLS:
        b["cwd"] = cwd if isinstance(cwd, str) else ""
    return b


def call_digest(b):
    return hashlib.sha256(canonical(b).encode("utf-8")).hexdigest()


def visible(text):
    """Text as the owner should see it: characters that would show as nothing, move other text or end a line
    unseen are written as \\u{XXXX}."""
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False, sort_keys=True)
    out = []
    for c in text:
        if c in "\n\t" or unicodedata.category(c) not in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"):
            out.append(c)
        else:
            out.append("\\u{%04X}" % ord(c))
    return "".join(out)


def pretty(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def render(lang, b, limit=None):
    """The instruction the owner signs for a call: deterministic, so the guard can rebuild it from the call when
    the call comes again and compare it with what was signed."""
    T = TEXT[lang]
    limit = limit or TEXT_MAX
    tool, inp = b["tool"], b["input"]
    tail = []
    if tool in SHELL_TOOLS and isinstance(inp.get("command"), str):
        head = [T[tool]]
        body = visible(inp["command"])
        tail.append(T["folder"].format(v=visible(b.get("cwd", ""))))
        rest = {k: v for k, v in inp.items() if k != "command"}
        if rest:
            tail.append(T["options"].format(v=visible(canonical(rest))))
    elif tool in FILE_WRITE_TOOLS and isinstance(inp.get("file_path"), str) and isinstance(inp.get("content"), str) \
            and set(inp) == {"file_path", "content"}:
        head = [T[tool], T["file"].format(v=visible(inp["file_path"]))]
        body = visible(inp["content"])
    elif tool in FILE_EDIT_TOOLS and isinstance(inp.get("file_path"), str) and isinstance(inp.get("old_string"), str) \
            and isinstance(inp.get("new_string"), str) and set(inp) <= {"file_path", "old_string", "new_string",
                                                                        "replace_all"} \
            and inp.get("replace_all", False) in (True, False):
        head = [T[tool], T["file"].format(v=visible(inp["file_path"]))]
        if inp.get("replace_all") is True:
            head.append(T["every"])
        body = "%s\n%s\n\n%s\n%s" % (T["old"], visible(inp["old_string"]), T["new"], visible(inp["new_string"]))
    else:
        head = [T["tool"].format(tool=visible(tool))]
        body = visible(pretty(inp))
    tail.append(T["check"].format(v=call_digest(b)))
    head_s, tail_s = "\n".join(head), "\n".join(tail)
    text = head_s + "\n\n" + body + "\n\n" + tail_s
    if len(text) <= limit and encoded(text) <= TEXT_BYTES:
        return text
    cut = T["cut"].format(n=10 ** 9)                   # room for the widest count
    room = limit - len(head_s) - len(tail_s) - len(cut) - 5
    room_bytes = TEXT_BYTES - encoded(head_s) - encoded(tail_s) - encoded(cut) - 10
    keep, spent = 0, 0
    for c in body:
        spent += encoded(c)
        if keep + 1 > room or spent > room_bytes:
            break
        keep += 1
    body = body[:keep] + "\n" + T["cut"].format(n=len(body) - keep)
    return head_s + "\n\n" + body + "\n\n" + tail_s


def encoded(text):
    """Bytes the text takes in the signed intent (canonical JSON escapes everything outside ASCII)."""
    return len(json.dumps(text, ensure_ascii=True)) - 2


def subject(b):
    """What --protect rules look at: a shell command, else the tool name and input."""
    if b["tool"] in SHELL_TOOLS and isinstance(b["input"].get("command"), str):
        return b["input"]["command"]
    return b["tool"] + " " + canonical(b["input"])


# ---------------------------------------------------------------- tailmsg.py and the approval relay
_tm = None
_ap = None


def known(e):
    """str(e) for the errors whose text is meant for people and agents, else the error's type."""
    kinds = (Fail,) + tuple(k for k in (getattr(_ap, "Fail", None), getattr(_tm, "ApprovalError", None)) if k)
    return str(e) if isinstance(e, kinds) else type(e).__name__


def load(core=None):
    """tailmsg.py (verification) and yobiru_approval.py (relay drops), loaded only when a call needs them."""
    global _tm, _ap
    if core is not None and _tm is None:
        _tm = core
    if _tm is None:
        spec = importlib.util.spec_from_file_location("yobiru_bundled_tailmsg", str(HERE / "tailmsg.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _tm = mod
    if _ap is None:
        spec = importlib.util.spec_from_file_location("yobiru_guard_approval", str(HERE / "yobiru_approval.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _ap = mod
    _ap._tm = _tm
    return _tm, _ap


class Config(object):
    """The hook's settings, all from its own command line."""

    def __init__(self, home, host, node=None, keys=(), lang="en", relay=None, expires=DEFAULT_EXPIRES, protect=(),
                 note=True):
        self.home, self.host, self.node = Path(home), host, node
        self.key_texts, self.lang, self.relay = list(keys), lang, relay
        self.expires, self.protect, self.note = expires, [re.compile(p) for p in protect], note
        self._keys = None

    def keys(self):
        if self._keys is None:
            tm, _ = load()
            self._keys = [tm.owner_key_from_text(k) for k in self.key_texts]
        return self._keys

    def to(self):
        to = {"host": self.host, "agent": AGENT}
        if self.node:
            to["node"] = self.node
        return to

    def requester(self):
        frm = {"host": self.host}
        if self.node:
            frm["node"] = self.node
        return frm


def open_request(cfg, b, digest):
    """Open a relay drop asking the owner to sign this call. -> the call's record, saved."""
    tm, ap = load()
    keys = cfg.keys()
    if not keys:
        raise Fail("no owner passkey is configured for this guard (`yobiru guard install` after `yobiru approval "
                   "setup`)")
    intent, raw = ap.new_intent(tm.APPROVAL_TYPE, cfg.to(), cfg.expires,
                                {"instruction": render(cfg.lang, b), "files": [], "uses": 1, "from": cfg.requester()})
    relay = ap.relay_url(cfg.relay)
    while True:     # a ref is typed as a command argument: never one that starts with "-"
        secret = secrets.token_bytes(32)
        did, key = ap.drop_parts(secret)
        if not did.startswith("-"):
            break
    sealed = ap.seal(key, did, "ask", ap.sign_ask(raw, keys))
    if len(sealed) > tm.DROP_SLOT_MAX:
        raise Fail("this call is too large to send for a signature")
    status, obj = ap.drop_call(relay, "POST", "/v1/approval-drops/%s/ask" % did, sealed)
    if status not in (200, 201) or obj.get("ok") is not True:
        raise Fail("the approval relay did not take the request (HTTP %s %s)" % (status, obj.get("error", "")))
    return {"relay": relay, "secret": tm.b64u(secret), "link": ap.link_for(relay, secret),
            "asked_at": iso(now()), "expires_at": intent["expires_at"]}


def poll(home, rec):
    """One look at the relay for the owner's answer. -> "waiting" | "gone" | "declined" | "signed" (saved)."""
    tm, ap = load()
    secret = tm.unb64u(rec["secret"], 64)
    did, key = ap.drop_parts(secret)
    status, obj = ap.drop_call(rec["relay"], "GET", "/v1/approval-drops/%s" % did)
    if status == 404:
        return "gone"
    if status != 200 or obj.get("ok") is not True:
        raise Fail("the approval relay answered HTTP %s %s" % (status, obj.get("error", "")))
    if not obj.get("answer"):
        return "waiting"
    answer = ap.unseal(key, did, "answer", obj["answer"])
    if answer.get("declined") is True:
        rec.update(state="declined", answered_at=iso(now()))
    else:
        rec.update(state="signed", answered_at=iso(now()), approval=answer.get("approval"))
    write_json(record_path(home, rec["call"]), rec)
    return rec["state"]


def verify(cfg, b, approval):
    """The guard's own verdict on an approval for this exact call. -> (intent digest, key) or raises Fail."""
    tm, _ = load()
    try:
        check = tm.verify_approval(approval, cfg.keys())
    except tm.ApprovalError as e:
        raise Fail(str(e))
    intent = check["intent"]
    if intent["to"] != cfg.to():
        raise Fail("signed for %s, not Claude Code on this computer" % tm.recipient_label(intent["to"]))
    if intent.get("from") != cfg.requester():
        raise Fail("signed for requests from another computer")
    if intent["uses"] != 1 or intent.get("files"):
        raise Fail("signed for something other than one tool call")
    if not any(intent["instruction"] == render(lang, b) for lang in LANGS):
        raise Fail("signed for a different call")
    return check["digest"], check["key"]


def spend(home, digest):
    """Use an approval once: True the first time only."""
    d = ensure_dir(guard_dir(home, "used"))
    try:
        fd = os.open(str(d / digest), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    os.write(fd, iso(now()).encode("ascii"))
    os.close(fd)
    for p in d.iterdir():           # forget markers older than any approval can live
        try:
            if time.time() - p.stat().st_mtime > USED_TTL:
                p.unlink()
        except OSError:
            pass
    for p in guard_dir(home, "calls").glob(".*.lock"):      # and locks of calls long gone
        try:
            if time.time() - p.stat().st_mtime > USED_TTL:
                p.unlink()
        except OSError:
            pass
    return True


def record_path(home, digest):
    return guard_dir(home, "calls", digest + ".json")


def load_record(home, digest):
    rec = read_json(record_path(home, digest))
    if not rec or rec.get("call") != digest:
        return None
    if rec.get("state") == "denied" and age(rec.get("denied_at")) > DENIAL_TTL:
        return None
    if rec.get("state") in ("waiting", "signed", "declined") and age(rec.get("asked_at")) > DROP_TTL:
        return None
    return rec


def find_record(home, ref):
    if not REF_RE.fullmatch(ref or ""):
        raise Fail("give the reference the guard printed (hex)")
    hits = sorted(guard_dir(home, "calls").glob(ref + "*.json")) if guard_dir(home, "calls").is_dir() else []
    if len(hits) != 1:
        raise Fail("no unique call waiting for a signature matches %s" % ref)
    rec = read_json(hits[0])
    if not rec or not DIGEST_RE.fullmatch(rec.get("call") or ""):
        raise Fail("the record for %s is damaged" % ref)
    return rec


# ---------------------------------------------------------------- hook events
def launcher_cmd(home):
    return "sh %s --home %s guard" % (shlex.quote(str(HERE / "yobiru")), shlex.quote(str(home)))


def pre_decision(decision, reason, context=None):
    out = {"hookEventName": "PreToolUse", "permissionDecision": decision, "permissionDecisionReason": reason}
    if context:
        out["additionalContext"] = context
    return {"hookSpecificOutput": out}


def ask_message(cfg, rec, again=False):
    ref = rec["call"][:16]
    first = ("Yobiru guard: this exact call is still waiting for your owner's passkey signature." if again else
             "Yobiru guard: this exact call can run only after your owner signs it with their passkey.")
    return (first + "\n"
            "1. Send your owner this link now, in a message they will see (and with a push notification too, if you "
            "have a tool for that): %s\n"
            "   The page shows them exactly this call; they approve it with Face ID, a fingerprint or their device "
            "PIN. Do not ask them to type an approval.\n"
            "2. Wait for their answer: %s wait %s (it waits up to %d minutes: run it in the background if you can, "
            "else give the shell that long).\n"
            "3. When it reports signed, retry exactly this call, unchanged. Any change needs a new signature.\n"
            "If your owner declines, stop and do not retry this call. If your owner tells you in this conversation to "
            "go ahead without signing, run %s forget %s and retry: auto mode then judges the call as usual."
            % (rec["link"], launcher_cmd(cfg.home), ref, WAIT_DEFAULT // 60, launcher_cmd(cfg.home), ref))


def on_pre_tool_use(cfg, ev):
    b = binding(ev.get("tool_name"), ev.get("tool_input"), ev.get("cwd"))
    digest = call_digest(b)
    protected = any(p.search(subject(b)) for p in cfg.protect)
    if load_record(cfg.home, digest) is None and not protected:
        return None                                     # the common case: not the guard's business
    try:
        with CallLock(cfg.home, digest):
            return decide(cfg, ev, b, digest)
    except Exception as e:      # never allow on an error; say what happened
        return pre_decision("deny", "Yobiru guard: this call needs your owner's passkey signature, but the guard "
                                    "could not go on (%s). Nothing ran; try again in a little while." % known(e))


def decide(cfg, ev, b, digest):
    rec = load_record(cfg.home, digest)
    if rec is None or rec.get("state") == "denied":
        rec = rec or {"v": 1, "call": digest, "tool": b["tool"], "reason": "protected"}
        rec.update(open_request(cfg, b, digest), state="waiting", session=ev.get("session_id"), call=digest)
        write_json(record_path(cfg.home, digest), rec)
        return pre_decision("deny", ask_message(cfg, rec))
    if rec.get("state") == "waiting":
        state = poll(cfg.home, rec)
        if state == "gone" or (state == "waiting" and age(rec.get("expires_at")) > 0):   # lost or expired: ask again
            rec.update(open_request(cfg, b, digest), state="waiting")
            write_json(record_path(cfg.home, digest), rec)
            return pre_decision("deny", ask_message(cfg, rec))
        if state == "waiting":
            return pre_decision("deny", ask_message(cfg, rec, again=True))
    if rec.get("state") == "declined":
        return pre_decision("deny", "Yobiru guard: your owner declined this exact call. Do not retry it; tell your "
                                    "owner and ask how they want to proceed.")
    if rec.get("state") != "signed":
        raise Fail("unknown record state")
    remove(record_path(cfg.home, digest))
    try:
        intent_digest, key = verify(cfg, b, rec.get("approval"))
    except Fail as e:
        return pre_decision("deny", "Yobiru guard: the signature for this call does not hold here (%s). Nothing ran. "
                                    "Retry the call to ask your owner again." % e)
    if not spend(cfg.home, intent_digest):
        return pre_decision("deny", "Yobiru guard: your owner's signature for this call was already used. Retry the "
                                    "call to ask your owner again.")
    note = "Yobiru guard: your owner signed this exact call with their passkey %s; it was allowed once." % \
        key["fingerprint"]
    return pre_decision("allow", note, note)


def on_permission_denied(cfg, ev):
    try:
        if not cfg.keys():
            return None
    except Exception:
        return None             # a guard without a usable owner key leaves denials as they are
    b = binding(ev.get("tool_name"), ev.get("tool_input"), ev.get("cwd"))
    digest = call_digest(b)
    rec = load_record(cfg.home, digest)
    if rec is None or rec.get("state") == "denied":
        write_json(record_path(cfg.home, digest),
                   {"v": 1, "call": digest, "tool": b["tool"], "state": "denied", "denied_at": iso(now()),
                    "reason": one_line(ev.get("reason")), "session": ev.get("session_id")})
    return {"hookSpecificOutput": {"hookEventName": "PermissionDenied", "retry": True}}


def on_session_start(cfg, ev):
    if not cfg.note:
        return None
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": NOTE}}


def handle(cfg, ev):
    name = ev.get("hook_event_name")
    if name == "PreToolUse":
        return on_pre_tool_use(cfg, ev)
    if name == "PermissionDenied":
        return on_permission_denied(cfg, ev)
    if name == "SessionStart":
        return on_session_start(cfg, ev)
    return None


def hook_parser():
    ap = argparse.ArgumentParser(prog="yobiru_guard.py hook")
    ap.add_argument("--home", required=True)
    ap.add_argument("--host", required=True)
    ap.add_argument("--node")
    ap.add_argument("--key", action="append", default=[])
    ap.add_argument("--lang", choices=LANGS, default="en")
    ap.add_argument("--relay")
    ap.add_argument("--expires", type=int, default=DEFAULT_EXPIRES)
    ap.add_argument("--protect", action="append", default=[])
    ap.add_argument("--no-note", action="store_true")
    return ap


def run_hook(argv, stdin=None, stdout=None):
    """The hook command. Exit 0 always: no output leaves the call to Claude Code's own checks."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    try:
        a = hook_parser().parse_args(argv)
        cfg = Config(Path(a.home).expanduser(), a.host, a.node, a.key, a.lang, a.relay, a.expires, a.protect,
                     not a.no_note)
        ev = json.loads(stdin.read(STDIN_MAX))
        if not isinstance(ev, dict):
            return 0
        out = handle(cfg, ev)
    except SystemExit:
        return 0
    except Exception as e:
        print("yobiru guard: %s" % type(e).__name__, file=sys.stderr)
        return 0
    if out:
        stdout.write(json.dumps(out, ensure_ascii=True) + "\n")
        stdout.flush()
    return 0


# ---------------------------------------------------------------- commands
def out(obj):
    print(json.dumps(obj, ensure_ascii=True, indent=2))


def cmd_wait(home, a):
    rec = find_record(home, a.ref)
    if rec.get("state") not in ("waiting", "signed", "declined"):
        raise Fail("this call has not been sent to your owner yet: retry it once to get the link")
    deadline = time.monotonic() + max(0, a.timeout)
    while rec.get("state") == "waiting":
        state = poll(home, rec)
        if state == "gone":
            raise Fail("the request is gone from the relay (it lasts a day): retry the call to ask again")
        if state != "waiting" or time.monotonic() >= deadline:
            break
        time.sleep(min(POLL_SECONDS, max(0.1, deadline - time.monotonic())))
    if rec["state"] == "signed":
        out({"ok": True, "state": "signed", "ref": rec["call"][:16],
             "next": "retry exactly the same call now, unchanged; the guard checks the signature and lets it run once"})
        return 0
    if rec["state"] == "declined":
        out({"ok": True, "state": "declined", "ref": rec["call"][:16],
             "next": "your owner declined: do not retry this call"})
        return 1
    out({"ok": True, "state": "waiting_for_owner", "ref": rec["call"][:16], "link": rec.get("link"),
         "next": "not signed yet; run this again to keep waiting"})
    return 3


def cmd_forget(home, a):
    """Drop a remembered call: it goes back to Claude Code's own checks (auto mode judges it again). This never
    allows anything, so an agent may run it when its owner says to go ahead without a signature."""
    rec = find_record(home, a.ref)
    remove(record_path(home, rec["call"]))
    out({"ok": True, "state": "forgotten", "ref": rec["call"][:16],
         "next": "a retry of this call is judged by Claude Code's own permission checks again"})
    return 0


def cmd_list(home, a):
    d = guard_dir(home, "calls")
    items = []
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        rec = read_json(p)
        if not rec or not DIGEST_RE.fullmatch(rec.get("call") or ""):
            continue
        live = load_record(home, rec["call"]) is not None
        items.append({"ref": rec["call"][:16], "tool": rec.get("tool"), "state": rec.get("state") if live else "stale",
                      "reason": rec.get("reason"), "since": rec.get("asked_at") or rec.get("denied_at"),
                      "link": rec.get("link") if rec.get("state") == "waiting" and live else None})
    out({"ok": True, "calls": items})
    return 0


def settings_path(a):
    if a.managed:
        path = MANAGED_SETTINGS.get(sys.platform if sys.platform == "darwin" else "linux")
        return Path(path)
    return Path(a.settings).expanduser() if a.settings else Path("~/.claude/settings.json").expanduser()


def is_guard_entry(entry):
    hooks = entry.get("hooks") if isinstance(entry, dict) else None
    return isinstance(hooks, list) and any(isinstance(h, dict) and isinstance(h.get("command"), str) and
                                           MARK in h["command"] and " hook " in h["command"] + " " for h in hooks)


def strip_guard(settings):
    hooks = settings.get("hooks")
    removed = 0
    if not isinstance(hooks, dict):
        return 0
    for event in list(hooks):
        entries = hooks[event]
        if not isinstance(entries, list):
            continue
        keep = [e for e in entries if not is_guard_entry(e)]
        removed += len(entries) - len(keep)
        if keep:
            hooks[event] = keep
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)
    return removed


def read_settings(path):
    if not path.exists():
        return {}
    try:
        obj = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError) as e:
        raise Fail("%s is not valid JSON (%s); fix it first" % (path, type(e).__name__))
    if not isinstance(obj, dict):
        raise Fail("%s is not a JSON object" % path)
    return obj


def write_settings(path, settings):
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = 0o644 if path.name.startswith("managed") else 0o600
    backup = None
    if path.exists():
        mode = path.stat().st_mode & 0o777
        backup = path.with_name(path.name + ".bak-yobiru-guard-" + now().strftime("%Y%m%dT%H%M%SZ"))
        backup.write_bytes(path.read_bytes())
        os.chmod(str(backup), mode & 0o600 or 0o600)
    tmp = path.with_name(".%s.%d.tmp" % (path.name, os.getpid()))
    tmp.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(str(tmp), mode)
    os.replace(str(tmp), str(path))
    return backup


def identity(home, a):
    """(host, node id) the approvals name: this computer's Tailscale name and node, else --host, else its name."""
    tm, ap = load()
    if a.host:
        host = a.host.rstrip(".").lower()
        node = None
    else:
        try:
            host, node = ap.self_identity(home)
        except Exception:
            host, node = socket.getfqdn().rstrip(".").lower(), None
    if not tm.HOST_RE.fullmatch(host or ""):
        raise Fail("this computer's name %r cannot be used in an approval; pass --host NAME" % host)
    return host, node


def install_keys(home, a):
    tm, _ = load()
    if a.key:
        try:
            recs = [tm.owner_key_from_text(k) for k in a.key]
        except tm.ApprovalError as e:
            raise Fail(str(e))
    else:
        try:
            recs = tm.load_owner_keys(home)
        except tm.ApprovalError as e:
            raise Fail(str(e))
    if not recs:
        raise Fail("no owner passkey yet: set one up first with `yobiru approval setup` (your owner opens the link "
                   "on their phone and confirms once with Face ID, using the passkey they already have, or creates "
                   "one there), or pass --key with the yobiru-owner-key:... text of one they already use")
    return recs


def hook_command(home, host, node, keys, a):
    tm, _ = load()
    argv = [sys.executable, str(HERE / MARK), "hook", "--home", str(Path(home).absolute()), "--host", host]
    if node:
        argv += ["--node", node]
    for k in keys:
        argv += ["--key", tm.owner_key_text(k)]
    if a.lang != "en":
        argv += ["--lang", a.lang]
    if a.relay:
        argv += ["--relay", _ap.relay_url(a.relay)]
    expires = parse_duration(a.expires)
    if expires != DEFAULT_EXPIRES:
        argv += ["--expires", str(expires)]
    for p in a.protect:
        try:
            re.compile(p)
        except re.error as e:
            raise Fail("--protect %r is not a valid regular expression (%s)" % (p, e))
        argv += ["--protect", p]
    if a.no_note:
        argv.append("--no-note")
    return " ".join(shlex.quote(x) for x in argv)


def cmd_install(home, a):
    host, node = identity(home, a)
    keys = install_keys(home, a)
    command = hook_command(home, host, node, keys, a)
    path = settings_path(a)
    settings = read_settings(path)
    replaced = strip_guard(settings)
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise Fail("%s has a \"hooks\" value that is not an object" % path)
    for event in EVENTS:
        entry = {"hooks": [{"type": "command", "command": command, "timeout": TIMEOUTS[event]}]}
        if event != "SessionStart":
            entry = dict({"matcher": "*"}, **entry)
        hooks.setdefault(event, []).append(entry)
    result = {"ok": True, "settings": str(path), "computer": host, "node": node, "agent": AGENT,
              "keys": [{"fingerprint": k["fingerprint"], "label": k["label"]} for k in keys], "lang": a.lang,
              "protect": a.protect, "replaced_entries": replaced}
    if a.print:
        result.update(state="not_written", hooks={e: hooks[e] for e in EVENTS})
        out(result)
        return 0
    backup = write_settings(path, settings)
    result.update(state="installed", backup=str(backup) if backup else None,
                  note="new Claude Code sessions on this computer use the guard; when auto mode blocks a call, the "
                       "owner can allow exactly that call once by signing it on yobiru.com/approve")
    out(result)
    return 0


def cmd_uninstall(home, a):
    path = settings_path(a)
    settings = read_settings(path)
    removed = strip_guard(settings)
    if not removed:
        out({"ok": True, "state": "not_installed", "settings": str(path)})
        return 0
    backup = write_settings(path, settings)
    out({"ok": True, "state": "removed", "settings": str(path), "removed_entries": removed, "backup": str(backup)})
    return 0


def installed_in(path):
    """The guard's hook command in a settings file, parsed, or None."""
    try:
        settings = read_settings(path)
    except Fail:
        return None
    for entries in (settings.get("hooks") or {}).values() if isinstance(settings.get("hooks"), dict) else []:
        for e in entries if isinstance(entries, list) else []:
            if is_guard_entry(e):
                cmd = next(h["command"] for h in e["hooks"] if MARK in h.get("command", ""))
                try:
                    argv = shlex.split(cmd)
                    a = hook_parser().parse_args(argv[argv.index("hook") + 1:])
                except (ValueError, SystemExit):
                    return {"command": cmd, "error": "unreadable hook command"}
                return {"script": argv[1] if len(argv) > 1 else None, "home": a.home, "computer": a.host,
                        "node": a.node, "keys": len(a.key), "lang": a.lang, "protect": a.protect,
                        "script_exists": len(argv) > 1 and Path(argv[1]).is_file()}
    return None


def cmd_status(home, a):
    places = {}
    for path in (Path("~/.claude/settings.json").expanduser(),
                 Path(MANAGED_SETTINGS["darwin" if sys.platform == "darwin" else "linux"])):
        found = installed_in(path) if path.exists() else None
        if found:
            places[str(path)] = found
    d = guard_dir(home, "calls")
    waiting = sum(1 for p in d.glob("*.json") if (read_json(p) or {}).get("state") == "waiting") if d.is_dir() else 0
    out({"ok": True, "installed": places, "waiting_for_owner": waiting,
         "note": None if places else "not installed: `yobiru guard install` (after `yobiru approval setup`)"})
    return 0


def parser():
    ap = argparse.ArgumentParser(prog="yobiru guard", description=__doc__.split("\n\n")[0])
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("install", help="add the guard's hooks to Claude Code settings on this computer")
    where = p.add_mutually_exclusive_group()
    where.add_argument("--settings", help="settings file (default ~/.claude/settings.json)")
    where.add_argument("--managed", action="store_true", help="the managed settings file (needs an administrator)")
    p.add_argument("--key", action="append", default=[], metavar="TEXT",
                   help="an owner passkey's yobiru-owner-key:... text (default: the keys this data home trusts)")
    p.add_argument("--host", help="this computer's name in approvals (default: its Tailscale name)")
    p.add_argument("--lang", choices=LANGS, default="en", help="language of what the owner reads (default en)")
    p.add_argument("--expires", default="1h", help="how long a request for a signature holds (default 1h)")
    p.add_argument("--protect", action="append", default=[], metavar="REGEX",
                   help="also require a signature for calls matching this (a shell command, else tool name + input)")
    p.add_argument("--relay", help="gateway that carries requests and answers (default https://bots.yobiru.com)")
    p.add_argument("--no-note", action="store_true", help="do not tell new sessions about the guard")
    p.add_argument("--print", action="store_true", help="show the hooks without writing them")
    p = sp.add_parser("uninstall", help="remove the guard's hooks from Claude Code settings")
    where = p.add_mutually_exclusive_group()
    where.add_argument("--settings")
    where.add_argument("--managed", action="store_true")
    sp.add_parser("status", help="where the guard is installed and how many calls wait for the owner")
    sp.add_parser("list", help="calls the guard remembers, with their state")
    p = sp.add_parser("forget", help="return a remembered call to Claude Code's own checks (allows nothing)")
    p.add_argument("ref")
    p = sp.add_parser("wait", help="wait for the owner to sign (or decline) a call")
    p.add_argument("ref", help="the reference the guard gave with the link")
    p.add_argument("--timeout", type=int, default=WAIT_DEFAULT, help="seconds (default %d; 0 checks once)"
                   % WAIT_DEFAULT)
    return ap


def main(home, argv, launcher=None):
    load(launcher.core() if launcher is not None else None)
    a = parser().parse_args(argv)
    try:
        return {"install": cmd_install, "uninstall": cmd_uninstall, "status": cmd_status, "list": cmd_list,
                "wait": cmd_wait, "forget": cmd_forget}[a.cmd](Path(home), a) or 0
    except (Fail, _tm.ApprovalError, _ap.Fail) as e:
        out({"ok": False, "error": str(e)})
        return 2


if __name__ == "__main__":
    if sys.argv[1:2] == ["hook"]:
        sys.exit(run_hook(sys.argv[2:]))
    ap = argparse.ArgumentParser(prog="yobiru_guard.py")
    ap.add_argument("--home", default=os.environ.get("TAILMSG_HOME") or "~/.tailmsg")
    known, rest = ap.parse_known_args()
    sys.exit(main(Path(known.home).expanduser(), rest))
