#!/usr/bin/env python3
"""Yobiru: portable launcher, setup and doctor around the bundled tailmsg CLI (Python 3.8+, stdlib only).

Yobiru speaks the tailmsg protocol v1. This file locates the bundled tailmsg.py next to itself, so it
works from any plugin install or cache path (including paths with spaces) and from any working
directory. All state lives in the tailmsg data home (--home, $TAILMSG_HOME or ~/.tailmsg), never in
the plugin directory, so existing tailmsg setups keep working unchanged.

Safety contract: read-only unless a command is given --apply. Nothing here starts, stops or restarts
services, changes Tailscale login/ACL/shields-up settings, or sends a message on its own; message
commands are passed to tailmsg.py exactly as given (argument arrays, never a shell).
"""
import argparse
import datetime as dt
import getpass
import hashlib
import http.client
import importlib.util
import json
import os
import plistlib
import re
import secrets
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import uuid
from pathlib import Path

sys.dont_write_bytecode = True   # keep installed plugin directories pristine (no __pycache__)

PRODUCT = "yobiru"
DISPLAY = "Yobiru"
HOME_ENV = "TAILMSG_HOME"        # shared with tailmsg so an existing ~/.tailmsg keeps working
DEFAULT_HOME = "~/.tailmsg"
PYTHON_ENV = "YOBIRU_PYTHON"     # interpreter for the bundled tailmsg.py (default: the one running this file)
MIN_PYTHON = (3, 8)
HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = HERE.parent
CORE = HERE / "tailmsg.py"
BRIDGE = HERE / "codex_bridge"
PUBLIC_BOTS = HERE / "public_bots"   # separate public gateway, its clients and the private calendar capability
CORE_COMMANDS = ("serve", "send", "reply", "request", "inbox", "show", "fetch", "ack", "wake")
LEVELS = ("none", "batch", "now")
SERVICE_LABEL = "dev.tailmsg"    # receiver service names stay tailmsg's, matching existing installs
LAUNCHD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"    # the PATH launchd gives a job without EnvironmentVariables
PEER_RULE_RE = re.compile(
    r"^(?:tag:[A-Za-z0-9][A-Za-z0-9_-]{0,62}|user:[^\s/]{1,253}|"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*)\Z")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d\Z")

USAGE = """usage: {p} [--home DIR] <command> [args]

{d} sends messages, files and wake-up requests between agents on your own Tailscale network
(tailmsg protocol v1). Data home: --home, ${env} or {home}.

Start here (read-only; works before anything is set up):
  onboard [--json]           step-by-step plan for this platform: Tailscale install and sign-in, config,
                             receiver, session binding, verification (installing the plugin ran nothing)
  agents [--online] [--json] which machines are online, their Yobiru version, and each agent's session
                             (name with its source + exact id); peers answer over authenticated /info

Check and set up (read-only unless --apply):
  paths                      where the plugin, bundled CLI, Python and data home are (JSON)
  doctor [--peer HOST[:PORT]] [--json]
                             check local setup; optionally probe peer health, identity access and pull support
  status [--json]            summarize config, agents, sessions, versions, wake settings and inbox counts
  session {{list,bind,unbind}} explicit agent -> existing session binding (exact id; bind/unbind need --apply)
  policy {{show,set}}         show or deliberately change incoming access policy (tailnet / allowlist + deny)
  update {{check,status,notify,maintain,reminder-template}}
                             official release check, versions, and upgrade notices to your known
                             installations (notify is a dry run unless --apply)
  setup ... [--apply]        create or extend config.json; new installs trust authenticated tailnet peers
  wake-config ... [--apply]  receiver-side wake policy and per-agent wake adapters
  install-runtime [--upgrade [--allow-downgrade]] [--apply]
                             copy the bundled tailmsg.py into the data home for a durable `serve`
                             (a newer runtime, such as a development build, is kept unless --allow-downgrade)
  service-template {{systemd,systemd-user,launchd,windows}}
                             print a reviewable service definition (never installs it)
  codex-bridge {{status,install,rollback}} ...
                             optional Codex bridge: macOS desktop app, or Linux user systemd + a local
                             app-server socket (install/rollback support --dry-run and --no-start)
  muse {{check,send,status,resume,list,read,fetch}}  outbound-only Muse custom connector (JSON checks/results)
  claude-bridge {{install,status,step,daemon}}  optional visible Claude Remote Control dispatcher
  mcp [--agent NAME]         local MCP server on stdio with the messaging commands as tools (the Claude Code
                             plugin registers it; other MCP clients can run it the same way)
  version

Public bots and Friends (a separate gateway, never the receiver on 7766;
see docs/PUBLIC_BOTS.md):
  gateway {{init,owner,migrate,serve,status}} [--gateway-home DIR]
                             the public mailbox: local-only owner bootstrap, loopback HTTP by default
                             (also serves /mcp to MCP clients with the same bearer credentials)
  public ...                 callers: connect LINK, enroll, enroll-bot, follow, unfollow, chat, status, history,
                             inbox, ack, verify, attachments; mcp (a local stdio MCP server)
                             owners: owner (directory, continue, decisions), bot (create/share/edit/publish),
                             review, worker bind, binding describe, connector bind
                             your own agent (native binding): job list/claim/reply/notices; connector run;
                             worker run
  calendar-service {{init,serve,check,preview}} --dir DIR
                             private read-only meeting-slot capability for one published bot
  resource-service {{init,add,remove,list,serve}} --dir DIR
                             private owner-approved text exports, released per caller through the gateway

Messaging (passed unchanged to the bundled tailmsg.py):
  request send PEER[/AGENT] TEXT [--transport push|pull] [--file F] [--timeout-seconds 900]
  request status ID [--refresh] [--json]     request list [--json]
  request resume ID                     retry the same pull request upload and submission
  request respond ID TEXT --state running|completed|failed|rejected|blocked
  send PEER[/AGENT] TEXT [--file F]... [--wake now|batch|none] [--agent A] [--kind K]
  reply ID TEXT [--file F]...      inbox [--pending] [--agent A]      show ID      fetch ID
  ack ID [ID...]                   wake ID                            serve
  peers                            same as `agents` (the richer view; tailmsg.py peers stays available)
Run `{p} <command> --help` for details.
"""


# ---------------------------------------------------------------- basics
def configure_cli_output():
    """Preserve stdout/stderr encoding and visibly escape unsupported characters.

    Keep this entry-point-local so missing-core diagnostics still work and imports
    do not change a caller's streams. Structured JSON output uses ensure_ascii=True
    to preserve emoji as valid JSON surrogate pairs on non-UTF-8 pipes.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(errors="backslashreplace")


def die(msg, code=1):
    print("%s: error: %s" % (PRODUCT, msg), file=sys.stderr)
    sys.exit(code)


def sha256_file(path):
    h = hashlib.sha256()
    with open(str(path), "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def plugin_version():
    try:
        return json.loads((PLUGIN_ROOT / "plugin.json").read_text(encoding="utf-8")).get("version") or "unknown"
    except (OSError, ValueError):
        return "unknown"


_core = None


def core():
    """The bundled tailmsg.py as a module (definitions only; its main() is not run)."""
    global _core
    if _core is None:
        if not CORE.is_file():
            die("bundled tailmsg.py is missing (expected %s); reinstall the plugin" % CORE, 2)
        spec = importlib.util.spec_from_file_location("yobiru_bundled_tailmsg", str(CORE))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _core = mod
    return _core


def resolve_home(arg):
    if arg:
        return Path(arg).expanduser().absolute(), "--home"
    if os.environ.get(HOME_ENV):
        return Path(os.environ[HOME_ENV]).expanduser().absolute(), "$" + HOME_ENV
    return Path(DEFAULT_HOME).expanduser().absolute(), "default"


def read_config(home):
    """-> (raw dict or None, error or None). Never creates anything."""
    path = home / "config.json"
    if not path.is_file():
        return None, None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, "cannot read %s: %s" % (path, e)
    if not isinstance(raw, dict):
        return None, "%s must contain a JSON object" % path
    return raw, None


def config_problems(cfg):
    """-> (fatal, warnings) for values tailmsg would fail on or silently ignore. cfg = parse_config() output."""
    fatal, warn = [], []
    port = cfg.get("port")
    try:
        ok = not isinstance(port, bool) and 1 <= int(port) <= 65535
    except (TypeError, ValueError):
        ok = False
    if not ok:
        fatal.append("port must be an integer 1-65535 (got %s)" % json.dumps(port))
    try:
        core().parse_access_policy(cfg)
    except RuntimeError as e:
        fatal.append(str(e))
    tc = cfg.get("tailscale_cmd")
    if not isinstance(tc, list) or not tc or not all(isinstance(x, str) and x for x in tc):
        fatal.append("tailscale_cmd must be a non-empty list of strings")
    if cfg.get("bind") is not None and not isinstance(cfg.get("bind"), str):
        fatal.append("bind must be a string or null")
    pol = cfg.get("wake_policy")
    if pol is not None and not isinstance(pol, dict):
        warn.append("wake_policy is not an object; tailmsg caps every sender at batch")
    elif pol:
        if pol.get("default_max", "batch") not in LEVELS:
            warn.append("wake_policy.default_max is not now/batch/none; tailmsg uses batch")
        peers = pol.get("peers") or {}
        if not isinstance(peers, dict) or not all(v in LEVELS for v in peers.values()):
            warn.append("wake_policy.peers values must be now/batch/none; invalid ones act as batch")
    q = cfg.get("quiet_hours")
    if q is not None and (not isinstance(q, dict) or not HHMM_RE.match(str(q.get("start")))
                          or not HHMM_RE.match(str(q.get("end")))):
        warn.append("quiet_hours must be {start: HH:MM, end: HH:MM[, tz]}; tailmsg ignores it as written")
    for key in ("wake_rate", "wake_attempts", "hook_timeout_s", "wake_backoff_s", "outbox_ttl_hours",
                "max_msg_bytes", "max_fetch_bytes"):
        v = cfg.get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
            warn.append("%s must be a non-negative number (got %s)" % (key, json.dumps(v)))
    return fatal, warn


def find_exe(name):
    if os.sep in name or (os.altsep and os.altsep in name):
        return name if os.path.isfile(name) and os.access(name, os.X_OK) else None
    return shutil.which(name)


def run_tailscale(cmd, args, timeout=10):
    """-> (stdout or None, error or None). Read-only queries only."""
    try:
        p = subprocess.run(list(cmd) + list(args), capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None, "not found"
    except subprocess.TimeoutExpired:
        return None, "timed out after %ss" % timeout
    except OSError as e:
        return None, "%s: %s" % (type(e).__name__, e)
    if p.returncode != 0:
        return None, ((p.stderr or p.stdout).strip()[-300:] or "exit %d" % p.returncode)
    return p.stdout, None


HEALTH_KEYS = ("ok", "v", "node", "receiver_alive", "status", "maintenance")


def error_name(error):
    """Keep socket timeout names stable on Python 3.8/3.9 and 3.10+."""
    return "TimeoutError" if isinstance(error, socket.timeout) else type(error).__name__


def probe_health(host, port, timeout=2.0, peer=None):
    """GET /health: transport reachability, plus the receiver's own maintenance summary when it reports one
    (kept as given, so doctor can tell a reachable but degraded receiver from a healthy one)."""
    c = None
    try:
        c = http.client.HTTPConnection(host, int(port), timeout=timeout)
        c.request("GET", "/health")
        r = c.getresponse()
        body = r.read(16384)
    except (OSError, http.client.HTTPException, ValueError) as e:
        return {"url": "http://%s:%s/health" % (host, port), "reachable": False,
                "error": error_name(e),
                "detail": core().transport_diagnostic(peer or host, host, port, "GET /health", e)}
    finally:
        if c is not None:
            c.close()
    out = {"url": "http://%s:%s/health" % (host, port), "reachable": True, "http_status": r.status}
    try:
        j = json.loads(body.decode("utf-8"))
        if isinstance(j, dict):
            out.update({k: j[k] for k in HEALTH_KEYS if k in j})
            if not isinstance(out.get("maintenance"), dict):
                out.pop("maintenance", None)
    except ValueError:
        pass
    return out


MAINTENANCE_FILE = "maintenance.json"
MAINTENANCE_WORKER_KEYS = ("status", "alive", "restarts", "cycles", "phase", "in_cycle_seconds", "last_cycle_at",
                           "last_outcomes", "last_counts", "last_full_success_at", "last_full_success_age_seconds",
                           "failure", "consecutive_failures", "last_exit", "stale_after_seconds")


def maintenance_record(home, live_instance=None):
    """The receiver's metadata heartbeat (<home>/maintenance.json), read without changing anything.

    The file only says what a receiver process reported when it wrote it. It describes the running receiver
    only when its instance matches the one the live /health reports; otherwise it is history (an earlier or
    stopped process), never evidence that workers are alive now."""
    raw, err = core().read_small_json(home / MAINTENANCE_FILE)
    if raw is None:
        return {"present": False} if err is None else {"present": True, "error": err}
    if not isinstance(raw, dict) or raw.get("schema") != 1 or not isinstance(raw.get("workers"), dict):
        return {"present": True, "error": "malformed"}
    try:
        age = int((dt.datetime.now(dt.timezone.utc) - core().parse_iso(raw.get("written_at"))).total_seconds())
    except (TypeError, ValueError):
        age = None
    workers = {}
    for name, worker in list(raw["workers"].items())[:8]:
        if isinstance(name, str) and isinstance(worker, dict):
            workers[name[:32]] = {k: worker.get(k) for k in MAINTENANCE_WORKER_KEYS}
    return {"present": True, "current": live_instance is not None and raw.get("instance") == live_instance,
            "instance": raw.get("instance"), "written_at": raw.get("written_at"), "age_seconds": age,
            "status": raw.get("status"), "reasons": raw.get("reasons"), "workers": workers,
            "heartbeat": raw.get("heartbeat")}


_bridge_module = None


def bridge_module():
    """The bundled codex_bridge.py, for its read-only status projection (POSIX only; no bytecode written)."""
    global _bridge_module
    if _bridge_module is None:
        spec = importlib.util.spec_from_file_location("yobiru_bundled_codex_bridge", str(BRIDGE / "codex_bridge.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _bridge_module = mod
    return _bridge_module


def dispatcher_diagnostics(home, cfg=None):
    """Read-only delivery/execution evidence of an installed Codex bridge (the projection `codex-bridge status`
    shows), or {"state": "unknown", "error": ...} when it cannot be computed. Never creates or repairs state."""
    try:
        if cfg is None:
            cfg = json.loads((home / "codex-bridge.json").read_text(encoding="utf-8"))
        module = bridge_module()
        return module.diagnostics(module.validate_config(dict(cfg)))
    except Exception as e:      # a diagnostic reports, it does not crash
        return {"state": "unknown", "error": error_name(e)}


def agent_names(cfg, agents):
    """agents the receiver wakes (same order as tailmsg serve's startup log); [None] = single-agent setup."""
    dflt = cfg.get("default_agent")
    if not agents and dflt is None:
        return [None]
    return ([dflt] if dflt is not None and dflt not in agents else []) + list(agents)


def secret_source_checks(adapter):
    """Checks for webhook url/secret sources without ever reading their values."""
    notes = []
    for key in ("url", "secret"):
        if adapter.get(key + "_file"):
            p = Path(adapter[key + "_file"]).expanduser()
            try:
                st = p.stat()
            except OSError:
                notes.append(("error", "%s_file %s does not exist" % (key, p)))
                continue
            if os.name == "posix" and st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                notes.append(("error", "%s_file %s must be mode 0600 (chmod 600)" % (key, p)))
            elif st.st_size == 0:
                notes.append(("error", "%s_file %s is empty" % (key, p)))
        if adapter.get(key + "_env") and not os.environ.get(adapter[key + "_env"]):
            notes.append(("warn", "$%s is not set in this shell; it must be set in the serve process "
                                  "environment" % adapter[key + "_env"]))
    if adapter.get("type") == "exec":
        exe = str((adapter.get("argv") or [""])[0])
        if not find_exe(exe):
            notes.append(("error", "exec adapter %s is not an executable file" % exe))
    return notes


def inbox_stats(home, default_agent):
    s = {"messages": 0, "pending": 0, "wake_failed": 0, "unknown_agent": 0, "unreadable": 0, "by_agent": {}}
    inbox = home / "inbox"
    if not inbox.is_dir():
        return s
    for p in sorted(inbox.glob("*.json")):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
            env, rv = r["envelope"], r.get("received") or {}
        except (OSError, ValueError, KeyError, TypeError):
            s["unreadable"] += 1
            continue
        s["messages"] += 1
        eff = (rv.get("wake") or {}).get("effective", "batch")
        if eff != "none" and not r.get("acked_at"):
            s["pending"] += 1
            agent = rv["agent"] if "agent" in rv else (env.get("agent") or default_agent)
            key = agent if agent is not None else "(none)"
            s["by_agent"][key] = s["by_agent"].get(key, 0) + 1
        if (r.get("wake_delivery") or {}).get("ok") is False:
            s["wake_failed"] += 1
        if rv.get("agent_status") == "unknown":
            s["unknown_agent"] += 1
    return s


# ---------------------------------------------------------------- facts shared by doctor/status/paths
def gather(home, home_source, probe=True):
    f = {"product": PRODUCT, "plugin_version": plugin_version(), "plugin_root": str(PLUGIN_ROOT),
         "core": str(CORE), "core_present": CORE.is_file(),
         "python": {"executable": sys.executable, "version": "%d.%d.%d" % sys.version_info[:3]},
         "platform": sys.platform, "home": str(home), "home_source": home_source,
         "home_exists": home.is_dir(), "config_path": str(home / "config.json")}
    raw, err = read_config(home)
    f["config_exists"] = (home / "config.json").is_file()
    f["config_error"] = err
    cfg, agents = None, {}
    if raw is not None:
        try:
            cfg, agents = core().parse_config(raw)
        except RuntimeError as e:
            f["config_error"] = str(e)
    f["config_warnings"] = []
    if cfg is not None:
        fatal, f["config_warnings"] = config_problems(cfg)
        if fatal:
            f["config_error"] = "; ".join(fatal)
            cfg = None
    f["cfg"] = cfg
    if cfg is not None:
        f["port"] = int(cfg["port"])
        f["allow"] = list(cfg["allow"])
        f["access_policy"] = cfg["access_policy"]
        f["deny"] = list(cfg["deny"])
        f["legacy_allowlist"] = "access_policy" not in raw and "allow" in raw
        f["self_agent"], f["default_agent"] = cfg.get("self_agent"), cfg.get("default_agent")
        f["wake_policy"] = cfg.get("wake_policy")
        f["quiet_hours"] = cfg.get("quiet_hours")
        f["wake_rate"] = cfg.get("wake_rate")
        f["adapters"] = []
        for name in agent_names(cfg, agents):
            entry = {"agent": name, "default": name is not None and name == cfg.get("default_agent"),
                     "wake_max": (agents.get(name) or {}).get("wake_max") if name is not None else None}
            try:
                a = core().Ctx.agent_adapter(_AdapterView(cfg, agents), name)
                entry["adapter"] = core().describe_adapter(a)
                entry["checks"] = secret_source_checks(a) if a else []
            except core().AdapterError as e:
                entry["adapter"] = "MISCONFIGURED: %s" % e
                entry["checks"] = [("error", str(e))]
            f["adapters"].append(entry)
        f["inbox"] = inbox_stats(home, cfg.get("default_agent"))
    ts = {"cmd": None, "found": None, "error": None, "probed": False}
    cmd = (cfg or {}).get("tailscale_cmd") or ["tailscale"]
    if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) and x for x in cmd):
        ts["error"] = "tailscale_cmd in config.json must be a non-empty list of strings"
    else:
        ts["cmd"] = cmd
        ts["found"] = find_exe(cmd[0])
        if not ts["found"]:
            ts["error"] = "%s not found" % cmd[0]
        elif probe:
            ts["probed"] = True
            out, e = run_tailscale(cmd, ["status", "--json"])
            if e:
                ts["error"] = "tailscale status failed: %s" % e
            else:
                try:
                    st = json.loads(out)
                    me = st.get("Self") or {}
                    ts["backend"] = st.get("BackendState")
                    ts["node"] = (me.get("DNSName") or "").rstrip(".").lower() or None
                    ts["ips"] = me.get("TailscaleIPs") or []
                except ValueError:
                    ts["error"] = "tailscale status returned invalid JSON"
            if not ts["error"]:
                out, e = run_tailscale(cmd, ["debug", "prefs"])
                try:
                    ts["shields_up"] = bool(json.loads(out).get("ShieldsUp")) if out else None
                except (ValueError, AttributeError):
                    ts["shields_up"] = None
    f["tailscale"] = ts
    if probe and cfg is not None:
        host = cfg.get("bind") or next((i for i in ts.get("ips") or [] if ":" not in i), None)
        f["receiver"] = probe_health(host, f["port"], peer=ts.get("node")) if host else {"reachable": False,
                                                                     "error": "no Tailscale IPv4 address"}
    runtime = home / "tailmsg.py"
    f["runtime"] = {"path": str(runtime), "present": runtime.is_file()}
    if runtime.is_file() and CORE.is_file():
        f["runtime"]["same_as_bundled"] = sha256_file(runtime) == sha256_file(CORE)
    f["codex_bridge_installed"] = (home / "codex-bridge.json").is_file()
    if cfg is not None:
        live = ((f.get("receiver") or {}).get("maintenance") or {}).get("instance")
        f["maintenance_record"] = maintenance_record(home, live)
        f["request_backlog"] = core().request_backlog(home)
    if f["codex_bridge_installed"]:
        f["dispatcher"] = dispatcher_diagnostics(home)
    f["runtime_version"] = runtime_version(home)[0]
    f["update"] = recorded_update(home) if home.is_dir() else None
    if cfg is not None:
        names = core().info_agent_names(cfg, agents)
        bound, problems = core().session_bindings(home, names)
        f["sessions"] = {"bound": {n: b["session_id"] for n, b in bound.items()},
                         "unbound": [n for n in names if n not in bound], "problems": problems}
    return f


class _AdapterView(object):
    """Just enough of tailmsg's Ctx for Ctx.agent_adapter (which only reads cfg and agents)."""

    def __init__(self, cfg, agents):
        self.cfg, self.agents = cfg, agents


# ---------------------------------------------------------------- paths / doctor / status
def cmd_paths(home, home_source, argv):
    argparse.ArgumentParser(allow_abbrev=False, prog=PRODUCT + " paths", description="print resolved locations (JSON)").parse_args(argv)
    print(json.dumps({
        "plugin_root": str(PLUGIN_ROOT), "plugin_version": plugin_version(),
        "cli": str(HERE / PRODUCT), "python_cli": str(Path(__file__).resolve()), "core": str(CORE),
        "codex_bridge": str(BRIDGE), "skills": str(PLUGIN_ROOT / "skills"),
        "python": sys.executable, "python_version": "%d.%d.%d" % sys.version_info[:3],
        "home": str(home), "home_source": home_source, "config": str(home / "config.json"),
        "config_exists": (home / "config.json").is_file(),
    }, indent=2, ensure_ascii=True))
    return 0


