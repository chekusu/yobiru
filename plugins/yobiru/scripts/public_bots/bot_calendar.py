#!/usr/bin/env python3
"""Yobiru private calendar capability service: read-only meeting-slot suggestions for one public bot.

This process holds the owner's private calendar data (an events file) and the owner's disclosure rules.
The public worker never reads that data: it can only ask this service, over loopback with an access
token, to answer one *started* work item identified by a capability ticket. The service redeems the
ticket at the gateway with its own per-bot verifier credential and receives the authoritative contact
subject, relationship level and the caller's stored query. It never accepts scopes, levels or queries
from the worker. Answers contain only slot start/end times, at most `max_slots`, inside work hours,
horizon, notice and duration rules, after conflicts with busy events (plus buffers) are removed, and
within per-contact and per-bot daily disclosure budgets. Event titles, attendees, locations, notes and
the busy map never leave this process.

Same-user processes are an interface boundary, not an OS sandbox: run this service as a different OS
user or in a separate container when you need the host to enforce the separation (docs/PUBLIC_BOTS.md).

  bot_calendar.py init --dir DIR --gateway URL --bot BOT_ID [--events FILE]
  bot_calendar.py serve --dir DIR [--port N]          (always 127.0.0.1)
  bot_calendar.py check --dir DIR                     (counts only, never event details)
  bot_calendar.py preview --dir DIR --duration N [--after D] [--before D]
Python 3.8+, stdlib only.
"""
import argparse
import bisect
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import bot_common as C  # noqa: E402

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)\Z")
OFFSET_RE = re.compile(r"^([+-])(\d\d):(\d\d)\Z")
LOCAL_DT_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?(?:Z|[+-]\d\d:\d\d)\Z")
SERVICE_KEYS = {"v", "gateway", "bot_id", "events_file", "verifier_token_file", "access_token_hash", "policy"}
POLICY_KEYS = {"timezone", "days", "hours", "durations", "horizon_days", "min_notice_minutes", "step_minutes",
               "buffer_minutes", "max_slots", "levels", "per_contact_per_day", "per_bot_per_day",
               "answer_ttl_minutes"}
OPTIONAL_POLICY_KEYS = {"answer_ttl_minutes"}
DEFAULT_POLICY = {"timezone": "UTC", "days": ["mon", "tue", "wed", "thu", "fri"],
                  "hours": [["09:00", "12:00"], ["13:00", "17:00"]], "durations": [30, 60], "horizon_days": 14,
                  "min_notice_minutes": 720, "step_minutes": 30, "buffer_minutes": 10, "max_slots": 3,
                  "levels": ["friend"], "per_contact_per_day": 3, "per_bot_per_day": 50, "answer_ttl_minutes": 15}
GRANT_IDENTITY = ("bot_id", "message_id", "conversation_id", "subject", "level", "generation", "capability", "query")
MAX_EVENTS = 20000


def log(*parts):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *parts, file=sys.stderr, flush=True)


def die(msg, code=1):
    print("bot_calendar: error: " + msg, file=sys.stderr)
    sys.exit(code)


class Refuse(Exception):
    """A fixed, data-free refusal code: never an event detail or a provider error body."""

    def __init__(self, status, code, detail=""):
        Exception.__init__(self, code)
        self.status, self.code, self.detail = status, code, detail


# ---------------------------------------------------------------- owner rules
def parse_tz(name):
    if name in ("UTC", "Z", "+00:00"):
        return dt.timezone.utc
    m = OFFSET_RE.match(name) if isinstance(name, str) else None
    if m:
        hours, minutes = int(m.group(2)), int(m.group(3))
        if hours > 14 or minutes > 59:
            raise ValueError("timezone offset out of range")
        delta = dt.timedelta(hours=hours, minutes=minutes)
        return dt.timezone(-delta if m.group(1) == "-" else delta)
    try:
        from zoneinfo import ZoneInfo               # Python 3.9+
    except ImportError:
        raise ValueError("IANA time zone names need Python 3.9+; use a fixed offset such as +09:00")
    try:
        return ZoneInfo(name)
    except Exception:
        raise ValueError("unknown time zone %r" % name)


