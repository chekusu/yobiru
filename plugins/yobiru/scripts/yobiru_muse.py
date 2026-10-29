"""Muse's outbound-only adapter. No listener, Tailscale install or new request ledger.

A custom connector can invoke this CLI from Muse's execution environment. The
host's official Tailscale connector must expose its own CLI identity and an
outbound TCP route; availability is checked, never assumed from the host name.
"""
import argparse
import json
from pathlib import Path

CONNECT = "Ask Muse to connect the official Tailscale connector (Settings > Connectors), then complete its own sign-in."
FALLBACK = "If that connector or its CLI/network access is unavailable, use Yobiru Friends with the intended bot's share link; see the yobiru-bot-contacts skill."


def _local_identity_ready(cfg, cli, add):
    """Plain status can diagnose CLI availability, never authenticate a pull sender."""
    stdout, error = cli.run_tailscale(cfg["tailscale_cmd"], ["status", "--json"], timeout=5)
    if error:
        _, plain_error = cli.run_tailscale(cfg["tailscale_cmd"], ["status"], timeout=5)
        if plain_error:
            add("error", "tailscale_unavailable", "Official connector status could not be read: " + error)
        else:
            add("error", "tailscale_json_unavailable",
                "The official connector answers plain `tailscale status`, but `tailscale status --json` failed: " + error +
                ". Pull requests require this environment's trusted StableID and DNSName; plain status, IP and HTTP health "
                "cannot supply them. Use --peer with the intended receiver's exact Tailscale IP to diagnose its route.")
        return False
    try:
        status = json.loads(stdout)
        own = status.get("Self") if isinstance(status, dict) else None
        if not isinstance(own, dict):
            raise ValueError("missing Self")
    except (ValueError, TypeError):
        add("error", "tailscale_invalid_status", "Tailscale returned no usable status object.")
        return False
    if status.get("BackendState") != "Running":
        add("error", "tailscale_not_connected", "Finish sign-in/device approval in the official connector.")
        return False
    if not cli.core().stable_peer_id(own.get("ID")) or not isinstance(own.get("DNSName"), str) or not own["DNSName"].rstrip("."):
        add("error", "tailscale_identity_unavailable", "Pull requests need this execution environment's StableID and DNSName. Do not copy an identity from another device.")
        return False
    ips = own.get("TailscaleIPs")
    if not isinstance(ips, list) or not any(isinstance(ip, str) and cli.core().is_tailnet_ip(ip) for ip in ips):
        add("error", "tailscale_address_unavailable", "The official connector did not expose a usable tailnet address to this environment.")
        return False
    add("ok", "tailscale_identity", "This environment has an authenticated Tailscale identity; inbound reachability is not required.")
    return True


def check(home, cli, peer=None):
    """Read config and LocalAPI; optional bounded peer probes. Never creates a home."""
    result = {"host": "muse", "transport": "pull", "changed": False,
              "receiver_required": False, "delivery_verified": False,
              "local_ready": False, "peer_checked": False, "checks": [],
              "connect": CONNECT, "fallback": FALLBACK}
    checks = result["checks"]
    def add(state, reason, detail):
        checks.append({"status": state, "check": reason, "detail": detail})
    raw, error = cli.read_config(Path(home))
    if error:
        add("error", "config_invalid", error)
        return result
    try:
        cfg, _ = cli.core().parse_config(raw or {})
        fatal, _ = cli.config_problems(cfg)
        if fatal:
            raise ValueError("; ".join(fatal))
    except (ValueError, RuntimeError) as exc:
        add("error", "config_invalid", str(exc))
        return result
    if raw is None:
        add("warn", "setup_needed", "No Yobiru config exists; check is read-only. Configure a separate data home after connecting Tailscale.")
    result["local_ready"] = _local_identity_ready(cfg, cli, add) and raw is not None
    if peer:
        target, port, _ = cli.parse_peer_spec(peer, int(cfg["port"]))
        result["peer_checked"] = True
        checks.extend(cli.peer_doctor_checks({"cfg": cfg}, target, port, timeout=3))
        result["peer_pull_advertised"] = any(c["check"] == "peer-pull" and c["status"] == "ok" for c in checks)
    return result


def parser():
    ap = argparse.ArgumentParser(prog="yobiru muse", allow_abbrev=False,
        description="Outbound-only Muse custom connector. Uses the official Tailscale connector and existing request ledger; never starts a receiver.")
    sub = ap.add_subparsers(dest="operation", required=True)
    p = sub.add_parser("check", allow_abbrev=False, help="read-only identity and optional peer checks (JSON)")
    p.add_argument("--peer", help="optional HOST[:PORT][/AGENT]; sends no messages")
    p = sub.add_parser("send", allow_abbrev=False, help="send one tracked pull request; save the returned ID")
    p.add_argument("--file", action="append", default=[])
    p.add_argument("--wake", choices=("none", "batch", "now"), default="batch")
    p.add_argument("--timeout-seconds", type=int, default=900)
    p.add_argument("peer")
    p.add_argument("text")
    p = sub.add_parser("status", allow_abbrev=False, help="refresh the original request's result; never resends")
    p.add_argument("--cached", action="store_true", help="read only the local ledger")
    p.add_argument("id")
    p = sub.add_parser("resume", allow_abbrev=False, help="resume upload/submission of the same saved pull request")
    p.add_argument("id")
    sub.add_parser("list", allow_abbrev=False, help="read the ledger to recover IDs after interrupted output")
    for op, help_text in (("read", "read a saved message/result"), ("fetch", "download and verify result attachments")):
        p = sub.add_parser(op, allow_abbrev=False, help=help_text)
        p.add_argument("id")
    return ap


def command(a):
    if a.operation == "send":
        if not 1 <= a.timeout_seconds <= 604800:
            raise ValueError("--timeout-seconds must be 1..604800")
        return ["request", "send", "--transport", "pull", "--as", "muse",
                "--wake", a.wake, "--timeout-seconds", str(a.timeout_seconds)] + [
                    item for path in a.file for item in ("--file", path)] + ["--", a.peer, a.text]
    if a.operation == "status":
        return ["request", "status", "--json"] + ([] if a.cached else ["--refresh"]) + ["--", a.id]
    if a.operation == "resume":
        return ["request", "resume", "--", a.id]
    if a.operation == "list":
        return ["request", "list", "--json"]
    return [{"read": "show", "fetch": "fetch"}[a.operation], "--", a.id]


def main(home, argv, cli):
    ap = parser()
    a = ap.parse_args(argv)
    if a.operation == "check":
        try:
            data = check(home, cli, a.peer)
        except ValueError as exc:
            ap.error(str(exc))
        print(json.dumps(data, ensure_ascii=True, indent=2))
        return 0 if data["local_ready"] and not any(c["status"] == "error" for c in data["checks"]) and (not a.peer or data.get("peer_pull_advertised")) else 2
    try:
        args = command(a)
    except ValueError as exc:
        ap.error(str(exc))
    # Existing tailmsg validation, identity pinning, durable submission and no-replay
    # rules remain authoritative. There is no alternate transport or fallback send.
    return cli.run_core(home, args[0], args[1:])