def doctor_checks(f):
    checks = []

    def add(status, name, detail, fix=None):
        checks.append({"status": status, "check": name, "detail": detail, "fix": fix})

    py = f["python"]
    add("ok" if sys.version_info[:2] >= MIN_PYTHON else "error", "python",
        "%s at %s" % (py["version"], py["executable"]),
        None if sys.version_info[:2] >= MIN_PYTHON else "install Python 3.8 or newer")
    add("ok" if f["core_present"] else "error", "plugin",
        "%s %s at %s" % (DISPLAY, f["plugin_version"], f["plugin_root"]),
        None if f["core_present"] else "the bundled tailmsg.py is missing; reinstall the plugin")
    if f["config_error"]:
        add("error", "config", f["config_error"], "fix config.json by hand; %s never overwrites it" % PRODUCT)
    elif not f["config_exists"]:
        add("error", "config", "not set up: no %s (nothing was created)" % f["config_path"],
            "%s setup --agent <name>   # dry run; add --apply after review" % PRODUCT)
    else:
        add("ok", "config", "%s (port %d, home from %s)" % (f["config_path"], f["port"], f["home_source"]))
        for w in f.get("config_warnings") or []:
            add("warn", "config", w)
        mode = f["access_policy"]
        detail = "tailnet: accepts authenticated Tailscale peers that can reach this receiver" if mode == "tailnet" \
            else "allowlist: accepts only matching peers: %s" % (", ".join(f["allow"]) or "(nobody)")
        detail += "; deny rules take precedence: %s" % (", ".join(f["deny"]) or "(none)")
        add("warn" if mode == "allowlist" and not f["allow"] else "ok", "access-policy", detail,
            "%s policy show; use `policy set` to change the policy deliberately" % PRODUCT
            if mode == "allowlist" and not f["allow"] else None)
        if f["legacy_allowlist"]:
            add("info", "policy-migration", "legacy allow list preserved; upgrading never broadens existing access",
                "to trust authenticated tailnet peers: `%s policy set --mode tailnet` (review, then --apply)" % PRODUCT)
    ts = f["tailscale"]
    state = classify_tailscale(ts)
    linux = sys.platform.startswith("linux")
    if ts.get("cmd") is None:
        add("error", "tailscale", ts["error"], "fix tailscale_cmd in config.json by hand, or use "
            "`%s setup --tailscale-cmd PATH --replace tailscale_cmd`" % PRODUCT)
    elif state == "missing":
        hint = "install Tailscale and sign this machine in; `%s onboard` prints the steps for %s (%s)" % (
            PRODUCT, sys.platform, TS_DOCS.get(sys.platform if sys.platform in ("darwin", "win32") else "linux"))
        if sys.platform == "darwin" and Path(MAC_TS_CLI).exists():
            hint = "the Tailscale app CLI exists; run `%s setup --tailscale-cmd %s`" % (PRODUCT, MAC_TS_CLI)
        elif sys.platform == "win32" and Path(WIN_TS_CLI).exists():
            hint = 'Tailscale is installed but not on PATH: `%s setup --tailscale-cmd "%s" --replace tailscale_cmd`' \
                % (PRODUCT, WIN_TS_CLI)
        add("error", "tailscale", ts["error"], hint)
    elif state == "permission":
        detail, cmds, _ = permission_guidance(environment_facts(), ts.get("cmd"))
        add("error", "tailscale", ts["error"], detail + ("" if not cmds else ": `%s`" % cmds[0]))
    elif state == "daemon_unavailable":
        facts = environment_facts() if linux else {}
        pre = root_prefix(facts) if linux else None
        add("error", "tailscale", "the Tailscale daemon is not available: %s" % ts["error"],
            ("start it: `%ssystemctl enable --now tailscaled`, or without systemd run tailscaled yourself "
             "(`%s onboard`)" % (pre, PRODUCT)) if linux and pre is not None else
            "an administrator must start tailscaled (this user has no root or sudo)" if linux else
            "open the Tailscale app and sign in")
    elif state == "error":
        add("error", "tailscale", ts["error"], "make sure tailscaled is running and this machine is signed in "
            "(`%s onboard`)" % PRODUCT)
    elif state == "unchecked":
        add("info", "tailscale", "found %s (state not checked: --no-probe)" % ts["found"])
    else:
        pre = root_prefix(environment_facts()) if linux else None
        if linux and (ts.get("cmd") or [""])[0] == "sudo":
            pre = ""
        up = "`%stailscale up`" % pre if pre is not None else "an administrator running `tailscale up`"
        login = "%s, then the user opens the login URL and chooses the account" % up if linux \
            else "sign in from the Tailscale app"
        fixes = {"logged_out": "this machine is logged out: %s (`%s onboard`)" % (login, PRODUCT),
                 "stopped": "Tailscale is stopped: %s" % (up if linux else "connect from the Tailscale app"),
                 "starting": "Tailscale is still starting; check again shortly",
                 "needs_approval": "a tailnet administrator must approve this device in the admin console",
                 "unknown": "sign this machine in to Tailscale (`tailscale up`)"}
        add("ok" if state == "running" else "error", "tailscale",
            "%s, node %s, ips %s" % (ts.get("backend"), ts.get("node"), ", ".join(ts.get("ips") or [])),
            fixes.get(state))
        if ts.get("shields_up") is True:
            add("error", "shields-up", "Tailscale shields-up blocks all incoming connections",
                "only if this machine should receive: `tailscale set --shields-up=false` (the owner's "
                "decision; %s never changes it)" % PRODUCT)
        elif ts.get("shields_up") is None:
            add("info", "shields-up", "could not read Tailscale prefs; check `tailscale debug prefs` yourself")
    if "receiver" in f:
        r = f["receiver"]
        if r.get("reachable") and r.get("http_status") == 200 and r.get("v") == 1 and r.get("node"):
            # Transport only: a reachable receiver can still have impaired maintenance (next check).
            add("ok", "receiver", "serving tailmsg v%s as %s at %s" % (r.get("v"), r.get("node"), r["url"]))
        elif r.get("reachable"):
            add("warn", "receiver", "something answered %s with HTTP %s" % (r["url"], r.get("http_status")))
        else:
            add("warn", "receiver", r.get("detail") or "receiver probe failed at %s (%s)" %
                (r.get("url", "-"), r.get("error")),
                "to receive messages run the receiver as a service: `%s install-runtime`, then install "
                "the definition printed by `%s service-template ...`" % (PRODUCT, PRODUCT))
    maintenance_check(f, add)
    backlog_check(f, add)
    dispatcher_check(f, add)
    rt = f["runtime"]
    if rt["present"] and rt.get("same_as_bundled") is False:
        add("info", "runtime", "%s differs from the bundled tailmsg.py" % rt["path"],
            "`%s install-runtime --upgrade` (backs up the old one; restart your service yourself)" % PRODUCT)
    elif rt["present"]:
        add("ok", "runtime", "%s matches the bundled tailmsg.py" % rt["path"])
    elif f["config_exists"]:
        add("info", "runtime", "no durable runtime at %s" % rt["path"],
            "`%s install-runtime` before installing a receiver service" % PRODUCT)
    for a in f.get("adapters", []):
        name = "wake" + ("" if a["agent"] is None else " @%s" % a["agent"])
        worst = "error" if any(s == "error" for s, _ in a["checks"]) else \
            "warn" if any(s == "warn" for s, _ in a["checks"]) else "ok"
        detail = a["adapter"] + ("" if not a["checks"] else "; " + "; ".join(m for _, m in a["checks"]))
        add(worst if a["adapter"] != "none" else "info", name, detail,
            None if a["adapter"] != "none" else "messages wait in `inbox --pending`; see `%s wake-config`" % PRODUCT)
    if f.get("inbox"):
        i = f["inbox"]
        add("info", "inbox", "%d messages, %d pending, %d failed wake-ups, %d for unknown agents"
            % (i["messages"], i["pending"], i["wake_failed"], i["unknown_agent"]))
    if f.get("sessions") is not None:
        s = f["sessions"]
        add("info" if not s["problems"] else "warn", "sessions",
            "bound: %s; unbound: %s%s" % (", ".join("@%s -> %s" % kv for kv in sorted(s["bound"].items())) or "none",
                                         ", ".join("@" + n for n in s["unbound"]) or "none",
                                         "; " + "; ".join(s["problems"]) if s["problems"] else ""),
            None if not s["unbound"] else "name each agent's existing session: `%s session bind --agent NAME "
            "--session-id ID` (see `%s agents`)" % (PRODUCT, PRODUCT))
    rt_version = f.get("runtime_version")
    if rt_version and rt_version != f["plugin_version"]:
        add("info", "versions", "plugin %s, durable runtime %s" % (f["plugin_version"], rt_version),
            runtime_step(f["plugin_version"], rt_version))
    up = f.get("update")
    if up and up["status"] == "update_available":
        add("info", "update", "Yobiru %s is available (checked %s, %s channel)" % (up["latest"], up["checked_at"],
                                                                                up["channel"]),
            "see `%s update check`; upgrade with the yobiru-upgrade skill" % PRODUCT)
    elif up is None and f["config_exists"]:
        add("info", "update", "no recorded update check", "`%s update check` (network; add --record to "
            "remember the result)" % PRODUCT)
    if any(c["status"] == "error" for c in checks):
        add("info", "next", "for a step-by-step plan run `%s onboard`" % PRODUCT)
    return checks


MAINTENANCE_FIXES = {
    "worker_dead": "a maintenance worker stopped; the receiver starts one replacement automatically (with "
                   "backoff). If it keeps stopping, read the receiver log",
    "worker_restarted": "a maintenance worker stopped and was replaced; it stays reported until a full cycle "
                        "succeeds",
    "cycle_failed": "a maintenance phase keeps failing (`doctor --json` names the phase and error type): check "
                    "disk space and permissions of the data home",
    "no_completed_cycle": "a worker is alive but has not finished a cycle (a slow peer or a stuck operation); live "
                          "workers are never restarted or duplicated automatically",
    "heartbeat_write_failed": "the maintenance heartbeat cannot be written (disk full or permissions); messages "
                              "are still accepted",
}
MAINTENANCE_FIXES["no_recent_full_cycle"] = MAINTENANCE_FIXES["no_completed_cycle"]
SEVERE_MAINTENANCE = ("worker_dead", "worker_restarted", "cycle_failed", "heartbeat_write_failed")


def maintenance_check(f, add):
    """Maintenance readiness, separate from transport reachability: from the live /health summary, with the
    metadata heartbeat file used for detail only when it belongs to the same running receiver."""
    r, record = f.get("receiver"), f.get("maintenance_record") or {}
    if r is None or not r.get("reachable"):
        if record.get("error"):
            add("warn", "maintenance", "maintenance.json is unreadable (%s); maintenance state unknown"
                % record["error"])
        elif record.get("present"):
            add("info", "maintenance", "last maintenance record written %s by a receiver process that was not "
                "checked live; it is history, not proof that maintenance runs now" % record.get("written_at"))
        return
    m = r.get("maintenance")
    if not isinstance(m, dict) or m.get("supported") is not True:
        add("warn", "maintenance", "this receiver does not report maintenance health (an older runtime), so "
            "response delivery, expiry and attachment cleanup cannot be checked",
            "`%s install-runtime --upgrade --apply`, then restart the receiver" % PRODUCT)
        return
    components = m.get("components") if isinstance(m.get("components"), dict) else {}
    reasons = [x for x in m.get("reasons") or [] if isinstance(x, str)]
    detail = "%s (%s)" % (m.get("status"), ", ".join("%s %s" % kv for kv in sorted(components.items())))
    if reasons:
        detail += "; reasons: " + ", ".join(reasons)
    if record.get("current"):
        ages = ["%s %ss ago" % (name, w.get("last_full_success_age_seconds")) for name, w in
                sorted((record.get("workers") or {}).items()) if w.get("last_full_success_age_seconds") is not None]
        if ages:
            detail += "; last full cycle as of the heartbeat (%ss old): %s" % (record.get("age_seconds"),
                                                                            ", ".join(ages))
    elif record.get("error"):
        detail += "; maintenance.json is unreadable (%s), so no timing detail is known" % record["error"]
    elif record.get("present"):
        detail += "; maintenance.json was written by another receiver process (history only)"
    if m.get("status") == "healthy":
        status = "ok"
    elif m.get("status") == "starting":
        status = "info"
    else:
        status = "error" if any(x in SEVERE_MAINTENANCE for x in reasons) or not reasons else "warn"
    add(status, "maintenance", detail, "; ".join(MAINTENANCE_FIXES[x] for x in reasons if x in MAINTENANCE_FIXES)
        or None)


def backlog_check(f, add):
    """Request ledger counts and ages (read-only). A ledger that cannot be read is never an empty backlog."""
    b = f.get("request_backlog")
    if not b:
        return
    if not b.get("available"):
        if b.get("reason") != "ledger_absent":
            add("warn", "requests", "the request ledger cannot be read (%s); backlog unknown" % b.get("reason"),
                "no application data was changed; check permissions of %s" % (Path(f["home"]) / "requests"))
        return
    inc, resp, out = b["incoming"], b["responses"], b["outgoing"]
    parts, status, fixes = [], "ok", []
    if inc["unstarted"] or inc["running"] or inc["timed_out"]:
        parts.append("incoming: %d without a start report, %d running, %d timed out (outcome unknown)"
                     % (inc["unstarted"], inc["running"], inc["timed_out"]))
        status = "info"
    if inc["overdue_not_persisted"]:
        parts.append("%d past the deadline without a recorded timeout" % inc["overdue_not_persisted"])
        status = "warn"
        fixes.append("a running receiver records timeouts within seconds; see the maintenance check")
    if inc.get("final_response_pending") or inc.get("final_response_unknown"):
        parts.append("%d final result(s) saved but not yet delivered to the sender" %
                     (inc.get("final_response_pending", 0) + inc.get("final_response_unknown", 0)))
        status = "info" if status == "ok" else status
    if resp["pending"]:
        parts.append("responses: %d waiting for delivery (%d due, %d never attempted, oldest %ss, up to %d attempts)"
                     % (resp["pending"], resp["due"], resp["never_attempted"], resp.get("oldest_pending_age_seconds"),
                        resp["max_attempts"]))
        status = "info" if status == "ok" else status
        if resp.get("unreadable_timestamps"):
            parts.append("%d with an unreadable timestamp (age unknown)" % resp["unreadable_timestamps"])
        if resp.get("age_scan_truncated"):
            parts.append("ages from the first %d pending responses only (partial)" % core().AGE_SCAN_LIMIT)
        if (resp.get("oldest_unattempted_due_age_seconds") or 0) > 300:
            status = "warn"
            fixes.append("responses are waiting without any delivery attempt: the receiver's delivery worker may not "
                         "run (see the maintenance check); retries reuse the same response IDs")
    if out["awaiting_result"]:
        parts.append("outgoing: %d awaiting a result (%d with an unknown submission, %d past the deadline)"
                     % (out["awaiting_result"], out["submission_unknown"], out["overdue"]))
        status = "info" if status == "ok" else status
    add(status, "requests", "; ".join(parts) or "no pending request work", "; ".join(fixes) or None)


def dispatcher_check(f, add):
    """The Codex bridge's own delivery/execution evidence and capabilities (read-only)."""
    d = f.get("dispatcher")
    if d is None:
        return
    caps = d.get("capabilities") or {}
    detail = "%s via %s (resident connection: %s)" % (d.get("state"), caps.get("transport", "?"),
                                                     caps.get("resident", "?"))
    if d.get("reasons"):
        detail += "; " + ", ".join(d["reasons"])
    oldest = (d.get("execution") or {}).get("oldest_without_start")
    if oldest:
        detail += "; oldest without start evidence: %ss (%s)" % (oldest.get("age_seconds"), oldest.get("phase"))
    add({"ok": "ok", "degraded": "warn"}.get(d.get("state"), "info"), "dispatcher", detail,
        "`%s codex-bridge status` lists the evidence and hints; never resend or delete the queue" % PRODUCT
        if d.get("reasons") else None)


def cmd_doctor(home, home_source, argv):
    ap = argparse.ArgumentParser(allow_abbrev=False, prog=PRODUCT + " doctor",
                                 description="read-only checks of local setup and optional peer transport, identity and pull support")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-probe", action="store_true", help="skip Tailscale queries and local/peer HTTP probes")
    ap.add_argument("--peer", metavar="HOST[:PORT]", help="also check this peer; sends no messages and changes no policy")
    ap.add_argument("--timeout", type=float, default=3.0, help="seconds per peer HTTP probe (0.2 to 30; default 3)")
    a = ap.parse_args(argv)
    peer = parse_peer_spec(a.peer, 7766) if a.peer else None
    if peer and peer[2] is not None:
        ap.error("--peer checks a machine, not an agent; omit /AGENT")
    try:
        facts = gather(home, home_source, probe=not a.no_probe)
        checks = doctor_checks(facts)
        if peer:
            if a.no_probe:
                checks.append({"status": "info", "check": "peer", "detail": "not checked: --no-probe", "fix": None})
            else:
                # Match discovery's default port while preserving an explicit --peer port.
                peer = parse_peer_spec(a.peer, facts.get("port") or 7766)
                checks.extend(peer_doctor_checks(facts, peer[0], peer[1], max(0.2, min(a.timeout, 30.0))))
    except Exception as e:  # a diagnostic must report, not crash
        checks = [{"status": "error", "check": "doctor", "detail": "unexpected %s: %s" % (type(e).__name__, e),
                   "fix": "please report this; nothing was changed"}]
    f = {"home": str(home)}
    errors = sum(c["status"] == "error" for c in checks)
    warns = sum(c["status"] == "warn" for c in checks)
    if a.json:
        print(json.dumps({"ok": errors == 0, "errors": errors, "warnings": warns, "home": f["home"],
                          "checks": checks, "changed": False}, indent=2, ensure_ascii=True))
    else:
        print("%s doctor (read-only: no application data, config, schema or queue is changed)" % DISPLAY)
        for c in checks:
            print("  %-6s %-14s %s" % (c["status"], c["check"], c["detail"]))
            if c["fix"]:
                print("  %-6s %-14s fix: %s" % ("", "", c["fix"]))
        print("result: %d error(s), %d warning(s)" % (errors, warns))
    return 1 if errors else 0


def cmd_status(home, home_source, argv):
    ap = argparse.ArgumentParser(allow_abbrev=False, prog=PRODUCT + " status", description="read-only summary")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-probe", action="store_true", help="skip Tailscale queries and the receiver health probe")
    a = ap.parse_args(argv)
    f = gather(home, home_source, probe=not a.no_probe)
    out = {k: f.get(k) for k in ("plugin_version", "home", "home_source", "config_path", "config_exists",
                                 "config_error", "port", "access_policy", "allow", "deny", "legacy_allowlist",
                                 "self_agent", "default_agent", "wake_policy",
                                 "quiet_hours", "wake_rate", "receiver", "runtime", "inbox",
                                 "codex_bridge_installed", "maintenance_record", "request_backlog", "dispatcher")
           if k in f}
    ts = f["tailscale"]
    out["tailscale"] = {k: ts.get(k) for k in ("backend", "node", "ips", "shields_up", "error") if k in ts}
    out["agents"] = [{"agent": x["agent"], "default": x["default"], "wake_max": x["wake_max"],
                      "wake_adapter": x["adapter"],
                      "session_id": (f.get("sessions") or {}).get("bound", {}).get(x["agent"])}
                     for x in f.get("adapters", [])]
    out["versions"] = {"plugin": f["plugin_version"], "runtime": f.get("runtime_version")}
    out["update"] = f.get("update")
    if a.json:
        print(json.dumps(out, indent=2, ensure_ascii=True))
        return 0
    print("%s %s  home %s (%s)" % (DISPLAY, f["plugin_version"], f["home"], f["home_source"]))
    if not f["config_exists"]:
        print("  not set up yet (no config.json); run `%s doctor`" % PRODUCT)
    elif f["config_error"]:
        print("  config error: %s" % f["config_error"])
    else:
        print("  node      %s   port %s" % (ts.get("node") or "?", f["port"]))
        print("  policy    %s%s" % (f["access_policy"], " (legacy allow list preserved)" if f["legacy_allowlist"] else ""))
        print("  allow     %s%s" % (", ".join(map(str, f["allow"])) or "(none)",
                                     " (inactive in tailnet mode)" if f["access_policy"] == "tailnet" else ""))
        print("  deny      %s" % (", ".join(map(str, f["deny"])) or "(none)"))
        print("  identity  self_agent=%s default_agent=%s" % (f["self_agent"], f["default_agent"]))
        for x in out["agents"]:
            print("  agent     %s%s: wake %s%s; session %s" % (
                "-" if x["agent"] is None else "@" + x["agent"], " (default)" if x["default"] else "",
                x["wake_adapter"], " max %s" % x["wake_max"] if x["wake_max"] else "",
                x["session_id"] or "not bound"))
        r = f.get("receiver") or {}
        m = r.get("maintenance") if isinstance(r.get("maintenance"), dict) else {}
        print("  receiver  %s" % ("serving v%s, maintenance %s" % (r.get("v"), m.get("status") or "not reported")
                                  if r.get("reachable") and r.get("v") == 1 else "not reachable"))
        b = f.get("request_backlog") or {}
        if b.get("available"):
            print("  requests  %d incoming without a start report, %d running, %d responses waiting for delivery" % (
                b["incoming"]["unstarted"], b["incoming"]["running"], b["responses"]["pending"]))
        if f.get("dispatcher"):
            print("  bridge    %s (%s)" % (f["dispatcher"].get("state"),
                                         ", ".join(f["dispatcher"].get("reasons") or []) or "no findings"))
        i = f["inbox"]
        print("  inbox     %d messages, %d pending %s" % (i["messages"], i["pending"],
                                                         json.dumps(i["by_agent"], ensure_ascii=False)))
        print("  versions  plugin %s, durable runtime %s" % (f["plugin_version"], f.get("runtime_version") or
                                                             ("older than 0.3.0" if f["runtime"]["present"]
                                                              else "not installed")))
        up = f.get("update")
        if up and up["status"] == "update_available":
            print("  update    %s available (checked %s): use the yobiru-upgrade skill" % (up["latest"],
                                                                                         up["checked_at"]))
        elif not up:
            print("  update    not checked yet: `%s update check`" % PRODUCT)
    return 0