def minutes(hhmm):
    m = HHMM_RE.match(hhmm) if isinstance(hhmm, str) else None
    if not m:
        raise ValueError("times are HH:MM")
    return int(m.group(1)) * 60 + int(m.group(2))


def validate_policy(raw):
    if not isinstance(raw, dict) or set(raw) - POLICY_KEYS or set(POLICY_KEYS) - set(raw) - OPTIONAL_POLICY_KEYS:
        raise ValueError("policy needs exactly: %s" % ", ".join(sorted(POLICY_KEYS)))
    raw = dict(raw)
    raw.setdefault("answer_ttl_minutes", DEFAULT_POLICY["answer_ttl_minutes"])
    parse_tz(raw["timezone"])
    if not isinstance(raw["days"], list) or not raw["days"] or any(d not in DAYS for d in raw["days"]):
        raise ValueError("policy.days: a non-empty subset of %s" % ", ".join(DAYS))
    hours = raw["hours"]
    if not isinstance(hours, list) or not 1 <= len(hours) <= 6:
        raise ValueError("policy.hours: 1-6 [start, end] windows")
    spans = []
    for w in hours:
        if not isinstance(w, list) or len(w) != 2:
            raise ValueError("policy.hours: each window is [\"HH:MM\", \"HH:MM\"]")
        s, e = minutes(w[0]), minutes(w[1])
        if s >= e:
            raise ValueError("policy.hours: a window must end after it starts")
        spans.append((s, e))
    spans.sort()
    if any(a[1] > b[0] for a, b in zip(spans, spans[1:])):
        raise ValueError("policy.hours: windows must not overlap")
    durs = raw["durations"]
    if not isinstance(durs, list) or not 1 <= len(durs) <= 6 or any(not C.int_in(d, 5, 480) for d in durs):
        raise ValueError("policy.durations: 1-6 meeting lengths of 5-480 minutes")
    for key, low, high in (("horizon_days", 1, 90), ("min_notice_minutes", 0, 10080), ("step_minutes", 5, 240),
                           ("buffer_minutes", 0, 240), ("max_slots", 1, 5), ("per_contact_per_day", 1, 100),
                           ("per_bot_per_day", 1, 10000), ("answer_ttl_minutes", 1, 1440)):
        if not C.int_in(raw[key], low, high):
            raise ValueError("policy.%s must be an integer %d-%d" % (key, low, high))
    if not isinstance(raw["levels"], list) or not raw["levels"] or \
            any(x not in ("visitor", "friend") for x in raw["levels"]):
        raise ValueError("policy.levels: a non-empty subset of visitor, friend")
    return raw


# ---------------------------------------------------------------- private events
def parse_event_time(text):
    if not isinstance(text, str) or not LOCAL_DT_RE.match(text):
        raise ValueError("event times are ISO 8601 with Z or an offset")
    value = text.replace("Z", "+00:00")
    if len(value) == len("YYYY-MM-DDTHH:MM+00:00"):
        value = value[:16] + ":00" + value[16:]
    return dt.datetime.strptime(value[:19] + value[19:].replace(":", ""), "%Y-%m-%dT%H:%M:%S%z").timestamp()


def read_events(path):
    """-> (parsed events document, sha256 of the exact bytes): the hash versions cached answers."""
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("events file must be a regular file of at most 16 MiB")
    raw = path.read_bytes()
    try:
        return C.strict_loads(raw), hashlib.sha256(raw).hexdigest()
    except C.StrictJSONError as e:
        raise ValueError("events file: %s" % e)


def load_busy(path, tz, with_hash=False):
    """Busy intervals [start, end) as epoch seconds. Any malformed event fails the whole calendar:
    silently skipping one could invent availability."""
    data, digest = read_events(path)
    busy = parse_busy(data, tz)
    return (busy, digest) if with_hash else busy


