"""A synthetic native host for tests: NOT an agent and not evidence of any real host.

The connector's `exec` wake route runs it as
    python3 synthetic_host.py CLIENT_PY HOST_HOME BOT_ID {job_id} {phase} {wake_id} {check_id}
It appends every invocation to HOST_HOME/wakes.log (so tests can count wakes). For phase `ready` it claims the job
through the real CLI with the binding credential stored in HOST_HOME, then replies with a deterministic text that
proves which context it received: the caller's text, how many earlier transcript entries it got, and whether the
gateway flagged a context reset. A binding check (non-empty CHECK_ID) is answered with `job check-in`. It
never sees anything the CLI does not hand it.
"""
import json
import subprocess
import sys
from pathlib import Path


def cli(client, home, *args):
    r = subprocess.run([sys.executable, client, "--home", home] + list(args), capture_output=True, text=True,
                       timeout=60)
    try:
        return r.returncode, json.loads(r.stdout)
    except ValueError:
        return r.returncode, {"raw": r.stdout, "stderr": r.stderr}


def main():
    client, home, bot, job, phase, wake_id, check = sys.argv[1:8]
    log = Path(home) / "wakes.log"
    with log.open("a") as f:
        f.write(json.dumps({"job": job, "phase": phase, "wake_id": wake_id, "check": check}) + "\n")
    if check:
        code, _ = cli(client, home, "job", "check-in", bot, check)
        return 0 if code == 0 else 3
    if phase != "ready":
        return 0
    code, claimed = cli(client, home, "job", "claim", bot, job)
    if code != 0 or not claimed.get("ok"):
        return 0                      # nothing to do (already taken, blocked, cancelled): a no-op run
    said = claimed["untrusted_content"]["text"]
    earlier = [t["text"] for t in claimed["transcript"] if t["role"] == "bot"]
    reply = "SYNTHETIC-HOST reply to %r; earlier bot turns: %d%s%s" % (
        said, len(earlier), "; last: %s" % earlier[-1] if earlier else "",
        "; context reset" if claimed.get("context_reset") else "")
    code, done = cli(client, home, "job", "reply", bot, job, "--text", reply)
    return 0 if code == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