# ---------------------------------------------------------------- config planning (setup / wake-config)
class Plan(object):
    """Collects intended config.json changes; writes only on --apply, with a backup of the old file."""

    def __init__(self, home, title):
        self.home, self.title = home, title
        self.path = home / "config.json"
        self.exists = self.path.is_file()
        self.raw_bytes = self.path.read_bytes() if self.exists else None
        self.before = None
        if self.exists:
            try:
                self.before = json.loads(self.raw_bytes.decode("utf-8"))
            except ValueError as e:
                die("%s is not valid JSON (%s); fix it by hand, %s will not overwrite it" % (self.path, e, PRODUCT))
            if not isinstance(self.before, dict):
                die("%s must contain a JSON object; %s will not overwrite it" % (self.path, PRODUCT))
        self.after = json.loads(json.dumps(self.before)) if self.before is not None else {}
        self.changes, self.errors, self.notes = [], [], []

    def change(self, text):
        self.changes.append(text)

    def error(self, text):
        self.errors.append(text)

    def note(self, text):
        self.notes.append(text)

    def finish(self, apply, as_json):
        if not self.errors and self.changes:
            try:
                cfg, agents = core().parse_config(self.after)
                for name in agent_names(cfg, agents):
                    core().Ctx.agent_adapter(_AdapterView(cfg, agents), name)
            except (RuntimeError, core().AdapterError) as e:
                self.error("resulting config would be invalid: %s" % e)
        action = "none" if not self.changes else ("update" if self.exists else "create")
        written, backup = False, None
        if apply and self.changes and not self.errors:
            backup = self._write()
            written = True
        result = {"command": self.title, "config": str(self.path), "action": action, "dry_run": not apply,
                  "written": written, "backup": backup, "changes": self.changes, "errors": self.errors,
                  "notes": self.notes}
        if as_json:
            print(json.dumps(result, indent=2, ensure_ascii=True))
        else:
            head = "applied" if written else "refused, nothing written" if self.errors else \
                "dry run, nothing written" if self.changes else "nothing to change"
            print("%s %s (%s)" % (PRODUCT, self.title, head))
            print("  config: %s (%s)" % (self.path, {"none": "unchanged", "update": "existing file",
                                                     "create": "new file"}[action]))
            for c in self.changes:
                print("  change: %s" % c)
            for e in self.errors:
                print("  error:  %s" % e)
            for n in self.notes:
                print("  note:   %s" % n)
            if backup:
                print("  backup: %s" % backup)
            if self.changes and not apply and not self.errors:
                print("  review the changes above, then re-run the same command with --apply")
        return 1 if self.errors else 0

    def _write(self):
        if self.exists and self.path.read_bytes() != self.raw_bytes:
            die("%s changed while planning; nothing written, re-run the command" % self.path)
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = (json.dumps(self.after, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        mode = stat.S_IMODE(self.path.stat().st_mode) if self.exists else 0o600
        backup = None
        if self.exists:
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = self.path.with_name("config.json.bak-" + stamp)
            n = 1
            while backup.exists():
                backup = self.path.with_name("config.json.bak-%s-%d" % (stamp, n))
                n += 1
            fd = os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(self.raw_bytes)
        tmp = self.path.with_name(".config.json.%s-%d.tmp" % (PRODUCT, os.getpid()))
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.chmod(str(tmp), mode)
        os.replace(str(tmp), str(self.path))
        return str(backup) if backup else None


def valid_agent(name, what="agent name"):
    err = core().agent_name_error(name)
    if err:
        die("bad %s %r: %s" % (what, name, err), 2)
    return unicodedata.normalize("NFC", name)


def valid_peer(rule, what="peer"):
    try:
        core().parse_access_policy({"allow": [rule]})
    except RuntimeError:
        die("bad %s %r: use a Tailscale hostname or FQDN, tag:<name> or user:<login>" % (what, rule), 2)
    return rule


def set_scalar(plan, key, value, replace, label=None):
    label = label or key
    cur = plan.after.get(key)
    if cur == value or (isinstance(cur, str) and unicodedata.normalize("NFC", cur) == value):
        return
    if cur is not None and not replace:
        plan.error("%s is already %s; pass --replace %s to change it to %s"
                   % (label, json.dumps(cur, ensure_ascii=False), key, json.dumps(value, ensure_ascii=False)))
        return
    plan.after[key] = value
    plan.change("%s: %s -> %s" % (label, json.dumps(cur, ensure_ascii=False), json.dumps(value, ensure_ascii=False)))


def cmd_policy(home, home_source, argv):
    ap = argparse.ArgumentParser(allow_abbrev=False, prog=PRODUCT + " policy",
                                 description="Read or deliberately change incoming access policy. Tailscale "
                                             "authentication and network rules always apply; deny rules win. "
                                             "Wake permissions and session bindings are independent.")
    sub = ap.add_subparsers(dest="action", required=True)
    show = sub.add_parser("show", allow_abbrev=False, help="show effective policy without network calls")
    show.add_argument("--json", action="store_true")
    change = sub.add_parser("set", allow_abbrev=False, help="plan a policy change; --apply writes a backed-up config")
    change.add_argument("--mode", choices=("tailnet", "allowlist"), required=True)
    for name in ("allow", "deny"):
        group = change.add_mutually_exclusive_group()
        group.add_argument("--" + name, action="append", metavar="PEER",
                           help="replace the %s list with these rules (repeatable); omitted keeps existing rules" % name)
        group.add_argument("--clear-" + name, action="store_true", help="explicitly empty the %s list" % name)
    change.add_argument("--apply", action="store_true", help="write config.json and keep a backup")
    change.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    raw, error = read_config(home)
    if error:
        die(error, 2)
    if a.action == "show":
        try:
            policy = core().parse_access_policy(raw or {})
        except RuntimeError as e:
            die(str(e), 2)
        legacy = raw is not None and "access_policy" not in raw and "allow" in raw
        result = {"config": str(home / "config.json"), "config_exists": raw is not None,
                  "policy": policy, "legacy_allowlist": legacy, "changed": False}
        if a.json:
            print(json.dumps(result, indent=2, ensure_ascii=True))
        else:
            print("%s access policy (read-only): %s%s" % (
                PRODUCT, policy["access_policy"], " (new-install default; no config yet)" if raw is None else ""))
            print("  allow: %s%s" % (", ".join(policy["allow"]) or "(none)",
                                       " (inactive in tailnet mode)" if policy["access_policy"] == "tailnet" else ""))
            print("  deny: %s (takes precedence)" % (", ".join(policy["deny"]) or "(none)"))
            print("  Tailscale authentication and network rules always apply; wake permissions are separate.")
            if legacy:
                print("  legacy allow list preserved. To migrate: `%s policy set --mode tailnet` "
                      "(review, then --apply)." % PRODUCT)
        return 0
    plan = Plan(home, "policy set")
    if not plan.exists:
        die("not set up: run `%s setup` first (dry run, then --apply)" % PRODUCT, 2)
    # Validate the current policy too: do not normalize a malformed config into a broader policy.
    try:
        core().parse_access_policy(plan.before)
    except RuntimeError as e:
        die(str(e), 2)
    set_scalar(plan, "access_policy", a.mode, True)
    for name in ("allow", "deny"):
        supplied = getattr(a, name)
        if supplied is None and not getattr(a, "clear_" + name):
            continue
        rules = []
        for rule in supplied or []:
            rule = valid_peer(rule, name + " rule")
            if rule.lower().rstrip(".") not in [r.lower().rstrip(".") for r in rules]:
                rules.append(rule)
        if plan.after.get(name) != rules:
            plan.after[name] = rules
            plan.change("%s = %s" % (name, json.dumps(rules, ensure_ascii=False)))
    if a.mode == "tailnet":
        plan.note("accept authenticated Tailscale peers that can reach this receiver, except deny matches; "
                  "saved allow rules are inactive in tailnet mode")
    elif not plan.after.get("allow"):
        plan.note("allowlist with no allow rules rejects incoming messages from every peer")
    plan.note("deny rules take precedence in both modes; Tailscale network rules and shields-up are unchanged")
    plan.note("a 0.4.0+ receiver reloads access policy for each request; an older receiver must be upgraded first")
    return plan.finish(a.apply, a.json)


def cmd_setup(home, home_source, argv):
    ap = argparse.ArgumentParser(allow_abbrev=False,
        prog=PRODUCT + " setup",
        description="Create or extend config.json in the data home. Dry run unless --apply. Only adds what you "
                    "name: existing access policy, agents, adapters and unknown keys are kept as they are. "
                    "New configs default to authenticated tailnet peers; --allow selects allowlist on first setup.")
    ap.add_argument("--allow", action="append", default=[], metavar="PEER",
                    help="add an allow rule: hostname, FQDN, tag:<name> or user:<login> (repeatable); selects "
                         "allowlist for a new config, preserves the mode for an existing config")
    ap.add_argument("--send-only", action="store_true",
                    help="create a config that accepts no incoming messages (empty allow list)")
    ap.add_argument("--agent", action="append", default=[], metavar="NAME",
                    help="a local agent that can be addressed as <this-machine>/NAME (repeatable)")
    ap.add_argument("--self-agent", metavar="NAME", help="this agent's name, sent as from_agent so replies come back")
    ap.add_argument("--default-agent", metavar="NAME", help="agent that receives messages that name no agent")
    ap.add_argument("--port", type=int, help="receiver port (default 7766)")
    ap.add_argument("--tailscale-cmd", metavar="CMD",
                    help='tailscale CLI path, or a JSON list such as ["sudo","-n","tailscale"]')
    ap.add_argument("--replace", action="append", default=[],
                    choices=("self_agent", "default_agent", "port", "tailscale_cmd"),
                    help="allow changing this already-set field (repeatable)")
    ap.add_argument("--apply", action="store_true", help="write the change (a backup of the old file is kept)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.send_only and a.allow:
        die("--send-only and --allow cannot be used together", 2)
    plan = Plan(home, "setup")
    if not plan.exists:
        mode = "allowlist" if a.allow or a.send_only else "tailnet"
        plan.after = {"port": 7766, "access_policy": mode, "allow": [], "deny": []}
        plan.change("create %s with port 7766" % plan.path)
        plan.note("incoming access policy: %s; authenticated Tailscale identity is always required" % mode)
    elif a.send_only:
        plan.note("--send-only only applies to a new config; the existing access policy is unchanged")
    try:
        effective_policy = core().parse_access_policy(plan.after)
    except RuntimeError as e:
        die(str(e), 2)
    if plan.exists and "access_policy" not in plan.after and "allow" not in plan.after and a.allow:
        # Adding allow rules to an implicit tailnet config must not silently change its effective mode.
        plan.after["access_policy"] = effective_policy["access_policy"]
        plan.change("access_policy = tailnet (preserve existing effective mode)")
    if plan.exists and a.allow and effective_policy["access_policy"] == "tailnet":
        plan.note("allow rules are saved but inactive in tailnet mode; use `policy set --mode allowlist` to restrict access")
    allow = plan.after.get("allow")
    if allow is None:
        allow = []
    elif not isinstance(allow, list):
        die("config allow must be a list; fix it by hand", 2)
    grew = False
    for rule in a.allow:
        rule = valid_peer(rule)
        if rule.lower() in [str(x).lower().rstrip(".") for x in allow]:
            continue
        allow.append(rule)
        grew = True
        plan.change("allow += %s" % rule)
    if grew or "allow" in plan.after:
        plan.after["allow"] = allow
    agents = plan.after.get("agents") if plan.after.get("agents") is not None else {}
    if not isinstance(agents, dict):
        die("config agents must be an object; fix it by hand", 2)
    for name in a.agent:
        name = valid_agent(name)
        if name not in agents and name not in [unicodedata.normalize("NFC", k) for k in agents]:
            agents[name] = {}
            plan.change("agents.%s = {} (addressable, no wake adapter yet)" % name)
    if agents or "agents" in plan.after:
        plan.after["agents"] = agents
    if a.self_agent is not None:
        set_scalar(plan, "self_agent", valid_agent(a.self_agent, "self agent"), "self_agent" in a.replace)
    if a.default_agent is not None:
        set_scalar(plan, "default_agent", valid_agent(a.default_agent, "default agent"),
                   "default_agent" in a.replace)
    if a.port is not None:
        if not 1 <= a.port <= 65535:
            die("--port must be 1-65535", 2)
        if plan.exists:
            set_scalar(plan, "port", a.port, "port" in a.replace)
            if plan.before.get("port", 7766) != a.port:
                plan.note("peers address this machine by port; a new port needs `host:port` on their side")
        elif a.port != 7766:
            plan.after["port"] = a.port
            plan.changes[0] = "create %s with port %d" % (plan.path, a.port)
    if a.tailscale_cmd is not None:
        cmd = a.tailscale_cmd
        if cmd.startswith("["):
            try:
                cmd = json.loads(cmd)
            except ValueError:
                die("--tailscale-cmd JSON list is invalid", 2)
        else:
            cmd = [cmd]
        if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) and x and "\n" not in x for x in cmd):
            die("--tailscale-cmd must be a path or a JSON list of strings", 2)
        set_scalar(plan, "tailscale_cmd", cmd, "tailscale_cmd" in a.replace)
    if plan.changes:
        plan.note("a 0.4.0+ receiver reloads access policy per request; other config changes require a receiver restart "
                  "(%s never restarts services)" % PRODUCT)
        plan.note("Tailscale ACLs and shields-up are not touched: the tailnet policy must let these peers reach "
                  "tcp:%s" % plan.after.get("port", 7766))
    return plan.finish(a.apply, a.json)


def check_secret_file(plan, key, value):
    p = Path(value).expanduser()
    if not p.is_absolute():
        plan.error("--%s-file must be an absolute path (got %s)" % (key, value))
        return None
    try:
        st = p.stat()
    except OSError:
        plan.error("%s file %s does not exist; create it first with `umask 077` (never paste the value into a "
                   "command line or chat)" % (key, p))
        return None
    if os.name == "posix" and st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        plan.error("%s file %s must be mode 0600: chmod 600 '%s'" % (key, p, p))
    return str(p)


def parse_level(value, what):
    if value not in LEVELS:
        die("%s must be one of now/batch/none" % what, 2)
    return value


def adapter_for_agent(plan, a, spec, where, name, before_eff, new_adapter):
    own = spec.get("wake_adapter")
    if a.remove_adapter:
        if own is None:
            plan.note("%s is not set; nothing to remove (@%s is woken by: %s)" % (where, name, describe(before_eff)))
            return
        del spec["wake_adapter"]
        after_eff = effective_adapter(plan.after, name)
        if after_eff is not None:
            plan.change("%s removed; @%s now falls back to the top-level adapter: %s"
                        % (where, name, describe(after_eff)))
            plan.note("to stop waking @%s entirely use --wake-max batch (its messages then wait in "
                      "`inbox --pending`)" % name)
        else:
            plan.change("%s removed; nobody is woken for @%s: its messages wait in `inbox --pending`"
                        % (where, name))
        return
    if same_adapter(own, new_adapter):
        return
    if before_eff is not None and not same_adapter(before_eff, new_adapter) and not a.replace:
        plan.error("@%s is already woken by %s adapter (%s); pass --replace to change it"
                   % (name, "its own" if own is not None else "the inherited top-level", describe(before_eff)))
        return
    spec["wake_adapter"] = new_adapter
    plan.change("%s = %s" % (where, describe(new_adapter)))


def adapter_top_level(plan, a, cfg, new_adapter):
    cur = cfg.get("wake_adapter")
    if cur is None and cfg.get("on_message"):
        cur = {"type": "exec", "argv": cfg["on_message"], "_alias": "on_message"}
    dflt = cfg.get("default_agent")
    if a.remove_adapter:
        if cur is None:
            plan.note("no top-level adapter is set; nothing to remove")
            return
        cfg.pop("wake_adapter", None)
        if cfg.get("on_message"):
            cfg.pop("on_message")
            plan.change("on_message removed (deprecated alias of the top-level exec adapter)")
        after_eff = effective_adapter(plan.after, unicodedata.normalize("NFC", dflt) if dflt else None)
        plan.change("wake_adapter removed; %s" % (
            "default agent @%s keeps its own adapter: %s" % (dflt, describe(after_eff)) if after_eff is not None
            else "messages for the default agent (or with no agent) now wait in `inbox --pending`"))
        return
    if same_adapter(cur, new_adapter):
        return
    if cur is not None and not a.replace:
        plan.error("the top-level adapter is already set (%s); pass --replace to change it" % describe(cur))
        return
    cfg["wake_adapter"] = new_adapter
    if cfg.get("on_message"):
        plan.note("deprecated on_message stays in the file but wake_adapter now takes precedence")
    plan.change("wake_adapter = %s" % describe(new_adapter))


def cmd_wake_config(home, home_source, argv):
    ap = argparse.ArgumentParser(allow_abbrev=False,
        prog=PRODUCT + " wake-config",
        description="Receiver-side wake settings. The receiver always decides; a sender's --wake is only a request. "
                    "Dry run unless --apply. Only the agent or fields you name are touched; replacing an "
                    "existing value needs --replace. Webhook URLs and secrets are referenced by file or "
                    "environment variable name and are never read, printed or stored by this command.")
    target = ap.add_mutually_exclusive_group()
    target.add_argument("--agent", metavar="NAME", help="configure this local agent's adapter / wake_max")
    target.add_argument("--top-level", action="store_true",
                        help="configure the top-level adapter (used by the default agent or single-agent setups)")
    kind = ap.add_mutually_exclusive_group()
    kind.add_argument("--webhook", action="store_true", help="wake by HTTP POST (for example a Grok Bot routine)")
    kind.add_argument("--exec", dest="exec_path", metavar="PATH", help="wake by running this local program")
    kind.add_argument("--remove-adapter", action="store_true",
                      help="remove this adapter; the output says what wakes the agent afterwards (the default agent "
                           "falls back to the top-level adapter if one is set; otherwise its messages stay pending). "
                           "To stop waking an agent entirely use --wake-max batch")
    ap.add_argument("--url-file", help="0600 file holding the webhook URL")
    ap.add_argument("--url-env", help="environment variable holding the webhook URL (in the serve process)")
    ap.add_argument("--secret-file", help="0600 file holding the webhook key")
    ap.add_argument("--secret-env", help="environment variable holding the webhook key")
    ap.add_argument("--header", help="auth header name (default Authorization)")
    ap.add_argument("--scheme", help='auth scheme (default Bearer; "" sends the raw key)')
    ap.add_argument("--exec-arg", action="append", default=[], metavar="ARG",
                    help="extra argument for the --exec program (repeatable)")
    ap.add_argument("--wake-max", metavar="LEVEL", help="cap for this agent: now, batch or none")
    ap.add_argument("--peer-max", action="append", default=[], metavar="PEER=LEVEL",
                    help="cap for a sender (wake_policy.peers), e.g. bot-box=now (repeatable)")
    ap.add_argument("--default-max", metavar="LEVEL", help="cap for every other sender (wake_policy.default_max)")
    ap.add_argument("--quiet-hours", metavar="HH:MM-HH:MM|off", help="downgrade now to batch in this window")
    ap.add_argument("--tz", metavar="ZONE", help="IANA time zone for --quiet-hours (default: local time)")
    ap.add_argument("--rate", type=int, metavar="N", help="max effective `now` per sender and agent per hour")
    ap.add_argument("--replace", action="store_true", help="allow replacing values this command touches")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    plan = Plan(home, "wake-config")
    if not plan.exists:
        die("no config at %s; run `%s setup` first" % (plan.path, PRODUCT), 2)
    cfg = plan.after
    wants_adapter = a.webhook or a.exec_path or a.remove_adapter
    if (wants_adapter or a.wake_max) and not (a.agent or a.top_level):
        die("name the target: --agent NAME (or --top-level for the default adapter)", 2)
    if a.wake_max and a.top_level:
        die("--wake-max is per agent: use --agent NAME", 2)
    if not any([wants_adapter, a.wake_max, a.peer_max, a.default_max, a.quiet_hours, a.rate is not None]):
        die("nothing to change; see `%s wake-config --help`" % PRODUCT, 2)
    if not a.webhook and any(x is not None for x in (a.url_file, a.url_env, a.secret_file, a.secret_env,
                                                     a.header, a.scheme)):
        die("--url-*/--secret-*/--header/--scheme belong to --webhook", 2)
    if a.exec_arg and not a.exec_path:
        die("--exec-arg belongs to --exec", 2)
    spec, key, name, before_eff = None, None, None, None
    if a.agent:
        name = valid_agent(a.agent)
        agents = cfg.get("agents") if cfg.get("agents") is not None else {}
        if not isinstance(agents, dict):
            die("config agents must be an object; fix it by hand", 2)
        same = [k for k in agents if unicodedata.normalize("NFC", k) == name]
        if len(same) > 1:
            die("config has %d spellings of agent %r (%s); merge them by hand first"
                % (len(same), name, ", ".join(json.dumps(k) for k in same)), 2)
        key = same[0] if same else name   # tailmsg compares names after NFC: reuse the existing spelling
        before_eff = effective_adapter(plan.before, name)
        if not same:
            agents[key] = {}
            plan.change("agents.%s = {} (new addressable agent)" % key)
        if agents[key] is None:
            agents[key] = {}
        cfg["agents"] = agents
        spec = agents[key]
        where = "agents.%s.wake_adapter" % key
    else:
        spec = cfg
        where = "wake_adapter"
    new_adapter = None
    if a.webhook:
        if bool(a.url_file) == bool(a.url_env):
            die("--webhook needs exactly one of --url-file / --url-env", 2)
        if a.secret_file and a.secret_env:
            die("use only one of --secret-file / --secret-env", 2)
        new_adapter = {"type": "webhook"}
        if a.url_file:
            new_adapter["url_file"] = check_secret_file(plan, "url", a.url_file)
        else:
            if not ENV_NAME_RE.match(a.url_env):
                die("bad --url-env name", 2)
            new_adapter["url_env"] = a.url_env
        if a.secret_file:
            new_adapter["secret_file"] = check_secret_file(plan, "secret", a.secret_file)
        elif a.secret_env:
            if not ENV_NAME_RE.match(a.secret_env):
                die("bad --secret-env name", 2)
            new_adapter["secret_env"] = a.secret_env
        else:
            plan.note("no secret configured: the webhook is called without an auth header")
        for env_key in ("url_env", "secret_env"):
            if new_adapter.get(env_key) and not os.environ.get(new_adapter[env_key]):
                plan.note("$%s must be set in the environment of the serve process" % new_adapter[env_key])
        if a.header is not None:
            new_adapter["header"] = a.header
        if a.scheme is not None:
            if len(a.scheme) > 32 or re.search(r"[\r\n\s]", a.scheme):
                die("bad --scheme", 2)
            new_adapter["scheme"] = a.scheme
    elif a.exec_path:
        path = os.path.expanduser(a.exec_path)
        if not os.path.isabs(path):
            die("--exec needs an absolute path (the serve process may have a different PATH and cwd)", 2)
        if not (os.path.isfile(path) and os.access(path, os.X_OK)):
            plan.error("%s is not an executable file" % path)
        if any("\x00" in x or "\n" in x for x in a.exec_arg):
            die("bad --exec-arg", 2)
        new_adapter = {"type": "exec", "argv": [path] + list(a.exec_arg)}
    if new_adapter is not None or a.remove_adapter:
        if new_adapter is not None and not any(v is None for v in new_adapter.values()):
            try:
                core().adapter_cfg({"wake_adapter": new_adapter})
            except core().AdapterError as e:
                plan.error(str(e))
        if a.agent:
            adapter_for_agent(plan, a, spec, where, name, before_eff, new_adapter)
        else:
            adapter_top_level(plan, a, cfg, new_adapter)
    if a.wake_max:
        level = parse_level(a.wake_max, "--wake-max")
        cur = spec.get("wake_max")
        if cur != level:
            if cur is not None and not a.replace:
                plan.error("agents.%s.wake_max is already %s; pass --replace" % (key, cur))
            else:
                spec["wake_max"] = level
                plan.change("agents.%s.wake_max: %s -> %s" % (key, cur, level))
    if a.peer_max or a.default_max:
        pol = cfg.get("wake_policy") if cfg.get("wake_policy") is not None else {}
        if not isinstance(pol, dict):
            die("config wake_policy must be an object; fix it by hand", 2)
        peers = pol.get("peers") if pol.get("peers") is not None else {}
        if not isinstance(peers, dict):
            die("config wake_policy.peers must be an object; fix it by hand", 2)
        for item in a.peer_max:
            if "=" not in item:
                die("--peer-max wants PEER=LEVEL", 2)
            peer, level = item.rsplit("=", 1)
            peer, level = valid_peer(peer), parse_level(level, "--peer-max level")
            existing = [k for k in peers if str(k).lower().rstrip(".") == peer.lower()]
            k = existing[0] if existing else peer
            if peers.get(k) == level:
                continue
            if existing and not a.replace:
                plan.error("wake_policy.peers.%s is already %s; pass --replace" % (k, peers[k]))
                continue
            plan.change("wake_policy.peers.%s: %s -> %s" % (k, peers.get(k), level))
            peers[k] = level
        if a.default_max:
            level = parse_level(a.default_max, "--default-max")
            cur = pol.get("default_max")
            if cur != level:
                if cur is not None and not a.replace:
                    plan.error("wake_policy.default_max is already %s; pass --replace" % cur)
                else:
                    pol["default_max"] = level
                    plan.change("wake_policy.default_max: %s -> %s" % (cur, level))
                    if level == "now":
                        plan.note("default_max now lets every allowed sender wake this machine immediately")
        if peers or "peers" in pol:
            pol["peers"] = peers
        cfg["wake_policy"] = pol
    if a.quiet_hours:
        if a.quiet_hours == "off":
            q = None
        else:
            m = re.match(r"^(\d\d:\d\d)-(\d\d:\d\d)\Z", a.quiet_hours)
            if not m or not HHMM_RE.match(m.group(1)) or not HHMM_RE.match(m.group(2)):
                die("--quiet-hours wants HH:MM-HH:MM (24h) or off", 2)
            q = {"start": m.group(1), "end": m.group(2)}
            if a.tz:
                try:
                    from zoneinfo import ZoneInfo
                    ZoneInfo(a.tz)
                except ImportError:
                    plan.note("cannot validate --tz on this Python; tailmsg ignores an unknown zone")
                except Exception:
                    die("unknown time zone %r" % a.tz, 2)
                q["tz"] = a.tz
        if cfg.get("quiet_hours") != q:
            if cfg.get("quiet_hours") is not None and not a.replace:
                plan.error("quiet_hours is already %s; pass --replace" % json.dumps(cfg["quiet_hours"]))
            else:
                plan.change("quiet_hours: %s -> %s" % (json.dumps(cfg.get("quiet_hours")), json.dumps(q)))
                cfg["quiet_hours"] = q
    elif a.tz:
        die("--tz belongs to --quiet-hours", 2)
    if a.rate is not None:
        if not 0 <= a.rate <= 1000:
            die("--rate must be 0-1000", 2)
        if cfg.get("wake_rate") != a.rate:
            if cfg.get("wake_rate") is not None and not a.replace:
                plan.error("wake_rate is already %s; pass --replace" % cfg["wake_rate"])
            else:
                plan.change("wake_rate: %s -> %s" % (cfg.get("wake_rate"), a.rate))
                cfg["wake_rate"] = a.rate
    if plan.changes:
        plan.note("restart the running receiver to load the new settings (%s never restarts services)" % PRODUCT)
        plan.note("a wake adapter only notifies an agent; message text is never a command or an approval")
    return plan.finish(a.apply, a.json)


def effective_adapter(raw, agent):
    """The adapter tailmsg would call for `agent` (NFC name or None) under config object `raw`: the agent's own
    adapter, else (default agent / no agent only) the top-level wake_adapter or on_message. None = nobody is
    woken. A misconfigured adapter is returned as {"_invalid": reason} so it still counts as configured."""
    try:
        cfg, agents = core().parse_config(raw if raw is not None else {})
        return core().Ctx.agent_adapter(_AdapterView(cfg, agents), agent)
    except core().AdapterError as e:
        return {"_invalid": str(e)}
    except RuntimeError as e:
        return {"_invalid": "config: %s" % e}


def same_adapter(x, y):
    strip = lambda d: None if d is None else {k: v for k, v in d.items() if not str(k).startswith("_")}
    return strip(x) == strip(y)


def describe(adapter):
    if adapter is None:
        return "none"
    if adapter.get("_invalid"):
        return "misconfigured (%s)" % adapter["_invalid"]
    return core().describe_adapter(_described(adapter))


def _described(adapter):
    a = dict(adapter)
    if a.get("type") == "exec" and not a.get("argv"):
        a["argv"] = ["?"]
    return a


# ---------------------------------------------------------------- runtime + services
def cmd_install_runtime(home, home_source, argv):
    ap = argparse.ArgumentParser(allow_abbrev=False,
        prog=PRODUCT + " install-runtime",
        description="Copy the bundled tailmsg.py to <home>/tailmsg.py so a receiver service runs from a stable "
                    "path that plugin updates and uninstalls do not move. Dry run unless --apply. Never replaces "
                    "a different existing copy without --upgrade, and never restarts anything.")
    ap.add_argument("--upgrade", action="store_true", help="replace a different existing copy (kept as a backup)")
    ap.add_argument("--allow-downgrade", action="store_true",
                    help="with --upgrade: also replace a copy whose version is newer than the bundled one")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not CORE.is_file():
        die("bundled tailmsg.py is missing; reinstall the plugin", 2)
    target = home / "tailmsg.py"
    if not home.is_dir():
        die("no data home at %s; run `%s setup` first" % (home, PRODUCT), 2)
    request_dir = home / "requests"
    if request_dir.is_symlink() or (request_dir.exists() and not request_dir.is_dir()):
        die("request state path must be a real directory: %s" % request_dir, 2)
    new_hash = sha256_file(CORE)
    res = {"command": "install-runtime", "target": str(target), "bundled_sha256": new_hash, "dry_run": not a.apply,
           "written": False, "backup": None, "request_state_dir": str(request_dir),
           "bundled_version": file_product_version(CORE)[0], "existing_version": runtime_version(home)[0]}
    if target.is_file():
        old_hash = sha256_file(target)
        res["existing_sha256"] = old_hash
        if old_hash == new_hash:
            res["action"] = "none"
        elif not a.upgrade:
            res["action"] = "refused"
            res["error"] = ("%s already exists and differs from the bundled copy; it may be a running receiver. "
                            "Re-run with --upgrade (and --apply) to replace it with a backup." % target)
        elif compare_versions(res["existing_version"], res["bundled_version"]) == 1 and not a.allow_downgrade:
            # SemVer order: a development build such as 0.6.0-dev.1 also sorts above 0.6.0-beta.1. Replacing a newer
            # runtime can drop features and data handling it has, so it takes an explicit decision.
            res["action"] = "refused"
            res["error"] = ("%s is version %s, newer than the bundled %s; replacing it would downgrade the receiver "
                            "runtime. Keep it, or re-run with --upgrade --allow-downgrade (and --apply) if you "
                            "decided to go back." % (target, res["existing_version"], res["bundled_version"]))
        else:
            res["action"] = "upgrade"
    else:
        res["action"] = "install"
    if a.apply and res["action"] in ("install", "upgrade"):
        if res["action"] == "upgrade":
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = target.with_name("tailmsg.py.bak-" + stamp)
            shutil.copy2(str(target), str(backup))
            res["backup"] = str(backup)
        tmp = target.with_name(".tailmsg.py.%s-%d.tmp" % (PRODUCT, os.getpid()))
        shutil.copyfile(str(CORE), str(tmp))
        os.chmod(str(tmp), 0o644)
        os.replace(str(tmp), str(target))
        res["written"] = True
        res["next"] = "restart the receiver service if one is running (%s never restarts services)" % PRODUCT
    if a.apply and res["action"] != "refused":
        request_dir.mkdir(mode=0o700, exist_ok=True)
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        verb = {"none": "already current", "refused": "refused", "install": "install", "upgrade": "upgrade"}
        print("%s install-runtime: %s %s%s" % (PRODUCT, verb[res["action"]], target,
                                               "" if res["written"] or res["action"] in ("none", "refused")
                                               else " (dry run; add --apply)"))
        print("  versions: existing %s, bundled %s" % (res["existing_version"] or "-", res["bundled_version"] or "-"))
        for k in ("error", "backup", "next"):
            if res.get(k):
                print("  %s: %s" % (k, res[k]))
    return 1 if res["action"] == "refused" else 0


def systemd_quote(arg):
    return '"%s"' % arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")


def service_python():
    """Keep the interpreter entry point, including symlinks and virtual environments.

    Homebrew can report an executable inside a versioned Cellar keg even when
    launched through a stable symlink. In that case, use a matching stable path
    from the same prefix only; never select an arbitrary Python from PATH.
    """
    executable = sys.executable
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix) or hasattr(sys, "real_prefix"):
        return executable
    path = Path(executable)
    parts = path.parts
    for i, part in enumerate(parts):
        if (part != "Cellar" or len(parts) <= i + 3 or
                not re.fullmatch(r"python(?:@3(?:\.\d+)*)?", parts[i + 1])):
            continue
        prefix = Path(*parts[:i])
        formula = parts[i + 1]
        opt = prefix / "opt" / formula
        candidates = [prefix / "bin" / "python3", prefix / "bin" / path.name,
                      opt.joinpath(*parts[i + 3:])]
        for candidate in candidates:
            try:
                if candidate.is_file() and os.access(str(candidate), os.X_OK) and candidate.samefile(path):
                    return str(candidate)
            except OSError:
                pass  # Broken/removed links must not make a read-only template fail.
        break
    return executable


