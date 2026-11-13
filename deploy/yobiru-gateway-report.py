#!/usr/bin/python3 -I
"""Operations snapshot of the official gateway (bots.yobiru.com), for the twice-daily report (stdlib only).

Installed once on the server, owned by root, next to the deploy script:
  install -o root -g root -m 0755 deploy/yobiru-gateway-report.py /usr/local/sbin/yobiru-gateway-report
and run every two minutes by deploy/yobiru-gateway-report.timer as the gateway's user, with read access to the
journal and nginx logs (deploy/yobiru-gateway-report.service). Each run:

  1. reads counts from the gateway database (read-only connection, aggregate numbers only),
  2. samples the host: connections held on the gateway port (long-polls of online connectors), the gateway's memory
     and CPU, its units, two loopback probes (/v1/health and one public card), disk and load,
  3. reads the gateway, mail relay and Litestream journal entries since the previous run, and the bots.yobiru.com
     nginx log since its last position, and keeps per-run counts (requests by status, error lines),
  4. appends that sample to <state>/samples.jsonl (8 days kept), writes <state>/latest.json and uploads it to the
     blob store's bucket as ops/gateway-report/latest.json, plus one copy per day under ops/gateway-report/daily/.

Nothing it writes or uploads holds an address, a token, a handle or a message: counts, sizes, timings and log lines
with addresses and long tokens masked. `--print` prints the snapshot and changes nothing (no state, no upload).
"""
import argparse
import datetime
import hashlib
import hmac
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = 1
TZ_OFFSET = 8 * 3600                       # report days are Asia/Shanghai days (UTC+8)
KEEP_SECONDS = 8 * 86400
UNITS = ("yobiru-gateway.service", "yobiru-gateway-mail.service", "yobiru-litestream.service", "nginx.service")
UNIT_PROPS = ("Id", "LoadState", "ActiveState", "SubState", "NRestarts", "ActiveEnterTimestamp", "MemoryCurrent",
              "MemoryPeak", "MemoryHigh", "MemoryMax", "CPUUsageNSec", "TasksCurrent", "TasksMax", "Result")
TIMER_PROPS = ("Id", "ActiveState", "LastTriggerUSec", "NextElapseUSecRealtime", "Result")
PENDING = ("queued", "claimed", "started", "review", "uncertain")
REQUEST_RE = re.compile(r"^(?:\d{4}-\d\d-\d\d \d\d:\d\d:\d\d )?\S+ (GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) (.+) "
                        r"(\d{3}|-)$")
# First match wins. "start" lines are counted but never quoted.
ERROR_PATTERNS = (
    ("internal_error", re.compile(r"internal error")),
    ("storage_error", re.compile(r"storage error")),
    ("aborted", re.compile(r"request aborted")),
    ("recycled", re.compile(r"process \d+ recycled")),     # replaced past limits.process_memory_mb: counted only
    ("process_exit", re.compile(r"process \d+ exited|keep exiting|process failed")),
    ("maintenance", re.compile(r"maintenance failed|change watch failed|blob delete failed|hosted wakes failed")),
    ("mail", re.compile(r"mail relay failed|mail relay: sent \d+, failed [1-9]")),
    ("litestream_error", re.compile(r"level=ERROR|replicate error|sync error")),
    ("warning", re.compile(r"WARNING|level=WARN")),
    ("traceback", re.compile(r"Traceback|Error:|Exception")),
    ("start", re.compile(r"serving on|mail relay: \S+:\d+ \(|litestream v?\d|initialized db")),
)
ROUTE_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
NGINX_RE = re.compile(r'^\S+ \S+ \S+ \[[^\]]+\] "(?:[^"\\]|\\.)*" (\d{3}) ')
NGINX_TIME_RE = re.compile(r"(?:\brt=|request_time=)(\d+(?:\.\d+)?)")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{24,}")
HANDLE_RE = re.compile(r"@[A-Za-z0-9_.-]{1,64}")
IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b|\b[0-9a-f]{0,4}(?::[0-9a-f]{0,4}){3,7}\b", re.I)


