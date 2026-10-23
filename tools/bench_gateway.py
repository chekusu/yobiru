#!/usr/bin/env python3
"""Load benchmark for the Friends gateway: a fresh gateway with synthetic bots and stored history, then many
concurrent agents polling it over loopback, the way connectors poll every few seconds.

Everything is synthetic and stays in a new temporary directory (or --keep DIR). The gateway is the real
public_bots/bot_gateway.py from this tree, started as `serve`; the clients are separate processes with raw sockets
so that the measuring side is not what saturates first. Run it before a release that touches the gateway's request
path, and compare with the previous release on the same machine:

  python3 tools/bench_gateway.py                                  # 1,000 bots, 100,000 messages, 1 process
  python3 tools/bench_gateway.py --processes 8 --concurrency 256  # the multi-process gateway
  python3 tools/bench_gateway.py --route inbox --seconds 30
  python3 tools/bench_gateway.py --processes 8 --hold 5000        # 5,000 connectors waiting for work

Routes: jobs (GET /v1/connector/jobs with a connector credential), inbox (GET /v1/inbox with a source bot
credential), profile (GET /v1/bots/<ref>, public). It prints one JSON line: requests per second answered with 200,
latency percentiles, status counts and the size of the database and its write-ahead log after the run.

--hold N instead keeps N connector polls waiting for a change (one per bot, so at most --bots), sends --probes chats
to some of those bots and reports how soon their polls returned, with the gateway's CPU time and memory while it
holds them (Linux). The process may need a higher open-file limit (ulimit -n) for thousands of connections.
"""
import argparse
import asyncio
import json
import multiprocessing
import os
import random
import socket
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "public_bots"))

from support.public_harness import FriendsGateway  # noqa: E402
import bot_gateway as G  # noqa: E402

BIG = 10 ** 9
# Quotas and rates that would refuse the synthetic setup; everything else keeps the gateway's defaults.
UNLIMITED = {"max_actors", "max_self_enrolled_owners", "max_messages", "pending_total", "pending_per_bot",
             "pending_per_contact", "pending_contact_requests_per_bot", "conversations_per_contact",
             "turns_per_conversation", "max_following_per_bot"}


def limits(concurrency):
    out = {k: BIG for k in G.LIMIT_DEFAULTS
           if k in UNLIMITED or "per_hour" in k or "per_minute" in k or "per_day" in k}
    out["max_concurrent_requests"] = concurrency
    return out


def seed(gw, bots, messages, chats, say):
    """Bots with a native binding and a connector, some live chats (queued jobs for the connectors) and completed
    history copied in bulk up to `messages` rows."""
    out, t0 = [], time.time()
    for i in range(bots):
        bid = gw.bot("bench-%05d" % i)
        src = gw.source_for(bid)
        _, wid = gw.native(bid)
        ctok = gw.ok("POST", "/v1/owner/bots/%s/workers/%s/connector" % (bid, wid), {}, gw.owner_token)["token"]
        out.append({"bot_id": bid, "source": src, "connector": ctok})
        if (i + 1) % 200 == 0:
            say("bots %d/%d (%.0fs)" % (i + 1, bots, time.time() - t0))
    for i in range(min(chats, bots)):
        a, b = out[i], out[(i + 1) % bots]
        if a is b:
            break
        gw.follow(a["source"], b["bot_id"])
        gw.follow(b["source"], a["bot_id"])
        gw.chat(gw.conversation(b["bot_id"], a["source"]), a["source"], "hello %d" % i)
    db = sqlite3.connect(str(gw.home / "gateway.sqlite3"))
    try:
        cols = [r[1] for r in db.execute("PRAGMA table_info(messages)")]
        have, k = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0
        while have and have < messages:
            # Copies get ids outside the API's alphabet ("-c<k>"), so they are history only, never addressable.
            pick = ",".join("%s||'-c%d'" % (c, k) if c in ("id", "op_key") else
                            "'completed'" if c == "state" else
                            "CAST(strftime('%s','now') AS INTEGER)" if c == "finished_ts" else c for c in cols)
            db.execute("INSERT INTO messages(%s) SELECT %s FROM messages WHERE id NOT LIKE '%%-%%' LIMIT %d"
                       % (",".join(cols), pick, messages - have))
            db.commit()
            have, k = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], k + 1
    finally:
        db.close()
    say("seeded %d bots and %d messages in %.0fs" % (bots, have, time.time() - t0))
    return out, have