def cmd_service_template(home, home_source, argv):
    ap = argparse.ArgumentParser(allow_abbrev=False, prog=PRODUCT + " service-template",
                                 description="Print a receiver service definition to review and install yourself. "
                                             "Nothing is written or started.")
    ap.add_argument("kind", choices=("systemd", "systemd-user", "launchd", "windows"))
    ap.add_argument("--python", default=None, help="Python for the service (default: this one)")
    a = ap.parse_args(argv)
    python = a.python or service_python()
    runtime = home / "tailmsg.py"
    args = [python, str(runtime)]
    if home != Path(DEFAULT_HOME).expanduser().absolute():
        args += ["--home", str(home)]
    args.append("serve")
    warn = [] if runtime.is_file() else ["%s does not exist yet: run `%s install-runtime --apply` first"
                                         % (runtime, PRODUCT)]
    if a.kind in ("systemd", "systemd-user"):
        user = a.kind == "systemd-user"
        lines = ["# %s receiver (tailmsg protocol v1). Review, then install it yourself:" % DISPLAY]
        if user:
            lines += ["#   ~/.config/systemd/user/tailmsg.service, then: systemctl --user daemon-reload &&",
                      "#   systemctl --user enable --now tailmsg   (loginctl enable-linger keeps it running "
                      "after logout)"]
        else:
            lines += ["#   /etc/systemd/system/tailmsg.service, then: sudo systemctl daemon-reload &&",
                      "#   sudo systemctl enable --now tailmsg"]
        lines += ["# WARNING: %s" % w for w in warn]
        lines += ["[Unit]", "Description=tailmsg receiver (%s)" % DISPLAY,
                  "After=network-online.target" + ("" if user else " tailscaled.service")]
        if not user:
            lines.append("Wants=tailscaled.service")
        lines += ["", "[Service]"]
        if not user:
            lines.append("User=%s" % getpass.getuser())
        lines += ["ExecStart=%s" % " ".join(systemd_quote(x) for x in args), "Restart=always", "RestartSec=5",
                  "", "[Install]", "WantedBy=%s" % ("default.target" if user else "multi-user.target")]
        print("\n".join(lines))
    elif a.kind == "launchd":
        # stdout must stay a valid plist (the XML declaration has to come first): guidance goes to stderr
        for w in warn:
            print("WARNING: %s" % w, file=sys.stderr)
        print("Review, save stdout as ~/Library/LaunchAgents/%s.plist, then: launchctl load -w <that file>"
              % SERVICE_LABEL, file=sys.stderr)
        plist = {"Label": SERVICE_LABEL, "ProgramArguments": args, "RunAtLoad": True, "KeepAlive": True,
                 "StandardErrorPath": str(home / "serve.log")}
        # launchd starts jobs with PATH=/usr/bin:/bin:/usr/sbin:/sbin, so a bare tailscale_cmd that resolves here
        # (e.g. the standalone app's /usr/local/bin/tailscale) would not be found by the service.
        cfg, _ = read_config(home)
        ts_cmd = (cfg or {}).get("tailscale_cmd") or ["tailscale"]
        found = shutil.which(ts_cmd[0]) if isinstance(ts_cmd, list) and ts_cmd and "/" not in str(ts_cmd[0]) else None
        if found and os.path.dirname(found) not in LAUNCHD_PATH.split(":"):
            plist["EnvironmentVariables"] = {"PATH": LAUNCHD_PATH + ":" + os.path.dirname(found)}
        sys.stdout.write(plistlib.dumps(plist).decode("utf-8"))
    else:
        def ps(s):
            return "'" + s.replace("'", "''") + "'"
        inner = " ".join('"%s"' % x for x in args[1:])
        for w in warn:
            print("# WARNING: %s" % w)
        print("# Review, then run these PowerShell lines yourself (starts the receiver at logon):")
        print("$a = New-ScheduledTaskAction -Execute %s -Argument %s" % (ps(python), ps(inner)))
        print("$s = New-ScheduledTaskSettingsSet -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) "
              "-ExecutionTimeLimit 0")
        print("Register-ScheduledTask -TaskName tailmsg -Action $a -Trigger (New-ScheduledTaskTrigger -AtLogOn) "
              "-Settings $s")
    return 0


# ---------------------------------------------------------------- optional Codex bridge
BRIDGE_SERVICE = {"darwin": "dev.tailmsg.codex-bridge (launchd)", "linux": "tailmsg-codex-bridge.service (systemd --user)"}


def cmd_codex_bridge(home, home_source, argv):
    ap = argparse.ArgumentParser(allow_abbrev=False,
        prog=PRODUCT + " codex-bridge",
        description="Optional: queue authenticated messages into one fixed, existing Codex session. macOS uses the "
                    "Codex desktop app; Linux uses a user systemd service and your running local Codex app-server "
                    "through an explicit unix:/// socket. Install and rollback run the bundled installer, which "
                    "supports --dry-run (no writes, no service calls) and --no-start (files only) and refuses to "
                    "replace an existing dispatcher session or a different codex wake adapter.")
    sub = ap.add_subparsers(dest="action")
    sub.required = True
    sub.add_parser("status", help="read-only configuration, socket and queue diagnosis")
    pi = sub.add_parser("install", help="install or upgrade the bridge (macOS or Linux)")
    pi.add_argument("--thread-id", required=True, help="UUID of the existing Codex session used as the dispatcher")
    pi.add_argument("--codex-path", help="Codex CLI that provides `codex queue` (Linux default: `codex` on PATH)")
    pi.add_argument("--codex-remote", metavar="URL",
                    help="Linux, required: unix:///absolute/socket of your running local Codex app-server")
    pi.add_argument("--codex-home", help="Linux: CODEX_HOME of that app-server (default: installed value, "
                                         "$CODEX_HOME on first install, else ~/.codex)")
    replies = pi.add_mutually_exclusive_group()
    replies.add_argument("--allow-replies", action="store_true", help="only with the user's explicit authorization")
    replies.add_argument("--no-allow-replies", action="store_true",
                         help="turn automatic replies off (upgrades otherwise keep the installed setting)")
    pi.add_argument("--no-start", action="store_true", help="write files only; never touch services")
    pi.add_argument("--dry-run", action="store_true", help="run all checks and print the plan only")
    pr = sub.add_parser("rollback", help="restore an installation backup (inbox and queue are kept)")
    pr.add_argument("backup")
    pr.add_argument("--no-start", action="store_true", help="restore files only; never touch services")
    pr.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.action == "status":
        return bridge_status(home)
    if sys.platform not in BRIDGE_SERVICE:
        die("host capability missing: the Codex bridge manages a macOS LaunchAgent or a Linux user systemd "
            "service; on %s use `%s wake-config --agent codex --exec ...` instead" % (sys.platform, PRODUCT), 3)
    linux = sys.platform == "linux"
    if a.action == "install" and not linux and (a.codex_remote or a.codex_home):
        die("--codex-remote and --codex-home belong to the Linux bridge", 2)
    if a.action == "install" and linux and not a.codex_remote:
        die("the Linux bridge needs --codex-remote unix:///absolute/socket: the socket of your running local Codex "
            "app-server (this plugin does not start Codex)", 2)
    if not (home / "tailmsg.py").is_file():
        die("the bridge expects a durable receiver at %s; run `%s install-runtime` first"
            % (home / "tailmsg.py", PRODUCT), 2)
    installer = "install_codex_bridge_linux.py" if linux else "install_codex_bridge.py"
    cmd = [sys.executable, "-B", str(BRIDGE / installer), "--tailmsg-home", str(home)]
    if a.action == "install":
        cmd += ["--thread-id", a.thread_id]
        for flag, value in (("--codex-path", a.codex_path), ("--codex-remote", a.codex_remote),
                            ("--codex-home", a.codex_home)):
            if value:
                cmd += [flag, value]
        if a.allow_replies:
            cmd.append("--allow-replies")
        if a.no_allow_replies:
            cmd.append("--no-allow-replies")
    else:
        cmd += ["--rollback", a.backup]
    if a.no_start:
        cmd.append("--no-start")
    if a.dry_run:
        cmd.append("--dry-run")
    sys.stdout.flush()
    return subprocess.call(cmd)


def _queue_client():
    """The bundled queue client, for its socket checks (no bytecode is written into the plugin)."""
    spec = importlib.util.spec_from_file_location("yobiru_bundled_codex_queue_client", str(BRIDGE / "codex_queue_client.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BRIDGE_HINTS = {
    "socket_missing": "start your Codex app-server; known-unsent messages are retried automatically",
    "socket_stale": "nothing listens on the socket (stale file): restart the Codex app-server; messages are retried",
    "socket_unreachable": "the app-server socket does not accept connections; check the app-server",
    "socket_foreign": "the socket belongs to another user; point --codex-remote at your own app-server",
    "socket_invalid": "--codex-remote does not name a Unix socket",
    "socket_directory_unsafe": "other users can replace the socket; keep it in a private directory",
    "runtime_unavailable": "the Codex CLI is missing or not executable; reinstall with --codex-path",
    "app_unavailable": "the Codex desktop app is closed; messages are retried when it runs",
}


DIAGNOSTIC_HINTS = {
    "daemon_not_confirmed": "the bridge daemon has not reported recently (or never): check its service; without it "
                            "nothing is dispatched or observed. A stale heartbeat means busy or stopped, not running",
    "daemon_cycle_failed": "the bridge daemon's last cycle had a failing phase (diagnostics.daemon names it and the "
                           "error type)",
    "dispatch_blocked": "some notifications were not sent and wait for an automatic retry (known not sent)",
    "dispatch_stalled": "notifications are due but were not dispatched for minutes: check that the bridge daemon runs",
    "accepted_without_start": "accepted notifications show no start evidence past the threshold: the dispatcher "
                              "session may be closed, waiting for an approval, rate-limited or unloaded, and this "
                              "transport cannot tell which. Check the session; never resend or delete the queue",
    "deadline_passed_without_final": "a request passed its deadline without a final result: its outcome is unknown, "
                                     "not failed, and it must not be repeated automatically. After checking the actual "
                                     "effect, its owner may still record the real outcome with `request respond`",
    "running_without_recent_progress": "a request reported running without progress for longer than the threshold; "
                                       "that alone does not mean it failed or stopped",
}
DESKTOP_GAP = ("the desktop transport has no resident connection: the bridge cannot keep the session loaded, see rate "
               "limits or restart its consumer. Open the Codex desktop app and the dispatcher session yourself")


def bridge_status(home):
    cfg_path = home / "codex-bridge.json"
    out = {"installed": cfg_path.is_file(), "config": str(cfg_path)}
    if out["installed"]:
        cfg, db = {}, None
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            out["dispatcher_thread_id"] = cfg.get("dispatcher_thread_id")
            out["allow_replies"] = bool(cfg.get("allow_replies"))
            db = Path(cfg.get("bridge_home") or str(home / "codex-bridge")).expanduser().resolve() / "bridge.sqlite3"
        except (OSError, ValueError, AttributeError) as e:
            out["error"] = "cannot read bridge config: %s" % e
        if isinstance(cfg, dict) and cfg.get("excluded_kinds"):
            handler = cfg.get("excluded_kind_adapter")
            out["kind_routing"] = {"excluded_kinds": cfg.get("excluded_kinds"),
                                   "handler": handler[0] if isinstance(handler, list) and handler else None,
                                   "timeout_seconds": cfg.get("excluded_kind_timeout_seconds", 60)}
        remote = cfg.get("codex_remote") if isinstance(cfg, dict) else None
        out["transport"] = "unix_socket" if remote else "codex_desktop"
        out["service"] = BRIDGE_SERVICE["linux" if remote else "darwin"]
        hints = []
        if remote:
            out.update({"codex_remote": remote, "codex_home": cfg.get("codex_home"), "codex_path": cfg.get("codex_path")})
            cli = cfg.get("codex_path")
            out["codex_path_ok"] = bool(cli) and os.path.isfile(cli) and os.access(cli, os.X_OK)
            try:
                client = _queue_client()
                problem = client.unix_socket_problem(client.unix_socket_path(remote))
                out["socket"] = "listening" if problem is None else problem[0]
            except Exception as e:   # a diagnostic reports, it does not crash
                out["socket"] = "invalid_config"
                problem = ("invalid_config", str(e))
            if problem is not None:
                hints.append(BRIDGE_HINTS.get(problem[0], problem[1]))
            if not out["codex_path_ok"]:
                hints.append(BRIDGE_HINTS["runtime_unavailable"])
            try:
                resident_path = BRIDGE / "codex_resident.py"
                spec = importlib.util.spec_from_file_location("yobiru_codex_resident", str(resident_path))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                out["resident"] = module.resident_status(db.parent, cfg.get("dispatcher_thread_id"))
            except Exception:
                out["resident"] = {"state": "unknown"}
            if out["resident"]["state"] != "connected":
                hints.append("the fixed dispatcher is not confirmed resident; start/restart "
                             "tailmsg-codex-bridge.service and inspect its journal. A listening socket or "
                             "an enqueued receipt alone does not mean a turn can run")
        # The same read-only projection as the bridge's own `diagnose`: queue counts, execution evidence from the
        # request ledger, the daemon heartbeat and what this transport cannot observe.
        diag = dispatcher_diagnostics(home, cfg) if isinstance(cfg, dict) and cfg and db is not None else \
            {"state": "unknown", "error": "config_unreadable"}
        out["diagnostics"] = diag
        queue = diag.get("queue") or {}
        if queue.get("available"):
            out["queue"] = queue.get("states") or {}
            out["reasons"] = dict([("retry:%s" % k, n) for k, n in (queue.get("retry_reasons") or {}).items()] +
                                  [("uncertain:%s" % k, n) for k, n in (queue.get("uncertain_reasons") or {}).items()])
        elif queue.get("reason") not in (None, "queue_absent"):
            out["queue_error"] = queue["reason"]
        for reason in out.get("reasons", {}):
            state, error = reason.split(":", 1)
            if state == "retry" and error in BRIDGE_HINTS and BRIDGE_HINTS[error] not in hints:
                hints.append(BRIDGE_HINTS[error])
        if out.get("queue", {}).get("uncertain"):
            hints.append("uncertain: the queue command may have reached Codex, so it is never resent automatically. "
                         "The bridge keeps checking the dispatcher session (in its configured Codex home) for the "
                         "[TAILMSG:<id>] marker. If it never appears, handle that message by hand "
                         "(`inbox --pending`); do not delete the queue to force a resend")
        for reason in diag.get("reasons") or []:
            hint = DIAGNOSTIC_HINTS.get(reason)
            if hint and hint not in hints:
                hints.append(hint)
        if "accepted_without_start" in (diag.get("reasons") or []) and not remote:
            hints.append(DESKTOP_GAP)
        out["hints"] = hints
    print(json.dumps(out, indent=2, ensure_ascii=True))
    return 0


# ---------------------------------------------------------------- shared helpers for 0.3 commands
OFFICIAL_REPO = "chekusu/yobiru"
OFFICIAL_REPO_URL = "https://github.com/" + OFFICIAL_REPO
RELEASES_HOST = "api.github.com"
RELEASES_PATH = "/repos/chekusu/yobiru/releases?per_page=30"   # the list: works when every release is a prerelease
RELEASE_URL_PREFIX = OFFICIAL_REPO_URL + "/releases/"
MAX_RELEASES_BYTES = 2 * 1024 * 1024
MAX_REPLY_BYTES = 64 * 1024
NOTICE_KIND = "yobiru-upgrade-notice"
NOTICES_FILE = "upgrade-notices.json"
NOTICES_LOCK = ".upgrade-notices.lock"
UPDATE_FILE = "update-check.json"
SESSIONS_FILE = "sessions.json"
PRE_ID = r"(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"      # SemVer: numeric identifiers have no leading zero
SEMVER_RE = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
                       r"(?:-(" + PRE_ID + r"(?:\." + PRE_ID + r")*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z")
RUNTIME_VERSION_RE = re.compile(r'^PRODUCT_VERSION = "([0-9A-Za-z.+-]{1,64})"', re.M)
ISO_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")
ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{2,39}\Z")
MAC_TS_CLI = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
WIN_TS_CLI = "C:\\Program Files\\Tailscale\\tailscale.exe"
TS_DOCS = {"linux": "https://tailscale.com/docs/install/linux", "darwin": "https://tailscale.com/docs/install/mac",
           "win32": "https://tailscale.com/docs/install/windows", "download": "https://tailscale.com/download",
           "script": "https://tailscale.com/install.sh"}
NOTICE_STATUSES = ("pending", "dispatching", "notified", "blocked", "rejected", "uncertain", "verified")


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean(value, limit=200):
    """Remote or user text made safe for a terminal (no escape, control or bidi characters)."""
    return core().clean_text(value, limit)


def parse_semver(text):
    if not isinstance(text, str) or len(text) > 64:
        return None
    m = SEMVER_RE.match(text)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), tuple(m.group(4).split(".")) if m.group(4) else ())


def semver_key(v):
    """SemVer 2.0 precedence: a prerelease sorts before its release; numeric identifiers before text."""
    major, minor, patch, pre = v
    return (major, minor, patch, 0 if pre else 1,
            tuple((0, int(x), "") if x.isdigit() else (1, 0, x) for x in pre))


def fmt_semver(v):
    return "%d.%d.%d" % v[:3] + ("-" + ".".join(v[3]) if v[3] else "")


def compare_versions(a, b):
    """-1, 0, 1 for two version strings; None if either is not SemVer."""
    pa, pb = parse_semver(a), parse_semver(b)
    if pa is None or pb is None:
        return None
    ka, kb = semver_key(pa), semver_key(pb)
    return (ka > kb) - (ka < kb)


