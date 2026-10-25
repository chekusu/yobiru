"""Stand-in for the tailscale CLI in tests: answers read-only queries from a JSON state file and logs
every call, so tests can prove nothing ever ran `tailscale up`, `set` or similar.

State file (via `--state PATH` before the subcommand, or $FAKE_TS_STATE):
  {"status": {...}, "whois": {...}, "ip4": "127.0.0.1", "shields_up": false, "calls": "/path/calls.log",
   "status_error": "message printed by a failing `status --json`"}
"""
import json
import os
import sys

DEFAULT_STATUS = {"BackendState": "Running",
                  "Self": {"ID": "alpha-stable", "DNSName": "alpha.example.ts.net.", "HostName": "alpha",
                           "TailscaleIPs": ["127.0.0.1"], "Online": True}, "Peer": {}}

args = sys.argv[1:]
path = None
if args[:1] == ["--state"]:
    path, args = args[1], args[2:]
path = path or os.environ.get("FAKE_TS_STATE")
state = {}
if path:
    with open(path, encoding="utf-8") as f:
        state = json.load(f)
if state.get("calls"):
    with open(state["calls"], "a", encoding="utf-8") as f:
        f.write(json.dumps(args) + "\n")
if args == ["status", "--json"] and state.get("status_error"):
    sys.stderr.write(state["status_error"] + "\n")
    sys.exit(1)
elif args == ["status", "--json"]:
    print(json.dumps(state.get("status", DEFAULT_STATUS)))
elif args == ["ip", "-4"]:
    print(state.get("ip4", "127.0.0.1"))
elif args[:2] == ["whois", "--json"] and state.get("whois"):
    print(json.dumps(state["whois"]))
elif args == ["debug", "prefs"]:
    print(json.dumps({"ShieldsUp": bool(state.get("shields_up", False))}))
elif args == ["version"]:
    print("1.0.0-fake")
else:
    sys.stderr.write("fake tailscale: unsupported %r\n" % (args,))
    sys.exit(2)