def parse_busy(data, tz):
    events = data.get("events") if isinstance(data, dict) else None
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise ValueError("events file must be {\"events\": [...]}")
    busy = []
    for ev in events:
        if not isinstance(ev, dict):
            raise ValueError("event must be an object")
        status = ev.get("status", "confirmed")
        transparency = ev.get("transparency", "opaque")
        if status not in ("confirmed", "tentative", "cancelled") or transparency not in ("opaque", "transparent"):
            raise ValueError("event status/transparency not recognized")
        if ev.get("all_day") is True:
            day = C.parse_date(ev.get("date"))
            # the widest reading of a local day at a DST transition: busy time is never under-counted
            interval = (day_bound(day, tz, late=False), day_bound(day + dt.timedelta(days=1), tz, late=True))
        elif "all_day" in ev and ev["all_day"] is not False:
            raise ValueError("all_day must be true or false")
        else:
            interval = (parse_event_time(ev.get("start")), parse_event_time(ev.get("end")))
        if interval[1] <= interval[0]:
            raise ValueError("event must end after it starts")
        if status == "cancelled" or transparency == "transparent":
            continue                      # free time by the calendar's own definition
        busy.append(interval)
    busy.sort()
    return busy


def local_instants(day, minute_of_day, tz):
    """Every absolute instant (epoch seconds) at which the local wall time `day minute_of_day` occurs:
    one normally, none inside a DST gap, two when the clock falls back. Found by an exact round trip."""
    naive = dt.datetime(day.year, day.month, day.day) + dt.timedelta(minutes=minute_of_day)
    found = set()
    for fold in (0, 1):
        ts = naive.replace(tzinfo=tz, fold=fold).timestamp()
        if dt.datetime.fromtimestamp(ts, tz).replace(tzinfo=None) == naive:
            found.add(ts)
    return sorted(found)


def day_bound(day, tz, late):
    """Start of a local day as an instant; at a DST transition the narrower choice of the request window."""
    found = local_instants(day, 0, tz)
    if not found:                                   # midnight skipped by a DST gap
        naive = dt.datetime(day.year, day.month, day.day)
        found = sorted(naive.replace(tzinfo=tz, fold=f).timestamp() for f in (0, 1))
    return found[-1] if late else found[0]


def busy_index(busy):
    """Sorted starts plus the running maximum of ends: an overlap test in O(log n)."""
    starts, ends, high = [], [], float("-inf")
    for s, e in busy:
        starts.append(s)
        high = max(high, e)
        ends.append(high)
    return starts, ends


def overlaps(index, a, b):
    starts, ends = index
    i = bisect.bisect_left(starts, b)               # busy intervals starting before b
    return i > 0 and ends[i - 1] > a


def suggest(policy, busy, now, query):
    """Conflict-aware suggestions -> list of (start, end) epoch seconds, at most policy max_slots.

    Candidates step through absolute time between a work window's start and end, so every slot lasts
    exactly the requested duration, including on DST transition days. A window whose start or end does
    not exist (spring gap) or occurs twice (fall back) on that day is skipped: unclear local times are
    never offered."""
    tz = parse_tz(policy["timezone"])
    dur = query["duration_minutes"]
    if dur not in policy["durations"]:
        raise Refuse(422, "invalid_query", "duration not offered")
    earliest = now + policy["min_notice_minutes"] * 60
    latest = now + policy["horizon_days"] * 86400
    try:
        if query.get("after"):
            earliest = max(earliest, day_bound(C.parse_date(query["after"]), tz, late=True))
        if query.get("before"):
            latest = min(latest, day_bound(C.parse_date(query["before"]) + dt.timedelta(days=1), tz, late=False))
    except ValueError:
        raise Refuse(422, "invalid_query", "dates are YYYY-MM-DD")
    buffer_s, step_s, dur_s = policy["buffer_minutes"] * 60, policy["step_minutes"] * 60, dur * 60
    windows = sorted((minutes(s), minutes(e)) for s, e in policy["hours"])
    index = busy_index(busy)
    out, seen = [], set()
    if earliest >= latest:
        return out
    day = dt.datetime.fromtimestamp(earliest, tz).date()
    last_day = dt.datetime.fromtimestamp(latest, tz).date()
    while day <= last_day and len(out) < policy["max_slots"]:
        if DAYS[day.weekday()] in policy["days"]:
            for ws, we in windows:
                starts, ends = local_instants(day, ws, tz), local_instants(day, we, tz)
                if len(starts) != 1 or len(ends) != 1:
                    continue
                s = starts[0]
                while s + dur_s <= ends[0] and len(out) < policy["max_slots"]:
                    e = s + dur_s
                    if s >= earliest and e <= latest and (s, e) not in seen and \
                            not overlaps(index, s - buffer_s, e + buffer_s):
                        out.append((s, e))
                        seen.add((s, e))
                    s += step_s
        day += dt.timedelta(days=1)
    return sorted(out)