def scrub(text, limit=160):
    text = EMAIL_RE.sub("<email>", text)
    text = HANDLE_RE.sub("@<handle>", text)
    text = TOKEN_RE.sub("<redacted>", text)
    text = IP_RE.sub("x.x.x.x", text)
    return text[:limit]


def local_time(ts):
    return datetime.datetime.fromtimestamp(int(ts) + TZ_OFFSET, datetime.timezone.utc)


def day_of(ts):
    return local_time(ts).strftime("%Y-%m-%d")


def run(cmd, timeout=30):
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout, check=False)
        return out.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return ""


# ---- database

def db_stats(path, now):
    db = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(str(path)), uri=True, timeout=10)
    try:
        db.execute("PRAGMA query_only=1")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        one = lambda q, a=(): db.execute(q, a).fetchone()[0]  # noqa: E731
        out = {
            "owners": dict(db.execute("SELECT kind, COUNT(*) FROM owners GROUP BY kind").fetchall()),
            "owners_verified": one("SELECT COUNT(*) FROM owners WHERE email_hash IS NOT NULL"),
            "bots": dict(db.execute("SELECT state, COUNT(*) FROM bots GROUP BY state").fetchall()),
            "actors": one("SELECT COUNT(*) FROM actors"),
            "follows_active": one("SELECT COUNT(*) FROM follows WHERE state='active'"),
            "contacts": dict(db.execute("SELECT status, COUNT(*) FROM contacts GROUP BY status").fetchall()),
            "messages_total": one("SELECT COUNT(*) FROM messages"),
            "messages_pending": one("SELECT COUNT(*) FROM messages WHERE state IN (%s)" % ",".join("?" * len(PENDING)),
                                    PENDING),
        }
        # Totals as of fixed points in the past (rows that still exist), for growth rates.
        points = {"now": 0, "12h": 12 * 3600, "1d": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
        out["totals_at"] = {}
        for name, ago in points.items():
            t = now - ago
            out["totals_at"][name] = {
                "owners": one("SELECT COUNT(*) FROM owners WHERE kind='self_enrolled' AND created_ts<=?", (t,)),
                "bots": one("SELECT COUNT(*) FROM bots WHERE created_ts<=?", (t,)),
                "actors": one("SELECT COUNT(*) FROM actors WHERE created_ts<=?", (t,)),
            }
        # New rows per report day (UTC+8), last 35 days.
        since = now - 35 * 86400
        daily = {}
        for table, extra in (("owners", " AND kind='self_enrolled'"), ("bots", ""), ("actors", "")):
            for d, n in db.execute("SELECT date(created_ts+?, 'unixepoch'), COUNT(*) FROM %s WHERE created_ts>=?%s "
                                   "GROUP BY 1" % (table, extra), (TZ_OFFSET, since)):
                daily.setdefault(d, {})[table] = n
        for d, n, bots, actors in db.execute(
                "SELECT date(created_ts+?, 'unixepoch'), COUNT(*), COUNT(DISTINCT bot_id), COUNT(DISTINCT actor_id) "
                "FROM messages WHERE created_ts>=? GROUP BY 1", (TZ_OFFSET, since)):
            daily.setdefault(d, {}).update({"messages": n, "message_bots": bots, "message_actors": actors})
        out["daily"] = dict(sorted(daily.items()))
        out["first_owner_ts"] = one("SELECT MIN(created_ts) FROM owners WHERE kind='self_enrolled'")
        # Activity: bots whose worker checked in, and who sent or received messages (one pass over messages).
        spans = (("1h", 3600), ("12h", 12 * 3600), ("24h", 86400), ("7d", 7 * 86400))
        cols = ", ".join("SUM(created_ts>=%d), COUNT(DISTINCT CASE WHEN created_ts>=%d THEN bot_id END), "
                         "COUNT(DISTINCT CASE WHEN created_ts>=%d THEN actor_id END)" % ((now - ago,) * 3)
                         for _, ago in spans)
        row = db.execute("SELECT %s FROM messages WHERE created_ts>=?" % cols, (now - 7 * 86400,)).fetchone()
        out["active"] = {}
        for i, (name, ago) in enumerate(spans):
            out["active"][name] = {
                "worker_bots": one("SELECT COUNT(DISTINCT bot_id) FROM workers WHERE revoked_ts IS NULL AND "
                                   "last_seen_ts>=?", (now - ago,)),
                "messages": row[3 * i] or 0, "message_bots": row[3 * i + 1], "message_actors": row[3 * i + 2],
            }
        out["workers"] = dict(db.execute("SELECT adapter, COUNT(*) FROM workers WHERE revoked_ts IS NULL "
                                         "GROUP BY adapter").fetchall())
        if "bot_profiles" in tables:
            agents = {}
            for (raw,) in db.execute("SELECT agents FROM bot_profiles"):
                try:
                    listed = json.loads(raw or "[]")
                except ValueError:
                    continue
                for a in listed if isinstance(listed, list) else []:
                    if isinstance(a, str) and re.match(r"^[a-z0-9_-]{1,32}$", a):
                        agents[a] = agents.get(a, 0) + 1
            out["agents"] = dict(sorted(agents.items(), key=lambda kv: -kv[1]))
        mail = {"outbox_waiting": one("SELECT COUNT(*) FROM mail_outbox WHERE sent_ts IS NULL AND address IS NOT NULL"),
                "outbox_errors_24h": one("SELECT COUNT(*) FROM mail_outbox WHERE error IS NOT NULL AND created_ts>=?",
                                         (now - 86400,)),
                "codes_24h": one("SELECT COUNT(*) FROM mail_outbox WHERE created_ts>=?", (now - 86400,))}
        if "mail_notices" in tables:
            mail["notices_waiting"] = one("SELECT COUNT(*) FROM mail_notices WHERE sent_ts IS NULL AND "
                                          "address IS NOT NULL")
        out["mail"] = mail
        if "hosted_wakes" in tables:
            cols = {r[1] for r in db.execute("PRAGMA table_info(hosted_wakes)")}
            out["hosted_wakes"] = {
                "total": one("SELECT COUNT(*) FROM hosted_wakes"),
                "disabled": one("SELECT COUNT(*) FROM hosted_wakes WHERE disabled_ts IS NOT NULL")
                if "disabled_ts" in cols else None,
                "failing": one("SELECT COUNT(*) FROM hosted_wakes WHERE failures>0") if "failures" in cols else None,
            }
        return out
    finally:
        db.close()


# ---- host

def unit_props(units, props):
    text = run(["systemctl", "show", "--timestamp=unix", "-p", ",".join(props), "--"] + list(units))
    out, cur = [], {}
    for line in text.splitlines() + [""]:
        if not line.strip():
            if cur:
                out.append(cur)
            cur = {}
            continue
        k, _, v = line.partition("=")
        if v.startswith("@") and v[1:].isdigit():
            v = int(v[1:])
        elif v.isdigit():
            v = int(v)
        elif v in ("[not set]", "infinity", ""):
            v = None
        cur[k] = v
    return {u.get("Id"): u for u in out if u.get("Id")}


def timers():
    names = [line.split()[0] for line in run(["systemctl", "list-units", "--all", "--type=timer", "--plain",
                                               "--no-legend", "yobiru*", "*backup*"]).splitlines() if line.strip()]
    return unit_props(names, TIMER_PROPS) if names else {}


def held_connections(port):
    """Established TCP connections whose local port is the gateway's (each online connector holds one)."""
    hexport = ":%04X" % port
    n = 0
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(name) as f:
                next(f, None)
                for line in f:
                    parts = line.split()
                    if len(parts) > 3 and parts[1].endswith(hexport) and parts[3] == "01":
                        n += 1
        except OSError:
            pass
    return n


def probe(url, tries=3):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    times, codes = [], []
    for _ in range(tries):
        start = time.monotonic()
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                       "User-Agent": "yobiru-gateway-report"})
            with opener.open(req, timeout=10) as resp:
                resp.read(65536)
                codes.append(resp.status)
        except urllib.error.HTTPError as e:
            codes.append(e.code)
        except (OSError, ValueError):
            codes.append(0)
        times.append(round((time.monotonic() - start) * 1000, 1))
    times.sort()
    return {"ms": times[len(times) // 2], "max_ms": times[-1], "codes": codes}


def host_stats(paths):
    out = {"cpus": os.cpu_count()}
    try:
        out["load"] = [float(x) for x in Path("/proc/loadavg").read_text().split()[:3]]
    except (OSError, ValueError):
        pass
    try:
        mem = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines() if ":" in line)
        out["mem_total"] = int(mem["MemTotal"].split()[0]) * 1024
        out["mem_available"] = int(mem["MemAvailable"].split()[0]) * 1024
    except (OSError, KeyError, ValueError):
        pass
    disks = {}
    for p in paths:
        try:
            st = os.statvfs(p)
            disks[p] = {"total": st.f_blocks * st.f_frsize, "free": st.f_bavail * st.f_frsize}
        except OSError:
            pass
    out["disks"] = disks
    try:
        out["uptime_s"] = int(float(Path("/proc/uptime").read_text().split()[0]))
    except (OSError, ValueError):
        pass
    return out


def gateway_config(home):
    try:
        cfg = json.loads((home / "gateway.json").read_text())
    except (OSError, ValueError):
        return {}
    keep = ("max_concurrent_requests", "max_waiting_connectors", "max_actors", "max_self_enrolled_owners",
            "max_messages", "pending_total", "enroll_per_hour", "bot_enroll_per_hour", "email_codes_per_hour")
    limits = cfg.get("limits") or {}
    return {"processes": cfg.get("processes", 1), "port": cfg.get("port", 7780),
            "external_checkpoints": cfg.get("external_checkpoints", False),
            "limits": {k: limits[k] for k in keep if k in limits}}


def file_sizes(home):
    out = {}
    for name in ("gateway.sqlite3", "gateway.sqlite3-wal"):
        try:
            out[name] = (home / name).stat().st_size
        except OSError:
            out[name] = None
    return out


# ---- logs since the previous run

def journal_counts(state_dir, units, first_since="-3min"):
    cursor = state_dir / "journal.cursor"
    cmd = ["journalctl", "--no-pager", "-q", "-o", "json", "--output-fields=MESSAGE,_SYSTEMD_UNIT,PRIORITY"]
    for u in units:
        cmd += ["-u", u]
    if state_dir.is_dir():
        cmd.append("--cursor-file=%s" % cursor)
        if not cursor.exists():
            cmd.append("--since=%s" % first_since)
    else:
        cmd.append("--since=%s" % first_since)
    text = run(cmd, timeout=60)
    return count_journal(text.splitlines())


def count_journal(lines):
    req = {"total": 0, "2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0, "other": 0}
    routes, errors5, events, samples = {}, {}, {}, []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        msg = entry.get("MESSAGE")
        if isinstance(msg, list):
            msg = bytes(x & 0xFF for x in msg if isinstance(x, int)).decode("utf-8", "replace")
        if not isinstance(msg, str):
            continue
        m = REQUEST_RE.match(msg)
        if m:
            _, route, code = m.groups()
            route = route.split(" ")[0]           # "mcp tools/call <tool>" counts as mcp
            if not ROUTE_RE.match(route):
                route = "other"                   # route names only; never a caller-chosen string
            req["total"] += 1
            routes[route] = routes.get(route, 0) + 1
            if code.isdigit():
                req[code[0] + "xx"] = req.get(code[0] + "xx", 0) + 1
                if code.startswith("5"):
                    key = "%s %s" % (route, code)
                    errors5[key] = errors5.get(key, 0) + 1
            else:
                req["other"] += 1
            continue
        unit = (entry.get("_SYSTEMD_UNIT") or "?").replace(".service", "")
        for name, pattern in ERROR_PATTERNS:
            if pattern.search(msg):
                key = "%s:%s" % (unit, name)
                events[key] = events.get(key, 0) + 1
                if name not in ("start", "aborted", "recycled"):  # aborted = a client hung up (long-polls)
                    samples.append(scrub(msg))
                break
    return {"requests": req, "routes": routes, "errors_5xx": errors5, "events": events, "samples": samples[-5:]}


def nginx_counts(state_dir, path, first_tail=0):
    """Status counts (and request times when the log format has them) of nginx lines added since the last run."""
    pos_file = state_dir / "nginx.pos"
    try:
        st = os.stat(path)
    except OSError:
        return None
    try:
        ino, pos = (int(x) for x in pos_file.read_text().split())
    except (OSError, ValueError):
        ino, pos = st.st_ino, max(0, st.st_size - first_tail)   # first run: start at the end
    if ino != st.st_ino or pos > st.st_size:
        pos = 0                                   # rotated or truncated
    counts, times = {}, []
    with open(path, "rb") as f:
        f.seek(pos)
        data = f.read(64 * 1024 * 1024)
        pos = f.tell()
    for raw in data.splitlines():
        line = raw.decode("utf-8", "replace")
        m = NGINX_RE.match(line)
        if not m:
            continue
        counts[m.group(1)] = counts.get(m.group(1), 0) + 1
        t = NGINX_TIME_RE.search(line)
        if t:
            times.append(float(t.group(1)))
    if state_dir.is_dir():
        tmp = pos_file.with_suffix(".tmp")
        tmp.write_text("%d %d\n" % (st.st_ino, pos))
        os.replace(tmp, pos_file)
    times.sort()
    out = {"status": counts}
    if times:
        out["time"] = {"n": len(times), "p50": times[len(times) // 2], "p95": times[int(len(times) * 0.95)],
                       "max": times[-1]}
    return out


# ---- windows over samples

def windows(samples, now):
    out = {}
    for name, span in (("1h", 3600), ("12h", 12 * 3600), ("24h", 86400), ("7d", 7 * 86400)):
        sel = [s for s in samples if s["ts"] > now - span]
        if not sel:
            continue
        w = {"samples": len(sel), "from": sel[0]["ts"]}
        conns = sorted(s.get("conn", 0) for s in sel)
        w["conn"] = {"max": conns[-1], "p95": conns[int(len(conns) * 0.95)], "avg": round(sum(conns) / len(conns), 1)}
        mems = [s["mem"] for s in sel if s.get("mem")]
        if mems:
            w["mem_max"] = max(mems)
        health = sorted(s["health_ms"] for s in sel if s.get("health_ms") is not None)
        card = sorted(s["card_ms"] for s in sel if s.get("card_ms") is not None)
        for key, vals in (("health_ms", health), ("card_ms", card)):
            if vals:
                w[key] = {"p50": vals[len(vals) // 2], "p95": vals[int(len(vals) * 0.95)], "max": vals[-1]}
        w["probe_failures"] = sum(1 for s in sel if s.get("probe_ok") is False)
        cpu = [s for s in sel if s.get("cpu_ns") is not None]
        if len(cpu) >= 2 and cpu[-1]["cpu_ns"] >= cpu[0]["cpu_ns"] and cpu[-1]["ts"] > cpu[0]["ts"]:
            w["cpu_cores_avg"] = round((cpu[-1]["cpu_ns"] - cpu[0]["cpu_ns"]) / 1e9 / (cpu[-1]["ts"] - cpu[0]["ts"]), 3)
        req, routes, err5, events, ngx = {}, {}, {}, {}, {}
        peak_rpm = 0.0
        prev = None
        for s in sel:
            j = s.get("journal") or {}
            for src, dst in ((j.get("requests"), req), (j.get("routes"), routes), (j.get("errors_5xx"), err5),
                             (j.get("events"), events), ((s.get("nginx") or {}).get("status"), ngx)):
                for k, v in (src or {}).items():
                    dst[k] = dst.get(k, 0) + v
            if prev is not None and s["ts"] > prev:
                peak_rpm = max(peak_rpm, (j.get("requests") or {}).get("total", 0) * 60.0 / (s["ts"] - prev))
            prev = s["ts"]
        w["requests"], w["errors_5xx"], w["events"], w["nginx_status"] = req, err5, events, ngx
        w["top_routes"] = dict(sorted(routes.items(), key=lambda kv: -kv[1])[:8])
        w["peak_requests_per_min"] = round(peak_rpm, 1)
        restarts = [s.get("restarts") for s in sel if s.get("restarts") is not None]
        if restarts:
            w["gateway_restarts"] = max(0, restarts[-1] - restarts[0])
        samples_text = [t for s in sel for t in ((s.get("journal") or {}).get("samples") or [])]
        w["error_samples"] = samples_text[-5:]
        out[name] = w
    # Daily peaks of held connections, for the capacity trend.
    peaks = {}
    for s in samples:
        d = day_of(s["ts"])
        peaks[d] = max(peaks.get(d, 0), s.get("conn", 0))
    out["daily_conn_peak"] = peaks
    return out


# ---- upload

def blob_store(home):
    try:
        cfg = json.loads((home / "blobs.json").read_text())
    except (OSError, ValueError):
        return None
    if cfg.get("kind") != "s3":
        return None
    secret_path = Path(cfg["secret_access_key_file"])
    secret_path = secret_path if secret_path.is_absolute() else home / secret_path
    return {"endpoint": cfg["endpoint"].rstrip("/"), "bucket": cfg["bucket"], "region": cfg.get("region", "auto"),
            "key_id": cfg["access_key_id"], "secret": secret_path.read_text().strip()}


def sign_put(store, path, body, now=None):
    """AWS Signature Version 4 headers for a PUT of `body` to `path` (path-style, as the gateway's blob store)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    host = urllib.parse.urlsplit(store["endpoint"]).netloc
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = amz_date[:8]
    payload = hashlib.sha256(body).hexdigest()
    signed = "host;x-amz-content-sha256;x-amz-date"
    canonical = "\n".join(["PUT", path, "", "host:%s\nx-amz-content-sha256:%s\nx-amz-date:%s\n" % (host, payload,
                                                                                                  amz_date),
                           signed, payload])
    scope = "%s/%s/s3/aws4_request" % (day, store["region"])
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    k = ("AWS4" + store["secret"]).encode()
    for part in (day, store["region"], "s3", "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    return {"x-amz-date": amz_date, "x-amz-content-sha256": payload,
            "Authorization": "AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s"
                             % (store["key_id"], scope, signed, sig)}


def upload(store, key, body):
    path = "/%s/%s" % (urllib.parse.quote(store["bucket"], safe=""), urllib.parse.quote(key, safe="/-_.~"))
    headers = sign_put(store, path, body)
    headers.update({"Content-Type": "application/json", "Content-Length": str(len(body))})
    req = urllib.request.Request(store["endpoint"] + path, data=body, method="PUT", headers=headers)
    with urllib.request.build_opener().open(req, timeout=30) as resp:
        return resp.status


# ---- main

def snapshot(a, now):
    home, state_dir = Path(a.gateway_home), Path(a.state_dir)
    cfg = gateway_config(home)
    port = cfg.get("port") or 7780
    snap = {"v": VERSION, "ts": now, "host": os.uname().nodename, "config": cfg}
    try:
        snap["db"] = db_stats(home / "gateway.sqlite3", now)
    except sqlite3.Error as e:
        snap["db_error"] = type(e).__name__
    snap["files"] = file_sizes(home)
    snap["version"] = run(["git", "-C", a.src, "describe", "--tags", "--always"]).strip() or None
    snap["units"] = unit_props(UNITS, UNIT_PROPS)
    snap["timers"] = timers()
    snap["host_stats"] = host_stats(["/", str(home)])
    base = "http://127.0.0.1:%d" % port
    health = probe(base + "/v1/health")
    card = probe(base + "/@%s.json" % a.probe_handle) if a.probe_handle else None
    gw = snap["units"].get("yobiru-gateway.service") or {}
    sample = {"ts": now, "conn": held_connections(port), "mem": gw.get("MemoryCurrent"),
              "cpu_ns": gw.get("CPUUsageNSec"), "restarts": gw.get("NRestarts"),
              "health_ms": health["ms"], "card_ms": card["ms"] if card else None,
              "probe_ok": all(c == 200 for c in health["codes"] + (card["codes"] if card else [])),
              "load1": (snap["host_stats"].get("load") or [None])[0]}
    # --print keeps no positions: it reads the last few minutes of the journal and the end of the nginx log.
    logs_state = Path("/nonexistent") if a.print_only else state_dir
    sample["journal"] = journal_counts(logs_state, ("yobiru-gateway.service", "yobiru-gateway-mail.service",
                                                    "yobiru-litestream.service"), first_since=a.since)
    if a.nginx_log:
        # --print reads the whole current file (nginx rotates it daily); a timer run starts at its end.
        sample["nginx"] = nginx_counts(logs_state, a.nginx_log, first_tail=1 << 40 if a.print_only else 0)
    snap["probes"] = {"health": health, "card": card}
    snap["sample"] = sample
    return snap


def load_samples(path, now):
    out = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    s = json.loads(line)
                except ValueError:
                    continue
                if isinstance(s, dict) and isinstance(s.get("ts"), int) and s["ts"] > now - KEEP_SECONDS:
                    out.append(s)
    except OSError:
        pass
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="yobiru-gateway-report", description=__doc__.split("\n")[0])
    ap.add_argument("--gateway-home", default="/srv/yobiru-gateway/gw")
    ap.add_argument("--src", default="/srv/yobiru-gateway/src")
    ap.add_argument("--state-dir", default=os.environ.get("STATE_DIRECTORY", "/var/lib/yobiru-gateway-report"))
    ap.add_argument("--nginx-log", default="/var/log/nginx/bots.yobiru.com.access.log")
    ap.add_argument("--probe-handle", default="guoyu", help="a published bot whose public card is timed")
    ap.add_argument("--prefix", default="ops/gateway-report/", help="key prefix in the blob store's bucket")
    ap.add_argument("--print", dest="print_only", action="store_true", help="print the snapshot; change nothing")
    ap.add_argument("--since", default="-3min", help="journal start when no position is kept yet, e.g. -24h "
                                                     "(with --print: the span counted)")
    a = ap.parse_args(argv)
    os.umask(0o077)
    now = int(time.time())
    snap = snapshot(a, now)
    state_dir = Path(a.state_dir)
    if a.print_only:
        # Only this run: its --since span differs from the two-minute spans of the timer's samples.
        snap["windows"] = windows([snap["sample"]], now)
        print(json.dumps(snap, indent=1, sort_keys=True))
        return 0
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    samples = load_samples(state_dir / "samples.jsonl", now) + [snap["sample"]]
    tmp = state_dir / "samples.jsonl.tmp"
    with open(tmp, "w") as f:
        for s in samples:
            f.write(json.dumps(s, separators=(",", ":"), sort_keys=True) + "\n")
    os.replace(tmp, state_dir / "samples.jsonl")
    snap["windows"] = windows(samples, now)
    body = json.dumps(snap, separators=(",", ":"), sort_keys=True).encode()
    (state_dir / "latest.json.tmp").write_bytes(body)
    os.replace(state_dir / "latest.json.tmp", state_dir / "latest.json")
    store = blob_store(Path(a.gateway_home))
    if store is None:
        print("no s3 blob store configured; kept %s only" % (state_dir / "latest.json"), file=sys.stderr)
        return 0
    try:
        upload(store, a.prefix + "latest.json", body)
        # The day's copy is refreshed once an hour, so it ends the day close to that day's last state.
        marker = state_dir / "daily.last"
        hour = local_time(now).strftime("%Y-%m-%dT%H")
        if not marker.exists() or marker.read_text().strip() != hour:
            upload(store, a.prefix + "daily/%s.json" % day_of(now), body)
            marker.write_text(hour + "\n")
    except (OSError, urllib.error.URLError) as e:
        print("upload failed: %s" % type(e).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