def write_json_atomic(path, obj, mode=0o600, backup=False):
    """Atomic replace through an exclusive temporary file; optional timestamped backup of the old file."""
    path = Path(path)
    saved = None
    if backup and path.is_file():
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        saved = path.with_name("%s.bak-%s" % (path.name, stamp))
        n = 1
        while saved.exists():
            saved = path.with_name("%s.bak-%s-%d" % (path.name, stamp, n))
            n += 1
        fd = os.open(str(saved), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(path.read_bytes())
    tmp = path.with_name(".%s.%s-%d.tmp" % (path.name, PRODUCT, os.getpid()))
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write((json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            out.flush()
            os.fsync(out.fileno())
        os.chmod(str(tmp), mode)
        os.replace(str(tmp), str(path))
    except BaseException:
        if os.path.lexists(str(tmp)):
            os.unlink(str(tmp))
        raise
    return str(saved) if saved else None


class LockBusy(Exception):
    pass


class StateLock(object):
    """Cross-process exclusive lock on a file in the data home (flock on POSIX, msvcrt on Windows)."""

    def __init__(self, path):
        self.path, self.fd = Path(path), None

    def __enter__(self):
        self.fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                import fcntl
            except ImportError:
                import msvcrt
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            self.fd = None
            raise LockBusy("another %s process is updating %s" % (PRODUCT, self.path.name))
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)     # closing releases flock / msvcrt locks
            self.fd = None


def runtime_version(home):
    """Version of the durable <home>/tailmsg.py, read as text (never executed). -> (version or None, how)."""
    return file_product_version(home / "tailmsg.py")


def file_product_version(path):
    """PRODUCT_VERSION of a tailmsg.py, read as text (never executed). -> (version or None, how)."""
    if not path.is_file():
        return None, "absent"
    try:
        with open(str(path), "rb") as fh:
            text = fh.read(4 * 1024 * 1024).decode("utf-8", "replace")
    except OSError:
        return None, "unreadable"
    m = RUNTIME_VERSION_RE.search(text)
    if m and parse_semver(m.group(1)):
        return m.group(1), "file"
    return None, "unversioned"      # releases before 0.3.0 carry no product version


class Deadline(object):
    """An absolute time limit for one HTTP exchange: when it passes, the socket is shut down, so a peer that
    trickles headers or body cannot hold a probe longer than `seconds` (per-read timeouts alone would)."""

    def __init__(self, conn, seconds):
        import threading
        self.conn, self.expired, self.sock = conn, False, None
        self.lock = threading.Lock()
        self.timer = threading.Timer(max(0.01, seconds), self._expire)
        self.timer.daemon = True

    def _expire(self):
        with self.lock:
            self.expired = True
            sock = self.sock or self.conn.sock
        self._abort(sock)

    @staticmethod
    def _abort(sock):
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def connect(self):
        """Connect, then keep the transport socket itself: for HTTP/1.0 or `Connection: close` replies
        getresponse() detaches conn.sock while the response still reads from that socket."""
        self.conn.connect()
        with self.lock:
            self.sock = self.conn.sock
            late = self.expired
        if late:
            self._abort(self.sock)
            raise socket.timeout("deadline exceeded")

    def __enter__(self):
        self.timer.start()
        return self

    def __exit__(self, *exc):
        self.timer.cancel()
        self.conn.close()

    def check(self):
        if self.expired:
            raise socket.timeout("deadline exceeded")


def http_request(ip, port, method, path, body=None, host=None, timeout=3.0):
    """-> (status, bytes) with a bounded body, all within `timeout` seconds in total (connect, headers and
    body). Raises socket.timeout when the deadline passes, otherwise OSError / HTTPException."""
    c = http.client.HTTPConnection(ip, int(port), timeout=timeout)
    with Deadline(c, timeout) as d:
        try:
            d.connect()
            headers = {"Host": "%s:%d" % (host or ip, int(port)), "Accept": "application/json"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            c.request(method, path, body=body, headers=headers)
            r = c.getresponse()
            data = r.read(MAX_REPLY_BYTES + 1)
        except (OSError, http.client.HTTPException):
            d.check()
            raise
        d.check()
        return r.status, data


def validate_session_view(s):
    if not isinstance(s, dict):
        return None
    sid = s.get("id")
    if not isinstance(sid, str) or not core().SESSION_ID_RE.match(sid):
        return None
    runtime = s.get("runtime") if isinstance(s.get("runtime"), str) and core().RUNTIME_RE.match(s["runtime"]) else "other"
    if (runtime == "codex" or s.get("id_source") == "codex-bridge") and not core().UUID_RE.match(sid):
        return None                   # a Codex session is always identified by its exact lowercase UUID
    if s.get("id_source") == "codex-bridge" and runtime != "codex":
        return None
    status = s.get("native_name_status")
    out = {"runtime": runtime, "id": sid,
           "id_source": s.get("id_source") if s.get("id_source") in ("codex-bridge", "configured") else "unknown",
           "display_name": clean(s.get("display_name")), "confirmed_title": clean(s.get("confirmed_title")),
           "confirmed_at": s.get("confirmed_at") if isinstance(s.get("confirmed_at"), str)
           and ISO_RE.match(s["confirmed_at"]) else None,
           "native_name": clean(s.get("native_name")),
           "native_name_status": status if status in ("ok", "unnamed", "not_supported", "unavailable", "pending")
           else "unknown",
           "native_name_checked_at": s.get("native_name_checked_at") if isinstance(s.get("native_name_checked_at"), str)
           and ISO_RE.match(s["native_name_checked_at"]) else None,
           "native_name_cached": s.get("native_name_cached") is True,
           "native_name_stale": s.get("native_name_stale") is True, "activity": "unknown"}
    err = s.get("native_name_error")
    if isinstance(err, str) and ERROR_CODE_RE.match(err):
        out["native_name_error"] = err
    if out["native_name_status"] != "ok":
        out["native_name"] = None
        out["native_name_stale"] = False
    return out


def validate_info(body, expect_node=None):
    """Bounded, typed view of a peer's /info body -> (info or None, problem or None). Never trusts it."""
    if len(body) > MAX_REPLY_BYTES:
        return None, "response too large"
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "invalid JSON"
    if not isinstance(obj, dict) or obj.get("ok") is not True or obj.get("v") != 1 or obj.get("product") != PRODUCT:
        return None, "not a Yobiru /info response"
    version = parse_semver(obj.get("version"))
    raw_agents = obj.get("agents")
    if not isinstance(raw_agents, list):
        return None, "agents missing"
    agents = []
    for item in raw_agents[:32]:
        if not isinstance(item, dict):
            continue
        name = item.get("agent")
        if not isinstance(name, str) or core().agent_name_error(name) or clean(name, 64) != name:
            continue
        agents.append({"agent": name, "default": item.get("default") is True,
                       "session": validate_session_view(item.get("session"))})
    node = clean(obj.get("node"), 253)
    info = {"version": fmt_semver(version) if version else None, "node": node, "agents": agents, "warnings": []}
    caps = obj.get("capabilities")
    info["capabilities"] = [c for c in caps[:32] if isinstance(c, str) and
                            re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", c)] if isinstance(caps, list) else []
    total = obj.get("agents_total")
    if obj.get("agents_truncated") is True or len(raw_agents) > 32 or \
            isinstance(total, int) and not isinstance(total, bool) and total > len(agents):
        info["warnings"].append("agent list truncated: showing %d of %s agents" % (
            len(agents), total if isinstance(total, int) and not isinstance(total, bool) else "more"))
    if expect_node and (node or "").lower().rstrip(".") != expect_node:
        info["warnings"].append("the receiver calls itself %s" % (node or "?"))
    return info, None


def identity_unavailable(body):
    """A receiver's documented 503 answer: its Tailscale identity lookup is temporarily unavailable."""
    try:
        obj = json.loads(body.decode("utf-8")) if len(body) <= MAX_REPLY_BYTES else None
    except (UnicodeDecodeError, ValueError):
        return False
    return isinstance(obj, dict) and obj.get("ok") is False and obj.get("code") == "identity_unavailable"


def probe_info(ip, port, timeout=3.0, expect_node=None, host=None):
    """What a receiver tells this machine. kind: info | legacy | blocked | unreachable | malformed | error.
    A 404 on /info is an older receiver (0.2.x or earlier), not a missing installation; 403 is policy."""
    res = {"address": "%s:%s" % (ip, port), "kind": "unreachable", "http_status": None, "info": None, "error": None}
    deadline = time.monotonic() + timeout          # one budget for /info and the /health fallback
    try:
        status, body = http_request(ip, port, "GET", "/info", host=host, timeout=timeout)
    except socket.timeout as e:
        res["error"] = "timeout"
        res["detail"] = core().transport_diagnostic(expect_node or host or ip, ip, port, "GET /info", e)
        return res
    except (OSError, http.client.HTTPException, ValueError) as e:
        res["error"] = type(e).__name__
        res["detail"] = core().transport_diagnostic(expect_node or host or ip, ip, port, "GET /info", e)
        return res
    res["http_status"] = status
    if status == 200:
        info, problem = validate_info(body, expect_node)
        res.update(kind="info", info=info) if info else res.update(kind="malformed", error=problem)
    elif status == 403:
        res["kind"] = "blocked"
    elif status == 503 and identity_unavailable(body):
        res.update(kind="error", error="HTTP 503: the receiver could not verify this machine's identity yet; "
                                        "try again shortly")
    elif status == 404:
        try:
            left = deadline - time.monotonic()
            if left <= 0.05:
                raise socket.timeout("deadline exceeded")
            hs, hb = http_request(ip, port, "GET", "/health", host=host, timeout=left)
            h = json.loads(hb.decode("utf-8")) if hs == 200 and len(hb) <= MAX_REPLY_BYTES else None
        except (OSError, http.client.HTTPException, ValueError):
            h = None
        if isinstance(h, dict) and h.get("ok") is True and h.get("v") == 1:
            res["kind"] = "legacy"
        else:
            res.update(kind="malformed", error="no /info and no tailmsg /health")
    else:
        res.update(kind="error", error="HTTP %d" % status)
    return res


def peer_doctor_checks(facts, peer, port, timeout=3.0):
    """Read-only peer checks. HTTP reachability does not reveal the tailnet's policy configuration."""
    checks = []

    def add(status, name, detail, fix=None):
        checks.append({"status": status, "check": name, "detail": detail, "fix": fix})

    ip, expect_node = None, None
    if valid_ip(peer):
        import ipaddress
        address = ipaddress.ip_address(peer)
        if not any(address in net for net in core().TAILNET_NETS):
            add("error", "peer-target", "use a Tailscale IP, hostname or full MagicDNS name")
            return checks
        # Shared peers may have no DNSName in status. Their explicit Tailscale address is still useful.
        ip = peer
    else:
        cmd = (facts.get("cfg") or {}).get("tailscale_cmd") or ["tailscale"]
        nodes, error = tailnet_nodes(cmd)
        if error:
            add("error", "peer-target", "cannot resolve peer: %s" % clean(error, 300),
                "check Tailscale status, or use the intended machine's exact Tailscale IP")
            return checks
        try:
            node = core().select_peer(nodes, peer)
        except ValueError as e:
            add("error", "peer-target", str(e), "use the intended exact MagicDNS name or Tailscale IP")
            return checks
        if node is None:
            add("error", "peer-target", "peer is not in this Tailscale snapshot: %s" % peer,
                "check that the share was accepted; use its full MagicDNS name or exact Tailscale IP")
            return checks
        ip, expect_node = node["ip"], node["node"]
    add("ok", "peer-target", "%s resolves to %s:%d" % (peer, ip, port))
    grant = {"src": ["<CALLER_TAILSCALE_IP>"], "dst": ["<RECEIVER_TAILSCALE_IP>"], "ip": ["tcp:%d" % port]}
    add("info", "peer-network-policy", "tailnet policy was not read; a timeout alone cannot identify an ACL/grant failure",
        "only with owner authorization, review this grant template alongside existing rules: %s; "
        "replace placeholders; this command applies nothing" % json.dumps(grant, separators=(",", ":")))
    try:
        status, body = http_request(ip, port, "GET", "/health", timeout=timeout, host=expect_node)
    except (OSError, http.client.HTTPException, ValueError) as e:
        reason = "timeout" if isinstance(e, socket.timeout) else type(e).__name__
        add("error", "peer-transport", "GET /health at %s:%d did not complete (%s)" % (ip, port, reason),
            "check the receiver/listen address, Tailscale path, firewall, grants and shields-up; "
            "ping success does not prove TCP access")
        return checks
    add("ok", "peer-transport", "HTTP answered at %s:%d" % (ip, port))
    try:
        health = json.loads(body.decode("utf-8")) if len(body) <= MAX_REPLY_BYTES else None
    except (UnicodeDecodeError, ValueError):
        health = None
    if status == 200 and isinstance(health, dict) and health.get("ok") is True and health.get("v") == 1:
        add("ok", "peer-health", "GET /health returned HTTP 200 with tailmsg v1 health")
    else:
        add("warn", "peer-health", "GET /health returned HTTP %d without valid tailmsg v1 health" % status,
            "check that the intended Yobiru receiver owns this port")
    result = probe_info(ip, port, timeout=timeout, expect_node=expect_node, host=expect_node)
    if result["kind"] == "blocked":
        add("error", "peer-identity", "GET /info returned HTTP 403: the receiver rejected identity or access policy",
            "inspect its authenticated Tailscale identity and Yobiru allow/deny policy; old receivers may reject "
            "shared peers with an empty name; do not widen policy automatically")
    elif result["kind"] == "info":
        add("ok", "peer-identity", "GET /info accepted this machine (Yobiru %s)" % (result["info"]["version"] or "unknown"))
        pull = "request-pull-v1" in result["info"]["capabilities"]
        add("ok" if pull else "warn", "peer-pull", "request-pull-v1 %s" % ("supported" if pull else "not advertised"),
            None if pull else "upgrade the target receiver before using request send --transport pull; "
            "legacy push replies and request attachments need a reverse connection")
        if pull:
            add("info", "peer-verification", "capability checks do not prove a message or attachment round trip",
                "with authorization, send a pull request with a harmless file, refresh its final response, "
                "fetch the response attachment and compare its SHA-256")
    elif result["kind"] == "legacy":
        add("warn", "peer-identity", "GET /info is unavailable on this legacy receiver; identity access is unverified")
        add("warn", "peer-pull", "request-pull-v1 not advertised", "upgrade the target receiver before using pull requests")
    else:
        add("error", "peer-identity", "GET /info could not verify identity access (%s: %s)" %
            (result["kind"], result.get("error") or "unknown"), "check the target receiver and retry; no message was sent")
    return checks


def tailnet_nodes(cmd, timeout=10):
    """-> ([{node, host, hostname, ip, ips, online, self, os}], error). Fresh Tailscale snapshot."""
    out, err = run_tailscale(cmd, ["status", "--json"], timeout)
    if err:
        return None, err
    try:
        st = json.loads(out)
    except ValueError:
        return None, "tailscale status returned invalid JSON"
    if not isinstance(st, dict):
        return None, "tailscale status returned invalid JSON"
    running = st.get("BackendState") == "Running"
    entries = [("self", st.get("Self"))] + [("peer", v) for v in (st.get("Peer") or {}).values()]
    nodes = []
    for kind, n in entries:
        if not isinstance(n, dict):
            continue
        fq = clean(str(n.get("DNSName") or ""), 253) or ""
        fq = fq.rstrip(".").lower()
        ips = [i for i in (n.get("TailscaleIPs") or []) if isinstance(i, str) and valid_ip(i)]
        if not fq or not ips:
            continue
        v4 = [i for i in ips if ":" not in i]
        nodes.append({"node": fq, "host": fq.split(".")[0], "ip": (v4 or ips)[0], "ips": ips,
                      "hostname": clean(n.get("HostName"), 253) or "",
                      "online": running if kind == "self" else n.get("Online") is True,
                      "self": kind == "self", "os": clean(n.get("OS"), 16)})
    return nodes, None


def valid_ip(text):
    import ipaddress
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


def parse_peer_spec(spec, default_port):
    """'host[:port][/agent]' -> (host, port, agent or None); dies on bad input."""
    host, agent = (spec.split("/", 1) + [None])[:2] if "/" in spec else (spec, None)
    port = default_port
    if host.count(":") == 1:
        host, p = host.split(":")
        try:
            port = int(p)
        except ValueError:
            die("bad port in --peer %r" % spec, 2)
        if not 1 <= port <= 65535:
            die("bad port in --peer %r" % spec, 2)
    if not host or not (PEER_RULE_RE.match(host) or valid_ip(host)) or host.startswith(("tag:", "user:")):
        die("bad --peer %r: use a Tailscale hostname, MagicDNS name or Tailscale IP" % spec, 2)
    if agent is not None:
        agent = valid_agent(agent, "agent in --peer")
    return host.lower().rstrip("."), port, agent


def match_node(nodes, host):
    try:
        return core().select_peer(nodes, host)
    except ValueError as e:
        die(str(e), 2)


def run_parallel(fn, items, limit=32):
    """fn(item) for each item with at most `limit` threads; results in input order."""
    import threading
    results = [None] * len(items)

    def worker(i):
        results[i] = fn(items[i])
    for start in range(0, len(items), limit):
        ts = [threading.Thread(target=worker, args=(i,)) for i in range(start, min(start + limit, len(items)))]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
    return results


def local_info(home, cfg, agents, me):
    """This machine's own projection from its files (when its receiver does not answer)."""
    payload = core().info_payload(home, cfg, agents, me or "this-machine",
                                  lambda b: dict(core().lookup_native_name(home, b), checked_at=now_iso()))
    info, _ = validate_info(json.dumps(payload).encode("utf-8"))
    return info


# ---------------------------------------------------------------- agents: who is online, which session
SOURCE_LABEL = {"codex-bridge": "fixed Codex bridge dispatcher", "configured": "configured binding",
                "unknown": "unknown source"}


def session_lines(s):
    if s is None:
        return ["no session bound on that machine (no session name or id to show; its wake adapter, if any, "
                "still decides how this agent is woken)"]
    lines = []
    if s.get("native_name"):
        lines.append('name "%s" (native %s session name, read %s%s)' % (
            s["native_name"], s["runtime"], s.get("native_name_checked_at") or "?",
            ", stale: a refresh did not finish" if s.get("native_name_stale") else
            ", cached" if s.get("native_name_cached") else ""))
    elif s["native_name_status"] == "pending":
        lines.append("name: native name lookup in progress on that machine; ask again in a few seconds")
    elif s["native_name_status"] == "unnamed":
        lines.append("name: the %s session has no native name set" % s["runtime"])
    elif s["native_name_status"] == "not_supported":
        lines.append("name: no read-only native name lookup for %s sessions" % s["runtime"])
    else:
        lines.append("name: native name unavailable (%s)" % (s.get("native_name_error") or "unknown"))
    if s.get("confirmed_title"):
        lines.append('confirmed title "%s" (confirmed by the user %s; may be outdated)'
                     % (s["confirmed_title"], s.get("confirmed_at") or "earlier"))
    if s.get("display_name"):
        lines.append('alias "%s" (configured label, not the native name)' % s["display_name"])
    lines.append("id %s (%s); busy/idle: unknown" % (s["id"], SOURCE_LABEL.get(s["id_source"], "unknown")))
    return lines


def discovery_rows(home, f, nodes, wanted, port, timeout, include_self):
    cfg = f.get("cfg")
    me = (f["tailscale"].get("node") or "")
    jobs = []
    selected = None
    if wanted is not None:
        selected = {}
        for host, peer_port in wanted:
            node = match_node(nodes, host)
            if node is not None:
                selected.setdefault(node["node"], peer_port)
    for n in nodes:
        if n["self"] and not include_self:
            continue
        if selected is not None:
            if n["node"] not in selected:
                continue
            p = selected[n["node"]]
        else:
            p = port
        jobs.append((n, p))

    def probe(job):
        n, p = job
        # The snapshot's Online flag may lag a working path. Keep it visible independently
        # and use the same bounded HTTP probe for every selected peer.
        host = n["ip"]
        if n["self"] and cfg is not None and cfg.get("bind"):
            host = cfg["bind"]
        return probe_info(host, p, timeout, expect_node=n["node"], host=n["node"])
    probes = run_parallel(probe, jobs)
    rows = []
    for (n, p), pr in zip(jobs, probes):
        row = {"machine": n["host"], "node": n["node"], "self": n["self"], "tailscale_online": n["online"],
               "address": "%s:%d" % (n["ip"], p), "receiver": "unknown" if pr is None else pr["kind"],
               "http_status": None if pr is None else pr["http_status"], "send_permission": "unknown",
               "version": None, "agents": None, "source": None, "detail": None, "warnings": []}
        if pr is None:
            row["detail"] = "receiver probe unavailable; reachability unknown"
        elif pr["kind"] == "info":
            row.update(version=pr["info"]["version"], agents=pr["info"]["agents"], source="/info",
                       send_permission="self" if n["self"] else "allowed", warnings=pr["info"]["warnings"])
            row["receiver"] = "ok"
        elif pr["kind"] == "legacy":
            row.update(detail="receiver runs an older Yobiru/tailmsg (0.2.x or earlier) without /info: version, "
                              "agents and sessions unknown", version="<=0.2.x")
        elif pr["kind"] == "blocked":
            row.update(send_permission="blocked", detail="the receiver answered 403: its access policy rejected "
                                                         "this machine (allowlist or deny rule; not a network failure)")
        elif pr["kind"] == "unreachable":
            row["detail"] = pr.get("detail") or "no receiver answered on port %d in time (%s): not running, slow, another port, " \
                            "ACL or shields-up; this does not mean the machine is offline or Yobiru is not " \
                            "installed" % (p, pr["error"])
        else:
            row["detail"] = "unexpected answer (%s); treated as unknown" % (pr["error"] or "?")
        if n["self"]:
            row["send_permission"] = "self"
            row["local_plugin_version"] = plugin_version()
            if pr is None or pr["kind"] != "info":
                running = {"legacy": "this machine's running receiver is older (0.2.x or earlier, no /info): "
                                     "`install-runtime --upgrade --apply`, then restart it",
                           "blocked": "this machine's receiver refused /info (403)"}.get(
                    pr["kind"] if pr else None, "this machine's receiver is not answering")
                if cfg is not None:
                    info = local_info(home, cfg, core().parse_config(cfg)[1], me)
                    row.update(agents=info["agents"], source="local files (not the running receiver)")
                    row["detail"] = running + "; agents and sessions below come from local files, the version " \
                                              "column is the running receiver's (installed plugin: %s)" % plugin_version()
                else:
                    row["detail"] = "this machine is not set up yet (no config.json)"
        rows.append(row)
    rows.sort(key=lambda r: (not r["self"], not r["tailscale_online"], r["node"]))
    return rows


def print_rows(rows):
    print("%-22s %-9s %-11s %-8s %-9s %s" % ("MACHINE", "TAILSCALE", "RECEIVER", "SEND", "VERSION", "AGENT"))
    for r in rows:
        machine = r["machine"] + (" (this)" if r["self"] else "")
        head = "%-22s %-9s %-11s %-8s %-9s " % (machine[:22], "online" if r["tailscale_online"] else "offline",
                                               r["receiver"], r["send_permission"], r["version"] or "-")
        if not r["agents"]:
            print(head + ("(no agents)" if r["agents"] == [] else "-"))
        for i, a in enumerate(r["agents"] or []):
            print((head if i == 0 else " " * len(head)) + "@%s%s" % (a["agent"], " (default)" if a["default"] else ""))
            for line in session_lines(a["session"]):
                print("      " + line)
        if r["detail"]:
            print("      note: " + r["detail"])
        for w in r["warnings"]:
            print("      warning: " + w)


def cmd_agents(home, home_source, argv):
    ap = argparse.ArgumentParser(allow_abbrev=False, prog=PRODUCT + " agents",   # also `peers`
                                 description="Which machines are online and which agent session handles messages on "
                                             "each (read-only). Peers are asked over their authenticated /info "
                                             "endpoint; nothing is written unless --remember is given.")
    ap.add_argument("--online", action="store_true", help="only machines Tailscale reports online")
    ap.add_argument("--no-self", action="store_true", help="leave out this machine")
    ap.add_argument("--peer", action="append", default=[], metavar="HOST[:PORT]",
                    help="only these machines (repeatable); a port for receivers not on the default port")
    ap.add_argument("--timeout", type=float, default=5.0,
                    help="seconds per receiver (default 5; receivers answer a cold session name lookup within "
                         "about 2 s and report it as pending if it takes longer)")
    ap.add_argument("--remember", action="store_true",
                    help="record answering installations for later upgrade notices (upgrade-notices.json)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    f = gather(home, home_source, probe=False)
    cfg = f.get("cfg")
    port = f.get("port") or 7766
    cmd = (cfg or {}).get("tailscale_cmd") or ["tailscale"]
    nodes, err = tailnet_nodes(cmd)
    if err:
        msg = "cannot read the tailnet: %s; run `%s onboard` for next steps" % (err, PRODUCT)
        print(json.dumps({"ok": False, "error": msg}) if a.json else "%s: %s" % (PRODUCT, msg))
        return 1
    me = next((n["node"] for n in nodes if n["self"]), None)
    f["tailscale"]["node"] = me
    wanted = [parse_peer_spec(p, port)[:2] for p in a.peer] or None
    rows = discovery_rows(home, f, nodes, wanted, port, max(0.2, min(a.timeout, 30.0)), not a.no_self)
    if a.online:
        rows = [r for r in rows if r["tailscale_online"]]
    remembered = None
    if a.remember:
        remembered = remember_installations(home, rows)
    if a.json:
        print(json.dumps({"ok": True, "this_machine": me, "machines": rows, "remembered": remembered,
                          "notes": ["session names carry their source; activity (busy/idle) is never claimed"]},
                         indent=2, ensure_ascii=True))
        return 0
    print("%s agents (read-only; names are labeled by source, busy/idle is not known)" % DISPLAY)
    print_rows(rows)
    if remembered is not None:
        print("remembered %d installation(s) in %s" % (remembered, home / NOTICES_FILE))
    return 0


# ---------------------------------------------------------------- session: explicit agent -> session binding
def load_sessions_raw(home):
    path = home / SESSIONS_FILE
    if not path.is_file():
        return {"v": 1, "agents": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        die("%s is not valid JSON (%s); fix it by hand, %s will not overwrite it" % (path, e, PRODUCT))
    if not isinstance(raw, dict) or not isinstance(raw.get("agents", {}), dict):
        die("%s must be an object with an agents object; fix it by hand" % path)
    raw.setdefault("agents", {})
    return raw


def label_text(value, what):
    if value is None:
        return None
    v = unicodedata.normalize("NFC", value)
    if not v or len(v) > 80 or clean(v, 80) != v:
        die("bad %s: 1-80 printable characters, no control characters or surrounding spaces" % what, 2)
    return v


def cmd_session(home, home_source, argv):
    ap = argparse.ArgumentParser(allow_abbrev=False, prog=PRODUCT + " session",
                                 description="Explicit agent -> session bindings shown by `agents` on every machine. "
                                             "A name never selects or renames a session: bind needs the exact "
                                             "session id (for Codex: the session UUID).")
    sub = ap.add_subparsers(dest="action")
    sub.required = True
    pl = sub.add_parser("list", help="this machine's bindings with their native names (read-only)")
    pl.add_argument("--no-lookup", action="store_true", help="do not read native names")
    pl.add_argument("--json", action="store_true")
    pb = sub.add_parser("bind", help="bind an agent to one existing session (dry run unless --apply)")
    pb.add_argument("--agent", required=True)
    pb.add_argument("--session-id", help="exact id of the existing session (Codex: its UUID); with an installed "
                                         "Codex bridge, agent codex always uses the bridge's dispatcher")
    pb.add_argument("--runtime", help="codex, grok, claude or another lowercase label (default: codex for agent "
                                      "codex, else other)")
    pb.add_argument("--display-name", help="local alias shown next to the native name (not a rename)")
    pb.add_argument("--confirmed-title", help="the session title exactly as the user confirmed it, when the runtime "
                                              "offers no read-only lookup")
    pb.add_argument("--replace", action="store_true", help="allow replacing an existing different session id")
    pb.add_argument("--apply", action="store_true")
    pb.add_argument("--json", action="store_true")
    pu = sub.add_parser("unbind", help="remove an agent's binding (dry run unless --apply)")
    pu.add_argument("--agent", required=True)
    pu.add_argument("--apply", action="store_true")
    pu.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    raw_cfg, err = read_config(home)
    if raw_cfg is None:
        die(err or "not set up: no %s; run `%s onboard`" % (home / "config.json", PRODUCT), 2)
    try:
        cfg, agents = core().parse_config(raw_cfg)
    except RuntimeError as e:
        die(str(e), 2)
    names = core().info_agent_names(cfg, agents)
    if a.action == "list":
        bound, errors = core().session_bindings(home, names)
        out = []
        for n in names:
            b = bound.get(n)
            native = {"status": "not_checked"}
            if b and not a.no_lookup:
                native = dict(core().lookup_native_name(home, b), checked_at=now_iso())
            out.append({"agent": n, "default": n == cfg.get("default_agent"),
                        "session": validate_session_view(core().session_view(b, native)) if b else None})
        if a.json:
            print(json.dumps({"agents": out, "problems": errors}, indent=2, ensure_ascii=True))
            return 0
        print("%s sessions on this machine (%s)" % (DISPLAY, home))
        for x in out:
            print("@%s%s" % (x["agent"], " (default)" if x["default"] else ""))
            for line in session_lines(x["session"]):
                print("    " + line)
        for e in errors:
            print("problem: %s" % e)
        if not out:
            print("no agent names configured; add one with `%s setup --agent NAME`" % PRODUCT)
        return 0
    name = valid_agent(a.agent)
    if name not in names:
        die("agent %r is not configured on this machine; add it first: `%s setup --agent %s`" % (name, PRODUCT, name), 2)
    raw = load_sessions_raw(home)
    same = [k for k in raw["agents"] if unicodedata.normalize("NFC", k) == name]
    key = same[0] if same else name
    current = raw["agents"].get(key) if isinstance(raw["agents"].get(key), dict) else None
    changes, errors, notes = [], [], []
    bridge = core().bridge_session(home) if name == "codex" else None
    if a.action == "unbind":
        if current is None:
            notes.append("@%s has no configured binding" % name)
        else:
            del raw["agents"][key]
            changes.append("remove binding of @%s (session %s)" % (name, current.get("session_id")))
        if bridge:
            notes.append("the installed Codex bridge still delivers @codex messages to %s and `agents` keeps "
                         "reporting it; use `codex-bridge rollback` to remove the bridge" % bridge)
    else:
        entry = dict(current or {})
        runtime = a.runtime or entry.get("runtime") or ("codex" if name == "codex" else "other")
        if not core().RUNTIME_RE.match(runtime):
            die("bad --runtime %r (lowercase letters, digits, '-')" % runtime, 2)
        sid = a.session_id
        if sid is not None and runtime == "codex":
            try:
                sid = str(uuid.UUID(sid))
            except ValueError:
                die("a Codex session id is the session's UUID", 2)
        if sid is not None and runtime != "codex" and not core().SESSION_ID_RE.match(sid):
            die("bad --session-id (1-128 letters, digits and ._:@+=-)", 2)
        if bridge:
            if runtime != "codex":
                errors.append("agent codex is served by the installed Codex bridge; its runtime is codex")
            if sid is not None and sid != bridge:
                errors.append("session %s conflicts with the installed Codex bridge dispatcher %s: the bridge "
                              "delivers @codex messages to that session. A binding cannot redirect it; change "
                              "the bridge only through `codex-bridge` after draining its queue" % (sid, bridge))
            sid = bridge
        if sid is None:
            sid = entry.get("session_id")
        if sid is None:
            die("--session-id is required: name the exact existing session (a name alone never selects one)", 2)
        old = entry.get("session_id")
        if old is not None and old != sid and not a.replace:
            errors.append("@%s is already bound to session %s; pass --replace to bind %s instead" % (name, old, sid))
        if old != sid:
            changes.append("@%s session: %s -> %s (%s)" % (name, old, sid, runtime))
        if entry.get("runtime") != runtime:
            if entry.get("runtime"):
                changes.append("@%s runtime: %s -> %s" % (name, entry.get("runtime"), runtime))
        entry.update(runtime=runtime, session_id=sid)
        dn = label_text(a.display_name, "--display-name")
        if dn is not None and entry.get("display_name") != dn:
            changes.append('@%s alias: %s -> "%s" (local label; the session itself is not renamed)'
                           % (name, json.dumps(entry.get("display_name"), ensure_ascii=False), dn))
            entry["display_name"] = dn
        ct = label_text(a.confirmed_title, "--confirmed-title")
        if ct is not None and entry.get("confirmed_title") != ct:
            changes.append('@%s confirmed title: "%s" (as confirmed by the user)' % (name, ct))
            entry.update(confirmed_title=ct, confirmed_at=now_iso())
        if old is not None and old != sid and entry.get("confirmed_title") and ct is None:
            entry.pop("confirmed_title", None)
            entry.pop("confirmed_at", None)
            notes.append("the confirmed title belonged to the old session and was dropped")
        if current is None:
            entry.setdefault("bound_at", now_iso())
        _, err = core().validate_binding(name, entry)
        if err:
            errors.append(err)
        if changes and not errors:
            raw["agents"][key] = entry
        if runtime == "codex" and not bridge:
            notes.append("native names are read through an installed Codex bridge; without one `agents` shows "
                         "the id, the alias and any confirmed title")
    written, backup = False, None
    if a.apply and changes and not errors:
        if not home.is_dir():
            die("no data home at %s" % home, 2)
        backup = write_json_atomic(home / SESSIONS_FILE, raw, backup=True)
        written = True
    res = {"command": "session " + a.action, "file": str(home / SESSIONS_FILE), "dry_run": not a.apply,
           "written": written, "backup": backup, "changes": changes, "errors": errors, "notes": notes}
    if a.json:
        print(json.dumps(res, indent=2, ensure_ascii=True))
    else:
        head = "applied" if written else "refused, nothing written" if errors else \
            "dry run, nothing written" if changes else "nothing to change"
        print("%s session %s (%s)" % (PRODUCT, a.action, head))
        for k, items in (("change", changes), ("error", errors), ("note", notes)):
            for x in items:
                print("  %-7s %s" % (k + ":", x))
        if backup:
            print("  backup: %s" % backup)
        if changes and not a.apply and not errors:
            print("  review the changes above, then re-run the same command with --apply")
    return 1 if errors else 0


# ---------------------------------------------------------------- update check
class UpdateError(Exception):
    def __init__(self, code):
        Exception.__init__(self, code)
        self.code = code


def parse_releases(data):
    """GitHub release list -> [{version, tag, prerelease, published_at, url}]. Only metadata fields are
    read; release notes are never printed or executed and URLs are rebuilt for the official repository."""
    if not isinstance(data, list):
        raise UpdateError("metadata_invalid")
    out = []
    for item in data[:100]:
        if not isinstance(item, dict) or item.get("draft") is True:
            continue
        tag = item.get("tag_name")
        v = parse_semver(tag)
        if v is None or clean(tag, 64) != tag:
            continue
        published = item.get("published_at")
        out.append({"version": fmt_semver(v), "tag": tag, "prerelease": item.get("prerelease") is True or bool(v[3]),
                    "published_at": published if isinstance(published, str) and ISO_RE.match(published) else None,
                    "url": RELEASE_URL_PREFIX + "tag/" + urllib.parse.quote(tag, safe="")})
    return out


def fetch_releases(timeout=10.0):
    """The official repository's release list (bounded size and time). Raises UpdateError(code)."""
    import ssl
    try:
        c = http.client.HTTPSConnection(RELEASES_HOST, 443, timeout=timeout, context=ssl.create_default_context())
        with Deadline(c, timeout) as d:
            try:
                d.connect()
                c.request("GET", RELEASES_PATH, headers={"Accept": "application/vnd.github+json",
                                                         "User-Agent": "%s/%s" % (PRODUCT, plugin_version()),
                                                         "X-GitHub-Api-Version": "2022-11-28"})
                r = c.getresponse()
                body = r.read(MAX_RELEASES_BYTES + 1)
                status = r.status
            except (OSError, http.client.HTTPException):
                d.check()
                raise
            d.check()
    except socket.timeout:
        raise UpdateError("timeout")
    except ssl.SSLError:
        raise UpdateError("tls_error")
    except (OSError, http.client.HTTPException):
        raise UpdateError("network_unavailable")
    if len(body) > MAX_RELEASES_BYTES:
        raise UpdateError("metadata_too_large")
    if status in (403, 429):
        raise UpdateError("rate_limited")
    if status == 404:
        raise UpdateError("repository_not_found")
    if status != 200:
        raise UpdateError("http_%d" % status)
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise UpdateError("metadata_invalid")
    return parse_releases(data)


def choose_release(releases, channel):
    pool = [r for r in releases if channel == "preview" or not r["prerelease"]]
    return max(pool, key=lambda r: semver_key(parse_semver(r["version"])), default=None)


def assess_update(installed, releases, channel):
    """Pure decision used by `update check`. status: update_available | current | ahead | no_release |
    no_stable_release."""
    res = {"channel": channel, "latest": choose_release(releases, channel),
           "latest_preview": choose_release(releases, "preview")}
    if not releases:
        res["status"] = "no_release"
    elif res["latest"] is None:
        res["status"] = "no_stable_release"
    else:
        c = compare_versions(installed, res["latest"]["version"])
        res["status"] = "unknown_installed" if c is None else {-1: "update_available", 0: "current", 1: "ahead"}[c]
    return res


def local_versions(home, f):
    rt, how = runtime_version(home)
    out = {"plugin": f["plugin_version"], "runtime": rt,
           "runtime_note": {"absent": "no durable runtime (install-runtime)", "unversioned":
                            "0.2.x or older (no version recorded)", "unreadable": "unreadable", "file": None}[how],
           "receiver": None, "receiver_note": None}
    r = f.get("receiver_info")
    if r is None:
        out["receiver_note"] = "not checked"
    elif r["kind"] == "info":
        out["receiver"] = r["info"]["version"]
    elif r["kind"] == "legacy":
        out["receiver_note"] = "running receiver is 0.2.x or older (no /info): restart it after install-runtime"
    else:
        out["receiver_note"] = "receiver not answering (%s)" % (r.get("error") or r["kind"])
    return out


def is_prerelease(version):
    v = parse_semver(version)
    return bool(v and v[3])


def runtime_step(plugin, runtime):
    """What to do when the durable runtime differs from this plugin; never advises a downgrade."""
    if compare_versions(runtime, plugin) == 1:
        return ("keep the durable runtime: it is newer than this plugin (a development build or a later release), "
                "and `%s install-runtime` refuses to replace it with an older one unless --allow-downgrade" % PRODUCT)
    return "`%s install-runtime --upgrade --apply`, then restart the receiver" % PRODUCT


def upgrade_steps(f, target=None, release=None):
    if is_prerelease(target):
        # Plugin pages and the repository marketplace follow the stable default branch: a prerelease is installed
        # only from its own tag, and only by an owner who chose it.
        tag = (release or {}).get("tag") or "v" + target
        steps = ["%s is a prerelease: plugin updates (Codex `codex plugin marketplace upgrade`, Cursor and Grok Bot "
                 "plugin pages) follow the stable default branch and never install it. Install exactly %s only "
                 "if the owner chose this prerelease" % (target, tag),
                 "from %s download `%s-%s-plugin.zip` and `SHA256SUMS`, compare the archive's SHA-256 with its "
                 "line in SHA256SUMS, extract it into a new folder (never the data home) and check that `sh "
                 "<folder>/%s/scripts/%s version` prints %s. Codex: `codex plugin marketplace remove %s` if one "
                 "is configured, then `codex plugin marketplace add %s --ref %s` and `codex plugin add %s@%s`. "
                 "Then start a new conversation"
                 % ((release or {}).get("url") or RELEASE_URL_PREFIX + "tag/" + urllib.parse.quote(tag, safe=""),
                    PRODUCT, target, PRODUCT, PRODUCT, target, PRODUCT, OFFICIAL_REPO, tag, PRODUCT, PRODUCT)]
    else:
        steps = ["update the plugin from the official repository (Codex: `codex plugin marketplace upgrade yobiru` "
                 "then `codex plugin add yobiru@yobiru`; Cursor/Grok Bot: update it from the plugin page), then "
                 "start a new conversation"]
    steps += ["`%s install-runtime --upgrade` (review), then `--apply`: a plugin update never changes the durable "
              "receiver runtime" % PRODUCT,
              "restart the receiver service so the running receiver loads the new runtime",
              "`%s doctor` and `%s update status`: plugin, runtime and running receiver should all show %s"
              % (PRODUCT, PRODUCT, target or "the new version"),
              "`%s update notify%s` (dry run), then `--apply`, to tell your other installations"
              % (PRODUCT, " --channel preview" if is_prerelease(target) else "")]
    bridge_at = len(steps) - 2
    if f.get("codex_bridge_installed"):
        steps.insert(bridge_at, "if the Codex bridge is installed: `%s codex-bridge install --thread-id <the same "
                                "UUID> ... --dry-run`, then without --dry-run (keeps the session, names and "
                                "dispatcher instructions)" % PRODUCT)
    return steps


def gather_versions(home, home_source, timeout=3.0):
    f = gather(home, home_source, probe=True)
    if f.get("cfg") is not None and f.get("receiver", {}).get("reachable"):
        host = f["cfg"].get("bind") or next((i for i in f["tailscale"].get("ips") or [] if ":" not in i), None)
        f["receiver_info"] = probe_info(host, f["port"], timeout, host=f["tailscale"].get("node")) if host else None
    elif f.get("cfg") is not None:
        f["receiver_info"] = {"kind": "unreachable", "error": (f.get("receiver") or {}).get("error")}
    return f


def cmd_update(home, home_source, argv):
    ap = argparse.ArgumentParser(allow_abbrev=False, prog=PRODUCT + " update",
                                 description="Update reminders and coordinated upgrade notices. Releases come only "
                                             "from %s; nothing is downloaded or installed here." % OFFICIAL_REPO_URL)
    sub = ap.add_subparsers(dest="action")
    sub.required = True
    pc = sub.add_parser("check", help="compare installed versions with the latest official release (network)")
    pc.add_argument("--channel", choices=("stable", "preview"), default="stable",
                    help="stable ignores prereleases; preview includes them (default stable)")
    pc.add_argument("--timeout", type=float, default=10.0)
    pc.add_argument("--record", action="store_true", help="save the result for doctor/status reminders")
    pc.add_argument("--json", action="store_true")
    ps = sub.add_parser("status", help="installed, durable runtime and running receiver versions, the last "
                                       "recorded check and upgrade notices (no network except this machine)")
    ps.add_argument("--json", action="store_true")
    pn = sub.add_parser("notify", help="send a versioned upgrade notice to your known installations "
                                       "(dry run unless --apply)")
    pn.add_argument("--version", help="the version this machine upgraded to (default: installed plugin version)")
    pn.add_argument("--peer", action="append", default=[], metavar="HOST[:PORT][/AGENT]",
                    help="also notify this installation (repeatable)")
    pn.add_argument("--no-discover", action="store_true",
                    help="only explicit peers and remembered installations; do not probe other online machines")
    pn.add_argument("--channel", choices=("stable", "preview"), default="stable",
                    help="the release channel of --version; a prerelease needs preview")
    pn.add_argument("--reconcile-uncertain", action="store_true",
                    help="re-send uncertain notices with the identical message id (receivers deduplicate it)")
    pn.add_argument("--retry-blocked", action="store_true",
                    help="ask installations that refused earlier (403) again; without it they are retried only "
                         "when their /info shows that this machine is allowed again")
    pn.add_argument("--timeout", type=float, default=3.0, help="seconds per discovery probe (default 3)")
    pn.add_argument("--apply", action="store_true")
    pn.add_argument("--json", action="store_true")
    pm = sub.add_parser("maintain", help="check for updates and retry previously authorized upgrade notices "
                                         "(dry run unless --apply; no new recipients)")
    pm.add_argument("--channel", choices=("stable", "preview"), default="stable")
    pm.add_argument("--timeout", type=float, default=3.0)
    pm.add_argument("--apply", action="store_true")
    pm.add_argument("--json", action="store_true")
    pr = sub.add_parser("reminder-template", help="print an opt-in periodic `update check --record` definition "
                                                   "(never installs it)")
    pr.add_argument("kind", choices=("systemd-user", "launchd", "cron"))
    pr.add_argument("--channel", choices=("stable", "preview"), default="stable")
    pr.add_argument("--retry-notices", action="store_true", help="every 15 minutes and after startup: record "
                    "update checks and retry already queued notices; never install software or change policy")
    a = ap.parse_args(argv)
    if a.action == "check":
        return update_check(home, home_source, a)
    if a.action == "status":
        return update_status(home, home_source, a)
    if a.action == "notify":
        return update_notify(home, home_source, a)
    if a.action == "maintain":
        return update_maintain(home, home_source, a)
    return reminder_template(home, a)


def update_check(home, home_source, a):
    f = gather_versions(home, home_source)
    versions = local_versions(home, f)
    res = {"source": OFFICIAL_REPO_URL, "checked_at": now_iso(), "installed": versions}
    try:
        releases = fetch_releases(max(1.0, min(a.timeout, 30.0)))
        res.update(assess_update(versions["plugin"], releases, a.channel))
    except UpdateError as e:
        res.update(channel=a.channel, status="error", error=e.code, latest=None, latest_preview=None)
    nxt = []
    if res["status"] == "update_available":
        nxt = upgrade_steps(f, res["latest"]["version"], res["latest"])
    elif res["status"] == "no_stable_release" and res.get("latest_preview"):
        nxt = ["no stable release yet; the latest preview is %s: `%s update check --channel preview`"
               % (res["latest_preview"]["version"], PRODUCT)]
    elif res["status"] == "ahead" and a.channel == "stable" and is_prerelease(versions["plugin"]):
        nxt = ["this machine runs the prerelease %s and the stable channel lists stable releases only: "
               "`%s update check --channel preview` also shows newer prereleases" % (versions["plugin"], PRODUCT)]
    elif res["status"] == "error":
        nxt = ["could not reach the official release list (%s); nothing changed, try again later" % res["error"]]
    if versions["runtime"] and versions["runtime"] != versions["plugin"] or versions["runtime_note"] and \
            "older" in versions["runtime_note"]:
        nxt.append("the durable runtime is %s but the plugin is %s: %s"
                   % (versions["runtime"] or "older", versions["plugin"],
                      runtime_step(versions["plugin"], versions["runtime"])))
    elif versions["receiver"] and versions["receiver"] != versions["runtime"]:
        nxt.append("the running receiver is %s: restart it to load %s" % (versions["receiver"], versions["runtime"]))
    elif versions["receiver_note"] and "older" in versions["receiver_note"]:
        nxt.append(versions["receiver_note"])
    res["next"] = nxt
    if a.record:
        if not home.is_dir():
            res["record"] = "not recorded: no data home yet"
        else:
            write_json_atomic(home / UPDATE_FILE, {k: res.get(k) for k in (
                "checked_at", "channel", "status", "error", "latest", "latest_preview", "installed")})
            res["record"] = str(home / UPDATE_FILE)
    if a.json:
        print(json.dumps(res, indent=2, ensure_ascii=True))
    else:
        print("%s update check (%s channel, source %s)" % (DISPLAY, a.channel, OFFICIAL_REPO_URL))
        print("  installed plugin   %s" % versions["plugin"])
        print("  durable runtime    %s" % (versions["runtime"] or versions["runtime_note"]))
        print("  running receiver   %s" % (versions["receiver"] or versions["receiver_note"]))
        latest = res.get("latest")
        print("  latest release     %s" % ("%s%s (%s)" % (latest["version"], " prerelease" if latest["prerelease"]
                                                         else "", latest["url"]) if latest else "-"))
        print("  result             %s%s" % (res["status"], " (%s)" % res["error"] if res.get("error") else ""))
        for x in nxt:
            print("  next: %s" % x)
        if res.get("record"):
            print("  recorded: %s" % res["record"])
    return 0 if res["status"] != "error" else 1


def recorded_update(home):
    raw, _ = core().read_small_json(home / UPDATE_FILE)
    if not isinstance(raw, dict) or raw.get("status") not in ("update_available", "current", "ahead", "no_release",
                                                               "no_stable_release", "error"):
        return None
    latest = raw.get("latest") if isinstance(raw.get("latest"), dict) else None
    version = latest.get("version") if latest and parse_semver(latest.get("version")) else None
    status = raw["status"]
    if status == "update_available" and version and compare_versions(plugin_version(), version) != -1:
        status = "current"          # updated since the check
    return {"checked_at": raw.get("checked_at") if isinstance(raw.get("checked_at"), str) and
            ISO_RE.match(raw["checked_at"]) else None, "status": status, "latest": version,
            "channel": raw.get("channel") if raw.get("channel") in ("stable", "preview") else None}


def update_status(home, home_source, a):
    f = gather_versions(home, home_source)
    versions = local_versions(home, f)
    state = load_notice_state(home, strict=False)
    counts, entries = {}, []
    for key, raw in state["notices"].items():
        e = dict(raw) if isinstance(raw, dict) else {}
        e["key"] = clean(e.get("key") or key, 250)
        if e.get("status") not in ("pending", "dispatching", "uncertain", "notified", "verified", "blocked", "rejected"):
            e["status"] = "invalid"
        counts[e["status"]] = counts.get(e["status"], 0) + 1
        entries.append(e)
    res = {"installed": versions, "last_check": recorded_update(home), "notices": counts,
           "installations_remembered": len(state["installations"])}
    if a.json:
        res["notice_entries"] = sorted(entries, key=lambda e: e.get("key") or "")
        print(json.dumps(res, indent=2, ensure_ascii=True))
        return 0
    print("%s update status" % DISPLAY)
    print("  installed plugin   %s" % versions["plugin"])
    print("  durable runtime    %s" % (versions["runtime"] or versions["runtime_note"]))
    print("  running receiver   %s" % (versions["receiver"] or versions["receiver_note"]))
    lc = res["last_check"]
    print("  last check         %s" % ("%s, %s (latest %s, %s channel)" % (lc["checked_at"], lc["status"],
                                                                          lc["latest"], lc["channel"])
                                       if lc else "never recorded (`%s update check`)" % PRODUCT))
    print("  upgrade notices    %s" % (", ".join("%s %d" % kv for kv in sorted(counts.items())) or "none"))
    for e in sorted(entries, key=lambda e: e.get("key") or ""):
        print("    %-48s %-11s %s" % (e.get("key", "?")[:48], e.get("status"), e.get("last_error") or ""))
    return 0


def reminder_template(home, a):
    python = service_python()
    retry = getattr(a, "retry_notices", False)
    args = [python, str(Path(__file__).resolve()), "--home", str(home), "update",
            "maintain" if retry else "check", "--apply" if retry else "--record",
            "--channel", a.channel]
    warn = "# The plugin path below changes when the plugin is updated: print this again after each update."
    if a.kind == "systemd-user":
        print("# Opt-in Yobiru update maintenance: existing queued notices retry every 15 minutes." if retry else
              "# Opt-in weekly Yobiru update reminder: records update checks for doctor/status.")
        print("# No software is installed and no access policy is changed. Review, then save as")
        print("#   ~/.config/systemd/user/yobiru-update-check.service and .timer, and run:")
        print("#   systemctl --user daemon-reload && systemctl --user enable --now yobiru-update-check.timer")
        print(warn)
        print("# ---- yobiru-update-check.service")
        print("[Unit]\nDescription=Yobiru update maintenance\n\n[Service]\nType=oneshot")
        print("ExecStart=%s" % " ".join(systemd_quote(x) for x in args))
        print("# ---- yobiru-update-check.timer")
        schedule = "OnCalendar=*:0/15\nOnBootSec=2min" if retry else "OnCalendar=weekly"
        print("[Unit]\nDescription=Yobiru update timer\n\n[Timer]\n%s\nPersistent=true\n\n"
              "[Install]\nWantedBy=timers.target" % schedule)
    elif a.kind == "launchd":
        print("Review, save stdout as ~/Library/LaunchAgents/dev.yobiru.update-check.plist, then: launchctl load -w "
              "<that file>. %s %s" % ("Retries existing notices and records update checks." if retry else
                                       "It only records update checks.", warn.lstrip("# ")), file=sys.stderr)
        sys.stdout.write(plistlib.dumps({"Label": "dev.yobiru.update-check", "ProgramArguments": args,
                                         "StartInterval": 900 if retry else 7 * 24 * 3600,
                                         "RunAtLoad": retry}).decode("utf-8"))
    else:
        quote = lambda s: "'" + s.replace("'", "'\\''") + "'"
        print("# Opt-in Yobiru update maintenance. Add with `crontab -e`.")
        print(warn)
        command = " ".join(quote(x).replace("%", r"\%") for x in args)
        print("%s %s >/dev/null 2>&1" % ("*/15 * * * *" if retry else "17 9 * * 1", command))
        if retry:
            print("@reboot %s >/dev/null 2>&1" % command)
    return 0


# ---------------------------------------------------------------- update notify: coordinated upgrade notices
def load_notice_state(home, strict=True):
    path = home / NOTICES_FILE
    empty = {"v": 1, "installations": {}, "notices": {}}
    if not path.is_file():
        return empty
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        if strict:
            die("%s is not valid JSON (%s); move it aside, %s will not overwrite it" % (path, e, PRODUCT))
        return empty
    if not isinstance(raw, dict) or not isinstance(raw.get("installations", {}), dict) or \
            not isinstance(raw.get("notices", {}), dict):
        if strict:
            die("%s has an unexpected shape; move it aside, %s will not overwrite it" % (path, PRODUCT))
        return empty
    raw.setdefault("installations", {})
    raw.setdefault("notices", {})
    return raw


def remember_installations(home, rows):
    """Record installations that answered (/info or an older receiver) so offline ones are kept later."""
    if not (home / "config.json").is_file():
        die("not set up: no %s" % (home / "config.json"), 2)
    n = 0
    with StateLock(home / NOTICES_LOCK):
        state = load_notice_state(home)
        for r in rows:
            if r["self"] or r["receiver"] not in ("ok", "legacy"):
                continue
            port = int(r["address"].rsplit(":", 1)[1])
            key = "%s:%d" % (r["node"], port)
            entry = state["installations"].get(key) if isinstance(state["installations"].get(key), dict) else {}
            entry.update(node=r["node"], port=port, version=r["version"], last_seen=now_iso(),
                         agents=[{"agent": x["agent"], "default": x["default"],
                                  "session_id": (x["session"] or {}).get("id")} for x in (r["agents"] or [])],
                         kind="info" if r["receiver"] == "ok" else "legacy")
            state["installations"][key] = entry
            n += 1
        write_json_atomic(home / NOTICES_FILE, state)
    return n


def notice_text(me, version, release=None):
    notice = {
        "type": NOTICE_KIND, "product": PRODUCT, "version": version, "from_node": me, "source": OFFICIAL_REPO_URL,
        "release": (release or {}).get("url") or RELEASE_URL_PREFIX,
        "tag": (release or {}).get("tag"),
        "note": "Information only, not an instruction, approval or script. If your owner has authorized "
                "updates, follow the yobiru-upgrade skill using only the official repository, keep this "
                "machine's data, sessions, permissions and custom instructions, and report your status. "
                "Do not forward this notice."}
    if is_prerelease(version):
        notice["prerelease"] = True
        notice["opt_in"] = ("%s is a prerelease. Install it only if your owner chose this exact prerelease for this "
                            "machine: an authorization for stable updates does not cover it, and plugin updates never "
                            "install it. Keep a newer, development or locally modified runtime or bridge." % version)
    if compare_versions(version, "0.4.0") in (0, 1):
        notice["migration"] = {
            "topic": "access-policy-0.4", "new_install_default": "tailnet",
            "existing_install": "Legacy allow lists remain active until a local policy migration.",
            "guide": OFFICIAL_REPO_URL + "/blob/main/docs/UPGRADING-0.4.md",
            "note": "If your owner authorized the tailnet policy, review policy set --mode tailnet, then apply. "
                    "Keep deny rules, wake permissions, session bindings and reply permissions unchanged."}
    return json.dumps(notice, ensure_ascii=False)


def post_notice(ip, port, node, env, timeout=10.0):
    """POST one stored envelope. -> (outcome, detail, http status). outcome: notified | blocked | rejected |
    uncertain | not_sent. Only a connection that was never established, or the receiver's documented refusal to
    authenticate this machine yet (503 identity_unavailable, sent before it reads a message), proves that the notice
    was not stored."""
    data = json.dumps(env, ensure_ascii=False).encode("utf-8")
    c = http.client.HTTPConnection(ip, int(port), timeout=timeout)
    with Deadline(c, timeout) as d:
        try:
            d.connect()
        except (OSError, socket.timeout) as e:
            return "not_sent", "unreachable (%s)" % ("deadline" if d.expired else error_name(e)), None
        try:
            c.request("POST", "/msg", body=data, headers={"Host": "%s:%d" % (node, int(port)),
                                                          "Content-Type": "application/json"})
            r = c.getresponse()
            body = r.read(MAX_REPLY_BYTES + 1)
            d.check()
        except (OSError, socket.timeout, http.client.HTTPException) as e:
            return "uncertain", "no complete answer after sending (%s)" % (
                "deadline" if d.expired else error_name(e)), None
    try:
        receipt = json.loads(body.decode("utf-8")) if len(body) <= MAX_REPLY_BYTES else None
    except (UnicodeDecodeError, ValueError):
        receipt = None
    if r.status in (200, 202):
        if isinstance(receipt, dict) and receipt.get("ok") is True and receipt.get("id") == env["id"]:
            return "notified", "HTTP %d%s" % (r.status, " (already stored earlier)" if r.status == 200 else ""), r.status
        return "uncertain", "HTTP %d without a valid receipt for this message id" % r.status, r.status
    if r.status == 403:
        return "blocked", "HTTP 403: the receiver's access policy refuses this machine", r.status
    if r.status == 503 and isinstance(receipt, dict) and receipt.get("ok") is False and \
            receipt.get("code") == "identity_unavailable":
        # The receiver could not look up this machine's Tailscale identity yet and stored nothing: the same stored
        # envelope is sent again on a later run. Any other 503 stays uncertain.
        return "not_sent", "HTTP 503: the receiver could not verify this machine's identity yet (retried later)", \
            r.status
    if r.status in (400, 404, 411, 413, 415):
        err = clean((receipt or {}).get("error") if isinstance(receipt, dict) else None, 120)
        return "rejected", "HTTP %d%s" % (r.status, ": " + err if err else ""), r.status
    return "uncertain", "HTTP %d" % r.status, r.status


def verify_local(home, f, target):
    v = local_versions(home, f)
    problems = []
    if v["plugin"] != target:
        problems.append("installed plugin is %s, not %s" % (v["plugin"], target))
    if v["runtime"] != target:
        problems.append("durable runtime is %s, not %s (`%s install-runtime --upgrade --apply`)"
                        % (v["runtime"] or v["runtime_note"], target, PRODUCT))
    if v["receiver"] != target:
        problems.append("running receiver is %s, not %s (restart the receiver service)"
                        % (v["receiver"] or v["receiver_note"], target))
    return {"ok": not problems, "versions": v, "problems": problems}


def notice_key(node, port, agent, version):
    return "%s:%d/%s@%s" % (node, port, agent or "", version)


def official_release(target, channel, timeout=10.0, fetch=None):
    """-> (release or None, problem or None). The target must be a published, non-draft release of the official
    repository; a prerelease needs the preview channel. Unreachable metadata is a refusal, never a pass."""
    try:
        releases = (fetch or fetch_releases)(timeout)
    except UpdateError as e:
        return None, "could not verify the release on %s (%s)" % (OFFICIAL_REPO_URL, e.code)
    rel = next((r for r in releases if r["version"] == target), None)
    if rel is None:
        return None, "%s is not a published release of %s" % (target, OFFICIAL_REPO_URL)
    if rel["prerelease"] and channel != "preview":
        return None, "%s is a prerelease: pass --channel preview to announce it" % target
    return rel, None


def decide_notice(status, kind, version, target, reconcile, retry_blocked=False):
    """Action for one (installation, agent, version) from its persisted status and what discovery saw now.
    uncertain/dispatching leave only by explicit reconciliation (same message id) or by an authenticated
    /info showing the target version; nothing else may downgrade them."""
    if status not in (None, "pending", "dispatching", "uncertain", "notified", "verified", "blocked", "rejected"):
        return "hold", "unrecognized stored notice state; repair it locally before retrying"
    at_target = version is not None and compare_versions(version, target) in (0, 1)
    if status in ("notified", "verified"):
        return ("verify", "its receiver now runs %s" % version) if status == "notified" and at_target \
            else ("skip", "already %s" % status)
    if at_target:
        return "verify", "its receiver already runs %s (authenticated /info)" % version
    if status in ("uncertain", "dispatching"):
        return ("resend_same_id", "reconcile with the identical message id") if reconcile else \
            ("hold", "outcome uncertain: not resent (see --reconcile-uncertain)")
    if status == "rejected":
        return "hold", "rejected earlier; not resent"
    if status == "blocked" and kind != "info" and not retry_blocked:
        # Only a fresh authenticated /info (it uses the same access policy as /msg) shows that permission came
        # back; older receivers cannot show it, so they are not asked again unless --retry-blocked.
        return "hold", "refused earlier (403); not resent until /info shows access again or --retry-blocked"
    if kind == "blocked":
        return "blocked", "the receiver's access policy refuses this machine (403); nothing sent"
    if kind in ("offline", "unreachable", "absent"):
        return "pending", {"absent": "not in the current tailscale status: kept pending",
                           "offline": "offline: kept pending for a later run"}.get(kind, "not answering: kept pending")
    if kind in ("malformed", "error"):
        return "pending", "unexpected answer (%s): kept pending" % kind
    return "send", "running %s" % (version or "an older receiver (no /info)")


def notice_candidates(state, nodes, peer_specs, port, discover, target):
    cands, unknown = {}, []

    def add(node, p, source):
        n = match_node(nodes, node)
        if n is not None and n["self"]:
            return None
        canon = n["node"] if n else node
        c = cands.setdefault("%s:%d" % (canon, p), {
            "node": canon, "port": p, "ip": n["ip"] if n else None, "online": bool(n and n["online"]),
            "in_status": n is not None, "sources": set(), "agents": [], "remembered": None})
        c["sources"].add(source)
        return c

    def want(c, agent):
        if agent not in c["agents"]:
            c["agents"].append(agent)
    for host, p, agent in peer_specs:
        if match_node(nodes, host) is None:
            unknown.append(host)
            continue
        c = add(host, p, "explicit")
        if c is not None:
            want(c, agent)
    for inst in state["installations"].values():
        if isinstance(inst, dict) and isinstance(inst.get("node"), str) and isinstance(inst.get("port"), int):
            c = add(inst["node"], inst["port"], "remembered")
            if c is not None:
                c["remembered"] = inst
                for x in inst.get("explicit_agents") or []:
                    if x is None or isinstance(x, str):
                        want(c, x)
    for e in state["notices"].values():           # unfinished notices for this version stay in every plan
        if isinstance(e, dict) and e.get("version") == target and e.get("status") not in ("notified", "verified") \
                and isinstance(e.get("node"), str) and isinstance(e.get("port"), int):
            c = add(e["node"], e["port"], "earlier notice")
            if c is not None:
                want(c, e.get("agent"))
    if discover:
        for n in nodes:
            if not n["self"] and n["online"]:
                add(n["node"], port, "discovered")
    return sorted(cands.values(), key=lambda c: (c["node"], c["port"])), unknown


def notice_plan(state, items, probes, target, reconcile, retry_blocked=False):
    plan = []
    for c, pr in zip(items, probes):
        kind = ("offline" if c["in_status"] else "absent") if pr is None else pr["kind"]
        if c["sources"] == {"discovered"} and kind not in ("info", "legacy", "blocked"):
            continue                  # not an installation we know of: never message arbitrary nodes
        info = pr["info"] if pr and pr["kind"] == "info" else None
        agents = list(c["agents"])
        if not agents:
            if info is not None:
                agents = [x["agent"] for x in info["agents"] if x["session"]] or \
                         [next((x["agent"] for x in info["agents"] if x["default"]), None)]
            elif c["remembered"] and c["remembered"].get("agents"):
                agents = [x.get("agent") for x in c["remembered"]["agents"] if isinstance(x, dict)
                          and x.get("session_id")] or [None]
            else:
                agents = [None]
        for agent in agents:
            key = notice_key(c["node"], c["port"], agent, target)
            entry = state["notices"].get(key) if isinstance(state["notices"].get(key), dict) else None
            version = info["version"] if info else None
            status = entry.get("status") if entry is not None else None
            if entry is not None and status is None:
                status = "invalid_missing_status"
            if entry is not None and any(entry.get(k) != v for k, v in {
                    "key": key, "node": c["node"], "port": c["port"], "agent": agent, "version": target}.items()):
                status = "invalid_record_identity"
            action, why = decide_notice(status, kind, version, target, reconcile, retry_blocked)
            plan.append({"key": key, "node": c["node"], "port": c["port"], "ip": c["ip"], "agent": agent,
                         "receiver": kind, "version": version, "status": (entry or {}).get("status"),
                         "action": action, "why": why, "sources": sorted(c["sources"])})
    return plan


def update_notify(home, home_source, a, fetch=None):
    f = gather_versions(home, home_source)
    if f.get("cfg") is None:
        die("not set up (%s); run `%s onboard`" % (f.get("config_error") or "no config.json", PRODUCT), 2)
    target_v = parse_semver(a.version or f["plugin_version"])
    if target_v is None:
        die("--version must be a release version such as 0.3.0", 2)
    target = fmt_semver(target_v)
    cfg, port = f["cfg"], f["port"]
    nodes, err = tailnet_nodes(cfg["tailscale_cmd"])
    if err:
        die("cannot read the tailnet: %s" % err, 1)
    me = next((n["node"] for n in nodes if n["self"]), None)
    verification = verify_local(home, f, target)
    release, release_problem = official_release(target, a.channel, fetch=fetch)
    state = load_notice_state(home)
    specs = [parse_peer_spec(x, port) for x in a.peer]
    items, unknown = notice_candidates(state, nodes, specs, port, not a.no_discover, target)
    timeout = max(0.2, min(a.timeout, 30.0))
    probes = run_parallel(lambda c: probe_info(c["ip"], c["port"], timeout, expect_node=c["node"], host=c["node"])
                          if c["online"] and c["ip"] else None, items)
    plan = notice_plan(state, items, probes, target, a.reconcile_uncertain, a.retry_blocked)
    res = {"command": "update notify", "version": target, "channel": a.channel, "dry_run": not a.apply,
           "verification": verification, "release": release, "release_problem": release_problem,
           "plan": plan, "unknown_peers": unknown, "results": []}
    if a.apply:
        if not verification["ok"]:
            res["error"] = "the local upgrade is not verified; notices are sent only after this machine runs %s" % target
        elif release_problem:
            res["error"] = release_problem
        else:
            try:
                res["results"] = apply_notices(home, me, target, items, probes, a.reconcile_uncertain,
                                               release=release, retry_blocked=a.retry_blocked)
            except LockBusy as e:
                res["error"] = str(e)
    for item in plan:
        item.pop("ip", None)
    if a.json:
        print(json.dumps(res, indent=2, ensure_ascii=True))
    else:
        print("%s update notify %s (%s)" % (PRODUCT, target, "applied" if a.apply and not res.get("error")
                                            else "refused" if res.get("error") else "dry run, nothing sent or written"))
        print("  local upgrade: %s" % ("verified (plugin, durable runtime and running receiver at %s)" % target
                                       if verification["ok"] else "NOT verified: " + "; ".join(verification["problems"])))
        print("  release:       %s" % ("official %s%s (%s)" % (release["tag"], " prerelease" if release["prerelease"]
                                                                else "", release["url"]) if release
                                       else "NOT verified: " + release_problem))
        if not plan:
            print("  no known installations: name them with --peer HOST/AGENT, or run `%s agents --remember` while "
                  "they are online" % PRODUCT)
        for item in plan:
            print("  %-46s %-15s %s" % (item["key"][:46], item["action"], item["why"]))
        for r in res["results"]:
            print("  -> %-43s %-15s %s" % (r["key"][:43], r["status"], r.get("detail") or ""))
        for spec in unknown:
            print("  unknown peer (not in tailscale status, nothing recorded): %s" % spec)
        if res.get("error"):
            print("  error: %s" % res["error"])
        elif not a.apply and plan:
            print("  review the plan, then re-run with --apply")
    return 1 if res.get("error") else 0


def valid_notice_envelope(env, item, me, target):
    """A retry must keep the original id, authenticated sender and exact authorized agent route."""
    if not isinstance(env, dict) or core().validate_env(env):
        return False
    if env.get("from") != me or env.get("to") != item["node"] or env.get("agent") != item["agent"] \
            or env.get("kind") != NOTICE_KIND:
        return False
    try:
        notice = json.loads(env.get("text", ""))
    except (ValueError, TypeError):
        return False
    return isinstance(notice, dict) and all(notice.get(k) == v for k, v in {
        "type": NOTICE_KIND, "product": PRODUCT, "version": target, "from_node": me,
        "source": OFFICIAL_REPO_URL}.items())


def apply_notices(home, me, target, items, probes, reconcile, post=None, release=None, retry_blocked=False,
                  notice_keys=None):
    """Apply under the cross-process lock. Every action is recomputed from the state read under the lock, so a
    plan made earlier can never downgrade an uncertain or dispatching notice."""
    results = []
    with StateLock(home / NOTICES_LOCK):
        state = load_notice_state(home)
        for e in state["notices"].values():
            if isinstance(e, dict) and e.get("status") == "dispatching":
                # A previous run stopped after recording the attempt: the notice may have been delivered.
                e.update(status="uncertain", last_error="interrupted after the send was recorded",
                         updated_at=now_iso())
        for c, pr in zip(items, probes):
            key = "%s:%d" % (c["node"], c["port"])
            answered = pr is not None and pr["kind"] in ("info", "legacy")
            if not answered and "explicit" not in c["sources"]:
                continue
            inst = state["installations"].get(key) if isinstance(state["installations"].get(key), dict) else {}
            inst.update(node=c["node"], port=c["port"])
            if "explicit" in c["sources"]:
                inst["explicit_agents"] = sorted(set((inst.get("explicit_agents") or []) + c["agents"]),
                                                 key=lambda x: x or "")
            if answered:
                info = pr["info"]
                inst.update(last_seen=now_iso(), version=info["version"] if info else "<=0.2.x", kind=pr["kind"])
                if info:
                    inst["agents"] = [{"agent": x["agent"], "default": x["default"],
                                       "session_id": (x["session"] or {}).get("id")} for x in info["agents"]]
            state["installations"][key] = inst
        write_json_atomic(home / NOTICES_FILE, state)
        for item in notice_plan(state, items, probes, target, reconcile, retry_blocked):
            key = item["key"]
            if notice_keys is not None and key not in notice_keys:
                continue             # maintenance cannot create recipients or agent routes
            entry = state["notices"].get(key) if isinstance(state["notices"].get(key), dict) else None
            if notice_keys is not None and entry is None:
                continue             # a removed notice cancels its authorization to retry
            action = item["action"]
            if action in ("skip", "hold"):
                continue
            if entry is None:
                entry = {"key": key, "node": item["node"], "port": item["port"], "agent": item["agent"],
                         "version": target, "status": "pending", "attempts": 0, "created_at": now_iso()}
                state["notices"][key] = entry
            now = now_iso()
            if action == "verify":
                entry.update(status="verified", verified_at=now, updated_at=now,
                             detail="authenticated discovery shows the running receiver at %s (software only; no "
                                    "agent wake is implied)" % item["version"])
            elif action == "blocked":
                entry.update(status="blocked", updated_at=now, last_error="403 on /info: refused by access policy")
            elif action == "pending":
                entry.update(status="pending", updated_at=now, last_error=item["why"])
            elif action in ("send", "resend_same_id") and item["ip"]:
                env = entry.get("envelope")    # also keep the id after a known-unsent attempt
                attempts = entry.get("attempts", 0)
                bad_attempts = not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0
                needs_original = action == "resend_same_id" or bad_attempts or attempts > 0 or "envelope" in entry
                if needs_original and (bad_attempts or not valid_notice_envelope(env, item, me, target)):
                    entry.update(status="uncertain", updated_at=now,
                                 last_error="stored envelope missing, invalid or mismatched; cannot safely reuse its id/route")
                    write_json_atomic(home / NOTICES_FILE, state)
                    results.append({"key": key, "status": "uncertain", "detail": entry["last_error"]})
                    continue
                if not isinstance(env, dict):
                    env = {"v": 1, "id": secrets.token_hex(8), "from": me, "to": item["node"], "ts": now,
                           "kind": NOTICE_KIND, "text": notice_text(me, target, release),
                           "wake": "now"}     # a request only: the receiver's wake policy may lower it
                    if item["agent"] is not None:     # never dropped or retargeted, also for older receivers
                        env["agent"] = item["agent"]
                    entry["envelope"] = env
                entry.update(status="dispatching", attempts=int(entry.get("attempts") or 0) + 1, updated_at=now)
                write_json_atomic(home / NOTICES_FILE, state)          # intent is durable before the POST
                outcome, detail, code = (post or post_notice)(item["ip"], item["port"], item["node"], env)
                entry.update(updated_at=now_iso(), http_status=code, detail=detail,
                             last_error=None if outcome == "notified" else detail)
                entry["status"] = {"notified": "notified", "blocked": "blocked", "rejected": "rejected",
                                   "uncertain": "uncertain", "not_sent": "pending"}[outcome]
                if outcome == "notified":
                    entry["notified_at"] = entry["updated_at"]
            else:
                continue
            write_json_atomic(home / NOTICES_FILE, state)
            results.append({"key": key, "status": entry["status"], "detail": entry.get("detail") or
                            entry.get("last_error")})
    return results


def maintenance_releases(home, apply, timeout, fetch=None):
    """Rate-limit official metadata checks across timer runs. Only an official normalized release
    list is cached; release text and executable content are never kept. Failed checks retry next tick."""
    cache, _ = core().read_small_json(home / "maintenance-releases.json")
    if isinstance(cache, dict) and cache.get("source") == OFFICIAL_REPO_URL:
        try:
            age = time.time() - core().parse_iso(cache["checked_at"]).timestamp()
            if 0 <= age < 6 * 3600:
                releases = parse_releases(cache["releases"])
                return releases, cache["checked_at"], True
        except (KeyError, TypeError, ValueError, UpdateError):
            pass
    releases = (fetch or fetch_releases)(timeout)
    checked = now_iso()
    if apply:
        write_json_atomic(home / "maintenance-releases.json", {
            "source": OFFICIAL_REPO_URL, "checked_at": checked,
            "releases": [{"tag_name": r["tag"], "prerelease": r["prerelease"],
                           "published_at": r["published_at"]} for r in releases]})
    return releases, checked, False


def maintenance_candidates(state, nodes, target):
    """Only exact persisted recipients. No discovery and no newly bound sessions are enrolled."""
    selected = {key: e for key, e in state["notices"].items() if isinstance(e, dict) and
                e.get("version") == target and e.get("status") not in ("verified", "rejected")}
    candidates = {}
    for key, e in selected.items():
        node, port, agent = e.get("node"), e.get("port"), e.get("agent")
        if not isinstance(node, str) or not PEER_RULE_RE.fullmatch(node) or node.startswith(("tag:", "user:")) \
                or not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535 \
                or (agent is not None and core().agent_name_error(agent)) \
                or key != notice_key(node, port, agent, target):
            continue
        n = match_node(nodes, node)
        if n and n["self"]:
            continue
        c = candidates.setdefault((node, port), {
            "node": node, "port": port, "ip": n["ip"] if n else None, "online": bool(n and n["online"]),
            "in_status": n is not None, "sources": {"earlier notice"}, "agents": [], "remembered": None})
        if agent not in c["agents"]:
            c["agents"].append(agent)
    return list(candidates.values()), set(selected)


def maintain_once(home, home_source, a):
    f = gather_versions(home, home_source)
    if f.get("cfg") is None:
        die("not set up; run `%s onboard` first" % PRODUCT, 2)
    state = load_notice_state(home)
    versions = local_versions(home, f)
    res = {"command": "update maintain", "dry_run": not a.apply, "check": None, "plans": [],
           "results": [], "errors": [], "installed": versions}
    timeout = max(0.2, min(a.timeout, 30.0))
    try:
        releases, checked_at, cached = maintenance_releases(home, a.apply, max(1.0, timeout))
        check = {"checked_at": checked_at, "cached": cached, "installed": versions,
                 **assess_update(versions["plugin"], releases, a.channel)}
    except UpdateError as e:
        releases = None
        check = {"checked_at": now_iso(), "channel": a.channel, "status": "error", "error": e.code,
                 "installed": versions, "latest": None, "latest_preview": None}
        res["errors"].append("could not verify official releases: " + e.code)
    res["check"] = check
    if a.apply:
        write_json_atomic(home / UPDATE_FILE, check)
    targets = sorted({e.get("version") for e in state["notices"].values() if isinstance(e, dict) and
                      parse_semver(e.get("version")) and e.get("status") not in ("verified", "rejected")},
                     key=lambda v: semver_key(parse_semver(v)))
    if not targets:
        return res
    nodes, err = tailnet_nodes(f["cfg"]["tailscale_cmd"])
    if err:
        res["errors"].append("cannot read the tailnet: " + err)
        return res
    me = next((n["node"] for n in nodes if n["self"]), None)
    local_ok = verify_local(home, f, versions["plugin"])["ok"] and bool(me)
    for target in targets:
        items, keys = maintenance_candidates(state, nodes, target)
        probes = run_parallel(lambda c: probe_info(c["ip"], c["port"], timeout, expect_node=c["node"],
                                                   host=c["node"]) if c["online"] and c["ip"] else None, items)
        plan = [p for p in notice_plan(state, items, probes, target, False) if p["key"] in keys]
        rel = next((r for r in releases or [] if r["version"] == target), None)
        can_send = local_ok and compare_versions(versions["plugin"], target) in (0, 1) and rel is not None
        held = None
        if rel and rel["prerelease"] and a.channel != "preview":
            # A stable timer never delivers prerelease notices: an owner who announced a prerelease runs (or
            # installs a reminder template for) `update maintain --channel preview`.
            can_send = False
            held = ("%s is a prerelease and this run uses the stable channel: run `%s update maintain --channel "
                    "preview` (a reminder template made with --channel preview does)" % (target, PRODUCT))
        if a.apply and can_send:
            res["results"].extend(apply_notices(home, me, target, items, probes, False, release=rel,
                                                 notice_keys=keys))
        elif a.apply and plan:
            res["errors"].append("%s: retry held; %s" % (target, held or "need matching local plugin/runtime/"
                                 "receiver at this version or newer and its official release on the selected channel"))
        for p in plan:
            p.pop("ip", None)
        res["plans"].append({"version": target, "ready": can_send, "held": held, "plan": plan})
    return res


def update_maintain(home, home_source, a):
    if a.apply and not (home / "config.json").is_file():
        die("not set up; run `%s onboard` first" % PRODUCT, 2)
    try:
        if a.apply:
            with StateLock(home / ".upgrade-maintenance.lock"):
                res = maintain_once(home, home_source, a)
        else:
            res = maintain_once(home, home_source, a)
    except LockBusy as e:
        res = {"command": "update maintain", "dry_run": not a.apply, "check": None,
               "plans": [], "results": [], "errors": [str(e)]}
    if a.json:
        print(json.dumps(res, indent=2, ensure_ascii=True))
    else:
        print("%s update maintain (%s)" % (PRODUCT, "applied" if a.apply else "dry run; nothing written or sent"))
        if res["check"]:
            print("  release check: %s" % res["check"]["status"])
        for group in res["plans"]:
            if group.get("held"):
                print("  held: %s" % group["held"])
            for p in group["plan"]:
                print("  %-46s %-15s %s" % (p["key"][:46], p["action"], p["why"]))
        for r in res["results"]:
            print("  -> %-43s %s" % (r["key"][:43], r["status"]))
        for err in res["errors"]:
            print("  error: " + err)
        print("  No software installed, access policy changed, or new recipients enrolled.")
    return 1 if res["errors"] else 0


# ---------------------------------------------------------------- onboarding: platform-aware next steps
def environment_facts():
    import platform as _platform
    facts = {"platform": sys.platform, "system": _platform.system(), "release": _platform.release(),
             "machine": _platform.machine(), "root": hasattr(os, "geteuid") and os.geteuid() == 0,
             "sudo": bool(shutil.which("sudo")), "curl": bool(shutil.which("curl")),
             "workspace": Path("/workspace").is_dir()}
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/sys/kernel/osrelease") as fh:
                facts["wsl"] = "microsoft" in fh.read().lower()
        except OSError:
            facts["wsl"] = False
        facts["container"] = os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")
        facts["tun"] = os.path.exists("/dev/net/tun")
        facts["systemd"] = Path("/run/systemd/system").is_dir()
        facts["systemd_user"] = False
        if facts["systemd"] and shutil.which("systemctl"):
            try:
                facts["systemd_user"] = subprocess.run(["systemctl", "--user", "show-environment"],
                                                       capture_output=True, timeout=5).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                pass
    elif sys.platform == "darwin":
        facts["mac_app_cli"] = os.path.isfile(MAC_TS_CLI)
    elif sys.platform == "win32":
        facts["win_cli"] = os.path.isfile(WIN_TS_CLI)
    return facts


def root_prefix(facts):
    """'' when already root, 'sudo ' when sudo exists, None when neither (Linux/macOS shells only)."""
    if facts.get("root"):
        return ""
    return "sudo " if facts.get("sudo") else None


def permission_guidance(facts, ts_cmd):
    """How to let Yobiru query Tailscale after `permission denied`, for this platform and configured CLI."""
    plat = facts["platform"]
    if plat == "win32":
        return ("run Yobiru in the same Windows user session that is signed in to the Tailscale app (an "
                "administrator shell is not needed for status queries); if the CLI is not on PATH: "
                '`%s setup --tailscale-cmd "%s" --replace tailscale_cmd`' % (PRODUCT, WIN_TS_CLI), [], "user")
    if plat == "darwin":
        return ("use the Tailscale app's CLI as the signed-in user: `%s setup --tailscale-cmd %s --replace "
                "tailscale_cmd`" % (PRODUCT, MAC_TS_CLI), [], "agent")
    if (ts_cmd or [""])[0] == "sudo":
        return ("the configured command already uses sudo and was refused; passwordless sudo for tailscale is "
                "needed, or the administrator can make this user the Tailscale operator", [], "administrator")
    if facts.get("root"):
        return ("tailscale refused even root: check that tailscaled runs and its socket is reachable", [], "agent")
    if facts.get("sudo"):
        return ("with passwordless sudo, let Yobiru call tailscale through it; otherwise the administrator can make "
                "this user the Tailscale operator (their decision)",
                ['%s setup --tailscale-cmd \'["sudo","-n","tailscale"]\' --replace tailscale_cmd' % PRODUCT],
                "agent (only with passwordless sudo) or administrator")
    return ("this user cannot query tailscale and has no sudo: the administrator can make this user the "
            "Tailscale operator (their decision)", [], "administrator")


def classify_tailscale(ts):
    """missing | permission | daemon_unavailable | error | running | logged_out | needs_approval | stopped |
    starting | unchecked | unknown"""
    if ts.get("cmd") is None:
        return "error"
    if not ts.get("found"):
        return "missing"
    err = ts.get("error") or ""
    if re.search(r"permission denied|access denied|not permitted", err, re.I):
        return "permission"
    if re.search(r"tailscaled|connection refused|is not running|doesn.t appear to be running|failed to connect|"
                 r"no such file|cannot connect", err, re.I):
        return "daemon_unavailable"
    if err:
        return "error"
    if not ts.get("probed"):
        return "unchecked"
    return {"Running": "running", "NeedsLogin": "logged_out", "NeedsMachineAuth": "needs_approval",
            "Stopped": "stopped", "NoState": "starting", "Starting": "starting"}.get(ts.get("backend"), "unknown")


def tailscale_install_steps(facts):
    """(status, detail, commands, docs, who) for installing Tailscale on this platform."""
    p = facts["platform"]
    if p.startswith("linux"):
        docs = [TS_DOCS["linux"], TS_DOCS["download"]]
        cmds = ["curl -fsSL %s | sh" % TS_DOCS["script"]]
        notes = []
        if not facts.get("curl"):
            notes.append("curl is missing; install it with the system package manager first")
        if not facts.get("tun"):
            return ("blocked", "this host has no /dev/net/tun (container or restricted VM). Tailscale would only "
                    "run in userspace networking mode, which gives this machine no Tailscale IP address of its "
                    "own; the Yobiru receiver binds that address and connects to peers directly, so Yobiru cannot "
                    "work here. Use a TUN-capable host, or ask the administrator to provide /dev/net/tun",
                    [], docs, "administrator / another host")
        if not facts.get("systemd"):
            notes.append("no systemd: the install script cannot start tailscaled as a service; start "
                         "`tailscaled` yourself (for example in a supervisor or `nohup`) and keep it running")
        if facts.get("wsl"):
            notes.append("WSL: Tailscale can run inside the distribution or on Windows; a receiver inside WSL "
                         "needs Tailscale inside WSL")
        if facts.get("root") or facts.get("sudo"):
            return ("todo", "Tailscale is not installed. The official script installs the package (needs root"
                    + ("" if facts.get("root") else " through sudo") + ")" + ("; " + "; ".join(notes) if notes else ""),
                    cmds, docs, "agent may run it when the user asked to set up Yobiru")
        return ("blocked", "Tailscale is not installed and this user has neither root nor sudo, so it cannot be "
                "installed here; ask the machine's administrator (on Grok Bot team computers: the Enterprise Team "
                "Setup script)" + ("; " + "; ".join(notes) if notes else ""), cmds, docs, "administrator")
    if p == "darwin":
        if facts.get("mac_app_cli"):
            return ("todo", "the Tailscale app is installed but its CLI is not on PATH",
                    ["%s setup --tailscale-cmd %s --replace tailscale_cmd   # existing config" % (PRODUCT, MAC_TS_CLI),
                     "%s setup --tailscale-cmd %s   # new config" % (PRODUCT, MAC_TS_CLI)],
                    [TS_DOCS["darwin"]], "agent")
        return ("todo", "install the Tailscale app (Mac App Store or the standalone download), open it, approve "
                "the VPN / system extension prompt in System Settings, then sign in from the menu bar. The "
                "app's CLI is %s" % MAC_TS_CLI, [], [TS_DOCS["darwin"], TS_DOCS["download"]],
                "user (GUI approval needed)")
    if p == "win32":
        if facts.get("win_cli"):
            return ("todo", "Tailscale is installed but not on PATH for this Python",
                    ['%s setup --tailscale-cmd "%s" --replace tailscale_cmd' % (PRODUCT, WIN_TS_CLI)],
                    [TS_DOCS["win32"]], "agent")
        return ("todo", "run the Windows installer from the download page (administrator rights needed), then "
                "sign in from the Tailscale icon in the notification area. The CLI is %s; paths with spaces must "
                "stay quoted, and the Codex bridge is not available on Windows" % WIN_TS_CLI, [],
                [TS_DOCS["win32"], TS_DOCS["download"]], "user (administrator)")
    return ("blocked", "unsupported platform %s for these instructions" % p, [], [TS_DOCS["download"]], "user")


def onboarding_steps(home, f, facts):
    steps = []
    plat = facts["platform"]

    def step(sid, status, title, detail, commands=(), docs=(), who="agent"):
        steps.append({"id": sid, "status": status, "title": title, "detail": detail, "commands": list(commands),
                      "docs": list(docs), "who": who})
    ok_py = sys.version_info[:2] >= MIN_PYTHON
    step("python", "done" if ok_py else "blocked", "Python 3.8+", f["python"]["version"],
         who="agent" if ok_py else "user")
    ts = f["tailscale"]
    state = classify_tailscale(ts)
    if state == "missing":
        st, detail, cmds, docs, who = tailscale_install_steps(facts)
        step("tailscale-install", st, "Install Tailscale", detail, cmds, docs, who)
    elif state == "error" and ts.get("cmd") is None:
        step("tailscale-install", "todo", "Fix tailscale_cmd", ts.get("error") or "", [], [], "agent")
    else:
        step("tailscale-install", "done", "Install Tailscale", "found %s" % ts.get("found"))
    login_docs = [TS_DOCS.get(plat if plat in ("darwin", "win32") else "linux")]
    if state in ("missing",):
        step("tailscale-login", "todo", "Sign in to Tailscale", "after installing", who="user")
    elif state == "permission":
        detail, cmds, who = permission_guidance(facts, ts.get("cmd"))
        step("tailscale-login", "todo", "Let Yobiru query Tailscale",
             "tailscale refused this user (%s); %s" % (ts.get("error"), detail), cmds, login_docs, who)
    elif state == "daemon_unavailable":
        pre = root_prefix(facts)
        if plat.startswith("linux") and pre is not None:
            cmds = ["%ssystemctl enable --now tailscaled" % pre] if facts.get("systemd") else \
                ["%stailscaled   # keep it running (supervisor or nohup); needs /dev/net/tun" % pre]
        else:
            cmds = []
        step("tailscale-login", "todo", "Start the Tailscale daemon",
             "tailscaled is not running or not reachable (%s)%s" % (ts.get("error"), "; on macOS/Windows open the "
             "Tailscale app" if plat in ("darwin", "win32") else ""), cmds, login_docs,
             "agent with root/sudo, otherwise user")
    elif state in ("logged_out", "stopped", "starting", "unknown", "error"):
        cmd = ts.get("cmd") or ["tailscale"]
        if plat == "win32":
            exe = "& " + " ".join('"%s"' % x for x in cmd)          # PowerShell call operator for a quoted path
        else:
            exe = " ".join("'%s'" % x if " " in x else x for x in cmd)
        if plat.startswith("linux"):
            pre = root_prefix(facts)
            if (ts.get("cmd") or [""])[0] == "sudo":
                pre = ""
            up = "%s%s up" % (pre or "", exe)
            how = "Run `%s`: it prints a login URL%s" % (up, "" if pre is not None else
                                                        " (this user has no root or sudo: an administrator "
                                                        "or the machine owner must run it)")
        else:
            up = "%s up" % exe
            how = "Sign in from the Tailscale app (%s); `%s` (%s) from the configured CLI also works and opens " \
                  "the login page" % ("menu bar" if plat == "darwin" else "notification area icon", up,
                                      "PowerShell" if plat == "win32" else "Terminal")
        step("tailscale-login", "todo", "Sign in to Tailscale",
             {"logged_out": "this machine is logged out", "stopped": "Tailscale is stopped",
              "starting": "Tailscale is still starting; check again shortly",
              "unknown": "unexpected state %s" % ts.get("backend"),
              "error": ts.get("error") or "tailscale status failed"}[state] +
             ". %s; the user opens it and chooses the account and tailnet themselves (no auth keys in "
             "chat)" % how, [up], login_docs, "user (identity choice)")
    elif state == "needs_approval":
        step("tailscale-login", "todo", "Device approval", "the tailnet requires an administrator to approve this "
             "device in the Tailscale admin console", [], login_docs, "tailnet administrator")
    elif state == "unchecked":
        step("tailscale-login", "unknown", "Sign in to Tailscale", "not checked (--no-probe)")
    else:
        step("tailscale-login", "done", "Sign in to Tailscale", "running as %s" % ts.get("node"))
    if f["config_error"]:
        step("config", "blocked", "Configure Yobiru", f["config_error"], [], [], "user (fix config.json by hand)")
    elif not f["config_exists"]:
        step("config", "todo", "Choose agent names and review access policy",
             "new installs accept authenticated Tailscale peers that can reach this receiver; deny rules are "
             "optional. Choose this agent's name; nothing is created until --apply. Use --allow PEER on first "
             "setup only if you want an allowlist instead",
             ["%s setup --agent NAME --self-agent NAME --default-agent NAME" % PRODUCT,
              "%s setup --agent NAME --self-agent NAME --default-agent NAME --apply" % PRODUCT],
             [], "user chooses, agent applies")
    else:
        mode = f.get("access_policy", "allowlist")
        detail = "access policy: %s; allow: %s; deny: %s" % (
            mode, ", ".join(f.get("allow") or []) or "(none)", ", ".join(f.get("deny") or []) or "(none)")
        commands = []
        if f.get("legacy_allowlist"):
            detail += ". Legacy allow list preserved; optional migration to tailnet mode needs an explicit policy change"
            commands.append("%s policy set --mode tailnet   # review, then add --apply to migrate" % PRODUCT)
        elif mode == "allowlist" and not f.get("allow"):
            detail += ". Empty allowlist rejects incoming messages; retain it for a send-only installation"
            commands.append("%s policy show" % PRODUCT)
        step("config", "done" if mode == "tailnet" or f.get("allow") else "todo",
             "Review access policy and agent names", detail, commands)
        # Optional migration is visible even when the existing installation is already configured.
        if f.get("legacy_allowlist"):
            step("policy-migration", "optional", "Optional: migrate legacy incoming access",
                 "the existing allowlist remains active. To accept authenticated Tailscale peers except deny "
                 "matches, review the policy change below; keeping the allowlist is also supported", commands,
                 [], "user chooses whether to migrate")
    rt = f["runtime"]
    step("runtime", "done" if rt["present"] and rt.get("same_as_bundled") else "todo", "Durable receiver runtime",
         "%s %s" % (rt["path"], "matches the bundled runtime" if rt.get("same_as_bundled") else
                    "differs from the bundled runtime" if rt["present"] else "missing"),
         [] if rt["present"] and rt.get("same_as_bundled") else
         ["%s install-runtime%s --apply" % (PRODUCT, " --upgrade" if rt["present"] else "")])
    recv = f.get("receiver") or {}
    if recv.get("reachable") and recv.get("ok"):
        step("receiver", "done", "Receiver running", "serving at %s" % recv.get("url"))
    else:
        if plat == "darwin":
            kind = "launchd"
        elif plat == "win32":
            kind = "windows"
        elif facts.get("systemd_user"):
            kind = "systemd-user"
        elif facts.get("systemd") and (facts.get("root") or facts.get("sudo")):
            kind = "systemd"
        else:
            kind = None
        if kind:
            step("receiver", "todo", "Run the receiver as a service",
                 "print the definition, install it at the place named in its header and start it",
                 ["%s service-template %s" % (PRODUCT, kind)])
        else:
            step("receiver", "todo", "Run the receiver",
                 "no usable service manager here: run `%s serve` in a persistent terminal or supervisor (it stops "
                 "when that process ends; on Grok Bot computers long-running receivers are not verified)" % PRODUCT,
                 ["nohup sh <path to scripts/yobiru> serve >> %s 2>&1 &" % (home / "serve.log")])
    if f.get("cfg") is not None:
        names = core().info_agent_names(f["cfg"], core().parse_config(f["cfg"])[1])
        bound, _ = core().session_bindings(home, names)
        missing = [n for n in names if n not in bound]
        step("sessions", "done" if names and not missing else "todo", "Bind each agent to its session",
             "bound: %s; unbound: %s" % (", ".join(sorted(bound)) or "none", ", ".join(missing) or "none"),
             ["%s session bind --agent %s --session-id <exact id> --display-name <alias>" % (PRODUCT, n)
              for n in missing[:3]], [], "user names the existing session; agent applies")
    if f.get("cfg") is not None:
        silent = [a["agent"] for a in f.get("adapters", []) if a["adapter"] == "none"]
        broken = [a["agent"] for a in f.get("adapters", []) if any(st == "error" for st, _ in a["checks"])]
        cmds = []
        for n in (silent + broken)[:3]:
            if n == "codex" and plat in ("linux", "darwin"):
                cmds.append("%s codex-bridge install --thread-id <session UUID>%s --dry-run" % (
                    PRODUCT, " --codex-remote unix:///<app-server socket>" if plat == "linux" else ""))
            else:
                cmds.append("%s wake-config --agent %s --exec /absolute/notify-program   # or --webhook ..." % (
                    PRODUCT, n if n is not None else "NAME"))
        step("delivery", "done" if not silent and not broken else "todo", "Configure how each agent is woken",
             "a session binding only names the session; delivery is configured separately (Codex bridge, a "
             "local program or a webhook). Without an adapter, messages wait in `inbox --pending`. "
             + ("no adapter: %s; " % ", ".join("@%s" % x for x in silent) if silent else "")
             + ("misconfigured: %s; " % ", ".join("@%s" % x for x in broken) if broken else "")
             + "verify with a test message sent with --wake now from an allowed peer (or `wake ID`) and check "
               "`show ID` for wake_delivery", cmds, [], "agent, with the user's choice of adapter")
    step("verify", "todo", "Verify", "check this machine and the other installations",
         ["%s doctor" % PRODUCT, "%s agents --online" % PRODUCT, "%s send PEER/AGENT \"hello\"" % PRODUCT])
    return steps


def cmd_onboard(home, home_source, argv):
    ap = argparse.ArgumentParser(allow_abbrev=False, prog=PRODUCT + " onboard",
                                 description="Read-only, step-by-step setup plan for this machine and platform: "
                                             "Tailscale install and sign-in, Yobiru config, durable receiver, "
                                             "session binding and verification. Works before anything is set up.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-probe", action="store_true", help="skip Tailscale and receiver probes")
    a = ap.parse_args(argv)
    f = gather(home, home_source, probe=not a.no_probe)
    facts = environment_facts()
    steps = onboarding_steps(home, f, facts)
    nxt = next((s for s in steps if s["status"] in ("todo", "blocked")), None)
    if a.json:
        print(json.dumps({"platform": facts, "home": str(home), "steps": steps, "next": nxt and nxt["id"],
                          "changed": False}, indent=2, ensure_ascii=True))
        return 0
    print("%s onboarding for %s (read-only; nothing was changed)" % (DISPLAY, facts["system"] or facts["platform"]))
    print("Installing the plugin ran nothing: there is no install hook. These steps make it work.")
    marks = {"done": "[x]", "todo": "[ ]", "blocked": "[!]", "unknown": "[?]", "optional": "[i]"}
    for i, s in enumerate(steps, 1):
        print("%s %d. %s: %s" % (marks.get(s["status"], "[ ]"), i, s["title"], s["detail"]))
        if s["status"] in ("todo", "blocked", "optional"):
            for c in s["commands"]:
                print("        $ %s" % c)
            for d in s["docs"]:
                print("        docs: %s" % d)
            print("        who: %s" % s["who"])
    print("next: %s" % (nxt["title"] if nxt else "all steps done; try `%s agents --online`" % PRODUCT))
    return 0


# ---------------------------------------------------------------- dispatch
def run_core(home, cmd, rest):
    if not (home / "config.json").is_file():
        die("not set up: no %s (nothing was created). Run `%s doctor`, then `%s setup --agent <name>` "
            "(dry run, then --apply)." % (home / "config.json", PRODUCT, PRODUCT), 2)
    if not CORE.is_file():
        die("bundled tailmsg.py is missing; reinstall the plugin", 2)
    python = os.environ.get(PYTHON_ENV) or sys.executable
    argv = [python, str(CORE), "--home", str(home), cmd] + list(rest)
    sys.stdout.flush()
    sys.stderr.flush()
    if os.name == "posix":
        try:
            os.execvp(python, argv)
        except OSError as e:
            die("cannot run %s: %s" % (python, e), 127)
    return subprocess.call(argv)


def run_public(script, args):
    """Public bot contacts run as their own processes from the bundled public_bots/ folder: they never
    load tailmsg.py, read config.json or touch the inbox, so legacy private messaging is unaffected."""
    path = PUBLIC_BOTS / script
    if not path.is_file():
        die("bundled %s is missing; reinstall the plugin" % script, 2)
    python = os.environ.get(PYTHON_ENV) or sys.executable
    argv = [python, str(path)] + list(args)
    sys.stdout.flush()
    sys.stderr.flush()
    if os.name == "posix":
        try:
            os.execvp(python, argv)
        except OSError as e:
            die("cannot run %s: %s" % (python, e), 127)
    return subprocess.call(argv)


def cmd_gateway(home, home_source, argv):
    return run_public("bot_gateway.py", argv)


def cmd_public(home, home_source, argv):
    return run_public("bot_client.py", ["--home", str(home)] + list(argv))


def cmd_calendar_service(home, home_source, argv):
    return run_public("bot_calendar.py", argv)


def cmd_resource_service(home, home_source, argv):
    return run_public("bot_resources.py", argv)


def cmd_muse(home, home_source, argv):
    sys.path.insert(0, str(HERE))
    import yobiru_muse
    return yobiru_muse.main(home, argv, sys.modules[__name__])


def cmd_claude_bridge(home, home_source, argv):
    if not argv or argv[0] in ("--help", "-h"):
        print("yobiru claude-bridge install [--claude-path PATH --workdir DIR] [--apply]\n"
              "yobiru claude-bridge {status,step,daemon} [--config PATH]\n"
              "Install previews by default. step/daemon can launch visible Claude sessions; status is read-only.")
        return 0
    action, rest = argv[0], argv[1:]
    folder = HERE / "claude_bridge"
    if action == "install":
        command = [sys.executable, "-B", str(folder / "install_claude_bridge.py"),
                   "--tailmsg-home", str(home), "--plugin-root", str(PLUGIN_ROOT)] + rest
    elif action in ("status", "step", "daemon"):
        ap = argparse.ArgumentParser(prog=PRODUCT + " claude-bridge " + action, allow_abbrev=False)
        ap.add_argument("--config", default=str(home / "claude-visible" / "config.json"))
        args = ap.parse_args(rest)
        command = [sys.executable, "-B", str(folder / "claude_bridge.py"), "--config", args.config, action]
    else:
        die("unknown claude-bridge action", 2)
    return subprocess.call(command)


def cmd_mcp(home, home_source, argv):
    """Local stdio MCP server over the messaging commands (yobiru_mcp.py next to this file)."""
    sys.path.insert(0, str(HERE))
    import yobiru_mcp
    return yobiru_mcp.main(home, argv)


PLUGIN_COMMANDS = {"paths": cmd_paths, "doctor": cmd_doctor, "status": cmd_status, "setup": cmd_setup,
                   "policy": cmd_policy,
                   "wake-config": cmd_wake_config, "install-runtime": cmd_install_runtime,
                   "service-template": cmd_service_template, "codex-bridge": cmd_codex_bridge,
                   "onboard": cmd_onboard, "agents": cmd_agents, "peers": cmd_agents, "session": cmd_session, "update": cmd_update,
                   "gateway": cmd_gateway, "public": cmd_public, "calendar-service": cmd_calendar_service,
                   "resource-service": cmd_resource_service,
                   "mcp": cmd_mcp, "muse": cmd_muse, "claude-bridge": cmd_claude_bridge}


def main(argv=None):
    configure_cli_output()
    if sys.version_info[:2] < MIN_PYTHON:
        die("Python %d.%d+ is required (this is %s)" % (MIN_PYTHON + (sys.version.split()[0],)), 127)
    argv = list(sys.argv[1:] if argv is None else argv)
    home_arg, i = None, 0
    while i < len(argv) and argv[i].startswith("-"):
        if argv[i] == "--home":
            if i + 1 >= len(argv):
                die("--home needs a directory", 2)
            home_arg, i = argv[i + 1], i + 2
        elif argv[i].startswith("--home="):
            home_arg, i = argv[i][len("--home="):], i + 1
        elif argv[i] in ("-h", "--help"):
            print(USAGE.format(p=PRODUCT, d=DISPLAY, env=HOME_ENV, home=DEFAULT_HOME))
            return 0
        elif argv[i] == "--version":
            argv[i] = "version"
            break
        else:
            die("unknown option %s (see `%s --help`)" % (argv[i], PRODUCT), 2)
    if i >= len(argv):
        print(USAGE.format(p=PRODUCT, d=DISPLAY, env=HOME_ENV, home=DEFAULT_HOME), file=sys.stderr)
        return 2
    cmd, rest = argv[i], argv[i + 1:]
    home, source = resolve_home(home_arg)
    if cmd == "version":
        print("%s %s (tailmsg protocol v1; bundled tailmsg.py sha256 %s)"
              % (PRODUCT, plugin_version(), sha256_file(CORE)[:16] if CORE.is_file() else "missing"))
        return 0
    if cmd in CORE_COMMANDS:
        return run_core(home, cmd, rest)
    if cmd in PLUGIN_COMMANDS:
        return PLUGIN_COMMANDS[cmd](home, source, rest)
    die("unknown command %r (see `%s --help`)" % (cmd, PRODUCT), 2)


if __name__ == "__main__":
    sys.exit(main())