# ---------------------------------------------------------------- service state
class Service(object):
    def __init__(self, directory):
        self.dir = Path(directory).expanduser().absolute()
        raw = C.read_json_file(self.dir / "service.json", private=True, what="service config")
        if not isinstance(raw, dict) or set(raw) != SERVICE_KEYS or raw["v"] != 1:
            raise ValueError("service.json: unexpected fields or version")
        self.gateway = C.normalize_base_url(raw["gateway"])
        if not (isinstance(raw["bot_id"], str) and C.ID_RE.match(raw["bot_id"]) and raw["bot_id"].startswith("bot_")):
            raise ValueError("service.json: bot_id")
        if not re.match(r"^[0-9a-f]{64}\Z", str(raw["access_token_hash"])):
            raise ValueError("service.json: access_token_hash")
        self.bot_id, self.access_hash = raw["bot_id"], raw["access_token_hash"]
        self.policy = validate_policy(raw["policy"])
        self.policy_hash = hashlib.sha256(json.dumps(self.policy, sort_keys=True).encode()).hexdigest()
        self.tz = parse_tz(self.policy["timezone"])
        self.events_file = self.resolve(raw["events_file"])
        self.verifier_file = self.resolve(raw["verifier_token_file"])
        self.lock = threading.Lock()
        self.ledger = self.dir / "ledger.sqlite3"
        db = self.db()
        try:
            db.execute("CREATE TABLE IF NOT EXISTS slot_answers (message_id TEXT PRIMARY KEY, subject TEXT NOT NULL, "
                       "identity TEXT NOT NULL, day TEXT NOT NULL, policy_hash TEXT NOT NULL, "
                       "events_hash TEXT NOT NULL, result TEXT NOT NULL, created_ts INTEGER NOT NULL)")
        finally:
            db.close()

    def resolve(self, value):
        if not isinstance(value, str) or not value:
            raise ValueError("service.json: file paths must be strings")
        p = Path(value).expanduser()
        return p if p.is_absolute() else self.dir / p

    def db(self):
        db = sqlite3.connect(str(self.ledger), timeout=15, isolation_level=None)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def redeem(self, verifier, ticket):
        """Ask the gateway, now, whether this ticket's work is still authorized; -> the grant identity."""
        try:
            status, grant = C.api_call(self.gateway, "POST", "/v1/capability/redeem", {"ticket": ticket},
                                       token=verifier, timeout=10)
        except (C.ApiError, ValueError):
            raise Refuse(503, "gateway_unreachable")
        if status != 200 or grant.get("ok") is not True:
            raise Refuse(403, "not_authorized")
        if grant.get("bot_id") != self.bot_id or grant.get("capability") != "calendar_slots" or \
                not isinstance(grant.get("message_id"), str) or not isinstance(grant.get("subject"), str) or \
                not isinstance(grant.get("query"), dict) or not isinstance(grant.get("generation"), int):
            raise Refuse(403, "not_authorized")
        if grant.get("level") not in self.policy["levels"]:
            raise Refuse(403, "policy_denied")
        return {k: grant.get(k) for k in GRANT_IDENTITY}

    def answer(self, ticket, now=None):
        """Authorize, budget, compute, re-authorize, release. Every refusal is a fixed code.

        Authorization is asked of the gateway after this request holds the service lock and again
        immediately before anything derived from the calendar is released or budget is spent; both answers
        must describe the identical message, contact subject, grant generation, level and stored query.
        A retried claim of the same message gets the cached answer only while the owner's policy, the
        events file and the answer's freshness window are unchanged: otherwise `stale_answer`, never a
        recomputation that would bypass the disclosure budget."""
        try:
            verifier = C.read_token_file(self.verifier_file, "verifier")
        except ValueError:
            raise Refuse(503, "not_configured", "verifier credential missing")
        with self.lock:
            now = C.now_ts()                          # decision time after waiting for the lock
            grant = self.redeem(verifier, ticket)
            mid, subject, query = grant["message_id"], grant["subject"], grant["query"]
            identity = json.dumps(grant, sort_keys=True)
            day = dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y-%m-%d")
            try:
                busy, events_hash = load_busy(self.events_file, self.tz, with_hash=True)
            except (ValueError, OSError):
                raise Refuse(503, "calendar_unavailable")
            db = self.db()
            try:
                db.execute("BEGIN IMMEDIATE")
                prior = db.execute("SELECT * FROM slot_answers WHERE message_id=?", (mid,)).fetchone()
                if prior is not None:
                    _mid, p_subject, p_identity, _day, p_policy, p_events, p_result, p_created = prior
                    if p_subject != subject or p_identity != identity:
                        raise Refuse(403, "not_authorized")
                    if p_policy != self.policy_hash or p_events != events_hash or \
                            now - p_created > self.policy["answer_ttl_minutes"] * 60:
                        raise Refuse(409, "stale_answer")      # never recomputed for the same message
                    result = dict(json.loads(p_result), repeated=True)
                else:
                    used = db.execute("SELECT COUNT(*) FROM slot_answers WHERE subject=? AND day=?",
                                      (subject, day)).fetchone()[0]
                    total = db.execute("SELECT COUNT(*) FROM slot_answers WHERE day=?", (day,)).fetchone()[0]
                    if used >= self.policy["per_contact_per_day"] or total >= self.policy["per_bot_per_day"]:
                        raise Refuse(429, "budget_exhausted")
                    slots = suggest(self.policy, busy, now, query)
                    stored = {"slots": [{"start": C.iso(s), "end": C.iso(e)} for s, e in slots], "as_of": C.iso(now),
                              "budget_remaining": self.policy["per_contact_per_day"] - used - 1}
                    db.execute("INSERT INTO slot_answers(message_id, subject, identity, day, policy_hash, events_hash, "
                               "result, created_ts) VALUES(?,?,?,?,?,?,?,?)",
                               (mid, subject, identity, day, self.policy_hash, events_hash,
                                json.dumps(stored, sort_keys=True), int(now)))
                    result = dict(stored, repeated=False)
                if self.redeem(verifier, ticket) != grant:    # revoked or changed while computing
                    raise Refuse(403, "not_authorized")
                db.execute("COMMIT")
                return result
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
            finally:
                db.close()


