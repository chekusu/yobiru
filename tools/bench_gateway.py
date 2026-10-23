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

Routes: jobs (GET /v1/connector/jobs with a connector credential), inbox (GET /v1/inbox with a source bot
credential), profile (GET /v1/bots/<ref>, public). It prints one JSON line: requests per second answered with 200,
latency percentiles, status counts and the size of the database and its write-ahead log after the run.
"""
import argparse
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
        cfg_path.write_text(json.dumps(cfg))
        gw.restart()
        say("load: %s, %d concurrent, %.0fs, %d gateway process(es)" % (a.route, a.concurrency, a.seconds,
                                                                         a.processes))
        result = load(gw.port, a.route, bots, a.clients, a.concurrency, a.seconds)
    finally:
        gw.stop()
    db = gw.home / "gateway.sqlite3"
    wal = Path(str(db) + "-wal")
    result.update({"route": a.route, "processes": a.processes, "concurrency": a.concurrency, "bots": a.bots,
                   "messages": stored, "seconds": a.seconds, "db_bytes": db.stat().st_size,
                   "wal_bytes": wal.stat().st_size if wal.exists() else 0, "cpus": os.cpu_count()})
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
