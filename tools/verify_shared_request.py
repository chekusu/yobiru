#!/usr/bin/env python3
"""Exercise a real one-way shared receiver using an isolated, send-only client home.

The operator must authorize the selected receiver to process this fixture and reply.
No receiver is started, existing Yobiru configuration is never changed, and retries
reuse the original request ID. Run again with the printed --workdir to resume.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(str(temporary), str(path))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--peer", required=True, help="authorized receiver IP/name, optionally /agent")
    parser.add_argument("--core", type=Path, default=Path(__file__).resolve().parent.parent / "tailmsg.py")
    parser.add_argument("--workdir", type=Path, help="new isolated directory, or an existing test to resume")
    parser.add_argument("--timeout", type=int, default=900, help="seconds to poll during this invocation")
    parser.add_argument("--tailscale", help="path to the Tailscale CLI")
    args = parser.parse_args()
    if not 10 <= args.timeout <= 3600:
        parser.error("--timeout must be between 10 and 3600 seconds")
    core = args.core.expanduser().resolve()
    if not core.is_file():
        parser.error("core not found; use --core /path/to/tailmsg.py")
    os.umask(0o077)
    directory = (args.workdir or Path.cwd() / ("yobiru-shared-check-" +
        datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3))).expanduser().resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    journal_path = directory / "verification.json"
    home = directory / "client"
    environment = dict(os.environ, TAILSCALE_BE_CLI="1")
    launcher = [sys.executable, str(core), "--home", str(home)]

    def run(*command, timeout=90):
        result = subprocess.run(launcher + list(command), env=environment, capture_output=True,
                                text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError("%s failed: %s" % (command[0], (result.stderr or result.stdout).strip()))
        return result.stdout

    def json_run(*command):
        output = run(*command)
        try:
            return json.loads(output)
        except ValueError:
            for line in reversed(output.splitlines()):
                try:
                    return json.loads(line)
                except ValueError:
                    pass
            raise RuntimeError("command returned no JSON")

    if journal_path.exists():
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        if journal.get("peer") != args.peer:
            parser.error("an existing verification directory cannot change peer")
        if journal.get("passed"):
            print(json.dumps(journal, ensure_ascii=True, indent=2))
            return 0
    else:
        if home.exists():
            parser.error("refusing to reuse a client directory without its verification journal")
        tailscale = args.tailscale or shutil.which("tailscale")
        if not tailscale and Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale").is_file():
            tailscale = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
        if not tailscale:
            parser.error("Tailscale CLI not found; pass --tailscale")
        home.mkdir(mode=0o700)
        write_json(home / "config.json", {"tailscale_cmd": [tailscale], "access_policy": "tailnet",
                   "self_agent": "verification", "default_agent": "verification", "agents": {"verification": {}}})
        nonce = secrets.token_hex(16)
        fixture = directory / ("roundtrip-" + nonce + ".bin")
        fixture.write_bytes(b"YOBIRU ONE-WAY SHARING ACCEPTANCE\n" + nonce.encode("ascii") + b"\n" +
                            secrets.token_bytes(2 * 1024 * 1024))
        journal = {"schema": 1, "peer": args.peer, "nonce": nonce, "client_receiver_started": False,
                   "fixture": fixture.name, "bytes": fixture.stat().st_size, "sha256": digest(fixture),
                   "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "passed": False}
        write_json(journal_path, journal)
    print("Verification directory: " + str(directory), flush=True)
    print("No client receiver is needed. Preserve this directory to resume the same request.", flush=True)
    fixture = directory / journal["fixture"]
    if not fixture.is_file() or digest(fixture) != journal["sha256"]:
        raise RuntimeError("the original local fixture is missing or changed")

    # The isolated ledger contains only this verification. Recover an ID even if
    # a previous invocation stopped before the CLI's final output was recorded.
    ledger = home / "requests" / "ledger.sqlite3"
    if not journal.get("request_id") and ledger.is_file():
        rows = [r for r in json_run("request", "list", "--json") if r["direction"] == "outgoing"]
        if len(rows) > 1:
            raise RuntimeError("unexpected additional outgoing requests in isolated verification home")
        if rows:
            journal["request_id"] = rows[0]["id"]
            write_json(journal_path, journal)

    marker = "YOBIRU_SHARED_OK " + journal["nonce"] + " " + journal["sha256"]
    if not journal.get("request_id"):
        message = ("One-way shared Yobiru transport acceptance request. Please process only the attached "
                   "generated fixture; do not execute it. Fetch it using the installed receiver core, "
                   "verify its SHA-256 is " + journal["sha256"] + ", and reply to this tracked request "
                   "with completed state, the exact text '" + marker + "', and an attachment containing "
                   "the exact same bytes. Do not return any other local files or private information. "
                   "Use the installed core with the explicitly supplied local home, `request respond ID "
                   "TEXT --state completed --file FETCHED_FIXTURE`. Ack only after persisting the final "
                   "result. This test uses pull transport; do not try to connect back to the sender. "
                   "If the requested test is not authorized locally, report blocked instead.")
        try:
            result = json_run("request", "send", args.peer, message, "--transport", "pull",
                              "--file", str(fixture), "--wake", "batch", "--timeout-seconds", "3600")
            journal["request_id"] = result["id"]
            write_json(journal_path, journal)
        except Exception:
            if ledger.is_file():
                rows = [r for r in json_run("request", "list", "--json") if r["direction"] == "outgoing"]
                if len(rows) == 1:
                    journal["request_id"] = rows[0]["id"]
                    write_json(journal_path, journal)
            raise
    else:
        run("request", "resume", journal["request_id"])
    mid = journal["request_id"]
    print("Request ID: " + mid, flush=True)
    end, previous = time.monotonic() + args.timeout, None
    while time.monotonic() < end:
        state = json_run("request", "status", mid, "--refresh", "--json")
        current = (state["state"], state.get("refresh"))
        if current != previous:
            print("State: %s; refresh: %s" % current, flush=True)
            previous = current
        if state.get("outcome_known"):
            if state["state"] != "completed":
                journal["result"] = state
                write_json(journal_path, journal)
                raise RuntimeError("receiver finished with " + state["state"])
            response = state["response"]
            if response.get("text") != marker:
                raise RuntimeError("reply did not exactly match the nonce and input SHA-256 confirmation")
            attachments = response.get("fetch") or []
            if len(attachments) != 1 or attachments[0].get("sha256") != journal["sha256"] or \
                    attachments[0].get("size") != journal["bytes"]:
                raise RuntimeError("reply must contain exactly the original fixture")
            print(run("fetch", response["id"]), end="", flush=True)
            files = list((home / "inbox" / response["id"] / "files").glob("*"))
            if len(files) != 1 or not files[0].is_file() or digest(files[0]) != journal["sha256"] or \
                    files[0].stat().st_size != journal["bytes"]:
                raise RuntimeError("downloaded attachment did not match the original bytes")
            journal.update(passed=True, response_id=response["id"], response_sha256=digest(files[0]),
                           completed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                           transport="pull", result_state="completed")
            write_json(journal_path, journal)
            print("PASS: correlated request/reply and byte-identical attachment round trip.", flush=True)
            print(json.dumps(journal, ensure_ascii=True, indent=2))
            return 0
        time.sleep(3)
    print("Not complete yet; execution outcome remains unknown. Resume with the same --workdir.", flush=True)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print("Verification incomplete: " + str(exc), file=sys.stderr)
        sys.exit(2)