def make_handler(service):
    class Handler(BaseHTTPRequestHandler):
        server_version = "yobiru-capability"
        sys_version = ""
        timeout = 10

        def version_string(self):
            return self.server_version

        def log_message(self, fmt, *args):
            log("%s %s" % (self.client_address[0], (fmt % args)[:120]))

        def send_error(self, code, message=None, explain=None):
            self.reply(code, {"ok": False, "code": "bad_request"})

        def reply(self, status, obj):
            body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def do_GET(self):
            if self.path == "/v1/health":
                return self.reply(200, {"ok": True, "service": "yobiru-calendar-capability"})
            self.reply(404, {"ok": False, "code": "not_found"})

        def do_POST(self):
            try:
                if self.path != "/v1/suggest":
                    raise Refuse(404, "not_found")
                auth = self.headers.get("Authorization") or ""
                token = auth[7:].strip() if auth.startswith("Bearer ") else ""
                if C.token_kind(token) != "access" or \
                        not hmac.compare_digest(C.token_hash(token), service.access_hash):
                    raise Refuse(401, "unauthorized")
                try:
                    n = int(self.headers.get("Content-Length") or "-1")
                except ValueError:
                    n = -1
                if not 0 < n <= 4096 or self.headers.get("Transfer-Encoding"):
                    raise Refuse(400, "bad_request")
                try:
                    body = C.strict_loads(self.rfile.read(n))
                except C.StrictJSONError:
                    raise Refuse(400, "bad_request")
                if not isinstance(body, dict) or set(body) != {"ticket"} or C.token_kind(body["ticket"]) != "ticket":
                    raise Refuse(400, "bad_request", "the only accepted field is a capability ticket")
                result = service.answer(body["ticket"])
                self.reply(200, dict(result, ok=True))
            except Refuse as r:
                self.reply(r.status, {"ok": False, "code": r.code})
            except Exception as e:
                log("internal error:", type(e).__name__)
                self.reply(500, {"ok": False, "code": "internal_error"})

    return Handler