def target(route, bot):
    if route == "jobs":
        return "/v1/connector/jobs", bot["connector"]
    if route == "inbox":
        return "/v1/inbox", bot["source"]
    return "/v1/bots/%s" % bot["bot_id"], None


def client(port, route, bots, threads, seconds, seed_, queue):
    lat, codes, lock = [], {}, threading.Lock()
    stop = time.time() + seconds

    def run(n):
        rnd = random.Random(seed_ * 1000 + n)
        while time.time() < stop:
            path, token = target(route, bots[rnd.randrange(len(bots))])
            head = "GET %s HTTP/1.1\r\nHost: bench\r\nAccept: application/json\r\nConnection: close\r\n" % path
            if token:
                head += "Authorization: Bearer %s\r\n" % token
            t0 = time.time()
            try:
                s = socket.create_connection(("127.0.0.1", port), timeout=60)
                s.sendall((head + "\r\n").encode())
                buf = b""
                while True:
                    data = s.recv(65536)
                    if not data:
                        break
                    buf += data
                s.close()
                code = int(buf[9:12]) if len(buf) >= 12 else 0
            except (OSError, ValueError):
                code = -1
            with lock:
                lat.append(time.time() - t0)
                codes[code] = codes.get(code, 0) + 1
    ts = [threading.Thread(target=run, args=(n,)) for n in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    queue.put((lat, codes))


def load(port, route, bots, clients, concurrency, seconds):
    queue = multiprocessing.Queue()
    per = max(1, concurrency // clients)
    procs = [multiprocessing.Process(target=client, args=(port, route, bots, per, seconds, i, queue))
             for i in range(clients)]
    for p in procs:
        p.start()
    lat, codes = [], {}
    for _ in procs:
        part, counts = queue.get()
        lat += part
        for k, v in counts.items():
            codes[k] = codes.get(k, 0) + v
    for p in procs:
        p.join()
    lat.sort()

    def pct(q):
        return round(lat[min(len(lat) - 1, int(len(lat) * q))] * 1000, 1) if lat else None
    return {"ok_per_second": round(codes.get(200, 0) / seconds, 1), "requests": len(lat),
            "p50_ms": pct(0.50), "p95_ms": pct(0.95), "p99_ms": pct(0.99),
            "status": {str(k): v for k, v in sorted(codes.items())}}


def gateway_usage(pid):
    """CPU seconds and resident bytes of a gateway process and its children, from /proc (None elsewhere)."""
    if not os.path.isdir("/proc/%d" % pid):
        return None, None
    pids, cpu, rss = [pid], 0.0, 0
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                stat = Path("/proc/%s/stat" % entry).read_text()
            except OSError:
                continue
            if int(stat.rsplit(")", 1)[1].split()[1]) == pid:
                pids.append(int(entry))
    for p in pids:
        try:
            f = Path("/proc/%d/stat" % p).read_text().rsplit(")", 1)[1].split()
        except OSError:
            continue
        cpu += (int(f[11]) + int(f[12])) / float(os.sysconf("SC_CLK_TCK"))
        rss += int(f[21]) * os.sysconf("SC_PAGE_SIZE")
    return cpu, rss


async def http(port, method, path, token=None, body=None, timeout=60):
    reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 15)
    data = json.dumps(body).encode() if body is not None else b""
    head = "%s %s HTTP/1.1\r\nHost: bench\r\nAccept: application/json\r\nConnection: close\r\n" % (method, path)
    if token:
        head += "Authorization: Bearer %s\r\n" % token
    if body is not None:
        head += "Content-Type: application/json\r\nContent-Length: %d\r\n" % len(data)
    writer.write((head + "\r\n").encode() + data)
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(), timeout)
    writer.close()
    status, _, rest = raw.partition(b"\r\n\r\n")
    return int(status[9:12]), json.loads(rest) if rest else {}