# ---------------------------------------------------------------- commands
def cmd_init(a):
    os.umask(0o077)
    d = Path(a.dir).expanduser().absolute()
    if (d / "service.json").exists():
        die("%s already has service.json; nothing was changed" % d, 1)
    try:
        gateway = C.normalize_base_url(a.gateway)
    except ValueError as e:
        die("--gateway: %s" % e, 2)
    if not (C.ID_RE.match(a.bot) and a.bot.startswith("bot_")):
        die("--bot must be a bot id (bot_...)", 2)
    C.ensure_private_dir(d)
    token = C.new_token("access")
    C.create_private_file(d / "access.token", token + "\n")
    events = a.events or "events.json"
    C.create_private_file(d / "service.json", json.dumps({
        "v": 1, "gateway": gateway, "bot_id": a.bot, "events_file": events,
        "verifier_token_file": "verifier.token", "access_token_hash": C.token_hash(token),
        "policy": DEFAULT_POLICY}, indent=2) + "\n")
    print(json.dumps({"ok": True, "dir": str(d), "access_token_file": str(d / "access.token"),
                      "events_file": events, "verifier_token_file": str(d / "verifier.token"),
                      "next": ["write the owner-approved rules in service.json policy",
                               "create the verifier credential: yobiru public verifier bind BOT --token-file %s"
                               % (d / "verifier.token"),
                               "start: serve --dir %s" % d]}, indent=2))
    return 0


def cmd_serve(a):
    os.umask(0o077)
    try:
        service = Service(a.dir)
    except ValueError as e:
        die(str(e))
    srv = C.NumericHTTPServer(("127.0.0.1", a.port or 0), make_handler(service))
    srv.daemon_threads = True
    print(json.dumps({"listening": "http://127.0.0.1:%d" % srv.server_address[1],
                      "service": "yobiru-calendar-capability"}), flush=True)
    log("calendar capability service for %s on 127.0.0.1:%d" % (service.bot_id, srv.server_address[1]))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_check(a):
    try:
        service = Service(a.dir)
        busy = load_busy(service.events_file, service.tz)
    except ValueError as e:
        die(str(e))
    print(json.dumps({"ok": True, "bot_id": service.bot_id, "busy_intervals": len(busy),
                      "verifier_credential": service.verifier_file.is_file(), "policy": service.policy}, indent=2))
    return 0


def cmd_preview(a):
    """Owner-local preview of what would be offered now. No budget is spent, no gateway is contacted."""
    try:
        service = Service(a.dir)
        busy = load_busy(service.events_file, service.tz)
        slots = suggest(service.policy, busy, C.now_ts(),
                        {"duration_minutes": a.duration, "after": a.after, "before": a.before})
    except ValueError as e:
        die(str(e))
    except Refuse as r:
        die(r.code)
    print(json.dumps({"ok": True, "slots": [{"start": C.iso(s), "end": C.iso(e)} for s, e in slots]}, indent=2))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bot_calendar", description="private calendar capability service")
    sp = ap.add_subparsers(dest="cmd")
    sp.required = True
    pi = sp.add_parser("init")
    pi.add_argument("--dir", required=True)
    pi.add_argument("--gateway", required=True)
    pi.add_argument("--bot", required=True)
    pi.add_argument("--events", help="private events file (default: events.json in --dir)")
    ps = sp.add_parser("serve")
    ps.add_argument("--dir", required=True)
    ps.add_argument("--port", type=int, default=0)
    pc = sp.add_parser("check")
    pc.add_argument("--dir", required=True)
    pp = sp.add_parser("preview")
    pp.add_argument("--dir", required=True)
    pp.add_argument("--duration", type=int, required=True)
    pp.add_argument("--after")
    pp.add_argument("--before")
    a = ap.parse_args(argv)
    return {"init": cmd_init, "serve": cmd_serve, "check": cmd_check, "preview": cmd_preview}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