def listing_cursor(status, obj):
    """Only successful, well-formed listings can establish or change a held poll's cursor."""
    if status != 200 or not isinstance(obj, dict) or obj.get("ok") is not True:
        return None
    cursor = obj.get("cursor")
    if not isinstance(cursor, str) or len(cursor) != 32 or any(c not in "0123456789abcdef" for c in cursor):
        return None
    for key, ident in (("jobs", "job_id"), ("checks", "check_id"), ("replies", "message_id")):
        items = obj.get(key)
        if not isinstance(items, list) or any(not isinstance(item, dict) or
                                             not isinstance(item.get(ident), str) for item in items):
            return None
    return cursor


async def hold(port, pid, bots, probes, wait=25):
    """Every bot's connector waits for a change; then a visitor writes to `probes` of them."""
    settle = min(8.0, wait / 3.0)
    gate = asyncio.Semaphore(64)

    async def listing(bot):
        async with gate:
            cursor = listing_cursor(*(await http(port, "GET", "/v1/connector/jobs", bot["connector"])))
            if cursor is None:
                raise ValueError("invalid initial connector listing")
            return cursor
    cursors = await asyncio.gather(*(listing(b) for b in bots))
    done = {}

    async def held_poll(bot, cursor):
        t0 = time.monotonic()
        try:
            status, obj = await http(port, "GET", "/v1/connector/jobs?wait=%d&cursor=%s" % (wait, cursor),
                                     bot["connector"], timeout=wait + 30)
        except (OSError, ValueError, asyncio.TimeoutError):
            status, obj = -1, {}
        finished, current = time.monotonic(), listing_cursor(status, obj)
        done[bot["bot_id"]] = {"finished": finished, "status": status, "valid": current is not None,
                               "changed": current is not None and current != cursor, "seconds": finished - t0,
                               "jobs": {j["job_id"] for j in obj["jobs"]} if current is not None else set()}
    cpu0, _ = gateway_usage(pid)
    waits = [asyncio.ensure_future(held_poll(b, c)) for b, c in zip(bots, cursors)]
    await asyncio.sleep(settle)
    cpu1, rss = gateway_usage(pid)
    actor = (await http(port, "POST", "/v1/actors", body={"display_name": "Bench visitor"}))[1]["token"]
    sent, probe_statuses = {}, {}
    selected = random.Random(7).sample(bots, min(probes, len(bots)))
    for bot in selected:
        cid = (await http(port, "POST", "/v1/bots/%s/conversations" % bot["bot_id"], actor,
                          {"op_key": "probe%016x" % random.getrandbits(64)}))[1]["conversation_id"]
        started = time.monotonic()  # A held poll may finish before this sender receives its receipt.
        try:
            status, obj = await http(port, "POST", "/v1/conversations/%s/messages" % cid, actor,
                                     {"op_key": "probe%016x" % random.getrandbits(64), "kind": "chat", "text": "probe"})
        except (OSError, ValueError, asyncio.TimeoutError):
            status, obj = -1, {}
        probe_statuses[str(status)] = probe_statuses.get(str(status), 0) + 1
        if status == 202 and isinstance(obj, dict) and obj.get("ok") is True and isinstance(obj.get("message_id"), str):
            sent[bot["bot_id"]] = (started, obj["message_id"])
    await asyncio.sleep(settle / 2)
    cpu2, _ = gateway_usage(pid)
    await asyncio.gather(*waits)
    woke = sorted(done[b]["finished"] - t for b, (t, mid) in sent.items()
                  if done[b]["changed"] and mid in done[b]["jobs"] and done[b]["finished"] >= t)
    statuses = {}
    for result in done.values():
        status = result["status"]
        statuses[str(status)] = statuses.get(str(status), 0) + 1
    held = [d for d in done.values() if d["valid"] and not d["changed"] and d["seconds"] >= wait - 1]

    def ms(x):
        return round(x * 1000, 1)
    return {"held": len(bots), "held_full_wait": len(held), "status": statuses,
            "invalid_listings": sum(d["status"] == 200 and not d["valid"] for d in done.values()),
            "probes": len(selected), "probe_status": probe_statuses,
            "woken": len(woke), "wake_p50_ms": ms(woke[len(woke) // 2]) if woke else None,
            "wake_max_ms": ms(woke[-1]) if woke else None,
            "gateway_cpu_seconds_while_holding": round(cpu2 - cpu1, 2) if cpu1 is not None else None,
            "gateway_cpu_seconds_to_start_holding": round(cpu1 - cpu0, 2) if cpu0 is not None else None,
            "gateway_rss_bytes_while_holding": rss}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--bots", type=int, default=1000)
    ap.add_argument("--messages", type=int, default=100000, help="stored messages, mostly completed history")
    ap.add_argument("--chats", type=int, default=200, help="bot pairs with a live chat (queued jobs)")
    ap.add_argument("--processes", type=int, default=1, help="gateway request processes (gateway.json processes)")
    ap.add_argument("--route", choices=("jobs", "inbox", "profile"), default="jobs")
    ap.add_argument("--clients", type=int, default=4, help="client processes")
    ap.add_argument("--concurrency", type=int, default=64,
                    help="concurrent requests in total; also the gateway's max_concurrent_requests per process")
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--hold", type=int, help="instead: this many connector polls waiting for a change")
    ap.add_argument("--probes", type=int, default=20, help="with --hold: chats that end some of the waits")
    ap.add_argument("--wait-seconds", type=int, default=25, help="with --hold: how long each poll may wait")
    ap.add_argument("--keep", help="use (and keep) this directory instead of a temporary one")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    def say(text):
        if not a.quiet:
            print(text, file=sys.stderr, flush=True)
    root = Path(a.keep) if a.keep else Path(tempfile.mkdtemp(prefix="yobiru-bench-"))
    root.mkdir(parents=True, exist_ok=True)
    gw = FriendsGateway(root, limits=limits(a.concurrency)).start()
    try:
        bots, stored = seed(gw, a.bots, a.messages, a.chats, say)
        cfg_path = gw.home / "gateway.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["processes"] = a.processes
        if a.hold:
            cfg["limits"]["max_waiting_connectors"] = a.hold
        cfg_path.write_text(json.dumps(cfg))
        gw.restart()
        if a.hold:
            say("holding %d waiting connector polls, %d gateway process(es)" % (min(a.hold, len(bots)), a.processes))
            result = asyncio.run(hold(gw.port, gw.service.proc.pid, bots[:a.hold], a.probes, a.wait_seconds))
        else:
            say("load: %s, %d concurrent, %.0fs, %d gateway process(es)" % (a.route, a.concurrency, a.seconds,
                                                                             a.processes))
            result = load(gw.port, a.route, bots, a.clients, a.concurrency, a.seconds)
    finally:
        gw.stop()
    db = gw.home / "gateway.sqlite3"
    wal = Path(str(db) + "-wal")
    result.update({"route": "wait" if a.hold else a.route, "processes": a.processes, "concurrency": a.concurrency, "bots": a.bots,
                   "messages": stored, "seconds": a.seconds, "db_bytes": db.stat().st_size,
                   "wal_bytes": wal.stat().st_size if wal.exists() else 0, "cpus": os.cpu_count()})
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
