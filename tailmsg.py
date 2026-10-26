#!/usr/bin/env python3
"""tailmsg - minimal peer-to-peer messaging over Tailscale (protocol v1).

Every machine runs `tailmsg.py serve`. Messages are small JSON envelopes POSTed to
http://<peer>:7766/msg. Files are never inside a message: the sender stages them in a
read-only, expiring, unguessable URL on its own tailnet address; the receiver pulls
them itself and verifies sha256. Auth = Tailscale identity (`tailscale whois`) +
per-machine access policy. Python 3.8+ stdlib only. See SPEC.md.
"""
import argparse, datetime as dt, errno, hashlib, http.client, ipaddress, json, mimetypes, os
import re, secrets, shlex, shutil, socket, sqlite3, ssl, stat, subprocess, sys, threading, time, unicodedata, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer
from pathlib import Path

VERSION = 1                     # wire protocol version (unchanged)
PRODUCT = "yobiru"
PRODUCT_VERSION = "0.7.15"  # Yobiru release of this file; tools/build_plugin.py check keeps it in sync
DEFAULTS = {
    "port": 7766,
    "access_policy": "tailnet",    # authenticated Tailscale peers; use "allowlist" for opt-in restriction
    "allow": [],                    # e.g. ["bot-box", "tag:agent", "user:me@example.com"]
    "deny": [],                     # same rule syntax; deny always wins
    "tailscale_cmd": ["tailscale"], # e.g. ["sudo", "-n", "tailscale"] if the socket needs root
    "bind": None,                   # default: own Tailscale IPv4
    "wake_adapter": None,           # how to wake the local agent, see SPEC §5.8, e.g.
                                    #   {"type": "exec", "argv": ["/usr/local/bin/wake-codex"]}
                                    #   {"type": "webhook", "url_env": "TAILMSG_WAKE_URL", "secret_env": "TAILMSG_WAKE_SECRET"}
    "on_message": None,             # DEPRECATED alias of {"type":"exec","argv": on_message}
    "hook_timeout_s": 120,          # per-attempt timeout of the wake adapter
    "wake_attempts": 3,             # adapter attempts per message (1 + 2 retries), then give up -> stays pending
    "wake_backoff_s": 2,            # retry delay: backoff * 2**(n-1) seconds
    "outbox_ttl_hours": 24,
    "max_msg_bytes": 256 * 1024,
    "max_fetch_bytes": 1024 ** 3,
    # wake control: the receiver decides the effective level (see SPEC §5.6)
    "wake_policy": {"default_max": "batch", "peers": {}},  # e.g. {"peers": {"bot-box": "now"}}
    "quiet_hours": None,            # e.g. {"start": "23:00", "end": "08:00", "tz": "Asia/Tokyo"}
    "wake_rate": 6,                 # max effective `now` per (sender, agent) per hour; excess -> batch
    # @agent addressing (SPEC §5.9): several agents can share one machine / one tailmsg
    "self_agent": None,             # my own agent name, sent as `from_agent` so replies come back to me
    "default_agent": None,          # agent that gets messages without `agent` (uses top-level wake_adapter)
    "agents": {},                   # name -> {"wake_adapter": {...}, "wake_max": "now|batch|none"}
}
WAKE_LEVELS = ("none", "batch", "now")  # ascending
WAKE_DEFAULT = "batch"
TAILNET_NETS = [ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48")]
ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}\Z")      # \Z, not $: "$" would also accept a trailing newline
MSG_REF_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\Z")  # a message id or a unique prefix of one (CLI input)
INBOX_PREVIEW = 500                                 # characters of text per `inbox --json` row
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}\Z")
ENV_KEYS = {"v", "id", "from", "to", "ts", "kind", "text", "reply_to", "fetch", "wake", "agent", "from_agent", "request", "response"}
AGENT_MAX = 64
FETCH_KEYS = {"name", "url", "sha256", "size", "mime", "expires"}
STABLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}\Z")


def stable_peer_id(stable_id):
    """Case-preserving device identity in a canonical, case-insensitive wire spelling."""
    if not isinstance(stable_id, str) or not STABLE_ID_RE.fullmatch(stable_id):
        return None
    return "node:" + stable_id.encode("ascii").hex()


def peer_identity(ident):
    return stable_peer_id(ident.get("stable_id")) or ident.get("node")


def fresh_whois(ctx, ip):
    # Pull grants and stored ownership must come from LocalAPI now. The 60-second cache of ordinary traffic is
    # never read here: a lookup that started before the address was reassigned can refill it at any moment.
    return ctx.whois_uncached(ip)


def is_pull_request(env):
    return isinstance(env.get("request"), dict) and env["request"].get("transport") == "pull"


def sender_matches(env, ident):
    expected = peer_identity(ident) if is_pull_request(env) else ident.get("node")
    return bool(expected) and env.get("from", "").lower().rstrip(".") == expected


# ---------------------------------------------------------------- helpers
def configure_cli_output():
    """Keep the selected encoding, but escape text it cannot represent.

    Only called by main, so importing the core does not alter a host's streams.
    JSON printed by the CLI uses ensure_ascii=True separately: backslashreplace
    alone would produce invalid JSON escapes for emoji (\\U instead of \\u).
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(errors="backslashreplace")


def now_utc():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)

def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")

def parse_iso(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)

def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, file=sys.stderr, flush=True)

def die(msg, code=1):
    print("error: " + msg, file=sys.stderr)
    sys.exit(code)

def atomic_write(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp-" + secrets.token_hex(4))
    tmp.write_bytes(data)
    os.replace(tmp, path)


def write_private_json(path, obj):
    """Durably replace `path` with JSON, owner-only (0600): exclusive temporary file, fsync, atomic rename,
    then the directory where supported. The temporary file is removed on any failure."""
    path = Path(path)
    tmp = path.with_name(".%s.%d.%s.tmp" % (path.name, os.getpid(), secrets.token_hex(4)))
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write((json.dumps(obj, ensure_ascii=True, indent=2) + "\n").encode("ascii"))
            out.flush()
            os.fsync(out.fileno())
        os.replace(str(tmp), str(path))
    except BaseException:
        if os.path.lexists(str(tmp)):
            os.unlink(str(tmp))
        raise
    if os.name == "posix":
        fsync_directory(path.parent)


# A directory fsync that the filesystem does not support; every other error (EIO, ...) is a real failure.
UNSUPPORTED_SYNC = {getattr(errno, name) for name in ("EINVAL", "ENOTSUP", "EOPNOTSUPP") if hasattr(errno, name)}
# Only these mean another holder has the lock (flock: EWOULDBLOCK; msvcrt: EACCES or EDEADLOCK); any other
# locking error is raised, never reported as busy.
LOCK_CONTENTION = {getattr(errno, name) for name in (("EACCES", "EDEADLOCK") if os.name == "nt"
                                                     else ("EWOULDBLOCK", "EAGAIN")) if hasattr(errno, name)}


def fsync_directory(directory):
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in UNSUPPORTED_SYNC:
            raise
    finally:
        os.close(fd)


class FileLock(object):
    """Non-blocking, cross-process exclusive lock on a file in the data home: flock on POSIX, msvcrt on Windows.
    The kernel releases it when the holder exits, so a crashed process never leaves it held. acquire() is False
    only for real contention; any other error is raised so that it surfaces as a failure."""

    def __init__(self, path):
        self.path, self.fd = Path(path), None

    def acquire(self):
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                import fcntl
            except ImportError:
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in LOCK_CONTENTION:
                return False
            raise
        self.fd = fd
        return True

    def release(self):
        if self.fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            os.close(self.fd)        # closing also releases flock
            self.fd = None


def sync_directory(path):
    """Best-effort directory sync (not supported by every platform/filesystem)."""
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def private_open(path, truncate=False):
    """A new file only this user can read (0600) whatever the process umask, never following a link. Request
    inputs and results can be private even when a custom data home is traversable by other users."""
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | (os.O_TRUNC if truncate else os.O_EXCL)
    descriptor = os.open(str(path), flags, 0o600)
    if truncate and hasattr(os, "fchmod"):
        os.fchmod(descriptor, 0o600)                 # an earlier partial file keeps its mode otherwise
    return os.fdopen(descriptor, "wb")


def private_mkdir(path):
    """Create a directory and any missing parents as 0700; existing directories keep their mode."""
    path, missing = Path(path), []
    while not path.exists():
        missing.append(path)
        path = path.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass


def durable_write(path, data):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp-" + secrets.token_hex(8))
    try:
        with open(temporary, "xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()

def is_tailnet_ip(ip):
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    return any(a in n for n in TAILNET_NETS)

def select_peer(nodes, peer):
    """Resolve a name from a fresh normalized Tailscale snapshot, never by list order.

    Short names can follow a stable HostName across a reinstall. Explicit FQDNs and
    IPs stay exact: replies, file grants and saved notices must not change recipients.
    """
    p = peer.lower().rstrip(".")
    try:
        address = ipaddress.ip_address(p)
    except ValueError:
        address = None
    matches = []
    for n in nodes:
        if not n.get("node") or not n.get("ip"):
            continue
        if address is not None:
            ips = n.get("ips") or [n["ip"]]
            hit = any(str(address) == str(ipaddress.ip_address(ip)) for ip in ips)
        elif "." in p:
            hit = p == n["node"].lower().rstrip(".")
        else:
            hit = p in (n["node"].split(".")[0].lower(),
                        (n.get("hostname") or "").lower().rstrip("."))
        if hit:
            matches.append(n)
    if not matches:
        return None
    # A precise address is never resolved through another node's HostName.
    candidates = matches if address is not None or "." in p else \
        [n for n in matches if n.get("online") is True] or matches
    if len(candidates) != 1:
        names = ", ".join(sorted(n["node"] for n in candidates))
        raise ValueError("ambiguous peer %r: %s; use an exact MagicDNS name or IP" % (peer, names))
    return candidates[0]

def safe_name(name):
    name = Path(str(name).replace("\\", "/")).name
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", name).lstrip(".").strip()
    return name[:200] or "file"

def agent_name_error(name):
    """None if `name` is a valid agent name: 1-64 chars (Unicode ok), no '/', no control chars,
    no leading/trailing whitespace. Compared after NFC normalization."""
    if not isinstance(name, str) or not name:
        return "agent name must be a non-empty string"
    if len(name) > AGENT_MAX:
        return "agent name longer than %d chars" % AGENT_MAX
    if "/" in name:
        return "agent name must not contain '/'"
    if any(unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in name):
        return "agent name must not contain control characters"
    if name != name.strip():
        return "agent name must not start or end with whitespace"
    return None

def norm_agent(name):
    return unicodedata.normalize("NFC", name) if isinstance(name, str) else name

def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_access_policy(raw):
    """Validate policy without mutating config. An explicit legacy allow list stays restrictive.

    This must see the raw config, before defaults are merged: absence of `allow` is different from
    an existing, intentionally empty allow list. A mode change is always a local configuration choice.
    """
    if not isinstance(raw, dict):
        raise RuntimeError("config must be a JSON object")
    mode = raw.get("access_policy", "allowlist" if "allow" in raw else "tailnet")
    if mode not in ("tailnet", "allowlist"):
        raise RuntimeError("config access_policy must be tailnet or allowlist")
    policy = {"access_policy": mode}
    for field in ("allow", "deny"):
        rules = raw.get(field, [])
        if not isinstance(rules, list):
            raise RuntimeError("config %s must be a list of peer rules" % field)
        normalized = []
        for rule in rules:
            valid = isinstance(rule, str) and bool(rule) and len(rule) <= 320
            if valid:
                valid = not any(c.isspace() or unicodedata.category(c).startswith("C") for c in rule)
            if valid:
                r = rule.lower()
                if r.startswith("node:"):
                    valid = re.fullmatch(r"node:(?:[0-9a-f]{2}){1,128}", r) is not None
                elif r.startswith("user:"):
                    valid = bool(r[5:]) and not any(c in r[5:] for c in ":/*\\")
                elif r.startswith("tag:"):
                    valid = re.fullmatch(r"tag:[a-z0-9][a-z0-9_-]*", r) is not None
                else:
                    r = r.rstrip(".")
                    valid = len(r) <= 253 and re.fullmatch(
                        r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
                        r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*", r) is not None
            if not valid:
                raise RuntimeError("config %s contains an invalid peer rule" % field)
            normalized.append(r)
        policy[field] = normalized
    return policy


def peer_matches(rule, ident):
    """Shared host/FQDN/tag/user rule matching for transport, dispatcher and wake caps."""
    r = str(rule).lower()
    if r.startswith("node:"):
        return r == peer_identity(ident)
    if r.startswith("tag:"):
        return r in [t.lower() for t in ident.get("tags", [])]
    if r.startswith("user:"):
        return bool(ident.get("user")) and r[5:] == ident["user"].lower()
    r = r.rstrip(".")
    node = ident.get("node", "").lower().rstrip(".")
    dns = ident.get("dns_name", "").lower().rstrip(".")
    return bool(r) and r in {node, node.split(".")[0], ident.get("host", "").lower().rstrip("."),
                             dns, dns.split(".")[0]}


def access_decision(raw, ident, recipient_token=False, self_discovery=False):
    """(accepted, reason) for an already authenticated Tailscale identity; always fails closed.

    The HTTP boundary additionally requires the actual source IP to be in Tailscale's range and
    verified by LocalAPI whois. Bridge receipts come from that boundary, never envelope claims.
    Recipient-bound file tokens and local self-discovery keep their historical allow-list exemption;
    neither may bypass a deny rule or an invalid policy.
    """
    try:
        policy = parse_access_policy(raw)
    except (RuntimeError, TypeError, ValueError):
        return False, "invalid_policy"
    if not isinstance(ident, dict) or not isinstance(ident.get("node"), str) or not ident["node"].strip(".") or \
            any(c.isspace() or unicodedata.category(c).startswith("C") for c in ident["node"]):
        return False, "unknown_peer"
    if any(not isinstance(ident.get(k, ""), str) for k in ("host", "user", "dns_name")) or \
            not isinstance(ident.get("tags", []), list) or \
            any(not isinstance(t, str) for t in ident.get("tags", [])):
        return False, "unknown_peer"
    if "ip" in ident and (not isinstance(ident["ip"], str) or not is_tailnet_ip(ident["ip"])):
        return False, "unknown_peer"
    if any(peer_matches(rule, ident) for rule in policy["deny"]):
        return False, "denied"
    if policy["access_policy"] == "tailnet":
        return True, "tailnet"
    if recipient_token or self_discovery:
        return True, "recipient_token" if recipient_token else "self_discovery"
    if any(peer_matches(rule, ident) for rule in policy["allow"]):
        return True, "allowlist"
    return False, "not_allowed"


def parse_config(raw):
    """config.json object -> (cfg merged over DEFAULTS, {agent name: spec}). Pure: no files are read or
    created, so tools can validate a config without side effects. Raises RuntimeError if invalid."""
    if not isinstance(raw, dict):
        raise RuntimeError("config must be a JSON object")
    cfg = dict(DEFAULTS)
    cfg.update(raw)
    cfg.update(parse_access_policy(raw))
    for k in ("self_agent", "default_agent"):
        if cfg.get(k) is not None:
            err = agent_name_error(cfg[k])
            if err:
                raise RuntimeError("config %s: %s" % (k, err))
            cfg[k] = norm_agent(cfg[k])
    agents = cfg.get("agents") or {}
    if not isinstance(agents, dict):
        raise RuntimeError("config agents must be an object {name: {...}}")
    out = {}
    for name, spec in agents.items():
        err = agent_name_error(name)
        if err:
            raise RuntimeError("config agents[%r]: %s" % (name, err))
        if spec is None:
            spec = {}
        if not isinstance(spec, dict) or set(spec) - {"wake_adapter", "wake_max"}:
            raise RuntimeError("config agents[%r] must be {\"wake_adapter\": ..., \"wake_max\": ...}" % name)
        if spec.get("wake_max") is not None and spec["wake_max"] not in WAKE_LEVELS:
            raise RuntimeError("config agents[%r].wake_max must be one of now/batch/none" % name)
        out[norm_agent(name)] = spec
    return cfg, out


class TailscaleUnavailable(RuntimeError):
    """The CLI timed out or could not connect to the local Tailscale daemon."""


class Ctx:
    def __init__(self, home):
        self.home = Path(home).expanduser()
        self.inbox = self.home / "inbox"
        self.outbox = self.home / "outbox"
        for d in (self.home, self.inbox, self.outbox):
            d.mkdir(parents=True, exist_ok=True)
        cfg_path = self.home / "config.json"
        if not cfg_path.exists():
            atomic_write(cfg_path, json.dumps(DEFAULTS, indent=2).encode())
            log("created default config", cfg_path, "(authenticated Tailscale peers may communicate)")
        self.cfg, self.agents = parse_config(json.loads(cfg_path.read_text(encoding="utf-8")))
        self._whois_cache = {}
        self._now_hits = {}          # (peer identity, agent) -> [epoch seconds of effective `now`]
        self._wake_lock = threading.Lock()
        self._inbox_lock = threading.Lock()
        self._name_cache = {}        # session id -> (expires epoch, native name lookup result)
        self._name_guard = threading.Lock()
        self._name_lookup = threading.Lock()   # one lookup subprocess at a time
        self.maintenance = None                # set by `serve`: supervised maintenance workers
        self.receiver_identity = None          # set by `serve`: pinned LocalAPI identity, never rebound live

    def native_name(self, binding):
        """Read-only native session name for a bound session, cached (bounded subprocess cost)."""
        return cached_native_name(self.home, binding, self._name_cache, self._name_guard, self._name_lookup)

    # -------- tailscale CLI (LocalAPI) --------
    def ts(self, *args, timeout=15):
        cmd = list(self.cfg["tailscale_cmd"]) + list(args)
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise RuntimeError("tailscale CLI not found: %r (set tailscale_cmd in config.json)" % cmd[0])
        except subprocess.TimeoutExpired:
            raise TailscaleUnavailable("tailscale CLI timed out: %r" % cmd)
        if p.returncode != 0:
            err = (p.stderr or p.stdout).strip()
            if re.search(r"permission denied|access (?:is )?denied|operation not permitted", err, re.I):
                err += ' (hint: set "tailscale_cmd": ["sudo","-n","tailscale"] in config.json)'
            elif re.search(r"failed to connect to local (?:tailscaled|tailscale daemon)\b", err, re.I):
                raise TailscaleUnavailable("%s: %s" % (" ".join(cmd), err))
            raise RuntimeError("%s: %s" % (" ".join(cmd), err))
        return p.stdout

    def status(self):
        return json.loads(self.ts("status", "--json"))

    def whois(self, ip):
        """Ordinary traffic may reuse a lookup of this address for 60 seconds; pull grants use whois_uncached."""
        hit = self._whois_cache.get(ip)
        if hit and hit[0] > time.time():
            return hit[1]
        ident = self.whois_uncached(ip)
        if ident is not None:
            self._whois_cache[ip] = (time.time() + 60, ident)
        return ident

    def whois_uncached(self, ip):
        try:
            # Keep a stalled identity lookup below the sender's five-second /info timeout.
            d = json.loads(self.ts("whois", "--json", ip, timeout=3))
        except TailscaleUnavailable:
            log("whois temporarily unavailable for", ip)
            raise
        except Exception as e:
            log("whois failed for", ip, e)
            return None
        node, up = d.get("Node") or {}, d.get("UserProfile") or {}
        # Shared-out peers deliberately have no MagicDNS name. Trust only the LocalAPI's
        # node identity bound to this exact source address, never ComputedName or a claim
        # inside the HTTP envelope. StableID is case-sensitive; its wire spelling is hex.
        stable_id = node.get("StableID")
        address_matches = False
        try:
            source = ipaddress.ip_address(ip)
            address_matches = any(source == ipaddress.ip_interface(a).ip and
                                  ipaddress.ip_interface(a).network.num_addresses == 1
                                  for a in node.get("Addresses", []))
        except (ValueError, TypeError):
            pass
        stable_valid = (stable_peer_id(stable_id) is not None and address_matches and
                        type(node.get("ID")) is int and node["ID"] > 0 and
                        type(node.get("User")) is int and node["User"] > 0 and
                        type(up.get("ID")) is int and up["ID"] == node["User"])
        dns_name = (node.get("Name") or "").rstrip(".").lower()
        ident = {
            "ip": ip,
            "node": dns_name or (stable_peer_id(stable_id) if stable_valid else ""),
            "host": (((node.get("Hostinfo") or {}).get("Hostname") or "").lower() if dns_name else ""),
            "tags": node.get("Tags") or [],
            "user": up.get("LoginName") or "",
        }
        if stable_valid:
            ident.update(stable_id=stable_id, dns_name=dns_name)
        return ident

    def self_node(self):
        s = self.status()["Self"]
        return {"node": s["DNSName"].rstrip(".").lower(), "ips": s.get("TailscaleIPs") or [],
                "stable_id": s.get("ID")}

    def resolve(self, peer):
        """'ubuntu' | 'ubuntu.tailXXXX.ts.net' | '100.x.y.z' -> (fqdn, ipv4)"""
        st = self.status()
        nodes = []
        entries = [(True, st.get("Self"))] + [(False, n) for n in (st.get("Peer") or {}).values()]
        for is_self, n in entries:
            if not isinstance(n, dict):
                continue
            fq = (n.get("DNSName") or "").rstrip(".").lower()
            ips = []
            for ip in n.get("TailscaleIPs") or []:
                try:
                    ips.append(str(ipaddress.ip_address(ip)))
                except (ValueError, TypeError):
                    continue
            if not fq or not ips:
                continue
            v4 = [i for i in ips if ":" not in i]
            nodes.append({"node": fq, "hostname": n.get("HostName") or "", "ip": (v4 or ips)[0],
                          "ips": ips, "online": st.get("BackendState") == "Running" if is_self else
                          n.get("Online") is True})
        try:
            node = select_peer(nodes, peer)
        except ValueError as e:
            raise RuntimeError(str(e)) from e
        if node is not None:
            return node["node"], node["ip"]
        raise RuntimeError("peer %r not found in `tailscale status`" % peer)

    @staticmethod
    def matches(rule, ident):
        return peer_matches(rule, ident)

    def access_decision(self, ident, recipient_token=False, self_discovery=False):
        # Reload only access policy: a local deny/mode change takes effect on the next request, while
        # adapter configuration, wake caps, session bindings and all other runtime settings stay put.
        try:
            raw = json.loads((self.home / "config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            return False, "invalid_policy"
        return access_decision(raw, ident, recipient_token=recipient_token, self_discovery=self_discovery)

    def allowed(self, ident):
        return self.access_decision(ident)[0]

    def resolve_agent(self, requested):
        """-> (agent name or None, status). status: "default" (no `agent` in message), "known", "unknown"."""
        dflt = self.cfg.get("default_agent")
        if requested is None:
            return dflt, "default"
        a = norm_agent(requested)
        if a in self.agents or (dflt is not None and a == dflt):
            return a, "known"
        return a, "unknown"

    def agent_adapter(self, agent):
        """adapter for `agent`: its own agents[].wake_adapter; the default agent (or no agent at all)
        falls back to the top-level wake_adapter / on_message. Raises AdapterError if misconfigured."""
        spec = self.agents.get(agent) if agent is not None else None
        if spec and spec.get("wake_adapter"):
            return adapter_cfg({"wake_adapter": spec["wake_adapter"]})
        if agent is None or agent == self.cfg.get("default_agent"):
            return adapter_cfg(self.cfg)
        return None

    def wake_decision(self, requested, ident, now=None, agent=None, agent_status="default"):
        """-> (effective, reason). Receiver-side: min(requested, sender max, agent wake_max), unknown agent
        -> at most batch, then quiet hours, then rate limit per (sender, agent)."""
        pol = self.cfg.get("wake_policy") or {}
        cap = pol.get("default_max", WAKE_DEFAULT)
        for rule, lvl in (pol.get("peers") or {}).items():   # first matching rule wins
            if self.matches(rule, ident):
                cap = lvl
                break
        if cap not in WAKE_LEVELS:
            cap = WAKE_DEFAULT
        eff, reasons = requested, []
        if WAKE_LEVELS.index(eff) > WAKE_LEVELS.index(cap):
            eff = cap
            reasons.append("capped by wake_policy (max %s)" % cap)
        amax = (self.agents.get(agent) or {}).get("wake_max") if agent is not None else None
        if amax in WAKE_LEVELS and WAKE_LEVELS.index(eff) > WAKE_LEVELS.index(amax):
            eff = amax
            reasons.append("capped by agents[%s].wake_max (%s)" % (agent, amax))
        if agent_status == "unknown" and eff == "now":
            eff = "batch"
            reasons.append("unknown agent %r: nobody woken" % agent)
        if eff == "now" and in_quiet_hours(self.cfg.get("quiet_hours"), now):
            eff = "batch"
            reasons.append("quiet hours")
        if eff == "now":
            t = time.time()
            limit = int(self.cfg.get("wake_rate") or 0)
            with self._wake_lock:
                key = (peer_identity(ident), agent)     # one budget per peer, whether it pushes or pulls
                hits = [x for x in self._now_hits.get(key, []) if x > t - 3600]
                if len(hits) >= limit:
                    eff = "batch"
                    reasons.append("rate limit (%d now/hour)" % limit)
                else:
                    hits.append(t)
                self._now_hits[key] = hits
        return eff, "; ".join(reasons) or "as requested"


def in_quiet_hours(q, now=None):
    if not q:
        return False
    try:
        tz = None
        if q.get("tz"):
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(q["tz"])
        t = (now or dt.datetime.now(dt.timezone.utc)).astimezone(tz)
        cur = t.hour * 60 + t.minute
        s, e = [int(x.split(":")[0]) * 60 + int(x.split(":")[1]) for x in (q["start"], q["end"])]
    except Exception as ex:
        log("bad quiet_hours config, ignored:", ex)
        return False
    if s == e:
        return False
    return s <= cur < e if s < e else (cur >= s or cur < e)


# ---------------------------------------------------------------- envelope
def validate_env(env):
    if not isinstance(env, dict):
        return "envelope must be an object"
    extra = set(env) - ENV_KEYS
    if extra:
        return "unknown fields: %s" % sorted(extra)
    for k in ("v", "id", "from", "to", "ts"):
        if k not in env:
            return "missing field: " + k
    if env["v"] != VERSION:
        return "unsupported version"
    if not isinstance(env["id"], str) or not ID_RE.match(env["id"]):
        return "bad id"
    try:
        parse_iso(env["ts"])
    except Exception:
        return "bad ts"
    if not isinstance(env.get("kind", "text"), str) or len(env.get("kind", "text")) > 32:
        return "bad kind"
    if not isinstance(env.get("text", ""), str):
        return "bad text"
    if "wake" in env and env["wake"] not in WAKE_LEVELS:
        return "bad wake (must be one of now/batch/none)"
    for k in ("agent", "from_agent"):
        if k in env:
            err = agent_name_error(env[k])
            if err:
                return "bad %s: %s" % (k, err)
    rt = env.get("reply_to")
    if rt is not None and (not isinstance(rt, str) or not ID_RE.match(rt)):
        return "bad reply_to"
    if any(not isinstance(env[k], str) or not env[k] for k in ("from", "to")):
        return "bad from/to"
    req, response = env.get("request"), env.get("response")
    if "request" in env:
        pull = is_pull_request(env)
        allowed = {"timeout_seconds", "reply_port", "transport", "uploads"} if pull else {"timeout_seconds", "reply_port"}
        if (not isinstance(req, dict) or set(req) - allowed or not {"timeout_seconds", "reply_port"} <= set(req) or
                type(req.get("timeout_seconds")) is not int or not 1 <= req["timeout_seconds"] <= 604800 or
                type(req.get("reply_port")) is not int or not 1 <= req["reply_port"] <= 65535 or
                env.get("kind") != "request" or "response" in env or env.get("reply_to")):
            return "bad request (timeout_seconds 1..604800 and reply_port 1..65535 required)"
        try:
            parse_iso(env["ts"]) + dt.timedelta(seconds=req["timeout_seconds"])
        except (ValueError, OverflowError):
            return "request deadline outside supported timestamp range"
        if pull:
            if not re.fullmatch(r"node:(?:[0-9a-f]{2}){1,128}", env["from"]) or env.get("fetch"):
                return "pull requests require a stable node sender and uploaded attachments"
            uploads = req.get("uploads", [])
            if not isinstance(uploads, list) or len(uploads) > 100:
                return "bad request uploads"
            names, tokens = set(), set()
            for item in uploads:
                if (not isinstance(item, dict) or set(item) != {"token", "name", "sha256", "size", "mime"} or
                        not isinstance(item["token"], str) or not TOKEN_RE.fullmatch(item["token"]) or
                        not isinstance(item["name"], str) or item["name"] != safe_name(item["name"]) or
                        not item["name"] or item["name"] in names or item["token"] in tokens or
                        not isinstance(item["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) or
                        type(item["size"]) is not int or item["size"] < 0 or
                        not isinstance(item["mime"], str) or len(item["mime"]) > 128 or
                        any(ord(c) < 32 or ord(c) == 127 for c in item["mime"])):
                    return "bad request upload item"
                names.add(item["name"])
                tokens.add(item["token"])
    if "response" in env:
        if (not isinstance(response, dict) or set(response) != {"state", "seq"} or
                response.get("state") not in REQUEST_STATES or type(response.get("seq")) is not int or
                not 1 <= response["seq"] <= 9223372036854775807 or not env.get("reply_to") or env.get("wake") != "none"):
            return "bad response"
        expected = "request-result" if response["state"] in REQUEST_FINAL + ("timed_out",) else "request-receipt"
        if env.get("kind") != expected:
            return "response kind does not match state"
    if env.get("kind") in ("request-receipt", "request-result") and response is None:
        return "response metadata required"
    fl = env.get("fetch", [])
    if not isinstance(fl, list) or len(fl) > 100:
        return "bad fetch list"
    for f in fl:
        if not isinstance(f, dict) or set(f) - FETCH_KEYS or not {"url", "sha256", "size"} <= set(f):
            return "bad fetch item"
        if not re.match(r"^[0-9a-f]{64}$", str(f["sha256"])) or not isinstance(f["size"], int) or f["size"] < 0:
            return "bad fetch item hash/size"
        u = urllib.parse.urlparse(str(f["url"]))
        if u.scheme != "http" or (u.hostname or "").lower() != str(env["from"]).lower():
            return "fetch url must be http:// on the sender's own tailnet name"
    return None


# ---------------------------------------------------------------- durable request lifecycle (optional protocol v1 extension)
REQUEST_CAPABILITY = "request-lifecycle-v1"
REQUEST_PULL_CAPABILITY = "request-pull-v1"
REQUEST_STATES = ("received", "running", "completed", "failed", "rejected", "blocked", "timed_out")
REQUEST_FINAL = ("completed", "failed", "rejected", "blocked")
# Local lifecycle evidence for incoming requests (NULL = not known). received_at: durable receiver acceptance;
# started_at / progress_at: first / latest `running` the owner reported (the only start evidence; a queued or
# seen notification is not); final_at / final_response_id: the durable final result and its exact response.
LIFECYCLE_COLUMNS = ("received_at", "started_at", "progress_at", "final_at", "final_response_id")


class RequestConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def enable_wal(db, timeout=15.0):
    """Switch to WAL. Changing the journal mode does not wait on SQLite's busy handler, so a second process opening
    a ledger that is still in rollback mode at the same moment gets "database is locked" at once: retry instead."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            db.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) or time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def request_db(ctx):
    directory = ctx.home / "requests"
    directory.mkdir(mode=0o700, exist_ok=True)
    db = sqlite3.connect(str(directory / "ledger.sqlite3"), timeout=15, factory=RequestConnection)
    try:
        db.row_factory = sqlite3.Row
        enable_wal(db)
        db.execute("PRAGMA synchronous=FULL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS requests (
                direction TEXT NOT NULL, id TEXT NOT NULL, peer TEXT NOT NULL, port INTEGER NOT NULL,
                envelope TEXT NOT NULL, record TEXT, deadline TEXT NOT NULL, state TEXT NOT NULL,
                seq INTEGER NOT NULL DEFAULT 0, latest TEXT, submission TEXT, timed_out_at TEXT,
                PRIMARY KEY (direction,id));
            CREATE TABLE IF NOT EXISTS response_outbox (
                id TEXT PRIMARY KEY, request_id TEXT NOT NULL, peer TEXT NOT NULL, port INTEGER NOT NULL,
                envelope TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, delivered_at TEXT, last_error TEXT);
            CREATE TABLE IF NOT EXISTS response_events (
                id TEXT PRIMARY KEY, request_id TEXT NOT NULL, envelope TEXT NOT NULL, received_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pull_staging (
                id TEXT PRIMARY KEY, peer TEXT NOT NULL, envelope TEXT NOT NULL, expires TEXT NOT NULL);
        """)
        migrate_request_ledger(db)
    except BaseException:
        db.close()
        raise
    return db


def table_columns(db, table):
    return {row[1] for row in db.execute("PRAGMA table_info(%s)" % table)}


def migrate_request_ledger(db):
    """Add the lifecycle columns once (additive; older runtimes keep working on the same ledger).

    Serialized like every writer, so a receiver and a CLI opening an old ledger together add each column once.
    Backfill uses only evidence this receiver wrote itself: the stored receipt time, its own response envelopes
    (timestamps of `running` and final responses) and the exact final response. Anything else stays NULL."""
    if set(LIFECYCLE_COLUMNS) <= table_columns(db, "requests"):
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        missing = [c for c in LIFECYCLE_COLUMNS if c not in table_columns(db, "requests")]
        for column in missing:
            db.execute("ALTER TABLE requests ADD COLUMN %s TEXT" % column)
        if missing:
            backfill_lifecycle(db)
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _stamp(value):
    """A receiver-written UTC timestamp, or None."""
    try:
        return iso(parse_iso(value)) if isinstance(value, str) else None
    except ValueError:
        return None


def backfill_lifecycle(db):
    responses = {}
    for row in db.execute("SELECT request_id, envelope FROM response_outbox ORDER BY rowid"):
        try:
            env = json.loads(row["envelope"])
            state, seq, ts = env["response"]["state"], env["response"]["seq"], _stamp(env["ts"])
        except (ValueError, KeyError, TypeError):
            continue
        if ts is not None and type(seq) is int:
            responses.setdefault(row["request_id"], []).append((seq, state, ts))
    for row in db.execute("SELECT id, record, state, latest FROM requests WHERE direction='incoming'").fetchall():
        try:
            received = _stamp(json.loads(row["record"])["received"]["at"])
        except (ValueError, KeyError, TypeError):
            received = None
        # First and latest `running` in durable response order (seq), never by comparing wall-clock stamps:
        # a clock step backwards must not make a later report look like the first start.
        running = [ts for _, state, ts in sorted(responses.get(row["id"], [])) if state == "running"]
        final_at = final_id = None
        if row["state"] in REQUEST_FINAL and row["latest"]:
            try:
                latest = json.loads(row["latest"])
                if latest["response"]["state"] == row["state"]:
                    final_at, final_id = _stamp(latest["ts"]), latest["id"]
            except (ValueError, KeyError, TypeError):
                pass
        db.execute("UPDATE requests SET received_at=COALESCE(received_at,?), started_at=COALESCE(started_at,?), "
                   "progress_at=COALESCE(progress_at,?), final_at=COALESCE(final_at,?), "
                   "final_response_id=COALESCE(final_response_id,?) WHERE direction='incoming' AND id=?",
                   (received, running[0] if running else None, running[-1] if running else None,
                    final_at, final_id, row["id"]))


def json_canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def request_snapshot(home, mid, direction="incoming"):
    """Read-only bridge lookup, without creating a database or touching the network."""
    path = Path(home) / "requests" / "ledger.sqlite3"
    if not path.is_file():
        return None
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        row = request_row(db, direction, mid)
        return request_view(row) if row else None
    finally:
        db.close()


def request_snapshots(home):
    path = Path(home) / "requests" / "ledger.sqlite3"
    if not path.is_file():
        return []
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        return [request_view(row) for row in db.execute("SELECT * FROM requests ORDER BY rowid DESC").fetchall()]
    finally:
        db.close()


def request_record_response(home, mid, state, text):
    """Bridge-only local persistence; no network, no configuration or execution permission changes."""
    class LocalContext:
        pass
    ctx = LocalContext()
    ctx.home = Path(home)
    return respond_request(ctx, mid, state, text)


def request_row(db, direction, mid):
    return db.execute("SELECT * FROM requests WHERE direction=? AND id=?", (direction, mid)).fetchone()


def queue_response(db, row, state, text, fetch=None, max_bytes=256 * 1024):
    """Caller holds an IMMEDIATE transaction. Persist result and retry envelope together."""
    original = json.loads(row["envelope"])
    seq = row["seq"] + 1
    local_node = json.loads(row["record"])["received"].get("node", original["to"])
    env = {"v": VERSION, "id": secrets.token_hex(8), "from": local_node, "to": row["peer"],
           "ts": iso(now_utc()), "kind": "request-result" if state in REQUEST_FINAL + ("timed_out",) else "request-receipt",
           "text": text, "reply_to": original["id"], "wake": "none", "response": {"state": state, "seq": seq}}
    if original.get("agent") is not None:
        env["from_agent"] = original["agent"]
    if original.get("from_agent") is not None:
        env["agent"] = original["from_agent"]
    if fetch:
        env["fetch"] = fetch
    error = validate_env(env)
    packed = json_canonical(env)
    if error or len(packed.encode("utf-8")) > max_bytes:
        raise ValueError(error or "response exceeds max_msg_bytes; send large content as a file")
    # Lifecycle evidence is written in the same transaction as the state and its outbox response, with the
    # response's own timestamp. The first start is kept through timeout and finalization.
    running, final = state == "running", state in REQUEST_FINAL
    db.execute("UPDATE requests SET state=?,seq=?,latest=?,timed_out_at=COALESCE(timed_out_at,?),"
               "started_at=COALESCE(started_at,?),progress_at=COALESCE(?,progress_at),"
               "final_at=COALESCE(final_at,?),final_response_id=COALESCE(final_response_id,?) "
               "WHERE direction='incoming' AND id=?",
               (state, seq, packed, env["ts"] if state == "timed_out" else None,
                env["ts"] if running else None, env["ts"] if running else None,
                env["ts"] if final else None, env["id"] if final else None, row["id"]))
    if not is_pull_request(original):
        db.execute("INSERT INTO response_outbox(id,request_id,peer,port,envelope) VALUES(?,?,?,?,?)",
                   (env["id"], row["id"], row["peer"], row["port"], packed))
    return env


def save_outgoing_request(ctx, env, peer, port):
    deadline = iso(parse_iso(env["ts"]) + dt.timedelta(seconds=env["request"]["timeout_seconds"]))
    with request_db(ctx) as db:
        db.execute("INSERT INTO requests(direction,id,peer,port,envelope,deadline,state,submission) "
                   "VALUES('outgoing',?,?,?,?,?,'pending','prepared')",
                   (env["id"], peer, port, json_canonical(env), deadline))
        if is_pull_request(env):
            resolved, ip = ctx.resolve(peer)
            ident = fresh_whois(ctx, ip)
            key = stable_peer_id(ident.get("stable_id")) if ident else None
            if resolved != peer or not key or not ctx.access_decision(ident)[0]:
                raise ValueError("pull destination requires an authenticated stable node identity and local authorization")
            db.execute("UPDATE requests SET record=? WHERE direction='outgoing' AND id=?",
                       (json_canonical({"peer_identity": key}), env["id"]))


def accept_request(ctx, rec):
    """Durable receipt precedes HTTP 202. Duplicate ID must have the identical authenticated owner and envelope."""
    env, peer = rec["envelope"], rec["received"]["peer"]["node"]
    packed = json_canonical(env)
    with request_db(ctx) as db:
        db.execute("BEGIN IMMEDIATE")
        row = request_row(db, "incoming", env["id"])
        if row:
            if row["peer"] != peer or row["envelope"] != packed:
                raise ValueError("request ID already belongs to a different sender or envelope")
            return False
        deadline = iso(parse_iso(env["ts"]) + dt.timedelta(seconds=env["request"]["timeout_seconds"]))
        db.execute("INSERT INTO requests(direction,id,peer,port,envelope,record,deadline,state,received_at) "
                   "VALUES('incoming',?,?,?,?,?,?,'pending',?)",
                   (env["id"], peer, env["request"]["reply_port"], packed, json_canonical(rec), deadline,
                    _stamp(rec["received"].get("at"))))
        queue_response(db, request_row(db, "incoming", env["id"]), "received", "Request durably received; execution has not been confirmed.")
        if rec["received"].get("agent_status") == "unknown":
            queue_response(db, request_row(db, "incoming", env["id"]), "rejected", "The addressed agent does not exist on this receiver.")
        elif parse_iso(deadline) <= now_utc():
            queue_response(db, request_row(db, "incoming", env["id"]), "timed_out", "Request deadline already passed on arrival; no execution was started.")
    return True


def accept_response(ctx, env, peer):
    err = validate_env(env)
    if err or "response" not in env:
        raise ValueError(err or "missing response")
    packed = json_canonical(env)
    with request_db(ctx) as db:
        db.execute("BEGIN IMMEDIATE")
        row = request_row(db, "outgoing", env["reply_to"])
        if not row:
            raise ValueError("response does not match an outgoing request")
        original = json.loads(row["envelope"])
        if (peer != row["peer"] or env["from"].lower().rstrip(".") != peer or
                env["to"].lower().rstrip(".") != original["from"].lower().rstrip(".") or
                env.get("from_agent") != original.get("agent") or env.get("agent") != original.get("from_agent")):
            raise ValueError("response sender or agent does not match the original request")
        previous = db.execute("SELECT envelope FROM response_events WHERE id=?", (env["id"],)).fetchone()
        if previous:
            if previous["envelope"] != packed:
                raise ValueError("response ID conflicts with an earlier response")
            return False
        db.execute("INSERT INTO response_events VALUES(?,?,?,?)", (env["id"], env["reply_to"], packed, iso(now_utc())))
        state, seq = env["response"]["state"], env["response"]["seq"]
        # A delayed receipt must never replace a result. timed_out remains explicitly unknown and can
        # be replaced by a later, real result. Real terminal results are immutable.
        if row["state"] not in REQUEST_FINAL and seq > row["seq"] and not (
                row["state"] == "timed_out" and state not in REQUEST_FINAL):
            db.execute("UPDATE requests SET state=?,seq=?,latest=?,submission='accepted',"
                       "timed_out_at=COALESCE(timed_out_at,?) WHERE direction='outgoing' AND id=?",
                       (state, seq, packed, iso(now_utc()) if state == "timed_out" else None, row["id"]))
    return True


def same_result_files(stored, fresh):
    """Attachment identity for an identical retry. Each attempt stages its files under a new outbox token, so
    names, sizes and SHA-256 are compared in order, never the tokened URLs."""
    key = lambda items: [(i.get("name"), i.get("size"), i.get("sha256")) for i in items or []]  # noqa: E731
    return key(stored) == key(fresh)


def respond_request(ctx, mid, state, text, fetch=None):
    if state not in ("running",) + REQUEST_FINAL:
        raise ValueError("invalid response state")
    with request_db(ctx) as db:
        db.execute("BEGIN IMMEDIATE")
        row = request_row(db, "incoming", mid)
        if not row:
            raise ValueError("no tracked incoming request with this ID")
        if row["state"] in REQUEST_FINAL:
            previous = json.loads(row["latest"])
            if row["state"] == state and previous["text"] == text and same_result_files(previous.get("fetch"), fetch):
                return previous
            raise ValueError("request already has a durable final result")
        expired = parse_iso(row["deadline"]) <= now_utc()
        if (row["state"] == "timed_out" or expired) and state == "running":
            raise ValueError("request deadline passed; report an actual final outcome")
        if expired and row["state"] != "timed_out":
            queue_response(db, row, "timed_out", "Response deadline exceeded. Execution outcome is unknown; do not automatically repeat the task.")
            row = request_row(db, "incoming", mid)
        return queue_response(db, row, state, text, fetch, getattr(ctx, "cfg", {}).get("max_msg_bytes", 256 * 1024))


def expire_requests(ctx):
    with request_db(ctx) as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute("SELECT * FROM requests WHERE direction='incoming' AND state IN ('received','running') AND deadline<=?",
                          (iso(now_utc()),)).fetchall()
        for row in rows:
            queue_response(db, row, "timed_out", "Response deadline exceeded. Execution outcome is unknown; do not automatically repeat the task.")
        db.execute("UPDATE requests SET timed_out_at=COALESCE(timed_out_at,?) WHERE direction='outgoing' "
                   "AND state NOT IN ('completed','failed','rejected','blocked') AND deadline<=?",
                   (iso(now_utc()), iso(now_utc())))
    return {"expired": len(rows)}


def request_view(row):
    env = json.loads(row["envelope"])
    latest = json.loads(row["latest"]) if row["latest"] else None
    known = row["state"] in REQUEST_FINAL
    overdue = not known and parse_iso(row["deadline"]) <= now_utc()
    view = {"id": row["id"], "direction": row["direction"], "peer": row["peer"],
            "transport": env.get("request", {}).get("transport", "push"),
            "agent": env.get("agent"), "from_agent": env.get("from_agent"), "port": row["port"],
            "state": row["state"] if known or not overdue else "timed_out", "last_state": row["state"],
            "deadline": row["deadline"], "overdue": overdue, "outcome_known": known,
            "timed_out_at": row["timed_out_at"],
            "late": bool(known and (row["timed_out_at"] or (latest and latest["ts"] > row["deadline"]))),
            "seq": row["seq"], "submission": row["submission"], "response": latest}
    view.update(lifecycle_fields(row))
    return view


def lifecycle_fields(row):
    """Additive, metadata-only lifecycle fields of one ledger row (older ledgers: unknown -> None).

    `state` stays the computed view (an overdue request reads `timed_out` before maintenance persists it);
    `timeout_persisted` says whether that timeout is durable. Only an owner-reported `running` (or a later
    result after it) confirms a start: receipt, queueing or a seen notification never do."""
    keys = row.keys()
    get = lambda key: _stamp(row[key]) if key in keys else None      # an invalid stored time reads as unknown
    started = get("started_at")
    final_id = row["final_response_id"] if "final_response_id" in keys else None
    return {"received_at": get("received_at"), "started_at": started, "progress_at": get("progress_at"),
            "final_at": get("final_at"),
            "final_response_id": final_id if isinstance(final_id, str) and ID_RE.match(final_id) else None,
            "start_confirmed": started is not None or row["state"] == "running",
            "timeout_persisted": bool(row["timed_out_at"])}


def incoming_request_status(ctx, mid, peer):
    snapshot = request_snapshot(ctx.home, mid)
    return snapshot if snapshot and snapshot["peer"] == peer else None


# ---------------------------------------------------------------- read-only lifecycle diagnostics
LEDGER_READ_TIMEOUT = 2.0
METADATA_MAX_IDS = 1000
AGE_SCAN_LIMIT = 5000           # pending responses scanned for their oldest valid timestamp
STAMP_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z"
ERROR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}\Z")


def ledger_reader(home, timeout=LEDGER_READ_TIMEOUT):
    """-> (read-only connection, None) or (None, fixed reason). Never creates the ledger or its directory.

    mode=ro reads committed WAL data, so a running receiver's latest commits are seen. "Read-only" means no
    application data, schema or configuration change; SQLite may still create its own -wal/-shm coordination
    files next to a ledger whose writers have all closed. immutable=1 is deliberately not used: on a live WAL
    database it can miss committed writes."""
    path = Path(home) / "requests" / "ledger.sqlite3"
    try:
        if path.is_symlink() or not path.is_file():
            return None, "ledger_unreadable" if os.path.lexists(str(path)) else "ledger_absent"
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=timeout)
    except (OSError, sqlite3.Error):
        return None, "ledger_unreadable"
    db.row_factory = sqlite3.Row
    return db, None


def ledger_error(exc):
    text = str(exc).lower()
    if "locked" in text or "busy" in text:
        return "ledger_locked"
    if "no such table" in text or "no such column" in text:
        return "ledger_schema"
    if "malformed" in text or "not a database" in text:
        return "ledger_corrupt"
    return "ledger_unreadable"


def _age(stamp, now):
    try:
        return max(0, int((now - parse_iso(stamp)).total_seconds())) if stamp else None
    except (TypeError, ValueError):
        return None


def _epoch_stamp(value):
    try:
        return iso(dt.datetime.fromtimestamp(float(value), dt.timezone.utc).replace(microsecond=0))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def lifecycle_metadata(row, now):
    """Metadata of one incoming ledger row: states, deadline and lifecycle times, never text or attachments."""
    known = row["state"] in REQUEST_FINAL
    overdue = not known and parse_iso(row["deadline"]) <= now
    meta = {"state": row["state"] if known or not overdue else "timed_out", "last_state": row["state"],
            "deadline": row["deadline"], "overdue": overdue, "outcome_known": known, "seq": row["seq"],
            "final_persisted": known, "final_response_delivery": None, "final_response_attempts": None,
            "final_response_next_attempt": None, "final_response_error": None,
            "lifecycle_columns": "started_at" in row.keys()}
    meta.update(lifecycle_fields(row))
    if known and not meta["final_response_id"] and "latest" in row.keys() and row["latest"]:
        try:                                      # a ledger from before final_response_id: exact latest result
            latest = json.loads(row["latest"])
            meta["final_response_id"] = latest["id"] if latest["response"]["state"] == row["state"] else None
        except (ValueError, KeyError, TypeError):
            pass
    if not known:
        meta["final_response_id"] = None
    return meta


def request_metadata(home, ids, now=None):
    """Lifecycle metadata of incoming requests for status and diagnostics.

    Read-only and metadata-only: one mode=ro read transaction covers the requests and the delivery rows of their
    exact final responses (never an earlier receipt), so a concurrent final result is not half-seen. No message
    text, attachment, envelope or peer is returned; no application data is created, migrated, expired or sent
    (SQLite may manage its WAL coordination files, see ledger_reader).
    -> ({id: metadata}, None), or ({}, fixed reason) when the ledger cannot be read."""
    wanted = sorted({i for i in ids if isinstance(i, str) and ID_RE.match(i)})[:METADATA_MAX_IDS]
    db, reason = ledger_reader(home)
    if db is None:
        return {}, reason
    now = now or now_utc()
    out = {}
    try:
        db.execute("BEGIN")
        columns = table_columns(db, "requests")
        # `latest` is read (never returned) so that a final result written by an older runtime, whose lifecycle
        # columns are NULL or absent, still yields its exact final response ID; nothing else is inferred.
        select = ["id", "state", "deadline", "seq", "timed_out_at", "latest"] + \
            [c for c in LIFECYCLE_COLUMNS if c in columns]
        for start in range(0, len(wanted), 200):
            chunk = wanted[start:start + 200]
            for row in db.execute("SELECT %s FROM requests WHERE direction='incoming' AND id IN (%s)"
                                  % (",".join(select), ",".join("?" * len(chunk))), chunk).fetchall():
                try:
                    out[row["id"]] = lifecycle_metadata(row, now)
                except (TypeError, ValueError):
                    continue                     # malformed row: reported as unavailable by the caller
        finals = {}
        for mid, meta in out.items():
            if meta["final_response_id"]:
                meta["final_response_delivery"] = "unknown"   # no matching outbox row: never assumed delivered
                finals.setdefault(meta["final_response_id"], {})[mid] = meta
        keys = sorted(finals)
        for start in range(0, len(keys), 200):
            chunk = keys[start:start + 200]
            for row in db.execute("SELECT id,request_id,attempts,next_attempt,delivered_at,last_error FROM "
                                  "response_outbox WHERE id IN (%s)" % ",".join("?" * len(chunk)), chunk).fetchall():
                meta = finals[row["id"]].get(row["request_id"])
                if meta is None:
                    continue                      # an inconsistent row never confirms another request's result
                meta["final_response_delivery"] = "confirmed" if row["delivered_at"] else "pending"
                meta["final_response_attempts"] = row["attempts"]
                meta["final_response_next_attempt"] = None if row["delivered_at"] else _epoch_stamp(row["next_attempt"])
                error = row["last_error"]
                meta["final_response_error"] = error if error is None or ERROR_NAME_RE.match(str(error)) else "other"
        db.rollback()
    except sqlite3.Error as exc:
        return {}, ledger_error(exc)
    finally:
        db.close()
    return out, None


def request_backlog(home, now=None):
    """Read-only, metadata-only ledger summary for health, doctor and status: counts and ages in one read
    transaction with bounded queries. A ledger that cannot be read is reported with a fixed reason, never as an
    empty backlog. Never creates, migrates, expires, repairs or sends (SQLite may manage its WAL coordination
    files, see ledger_reader)."""
    db, reason = ledger_reader(home)
    if db is None:
        return {"available": False, "reason": reason}
    now = now or now_utc()
    stamp, epoch = iso(now), now.timestamp()
    final = "('completed','failed','rejected','blocked')"
    try:
        db.execute("BEGIN")
        columns = table_columns(db, "requests")
        lifecycle = set(LIFECYCLE_COLUMNS) <= columns
        one = lambda sql, *args: db.execute(sql, args).fetchone()[0]
        incoming = {
            "unstarted": one("SELECT COUNT(*) FROM requests WHERE direction='incoming' AND state='received' "
                             "AND deadline>?", stamp),
            "running": one("SELECT COUNT(*) FROM requests WHERE direction='incoming' AND state='running' "
                           "AND deadline>?", stamp),
            "overdue_not_persisted": one("SELECT COUNT(*) FROM requests WHERE direction='incoming' "
                                         "AND state IN ('received','running') AND deadline<=?", stamp),
            "timed_out": one("SELECT COUNT(*) FROM requests WHERE direction='incoming' AND state='timed_out'"),
            "final": one("SELECT COUNT(*) FROM requests WHERE direction='incoming' AND state IN %s" % final),
            "lifecycle_columns": lifecycle}
        if lifecycle:              # only well-formed stored times take part in the minimum
            incoming["oldest_unstarted_age_seconds"] = _age(one(
                "SELECT MIN(received_at) FROM requests WHERE direction='incoming' AND state='received' "
                "AND deadline>? AND received_at GLOB ?", stamp, STAMP_GLOB), now)
            incoming["oldest_progress_age_seconds"] = _age(one(
                "SELECT MIN(CASE WHEN progress_at GLOB ? THEN progress_at WHEN started_at GLOB ? THEN started_at "
                "END) FROM requests WHERE direction='incoming' AND state='running' AND deadline>?",
                STAMP_GLOB, STAMP_GLOB, stamp), now)
            # The exact final response of the same request: an earlier receipt never counts, nor another
            # request's row. A final result without its outbox row is unknown, never delivered.
            incoming["final_response_pending"] = one(
                "SELECT COUNT(*) FROM requests r JOIN response_outbox o ON o.id=r.final_response_id "
                "AND o.request_id=r.id WHERE r.direction='incoming' AND r.state IN %s "
                "AND o.delivered_at IS NULL" % final)
            incoming["final_response_unknown"] = one(
                "SELECT COUNT(*) FROM requests r LEFT JOIN response_outbox o ON o.id=r.final_response_id "
                "AND o.request_id=r.id WHERE r.direction='incoming' AND r.state IN %s AND o.id IS NULL" % final)
        responses = {
            "pending": one("SELECT COUNT(*) FROM response_outbox WHERE delivered_at IS NULL"),
            "due": one("SELECT COUNT(*) FROM response_outbox WHERE delivered_at IS NULL AND next_attempt<=?", epoch),
            "never_attempted": one("SELECT COUNT(*) FROM response_outbox WHERE delivered_at IS NULL AND attempts=0"),
            "max_attempts": one("SELECT COALESCE(MAX(attempts),0) FROM response_outbox WHERE delivered_at IS NULL"),
            "errors": {}}
        # Oldest by each response's own valid timestamp, not by insertion order (a clock can step back, a row
        # can be malformed). Due and never attempted is the only sign that delivery itself is not running.
        oldest, unattempted, unreadable = None, None, 0
        rows = db.execute("SELECT envelope, attempts, next_attempt FROM response_outbox WHERE delivered_at IS NULL "
                          "LIMIT ?", (AGE_SCAN_LIMIT + 1,)).fetchall()
        for row in rows[:AGE_SCAN_LIMIT]:
            try:
                created = parse_iso(json.loads(row["envelope"])["ts"])
            except (ValueError, KeyError, TypeError):
                unreadable += 1
                continue
            oldest = created if oldest is None else min(oldest, created)
            if row["attempts"] == 0 and row["next_attempt"] <= epoch:
                unattempted = created if unattempted is None else min(unattempted, created)
        responses.update(
            oldest_pending_age_seconds=None if oldest is None else max(0, int((now - oldest).total_seconds())),
            oldest_unattempted_due_age_seconds=None if unattempted is None else
            max(0, int((now - unattempted).total_seconds())),
            unreadable_timestamps=unreadable, age_scan_truncated=len(rows) > AGE_SCAN_LIMIT)
        for error, count in db.execute("SELECT last_error, COUNT(*) FROM response_outbox WHERE delivered_at IS NULL "
                                       "AND last_error IS NOT NULL GROUP BY last_error LIMIT 32").fetchall():
            key = error if ERROR_NAME_RE.match(str(error)) else "other"
            responses["errors"][key] = responses["errors"].get(key, 0) + count
        outgoing = {
            "awaiting_result": one("SELECT COUNT(*) FROM requests WHERE direction='outgoing' AND state NOT IN %s"
                                   % final),
            "submission_unknown": one("SELECT COUNT(*) FROM requests WHERE direction='outgoing' "
                                      "AND submission='unknown' AND state NOT IN %s" % final),
            "overdue": one("SELECT COUNT(*) FROM requests WHERE direction='outgoing' AND state NOT IN %s "
                           "AND deadline<=?" % final, stamp)}
        db.rollback()
    except sqlite3.Error as exc:
        return {"available": False, "reason": ledger_error(exc)}
    finally:
        db.close()
    return {"available": True, "checked_at": stamp, "incoming": incoming, "responses": responses,
            "outgoing": outgoing}


def prepare_pull_request(ctx, env, ident):
    """Reserve an immutable owner/request/manifest before any file is accepted or agent is woken."""
    error = validate_env(env)
    if error or not is_pull_request(env) or not sender_matches(env, ident):
        raise ValueError(error or "pull request sender does not match authenticated node")
    uploads = env["request"].get("uploads", [])
    if sum(item["size"] for item in uploads) > ctx.cfg["max_fetch_bytes"]:
        raise ValueError("request uploads exceed max_fetch_bytes")
    deadline = iso(parse_iso(env["ts"]) + dt.timedelta(seconds=env["request"]["timeout_seconds"]))
    packed, peer = json_canonical(env), peer_identity(ident)
    with request_db(ctx) as db:
        db.execute("BEGIN IMMEDIATE")
        existing = request_row(db, "incoming", env["id"])
        staged = db.execute("SELECT * FROM pull_staging WHERE id=?", (env["id"],)).fetchone()
        for row in (existing, staged):
            if row and (row["peer"] != peer or row["envelope"] != packed):
                raise ValueError("request ID already belongs to a different sender or envelope")
        if existing:
            return False
        if parse_iso(deadline) <= now_utc():
            raise ValueError("request upload deadline has passed")
        if staged:
            return False
        db.execute("INSERT INTO pull_staging VALUES(?,?,?,?)", (env["id"], peer, packed, deadline))
    return True


def pull_upload_manifest(ctx, mid, peer, token=None):
    with request_db(ctx) as db:
        row = db.execute("SELECT * FROM pull_staging WHERE id=? AND peer=?", (mid, peer)).fetchone()
        if not row:
            return None
        env = json.loads(row["envelope"])
        if token is None:
            return env
        # No mutation of request inputs is possible after the request was committed.
        if request_row(db, "incoming", mid) or parse_iso(row["expires"]) <= now_utc():
            return None
        return next((item for item in env["request"].get("uploads", []) if item["token"] == token), None)


def materialize_pull_uploads(ctx, env, ident):
    """Verify durable uploads and stage local inbox files before acknowledging request acceptance."""
    staged = pull_upload_manifest(ctx, env["id"], peer_identity(ident))
    if staged != env:
        raise ValueError("prepare the identical pull request before committing it")
    uploads = env["request"].get("uploads", [])
    if not uploads:
        return
    directory = ctx.inbox / env["id"] / "files"
    private_mkdir(directory)                       # the hard links share the uploads' 0600 inodes
    for item in uploads:
        source = ctx.home / "requests" / "uploads" / env["id"] / item["token"]
        if (not source.is_file() or source.is_symlink() or source.stat().st_size != item["size"] or
                sha256_file(source) != item["sha256"]):
            raise ValueError("request upload is missing or incomplete: " + item["name"])
        destination = directory / item["name"]
        if destination.exists():
            if destination.is_symlink() or destination.stat().st_size != item["size"] or sha256_file(destination) != item["sha256"]:
                raise ValueError("local request attachment conflicts with its immutable manifest")
        else:
            os.link(str(source), str(destination))
    sync_directory(directory)


def recover_request_inbox(ctx):
    """Recover inbox files lost between SQLite commit and materialization; never replay agent execution."""
    with request_db(ctx) as db:
        rows = db.execute("SELECT id,record FROM requests WHERE direction='incoming'").fetchall()
    restored = 0
    for row in rows:
        path = ctx.inbox / (row["id"] + ".json")
        if not path.exists():
            # Never overwrite an ack/wake update if another process materialized the inbox meanwhile.
            temporary = path.with_name(path.name + ".recover-" + secrets.token_hex(4))
            try:
                temporary.write_bytes(row["record"].encode("utf-8"))
                try:
                    os.link(str(temporary), str(path))
                    restored += 1
                except FileExistsError:
                    pass
            finally:
                if temporary.exists():
                    temporary.unlink()
    return {"restored": restored}


BUSY = "busy"                    # a phase found its work already running elsewhere and did nothing
FLUSH_LIMIT = 20                 # due responses considered per pass
FLUSH_BUDGET = 120               # seconds; no new delivery starts after this, the rest waits for the next pass
FLUSH_EXCHANGE_SECONDS = 10      # wall-clock limit of one POST after connecting (a socket timeout is per read)
FLUSH_REPLY_BYTES = 64 * 1024


def flush_responses(ctx, limit=FLUSH_LIMIT, budget=FLUSH_BUDGET, clock=time.monotonic):
    """Retry only immutable protocol responses. Unknown HTTP outcomes reuse the same response ID.

    One flush per data home at a time, across processes (the receiver's worker and `request maintain`): another
    caller gets BUSY and sends nothing. Destinations take turns: each pass visits the least recently attempted
    routes first, tries each route's next due response, continues on that route only while deliveries succeed,
    and leaves it after one failure, so an offline peer's backlog never starves another peer. Work is bounded:
    at most `limit` attempts, none started after `budget` seconds, and a hard deadline per exchange. A delivery
    failure is recorded on its row (backoff, error type only) and is not a maintenance failure. A ledger error
    after a possible POST propagates: the row stays pending with the same ID, so the peer deduplicates it."""
    (ctx.home / "requests").mkdir(mode=0o700, exist_ok=True)
    lock = FileLock(ctx.home / "requests" / "flush.lock")
    if not lock.acquire():
        return BUSY
    try:
        now = time.time()
        with request_db(ctx) as db:
            due = db.execute("SELECT COUNT(*) FROM response_outbox WHERE delivered_at IS NULL AND next_attempt<=?",
                             (now,)).fetchone()[0]
            routes = db.execute("SELECT peer, port FROM response_outbox WHERE delivered_at IS NULL GROUP BY peer, port "
                                "HAVING MIN(next_attempt)<=? ORDER BY MAX(next_attempt), MIN(rowid) LIMIT ?",
                                (now, limit)).fetchall()
        started, attempts, resolved = clock(), 0, {}
        counts = {"due": due, "delivered": 0, "failed": 0}
        for route in routes:
            if attempts >= limit or clock() - started >= budget:
                break
            with request_db(ctx) as db:
                rows = db.execute("SELECT * FROM response_outbox WHERE delivered_at IS NULL AND next_attempt<=? "
                                  "AND peer=? AND port=? ORDER BY next_attempt, rowid LIMIT ?",
                                  (now, route["peer"], route["port"], limit - attempts)).fetchall()
            for row in rows:
                if attempts >= limit or clock() - started >= budget:
                    break
                attempts += 1
                error = deliver_response(ctx, row, resolved)
                with request_db(ctx) as db:
                    db.execute("UPDATE response_outbox SET attempts=attempts+1,next_attempt=?,delivered_at=?,last_error=? WHERE id=? AND delivered_at IS NULL",
                               (time.time() + min(60, 2 ** min(row["attempts"] + 1, 6)),
                                iso(now_utc()) if error is None else None, error, row["id"]))
                counts["failed" if error else "delivered"] += 1
                if error:
                    break                         # one failed attempt per destination per pass
        counts["deferred"] = due - counts["delivered"] - counts["failed"]   # untouched, still due next pass
        return counts
    finally:
        lock.release()


def deliver_response(ctx, row, resolved):
    """POST one stored response to its original authenticated destination -> None, or the error type name."""
    try:
        guard = getattr(ctx, "receiver_identity", None)
        if guard is not None and guard.changed:
            return "receiver_identity_changed"
        if row["peer"] not in resolved:
            resolved[row["peer"]] = ctx.resolve(row["peer"])
        peer, ip = resolved[row["peer"]]
        if peer != row["peer"]:
            raise RuntimeError("response destination identity changed")
        ident = ctx.whois(ip)
        if not ident or ident.get("node") != row["peer"] or not ctx.access_decision(ident)[0]:
            raise RuntimeError("response destination denied by local access policy")
        identity_error = receiver_identity_error(ctx)
        if identity_error:
            return identity_error
        status, body = http_json(ip, row["port"], "POST", "/msg", json.loads(row["envelope"]), host=peer,
                                 timeout=3, deadline=FLUSH_EXCHANGE_SECONDS, limit=FLUSH_REPLY_BYTES)
        if status not in (200, 202) or not isinstance(body, dict) or body.get("ok") is not True or body.get("id") != row["id"]:
            raise RuntimeError("response delivery not confirmed (HTTP %s)" % status)
    except Exception as exc:
        return type(exc).__name__     # avoid storing URLs, message contents or arbitrary remote error bodies
    return None


class PhaseIncomplete(Exception):
    """A maintenance phase finished its bounded pass but some items failed; `code` is a fixed reason."""

    def __init__(self, code, counts=None):
        super().__init__(code)
        self.code, self.counts = code, counts or {}


REQUEST_PHASES = (("recover", lambda ctx: recover_request_inbox(ctx)), ("expire", lambda ctx: expire_requests(ctx)),
                  ("flush", lambda ctx: flush_responses(ctx)))


def run_phase(fn, ctx):
    """-> ("ok" | "busy" | "failed", counts, fixed error code or None). Errors are recorded by type name only."""
    try:
        identity_error = receiver_identity_error(ctx)
        if identity_error:
            raise PhaseIncomplete(identity_error)
        result = fn(ctx)
    except PhaseIncomplete as exc:
        return "failed", exc.counts, exc.code
    except Exception as exc:
        return "failed", {}, type(exc).__name__
    if result == BUSY:
        return "busy", {}, None
    return "ok", result if isinstance(result, dict) else {}, None


def request_maintenance(ctx):
    """One pass of inbox recovery, request expiry and response delivery. Each phase is attempted even when an
    earlier one fails, so a recovery error cannot stop expiry or delivery. -> {phase: outcome, ...}."""
    results = {}
    for name, fn in REQUEST_PHASES:
        outcome, counts, error = run_phase(fn, ctx)
        results[name] = outcome if error is None else "failed:" + error
    return results


# ---------------------------------------------------------------- server
class ReceiverIdentity(object):
    """Pin this process to its startup Tailscale identity; never rebind old work on rename/relogin.

    Each check asks trusted LocalAPI with a one-second subprocess timeout. The bounded lock serializes
    checks, so a slower old answer cannot overwrite a newer change. Unavailable state may recover;
    a confirmed change latches until the operator restarts the receiver with its preserved data home.
    """
    def __init__(self, ctx, initial):
        self.ctx, self.initial = ctx, self.key(initial)
        self.lock = threading.Lock()
        self.changed = False
        self.generation, self.last_error = 0, "receiver_identity_unavailable"

    @staticmethod
    def key(node):
        name, stable, ips = node.get("node"), node.get("stable_id"), node.get("ips")
        if not isinstance(name, str) or not name or not stable_peer_id(stable) or not isinstance(ips, list) or not ips:
            raise ValueError("receiver needs a Tailscale DNS name, StableID and addresses; check Tailscale login")
        return name.rstrip(".").lower(), stable, tuple(sorted(str(ipaddress.ip_address(ip)) for ip in ips))

    def check(self):
        # Concurrent callers may share a check completed after they arrived. A caller arriving
        # after completion always performs a new lookup: there is no cached authorization TTL.
        observed = self.generation
        if not self.lock.acquire(timeout=1.25):
            return "receiver_identity_unavailable"
        try:
            if self.changed:
                return "receiver_identity_changed"
            if self.generation != observed:
                return self.last_error
            error = "receiver_identity_unavailable"
            try:
                status = json.loads(self.ctx.ts("status", "--json", timeout=1))
                if status.get("BackendState") == "Running":
                    own = status["Self"]
                    current = self.key({"node": own.get("DNSName"), "stable_id": own.get("ID"),
                                        "ips": own.get("TailscaleIPs")})
                    self.changed = current != self.initial
                    error = "receiver_identity_changed" if self.changed else None
            except Exception:
                pass
            self.last_error = error
            self.generation += 1
            return error
        finally:
            self.lock.release()


def receiver_identity_error(ctx):
    guard = getattr(ctx, "receiver_identity", None)
    return guard.check() if guard is not None else None


def make_handler(ctx, me):
    cfg = ctx.cfg

    class H(BaseHTTPRequestHandler):
        server_version = "tailmsg/%d" % VERSION
        sys_version = ""

        def log_message(self, fmt, *a):
            log(self.client_address[0], fmt % a)

        def reply(self, code, obj, headers=None):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def check_receiver_identity(self):
            error = receiver_identity_error(ctx)
            if error:
                # Refuse before consuming a message body or applying any stored grant. A client may
                # retry an unavailable lookup with the same ID; changed identity needs operator recovery.
                self.close_connection = True
                detail = ("receiver identity changed; restart the existing receiver and reconcile original request IDs"
                          if error == "receiver_identity_changed" else "receiver identity lookup unavailable")
                self.reply(503, {"ok": False, "error": detail, "code": error}, {"Retry-After": "1"})
                return False
            return True

        def identify(self, fresh=False):
            ip = self.client_address[0]
            if not is_tailnet_ip(ip):
                self.reply(403, {"ok": False, "error": "not a tailnet address"})
                return None
            # A confirmed change stays locked, without spending another peer lookup. Otherwise
            # refresh self identity after the potentially slow peer lookup, before metadata/effects.
            guard = getattr(ctx, "receiver_identity", None)
            if guard is not None and guard.changed:
                self.check_receiver_identity()
                return None
            try:
                ident = fresh_whois(ctx, ip) if fresh else ctx.whois(ip)
            except TailscaleUnavailable:
                self.reply(503, {"ok": False, "error": "identity lookup unavailable",
                                 "code": "identity_unavailable"}, {"Retry-After": "1"})
                return None
            if not isinstance(ident, dict) or not ident.get("node"):
                self.reply(403, {"ok": False, "error": "unknown tailnet peer"})
                return None
            if not self.check_receiver_identity():
                return None
            return ident

        def authorize(self, ident, **options):
            accepted, reason = ctx.access_decision(ident, **options)
            if not accepted:
                error = {"not_allowed": "peer not in allow list", "denied": "peer denied by access policy",
                         "invalid_policy": "invalid access policy", "unknown_peer": "unknown tailnet peer"}[reason]
                log("access rejected:", ident.get("node"), reason)
                self.reply(403, {"ok": False, "error": error})
            return accepted

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/health":
                if not is_tailnet_ip(self.client_address[0]):
                    return self.reply(403, {"ok": False, "error": "not a tailnet address"})
                # 200 whenever the receiver answers (transport reachability); `ok`/`status` carry maintenance
                # readiness, so a degraded receiver is reachable but not reported healthy.
                return self.reply(200, health_payload(ctx, me))
            if path == "/info":
                # Authenticated discovery: Tailscale identity AND current policy (or this machine itself)
                # are checked before any metadata is read or any session name is looked up.
                ident = self.identify()
                if not ident:
                    return
                if not self.authorize(ident, self_discovery=ident["node"] == me):
                    return
                try:
                    body = info_payload(ctx.home, ctx.cfg, ctx.agents, me, ctx.native_name)
                except Exception as e:
                    log("info failed:", type(e).__name__)
                    return self.reply(500, {"ok": False, "error": "info unavailable"})
                return self.reply(200, body)
            if path.startswith("/requests/"):
                ident = self.identify(fresh=True)
                if not ident or not self.authorize(ident):
                    return
                mid = path[len("/requests/"):]
                state = (incoming_request_status(ctx, mid, ident["node"]) or
                         incoming_request_status(ctx, mid, peer_identity(ident))) if ID_RE.fullmatch(mid) else None
                return self.reply(200, {"ok": True, "request": state}) if state else self.reply(404, {"ok": False, "error": "request not found"})
            m = re.match(r"^/f/([^/]+)/([^/]+)$", path)
            if not m:
                return self.reply(404, {"ok": False, "error": "not found"})
            ident = self.identify()
            if not ident:
                return
            if not self.authorize(ident, recipient_token=True):
                return
            token, name = m.group(1), urllib.parse.unquote(m.group(2))
            d = ctx.outbox / token
            if not TOKEN_RE.match(token) or not (d / "meta.json").is_file():
                return self.reply(404, {"ok": False, "error": "not found"})
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
            if meta.get("persistent_request"):
                ident = self.identify(fresh=True)
                if not ident or not self.authorize(ident):
                    return
            if not meta.get("persistent_request") and parse_iso(meta["expires"]) <= now_utc():
                return self.reply(404, {"ok": False, "error": "expired"})
            if meta["to"] not in (ident["node"], peer_identity(ident)):
                return self.reply(404, {"ok": False, "error": "not found"})
            if name not in meta["files"]:
                return self.reply(404, {"ok": False, "error": "not found"})
            fp = d / "files" / name
            size = fp.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", meta["files"][name].get("mime") or "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(fp, "rb") as f:
                shutil.copyfileobj(f, self.wfile, 1 << 20)

        def do_PUT(self):
            match = re.fullmatch(r"/requests/([A-Za-z0-9_-]{8,64})/uploads/([A-Za-z0-9_-]{32,64})",
                                 urllib.parse.urlparse(self.path).path)
            if not match:
                return self.reply(404, {"ok": False, "error": "not found"})
            ident = self.identify(fresh=True)
            if not ident or not self.authorize(ident):
                return
            mid, token = match.groups()
            item = pull_upload_manifest(ctx, mid, peer_identity(ident), token)
            if item is None:
                return self.reply(404, {"ok": False, "error": "upload not found or request already committed"})
            lengths = self.headers.get_all("Content-Length") or []
            if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
                return self.reply(411, {"ok": False, "error": "one Content-Length required"})
            try:
                size = int(lengths[0])
            except ValueError:
                size = -1
            if size != item["size"] or size < 0 or size > cfg["max_fetch_bytes"]:
                return self.reply(413, {"ok": False, "error": "upload length does not match manifest or limit"})
            directory = ctx.home / "requests" / "uploads" / mid
            private_mkdir(directory)
            destination = directory / token
            temporary = directory / (token + ".part-" + secrets.token_hex(8))
            existed, digest, remaining = destination.is_file(), hashlib.sha256(), size
            old_timeout = self.connection.gettimeout()
            try:
                self.connection.settimeout(30)
                with private_open(temporary) as stream:
                    while remaining:
                        chunk = self.rfile.read(min(1 << 20, remaining))
                        if not chunk:
                            raise ValueError("incomplete upload")
                        stream.write(chunk)
                        digest.update(chunk)
                        remaining -= len(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                if digest.hexdigest() != item["sha256"]:
                    raise ValueError("upload sha256 mismatch")
                # Recheck local identity, peer access and reservation after a potentially long upload.
                if not self.check_receiver_identity():
                    return
                try:
                    current = fresh_whois(ctx, self.client_address[0])
                except TailscaleUnavailable:     # PR #17: retryable; the sender resumes the same request
                    self.close_connection = True
                    return self.reply(503, {"ok": False, "error": "identity lookup unavailable",
                                            "code": "identity_unavailable"}, {"Retry-After": "1"})
                if (not current or peer_identity(current) != peer_identity(ident) or not ctx.access_decision(current)[0] or
                        pull_upload_manifest(ctx, mid, peer_identity(current), token) != item):
                    return self.reply(409, {"ok": False, "error": "upload authorization or request state changed"})
                os.replace(temporary, destination)
                sync_directory(directory)
                return self.reply(200 if existed else 201, {"ok": True, "id": mid, "token": token,
                                                          "sha256": item["sha256"], "size": size})
            except (OSError, ValueError) as exc:
                self.close_connection = True
                return self.reply(400, {"ok": False, "error": str(exc) if isinstance(exc, ValueError) else "incomplete upload"})
            finally:
                self.connection.settimeout(old_timeout)
                if temporary.exists():
                    temporary.unlink()

        def do_POST(self):
            route = urllib.parse.urlparse(self.path).path
            if route not in ("/msg", "/requests/prepare"):
                return self.reply(404, {"ok": False, "error": "not found"})
            ident = self.identify(fresh=route == "/requests/prepare")
            if not ident:
                return
            if not self.authorize(ident):
                return
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self.reply(415, {"ok": False, "error": "need application/json"})
            try:
                n = int(self.headers.get("Content-Length") or "-1")
            except ValueError:
                n = -1
            if n < 0:
                return self.reply(411, {"ok": False, "error": "need Content-Length"})
            if n > cfg["max_msg_bytes"]:
                self.close_connection = True
                return self.reply(413, {"ok": False, "error": "message too large (max %d bytes)" % cfg["max_msg_bytes"]})
            raw = self.rfile.read(n)
            try:
                env = json.loads(raw.decode("utf-8"))
            except Exception:
                return self.reply(400, {"ok": False, "error": "invalid json"})
            err = validate_env(env)
            if err:
                return self.reply(400, {"ok": False, "error": err})
            if is_pull_request(env) and route == "/msg":
                ident = self.identify(fresh=True)
                if not ident or not self.authorize(ident):
                    return
            if not sender_matches(env, ident):
                return self.reply(403, {"ok": False, "error": "'from' does not match tailscale identity %s" % ident["node"]})
            if env["to"].lower().rstrip(".") not in (me, me.split(".")[0]):
                return self.reply(400, {"ok": False, "error": "misdirected: I am %s" % me})
            if not self.check_receiver_identity():
                return
            if route == "/requests/prepare":
                try:
                    fresh = prepare_pull_request(ctx, env, ident)
                except ValueError as exc:
                    return self.reply(409, {"ok": False, "error": str(exc)})
                return self.reply(201 if fresh else 200, {"ok": True, "id": env["id"],
                    "committed": request_snapshot(ctx.home, env["id"]) is not None})
            if is_pull_request(env):
                # Persist the authenticated canonical principal, not a name supplied by the sender.
                ident = dict(ident, node=peer_identity(ident))
            with ctx._inbox_lock:
                path = ctx.inbox / (env["id"] + ".json")
                if "response" in env:
                    try:
                        fresh = accept_response(ctx, env, ident["node"])
                    except ValueError as exc:
                        return self.reply(409, {"ok": False, "error": str(exc)})
                    return self.reply(202 if fresh else 200, {"ok": True, "id": env["id"], "duplicate": not fresh})
                if path.exists():
                    previous = json.loads(path.read_text(encoding="utf-8"))
                    if previous.get("received", {}).get("peer", {}).get("node") != ident["node"] or previous.get("envelope") != env:
                        return self.reply(409, {"ok": False, "error": "message ID conflicts with existing message"})
                    return self.reply(200, {"ok": True, "id": env["id"], "duplicate": True})
                requested = env.get("wake", WAKE_DEFAULT)
                agent, astatus = ctx.resolve_agent(env.get("agent"))
                eff, why = ctx.wake_decision(requested, ident, agent=agent, agent_status=astatus)
                rec = {"envelope": env, "received": {"at": iso(now_utc()), "peer": ident, "node": me,
                       "agent": agent, "agent_status": astatus,
                       "wake": {"requested": requested, "effective": eff, "reason": why}}}
                if "request" in env:
                    try:
                        if is_pull_request(env):
                            materialize_pull_uploads(ctx, env, ident)
                        fresh = accept_request(ctx, rec)
                    except ValueError as exc:
                        return self.reply(409, {"ok": False, "error": str(exc)})
                    if not fresh:
                        recover_request_inbox(ctx)
                        return self.reply(200, {"ok": True, "id": env["id"], "duplicate": True})
                atomic_write(path, json.dumps(rec, ensure_ascii=False, indent=2).encode("utf-8"))
                log("message", env["id"], "from", ident["node"] + ("/" + env["from_agent"] if env.get("from_agent") else ""),
                    "to @%s (%s)" % (agent, astatus), "files=%d" % len(
                        env["request"].get("uploads", []) if is_pull_request(env) else env.get("fetch", [])),
                    "wake=%s->%s (%s)" % (requested, eff, why))
                if astatus == "unknown":
                    log("WARNING: message", env["id"], "addressed to unknown agent %r: stored, nobody woken" % agent)
                if eff == "now" and ("request" not in env or request_snapshot(ctx.home, env["id"])["state"] not in REQUEST_FINAL + ("timed_out",)):
                    run_hook(ctx, path)
                return self.reply(202, {"ok": True, "id": env["id"]})

    return H


# ---------------------------------------------------------------- discovery (/info, SPEC §9)
SESSIONS_FILE = "sessions.json"     # explicit agent -> session bindings (written by `yobiru session bind`)
BRIDGE_FILE = "codex-bridge.json"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+=-]{0,127}\Z")
RUNTIME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}\Z")
LOOKUP_CODE_RE = re.compile(r"codex_bridge: ([a-z][a-z_]{2,39})")
NAME_MAX = 200
INFO_MAX_AGENTS = 32
NAME_TTL_OK = 300                   # a native rename shows up after at most this many seconds
NAME_TTL_FAIL = 60
NAME_LOOKUP_TIMEOUT = 15
SMALL_FILE_MAX = 256 * 1024


def clean_text(value, limit=NAME_MAX):
    """Display-safe text: NFC, no control/format/bidi/private-use characters, trimmed and bounded."""
    if not isinstance(value, str):
        return None
    value = "".join(c for c in unicodedata.normalize("NFC", value)
                    if unicodedata.category(c) not in ("Cc", "Cf", "Cs", "Co", "Zl", "Zp"))
    value = " ".join(value.split())
    return value[:limit] or None


def read_small_json(path, limit=SMALL_FILE_MAX):
    """-> (object or None, error or None); refuses symlinks, non-files and oversized files."""
    path = Path(path)
    try:
        if path.is_symlink() or not path.is_file():
            return None, None if not os.path.lexists(str(path)) else "not a regular file"
        if path.stat().st_size > limit:
            return None, "too large"
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, ValueError) as e:
        return None, type(e).__name__


def validate_binding(name, entry):
    """A sessions.json entry -> (normalized binding or None, error or None). Never raises."""
    if not isinstance(entry, dict):
        return None, "binding for %r must be an object" % name
    runtime = entry.get("runtime", "other")
    if not isinstance(runtime, str) or not RUNTIME_RE.match(runtime):
        return None, "binding for %r: bad runtime" % name
    sid = entry.get("session_id")
    if sid is not None:
        if runtime == "codex":
            if not isinstance(sid, str) or not UUID_RE.match(sid):
                return None, "binding for %r: a Codex session id must be a lowercase UUID" % name
        elif not isinstance(sid, str) or not SESSION_ID_RE.match(sid):
            return None, "binding for %r: bad session id" % name
    out = {"runtime": runtime, "session_id": sid}
    for key in ("display_name", "confirmed_title"):
        v = entry.get(key)
        if v is not None and clean_text(v) != v:
            return None, "binding for %r: bad %s" % (name, key)
        out[key] = v
    at = entry.get("confirmed_at")
    out["confirmed_at"] = at if isinstance(at, str) and len(at) <= 32 else None
    return out, None


def load_sessions(home):
    """-> ({agent: binding}, [errors]) from <home>/sessions.json; invalid entries are skipped."""
    raw, err = read_small_json(Path(home) / SESSIONS_FILE)
    if raw is None:
        return {}, ([] if err is None else ["sessions.json: %s" % err])
    agents = raw.get("agents") if isinstance(raw, dict) else None
    if not isinstance(agents, dict):
        return {}, ["sessions.json: agents must be an object"]
    out, errors = {}, []
    for name, entry in agents.items():
        if agent_name_error(name):
            errors.append("sessions.json: bad agent name %r" % name)
            continue
        b, e = validate_binding(name, entry)
        if e:
            errors.append("sessions.json: " + e)
        else:
            out[norm_agent(name)] = b
    return out, errors


def bridge_session(home):
    """The fixed Codex dispatcher session of an installed bridge (agent `codex`), or None."""
    raw, _ = read_small_json(Path(home) / BRIDGE_FILE)
    sid = raw.get("dispatcher_thread_id") if isinstance(raw, dict) else None
    return sid if isinstance(sid, str) and UUID_RE.match(sid) else None


def session_bindings(home, names):
    """{agent: binding} for explicitly bound agents among `names`. The installed Codex bridge is the truth
    for agent `codex`: its dispatcher UUID is reported even when a configured binding disagrees."""
    configured, errors = load_sessions(home)
    bridge = bridge_session(home)
    out = {}
    for name in names:
        b = dict(configured[name]) if name in configured else None
        if name == "codex" and bridge:
            if b is not None and b.get("session_id") not in (None, bridge):
                errors.append("sessions.json: codex binding conflicts with the Codex bridge dispatcher; the "
                              "bridge session is reported")
                b = {"runtime": "codex", "session_id": None, "display_name": None, "confirmed_title": None,
                     "confirmed_at": None}
            b = b or {"runtime": "codex", "session_id": None, "display_name": None, "confirmed_title": None,
                      "confirmed_at": None}
            b.update(runtime="codex", session_id=bridge, id_source="codex-bridge")
        elif b is not None:
            if b.get("session_id") is None:
                continue
            b["id_source"] = "configured"
        else:
            continue
        out[name] = b
    return out, errors


LOOKUP_OUTPUT_MAX = 65536


def run_bounded(argv, timeout, limit=LOOKUP_OUTPUT_MAX):
    """Run argv with empty stdin -> (exit code or None on timeout/overflow, stdout, stderr), each stream capped
    at `limit` bytes. On POSIX the child gets its own process group, and the whole group (for example a Codex
    app-server the child started) is killed on timeout or overflow, so nothing is left behind."""
    posix = os.name == "posix"
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         start_new_session=posix)
    bufs, over = {}, []

    def reader(name, stream):
        data = b""
        while True:
            chunk = stream.read(8192)
            if not chunk:
                break
            if len(data) < limit:
                data += chunk[:limit - len(data)]
            elif not over:
                over.append(name)
                kill()
        bufs[name] = data

    def kill():
        try:
            if posix:
                os.killpg(p.pid, 9)
            else:
                p.kill()
        except OSError:
            pass
    threads = [threading.Thread(target=reader, args=(n, s), daemon=True) for n, s in (("out", p.stdout), ("err", p.stderr))]
    for t in threads:
        t.start()
    try:
        code = p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill()
        p.wait()
        code = None
    if posix and code is not None:
        try:                       # the child exited; stop anything it left running in its group
            os.killpg(p.pid, 9)
        except OSError:
            pass
    for t in threads:
        t.join(5)
    for stream in (p.stdout, p.stderr):
        stream.close()
    if over:
        code = None
    return code, bufs.get("out", b"").decode("utf-8", "replace"), bufs.get("err", b"").decode("utf-8", "replace")


def lookup_native_name(home, binding, timeout=NAME_LOOKUP_TIMEOUT):
    """One read-only native-name lookup. Only the installed Codex bridge offers one: it reads the bound
    thread's metadata (thread/read, includeTurns false) and reports Thread.name, never a preview,
    transcript or another session. No model turn is started."""
    script = Path(home) / "codex-local" / "codex_bridge.py"
    if binding.get("id_source") != "codex-bridge" or not script.is_file():
        return {"status": "not_supported"}
    argv = [sys.executable, str(script), "--config", str(Path(home) / BRIDGE_FILE), "session-name"]
    try:
        code, out, err = run_bounded(argv, timeout)
    except OSError:
        return {"status": "unavailable", "error": "lookup_unavailable"}
    if code is None:
        return {"status": "unavailable", "error": "lookup_timeout"}
    if code != 0:
        m = LOOKUP_CODE_RE.search(err or "")
        return {"status": "unavailable", "error": m.group(1) if m else "lookup_failed"}
    try:
        res = json.loads(out)
    except ValueError:
        return {"status": "unavailable", "error": "lookup_invalid"}
    if not isinstance(res, dict) or res.get("thread_id") != binding.get("session_id"):
        return {"status": "unavailable", "error": "lookup_mismatch"}
    name = clean_text(res.get("name"))
    return {"status": "ok", "name": name} if name else {"status": "unnamed"}


NAME_WAIT = 1.5                     # /info waits at most this long for a cold lookup, then says "pending"


def cached_native_name(home, binding, cache, guard, single, lookup=None, wait=NAME_WAIT):
    """Native name from a per-process cache. A missing or expired entry is refreshed by one background
    lookup at a time; the caller waits at most `wait` seconds and otherwise gets the previous (stale) value
    or status "pending", so discovery stays fast even when the lookup subprocess is slow."""
    if binding.get("id_source") != "codex-bridge":
        # No lookup exists for this source: answer directly and never cache it, so a later bridge
        # binding of the same session id is looked up at once.
        return {"status": "not_supported"}
    key = (binding.get("runtime"), binding.get("session_id"), binding.get("id_source"))
    with guard:
        hit = cache.get(key)
    if hit and hit[0] > time.time():
        return dict(hit[1], cached=True)
    done = threading.Event()

    def refresh():
        try:
            res = (lookup or lookup_native_name)(home, binding)
        except Exception as e:      # a lookup failure is reported, never raised into /info
            res = {"status": "unavailable", "error": "lookup_failed"}
            log("native name lookup failed:", type(e).__name__)
        res["checked_at"] = iso(now_utc())
        ttl = NAME_TTL_OK if res["status"] in ("ok", "unnamed") else NAME_TTL_FAIL
        with guard:
            cache[key] = (time.time() + ttl, res)
        single.release()
        done.set()
    if single.acquire(blocking=False):
        threading.Thread(target=refresh, daemon=True).start()
        done.wait(wait)
        with guard:
            fresh = cache.get(key)
        if fresh and fresh[0] > time.time():
            return dict(fresh[1], cached=False)
    if hit:
        return dict(hit[1], cached=True, stale=True)
    return {"status": "pending"}


def info_agent_names(cfg, agents, limit=INFO_MAX_AGENTS):
    dflt = cfg.get("default_agent")
    names = ([dflt] if dflt is not None and dflt not in agents else []) + list(agents)
    return names[:limit] if limit else names


def session_view(binding, native):
    """Minimal projection of one explicit binding: ids and display metadata only, each with its source."""
    v = {"runtime": binding["runtime"], "id": binding["session_id"], "id_source": binding["id_source"],
         "display_name": binding.get("display_name"), "confirmed_title": binding.get("confirmed_title"),
         "confirmed_at": binding.get("confirmed_at") if binding.get("confirmed_title") else None,
         "native_name": None, "native_name_status": native.get("status", "unavailable"),
         "native_name_checked_at": native.get("checked_at"), "native_name_cached": bool(native.get("cached")),
         "native_name_stale": bool(native.get("stale")), "activity": "unknown"}
    if native.get("status") == "ok":
        v["native_name"] = native.get("name")
    if native.get("error"):
        v["native_name_error"] = native["error"]
    return v


def info_payload(home, cfg, agents, me, native_name):
    """Body of GET /info. Deliberately minimal: product version, node, addressable agent names and, for
    explicitly bound agents only, the session id plus display metadata. Never config values, paths,
    adapter argv, webhook URLs, secrets, message previews, transcripts or account details."""
    total = len(info_agent_names(cfg, agents, limit=None))
    names = info_agent_names(cfg, agents)
    bound, _ = session_bindings(home, names)
    out = []
    for name in names:
        b = bound.get(name)
        out.append({"agent": name, "default": name == cfg.get("default_agent"),
                    "session": session_view(b, native_name(b)) if b else None})
    return {"ok": True, "v": VERSION, "product": PRODUCT, "version": PRODUCT_VERSION, "node": me,
            "capabilities": [REQUEST_CAPABILITY, REQUEST_PULL_CAPABILITY], "agents": out, "agents_total": total, "agents_truncated": total > len(names),
            "generated_at": iso(now_utc())}


# ---------------------------------------------------------------- wake adapters (SPEC §5.8)
HEADER_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")
PREVIEW_CHARS = 200
_record_lock = threading.Lock()


class AdapterError(Exception):
    pass


def adapter_cfg(cfg):
    """-> normalized adapter dict or None. `on_message` is a deprecated alias for type exec."""
    a = cfg.get("wake_adapter")
    if a:
        if not isinstance(a, dict) or a.get("type") not in ("exec", "webhook"):
            raise AdapterError("wake_adapter.type must be 'exec' or 'webhook'")
        if a["type"] == "exec" and not (isinstance(a.get("argv"), list) and a["argv"]):
            raise AdapterError("wake_adapter exec needs a non-empty argv list")
        if a["type"] == "webhook":
            if bool(a.get("url_env")) == bool(a.get("url_file")):
                raise AdapterError("wake_adapter webhook needs exactly one of url_env / url_file")
            if a.get("secret_env") and a.get("secret_file"):
                raise AdapterError("wake_adapter webhook: use only one of secret_env / secret_file")
            if not HEADER_RE.match(str(a.get("header", "Authorization"))):
                raise AdapterError("wake_adapter webhook: bad header name")
        return a
    if cfg.get("on_message"):
        return {"type": "exec", "argv": list(cfg["on_message"]), "_alias": "on_message"}
    return None


def describe_adapter(a):
    """log-safe description: names of env vars / files only, never their values."""
    if not a:
        return "none"
    if a["type"] == "exec":
        return "exec %s%s" % (a["argv"][0], " (via deprecated on_message)" if a.get("_alias") else "")
    src = lambda k: ("env $%s" % a[k + "_env"]) if a.get(k + "_env") else ("file %s" % a[k + "_file"]) if a.get(k + "_file") else "none"
    return "webhook url=%s secret=%s header=%s scheme=%r" % (src("url"), src("secret"), a.get("header", "Authorization"),
                                                              a.get("scheme", "Bearer"))


def read_secret_value(a, key):
    """value of <key>_env or <key>_file; files must not be group/world accessible. Never logged."""
    if a.get(key + "_env"):
        v = os.environ.get(a[key + "_env"])
        if not v:
            raise AdapterError("env var $%s is not set in the serve process" % a[key + "_env"])
        return v.strip()
    if a.get(key + "_file"):
        p = Path(a[key + "_file"]).expanduser()
        try:
            st = p.stat()
        except OSError:
            raise AdapterError("%s_file %s not readable" % (key, p))
        if os.name == "posix" and st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise AdapterError("%s_file %s must be mode 0600 (chmod 600)" % (key, p))
        v = p.read_text(encoding="utf-8").strip()
        if not v:
            raise AdapterError("%s_file %s is empty" % (key, p))
        return v
    return None


def rec_agent(rec, default=None):
    """agent a stored record is for (records from before @agent support: envelope agent or default)."""
    rv = rec.get("received") or {}
    if "agent" in rv:
        return rv["agent"]
    a = (rec.get("envelope") or {}).get("agent")
    return norm_agent(a) if a is not None else default


def wake_payload(rec):
    e = rec["envelope"]
    d = {"v": 1, "event": "tailmsg.wake", "id": e["id"], "from": rec["received"]["peer"]["node"],
         "kind": e.get("kind", "text"), "text_preview": (e.get("text") or "")[:PREVIEW_CHARS],
         "received_at": rec["received"]["at"]}
    agent = rec_agent(rec)
    if agent is not None:      # only when addressed: keeps the payload identical for single-agent setups
        d["agent"] = agent
    if e.get("from_agent"):
        d["from_agent"] = e["from_agent"]
    return d


# The only error codes a webhook diagnostic may name: generic categories, printed in this spelling and never as
# received, so no prefix, suffix or variant of a remote code can carry private text into a log.
WEBHOOK_ERROR_CODES = frozenset((
    "bad_request", "invalid_request", "invalid_payload", "invalid_json", "invalid_hook", "unknown_hook",
    "unauthorized", "invalid_token", "expired_token", "invalid_signature", "forbidden", "not_found",
    "method_not_allowed", "conflict", "payload_too_large", "unsupported_media_type", "rate_limited",
    "too_many_requests", "internal_error", "bad_gateway", "service_unavailable", "gateway_timeout", "timeout"))
WEBHOOK_RETRY_AFTER_RE = re.compile(r"[0-9]{1,5}\Z")
# Likewise the only Retry-After delays it may name: any other number could wrap digits that were sent (a PIN
# inside 15739), so it is left out.
WEBHOOK_RETRY_AFTER_SECONDS = frozenset((1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600))


def webhook_sensitive(url, secret, payload):
    """What a webhook must never get back into logs or inbox records: the secret, each part of the URL that can
    carry a token (credentials, host, path, query, fragment and their segments, decoded too) and the message
    values sent in the payload. Every non-empty value counts, however short. The port is no secret: a number
    that only appears in it does not hide a Retry-After."""
    u = urllib.parse.urlsplit(url)
    values = [secret, u.hostname, u.path, u.query, u.fragment, u.username, u.password]
    values += re.split(r"[/;,&=?#]+", "%s?%s#%s" % (u.path, u.query, u.fragment))
    values += [payload.get(k) for k in ("id", "from", "agent", "from_agent", "text_preview")]
    values += [urllib.parse.unquote(v) for v in list(values) if isinstance(v, str)]
    return sorted({v for v in values if isinstance(v, str) and v}, key=len, reverse=True)


def repeats_sent(value, sensitive):
    """Whether a value from a webhook answer overlaps anything that was sent to it, ignoring case."""
    low = value.lower()
    return any(v.lower() in low or low in v.lower() for v in sensitive)


def webhook_error(status, retry_after, body, sensitive):
    """Bounded, non-secret diagnostics for a non-2xx webhook answer. A webhook can echo anything it received
    (its own path, headers, the message), so no free text from the body is ever kept: only the HTTP status, one
    error code from a JSON body (`code`, `error_code`, `error` or `error.code`) that names a category of the
    fixed WEBHOOK_ERROR_CODES, such as `invalid_hook`, and a Retry-After of the fixed WEBHOOK_RETRY_AFTER_SECONDS,
    each kept only when it repeats nothing that was sent (the Retry-After neither as written nor without its
    leading zeros)."""
    detail = []
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        obj = None
    if isinstance(obj, dict):
        nested = obj.get("error") if isinstance(obj.get("error"), dict) else {}
        for code in (obj.get("code"), obj.get("error_code"), obj.get("error"), nested.get("code")):
            low = code.lower() if isinstance(code, str) else ""
            if low in WEBHOOK_ERROR_CODES and not repeats_sent(low, sensitive):
                detail.append("code " + low)
                break
    seconds = retry_after.strip() if isinstance(retry_after, str) else ""
    if (WEBHOOK_RETRY_AFTER_RE.match(seconds) and int(seconds) in WEBHOOK_RETRY_AFTER_SECONDS
            and not any(repeats_sent(v, sensitive) for v in (seconds, str(int(seconds))))):
        detail.append("retry after %d s" % int(seconds))
    return "HTTP %d" % status + (" (%s)" % ", ".join(detail) if detail else "")


def call_webhook(a, rec, timeout):
    url = read_secret_value(a, "url")
    secret = read_secret_value(a, "secret")
    payload = wake_payload(rec)
    sensitive = webhook_sensitive(url, secret, payload)
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise AdapterError("webhook url must be http(s)://")
    headers = {"Content-Type": "application/json", "User-Agent": "tailmsg/%d" % VERSION}
    if secret:
        if re.search(r"[\r\n]", secret):
            raise AdapterError("secret contains a newline")
        scheme = a.get("scheme", "Bearer")
        headers[a.get("header", "Authorization")] = (scheme + " " + secret) if scheme else secret
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    c = None
    try:
        if u.scheme == "https":
            c = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout, context=ssl.create_default_context())
        else:
            c = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
        c.request("POST", (u.path or "/") + ("?" + u.query if u.query else ""), body=body, headers=headers)
        r = c.getresponse()
        resp = r.read(4096)
    except Exception as e:  # only the type: its text can echo the URL, secret or message, in any case or encoding
        raise AdapterError(type(e).__name__)
    finally:
        if c is not None:
            c.close()
    if not 200 <= r.status < 300:  # redirects are not followed (would forward the secret)
        raise AdapterError(webhook_error(r.status, r.getheader("Retry-After"), resp, sensitive))
    return "HTTP %d" % r.status


def call_exec(a, path, timeout, agent=None):
    env = dict(os.environ, TAILMSG_MSG_ID=Path(path).stem, TAILMSG_MSG_PATH=str(path))
    env.pop("TAILMSG_AGENT", None)
    if agent is not None:
        env["TAILMSG_AGENT"] = agent
    try:
        p = subprocess.run(list(a["argv"]) + [str(path)], capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise AdapterError("timeout after %ss" % timeout)
    except OSError as e:
        raise AdapterError("%s: %s" % (type(e).__name__, e))
    tail = (p.stderr or "").strip()[-300:]
    if p.returncode != 0:
        raise AdapterError("exit %d %s" % (p.returncode, tail))
    return "exit 0" + (" " + tail if tail else "")


def update_record(path, **fields):
    with _record_lock:
        r = json.loads(Path(path).read_text(encoding="utf-8"))
        r.update(fields)
        atomic_write(path, json.dumps(r, ensure_ascii=False, indent=2).encode("utf-8"))


def wake_access_decision(ctx, rec):
    """Revalidate the receiver-recorded sender before invoking a wake adapter, including manual retries."""
    if not isinstance(rec, dict) or not isinstance(rec.get("received"), dict) or \
            not isinstance(rec.get("envelope"), dict):
        return False, "missing_verified_receipt"
    peer = rec["received"].get("peer")
    sender = rec["envelope"].get("from")
    if not isinstance(peer, dict) or not isinstance(peer.get("node"), str) or not isinstance(sender, str) or \
            sender.lower().rstrip(".") != peer["node"].lower().rstrip("."):
        return False, "sender_mismatch"
    return ctx.access_decision(peer)


def deliver_wake(ctx, path):
    """Run the adapter with retries. Returns True on adapter success, even if recording fails.
    Best-effort recording of the outcome (no secrets) goes in the
    inbox record as `wake_delivery`; on give-up the message simply stays in `inbox --pending`."""
    mid = Path(path).stem
    try:
        agent = rec_agent(json.loads(Path(path).read_text(encoding="utf-8")), ctx.cfg.get("default_agent"))
    except Exception:
        agent = ctx.cfg.get("default_agent")
    try:
        a = ctx.agent_adapter(agent)
    except AdapterError as e:
        log("wake adapter for @%s misconfigured:" % agent, e)
        return False
    if not a:
        log("wake", mid, "@%s has no wake adapter; message stays pending" % agent)
        return False
    attempts = max(1, int(ctx.cfg.get("wake_attempts") or 3))
    backoff = float(ctx.cfg.get("wake_backoff_s") if ctx.cfg.get("wake_backoff_s") is not None else 2)
    timeout = ctx.cfg.get("hook_timeout_s") or 120
    err, n, attempted = None, 0, 0
    for n in range(1, attempts + 1):
        try:
            rec = json.loads(Path(path).read_text(encoding="utf-8"))
            if "response" in rec.get("envelope", {}):
                err = "protocol receipts/results must not wake an agent"
                log("wake", mid, "stopped:", err)
                break
            if "request" in rec.get("envelope", {}):
                snapshot = request_snapshot(ctx.home, mid)
                if not snapshot or snapshot["state"] in REQUEST_FINAL + ("timed_out",):
                    err = "tracked request expired, finalized, or missing durable state; execution was not restarted"
                    log("wake", mid, "stopped:", err)
                    break
            allowed, reason = wake_access_decision(ctx, rec)
            if not allowed:
                err = "access policy: " + reason
                log("wake", mid, "stopped before adapter invocation:", reason)
                break
            identity_error = receiver_identity_error(ctx)
            if identity_error:
                err = identity_error
                break
            attempted += 1
            res = call_webhook(a, rec, timeout) if a["type"] == "webhook" else call_exec(a, path, timeout, agent)
        except AdapterError as e:
            err = str(e)[-300:]
        except Exception as e:
            err = type(e).__name__
        else:
            # Notification succeeded. A bookkeeping failure must never send it again.
            log("wake", mid, "@%s" % agent, a["type"], "ok (attempt %d): %s" % (n, res))
            try:
                update_record(path, wake_delivery={"adapter": a["type"], "agent": agent, "ok": True, "attempts": attempted,
                                                   "at": iso(now_utc())})
            except Exception as e:
                log("wake", mid, "adapter succeeded but could not record wake_delivery:", type(e).__name__)
            return True
        log("wake", mid, "@%s" % agent, a["type"], "failed (attempt %d/%d): %s" % (n, attempts, err))
        if n < attempts:
            time.sleep(backoff * 2 ** (n - 1))
    log("wake", mid, "gave up after %d attempts; message stays pending in inbox" % attempted)
    try:
        update_record(path, wake_delivery={"adapter": a["type"], "agent": agent, "ok": False, "attempts": attempted,
                                           "at": iso(now_utc()), "error": err})
    except Exception as e:
        log("could not record wake_delivery:", e)
    return False


def run_hook(ctx, path):
    threading.Thread(target=deliver_wake, args=(ctx, path), daemon=True).start()


def sweep_outbox(ctx):
    """Delete expired attachment grants (one hour after expiry); each entry is handled on its own.

    -> counts. Unexpired grants are kept. An entry without metadata may still be prepared by a send: it is
    kept and counted as incomplete. Unreadable metadata and failed deletions are failures: the pass continues,
    then raises PhaseIncomplete so it is never reported as success. An outbox that cannot be listed raises."""
    now = now_utc()
    counts = {"scanned": 0, "active": 0, "deleted": 0, "incomplete": 0, "failed": 0, "ignored": 0}
    for d in ctx.outbox.iterdir():
        counts["scanned"] += 1
        if d.is_symlink() or not d.is_dir():
            counts["ignored"] += 1
            continue
        if not os.path.lexists(str(d / "meta.json")):
            counts["incomplete"] += 1
            continue
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
            expired = not meta.get("persistent_request") and \
                parse_iso(meta["expires"]) + dt.timedelta(hours=1) < now
        except Exception:
            counts["failed"] += 1
            continue
        if not expired:
            counts["active"] += 1
            continue
        try:
            shutil.rmtree(str(d))
            counts["deleted"] += 1
        except OSError:
            counts["failed"] += 1
    if counts["failed"]:
        raise PhaseIncomplete("attachment_cleanup_failed", counts)
    return counts


# ---------------------------------------------------------------- receiver maintenance supervision
MAINTENANCE_FILE = "maintenance.json"
HEARTBEAT_EVERY = 10              # seconds between routine heartbeat writes; changes are written at once
RESTART_BACKOFF_MAX = 60


def maintenance_workers():
    """name -> (phases, interval seconds, stale_after seconds). Local request lifecycle work and network
    response delivery are separate workers, so a slow or failing peer never delays expiry. stale_after is a
    diagnostic threshold only: a live worker is never restarted or duplicated because of it."""
    return {"requests": (REQUEST_PHASES[:2], 1, 120),
            "responses": (REQUEST_PHASES[2:], 1, 1800),
            "attachments": ((("sweep", lambda ctx: sweep_outbox(ctx)),), 300, 3600)}


class MaintenanceWorker(object):
    def __init__(self, name, phases, interval, stale_after):
        self.name, self.phases, self.interval, self.stale_after = name, tuple(phases), interval, stale_after
        self.thread = None
        self.generation = self.restarts = self.streak = self.cycles = self.failures = 0
        self.next_restart = 0.0          # monotonic; bounded backoff between restarts of a dying worker
        self.started = self.started_at = None
        self.cycle_started = self.cycle_started_at = self.phase = None
        self.outcomes, self.counts, self.last_cycle_at = {}, {}, None
        self.success = self.success_at = None     # end of the last cycle in which every phase succeeded
        self.failure = None              # {"phase", "error", "at"[, "exit"]} until a full cycle succeeds again
        self.exit = None                 # the last unexpected thread exit, kept as history


class Maintenance(object):
    """Supervises the receiver's maintenance workers: request lifecycle (inbox recovery, expiry), response
    delivery and attachment cleanup.

    service_actions() runs on the HTTP serving loop and only inspects real Thread objects: a worker whose thread
    has ended gets one replacement, after a short bounded backoff. A live worker is never restarted or
    duplicated, however long its cycle takes; staleness is reported, not acted on. A cycle attempts every phase
    and is a full success only when all succeed. Workers persist a metadata-only heartbeat (atomic, 0600,
    fsynced); a failed write degrades health until a later write succeeds. Each process has a new instance id,
    so an earlier process's heartbeat is never mistaken for a live worker."""

    def __init__(self, ctx, workers=None, clock=time.monotonic, heartbeat_every=HEARTBEAT_EVERY):
        self.ctx, self.clock, self.heartbeat_every = ctx, clock, heartbeat_every
        self.instance, self.started_at = secrets.token_hex(8), iso(now_utc())
        self.lock, self.write_lock, self.stop = threading.Lock(), threading.Lock(), threading.Event()
        self.path = Path(ctx.home) / MAINTENANCE_FILE
        self.workers = {name: MaintenanceWorker(name, phases, interval, stale_after)
                        for name, (phases, interval, stale_after) in (workers or maintenance_workers()).items()}
        self.heartbeat_failed, self.heartbeat_failures, self.heartbeat_error = False, 0, None
        self.heartbeat_attempt = None

    def start(self):
        self._clean_temporary()
        for worker in self.workers.values():
            self._launch(worker)

    def _launch(self, worker):
        """Start a worker thread. If the thread cannot start (for example a thread limit), the failure is
        recorded and the never-started thread counts as dead, so the supervisor retries with backoff while
        the listener keeps serving and reports itself degraded."""
        with self.lock:
            worker.generation += 1
            worker.started, worker.started_at = self.clock(), iso(now_utc())
            worker.cycle_started = worker.cycle_started_at = worker.phase = None
            worker.thread = threading.Thread(target=self._run, args=(worker,), daemon=True,
                                             name="yobiru-maintenance-" + worker.name)
            thread = worker.thread
        try:
            thread.start()
        except Exception as exc:
            error, at = type(exc).__name__, iso(now_utc())
            with self.lock:
                worker.exit = {"error": error, "at": at}
                worker.failure = {"phase": "start", "error": error, "at": at, "exit": True}
            log("maintenance worker %s could not start: %s" % (worker.name, error))

    def service_actions(self):
        try:
            self.supervise()
        except Exception as exc:          # supervision must never stop the serving loop
            log("maintenance supervision failed:", type(exc).__name__)

    def supervise(self):
        """Restart only workers whose thread has actually ended; never a live one, however stale."""
        if self.stop.is_set():
            return
        for worker in self.workers.values():
            with self.lock:
                now = self.clock()
                if worker.thread is None or worker.thread.is_alive() or now < worker.next_restart:
                    continue
                worker.restarts += 1
                worker.streak += 1
                worker.next_restart = now + min(RESTART_BACKOFF_MAX, 2 ** min(worker.streak, 6))
            log("maintenance worker %s had stopped; starting a replacement (restart %d)" % (worker.name, worker.restarts))
            self._launch(worker)

    def _run(self, worker):
        try:
            while not self.stop.is_set():
                self._cycle(worker)
                if self.stop.wait(worker.interval):
                    break
        except BaseException as exc:      # including SystemExit: recorded; the supervisor decides about a restart
            error, at = type(exc).__name__, iso(now_utc())
            with self.lock:
                worker.exit = {"error": error, "at": at}
                worker.failure = {"phase": worker.phase or "loop", "error": error, "at": at, "exit": True}
                worker.cycle_started = worker.cycle_started_at = worker.phase = None
            log("maintenance worker %s stopped unexpectedly: %s" % (worker.name, error))
            self.persist(force=True)

    def _cycle(self, worker):
        with self.lock:
            worker.cycles += 1
            worker.cycle_started, worker.cycle_started_at = self.clock(), iso(now_utc())
        outcomes, counts, failure = {}, {}, None
        for name, fn in worker.phases:
            with self.lock:
                worker.phase = name
            outcome, result, error = run_phase(fn, self.ctx)
            outcomes[name] = outcome
            if result:
                counts[name] = result
            if error and failure is None:
                failure = {"phase": name, "error": error}
        at = iso(now_utc())
        with self.lock:
            before = worker.failure
            worker.phase = worker.cycle_started = worker.cycle_started_at = None
            changed = worker.outcomes != outcomes
            worker.outcomes, worker.counts, worker.last_cycle_at = outcomes, counts, at
            if failure:
                failure["at"] = at
                worker.failure, worker.failures = failure, worker.failures + 1
            elif all(outcome == "ok" for outcome in outcomes.values()):
                worker.failure, worker.failures, worker.streak = None, 0, 0
                worker.success, worker.success_at = self.clock(), at
        if failure and (before is None or (before["phase"], before["error"]) != (failure["phase"], failure["error"])):
            log("maintenance %s: phase %s failed: %s" % (worker.name, failure["phase"], failure["error"]))
        elif before is not None and worker.failure is None:
            log("maintenance %s: full cycle completed again" % worker.name)
        self.persist(force=changed or (before is None) != (worker.failure is None))

    def persist(self, force=False):
        """Write the metadata heartbeat; routine writes at most every `heartbeat_every` seconds."""
        with self.write_lock:
            now = self.clock()
            if not force and self.heartbeat_attempt is not None and now - self.heartbeat_attempt < self.heartbeat_every:
                return
            self.heartbeat_attempt = now
            try:
                write_private_json(self.path, self.snapshot(detail=True))
            except Exception as exc:
                with self.lock:
                    first = not self.heartbeat_failed
                    self.heartbeat_failed, self.heartbeat_error = True, type(exc).__name__
                    self.heartbeat_failures += 1
                if first:
                    log("maintenance heartbeat could not be written:", type(exc).__name__)
            else:
                with self.lock:
                    recovered, self.heartbeat_failed = self.heartbeat_failed, False
                if recovered:
                    log("maintenance heartbeat written again")

    def _clean_temporary(self):
        """Remove heartbeat temporary files a killed process left behind (older than a minute)."""
        try:
            for path in Path(self.ctx.home).glob("." + MAINTENANCE_FILE + ".*.tmp"):
                try:
                    if time.time() - path.lstat().st_mtime > 60:
                        path.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    @staticmethod
    def _status(worker, now):
        if worker.thread is None or not worker.thread.is_alive():
            return "dead", "worker_dead"
        if worker.failure:
            return "failed", "worker_restarted" if worker.failure.get("exit") else "cycle_failed"
        if worker.success is None:
            return ("starting", None) if now - worker.started <= worker.stale_after else ("stalled", "no_completed_cycle")
        if now - worker.success > worker.stale_after:
            return "stalled", "no_recent_full_cycle"
        return "ok", None

    def snapshot(self, detail=False):
        """Aggregate status (for /health) or, with detail, the metadata heartbeat document. Live state only:
        the actual thread objects and monotonic times of this process."""
        now = self.clock()
        age = lambda t: None if t is None else round(max(0.0, now - t), 1)
        with self.lock:
            components, workers, reasons = {}, {}, []
            for worker in self.workers.values():
                status, reason = self._status(worker, now)
                components[worker.name] = status
                if reason and reason not in reasons:
                    reasons.append(reason)
                if detail:
                    workers[worker.name] = {
                        "status": status, "alive": bool(worker.thread and worker.thread.is_alive()),
                        "generation": worker.generation, "restarts": worker.restarts,
                        "interval_seconds": worker.interval, "stale_after_seconds": worker.stale_after,
                        "thread_started_at": worker.started_at, "cycles": worker.cycles,
                        "in_cycle_since": worker.cycle_started_at, "in_cycle_seconds": age(worker.cycle_started),
                        "phase": worker.phase, "last_cycle_at": worker.last_cycle_at,
                        "last_outcomes": dict(worker.outcomes),
                        "last_counts": {k: dict(v) for k, v in worker.counts.items()},
                        "last_full_success_at": worker.success_at, "last_full_success_age_seconds": age(worker.success),
                        "failure": dict(worker.failure) if worker.failure else None,
                        "consecutive_failures": worker.failures, "last_exit": dict(worker.exit) if worker.exit else None}
            if self.heartbeat_failed:
                reasons.append("heartbeat_write_failed")
            statuses = set(components.values())
            state = "degraded" if statuses & {"dead", "failed", "stalled"} or self.heartbeat_failed else \
                "starting" if "starting" in statuses else "healthy"
            if not detail:
                return {"supported": True, "status": state, "components": components, "reasons": reasons,
                        "instance": self.instance}
            return {"schema": 1, "instance": self.instance, "started_at": self.started_at,
                    "written_at": iso(now_utc()), "status": state, "reasons": reasons, "workers": workers,
                    "heartbeat": {"every_seconds": self.heartbeat_every, "failures": self.heartbeat_failures,
                                  "last_error": self.heartbeat_error}}


def health_payload(ctx, me):
    """GET /health for any tailnet address: receiver liveness, aggregate maintenance status, component states
    and fixed reason codes only. `ok` is false while maintenance is degraded, so a reachable but impaired
    receiver never looks healthy. Timings, counts and backlog stay local (maintenance.json, doctor)."""
    body = {"ok": True, "v": VERSION, "node": me, "receiver_alive": True}
    maintenance = getattr(ctx, "maintenance", None)
    if maintenance is None:
        body.update(status="unknown", maintenance={"supported": False})
    else:
        summary = maintenance.snapshot()
        body.update(ok=summary["status"] != "degraded", status=summary["status"], maintenance=summary)
    if getattr(ctx, "receiver_identity", None) is not None:
        error = receiver_identity_error(ctx)
        body["receiver_identity"] = {"status": error or "ok", "restart_required": error == "receiver_identity_changed"}
        if error:
            body.update(ok=False, status="degraded")
    return body


class NumericHTTPServer(ThreadingHTTPServer):
    """Bind normally, but do not require reverse DNS for HTTP server metadata."""

    def server_bind(self):
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def cmd_serve(ctx, a):
    me = ctx.self_node()
    ctx.receiver_identity = ReceiverIdentity(ctx, me)
    bind = ctx.cfg.get("bind") or ctx.ts("ip", "-4").split()[0]
    port = int(ctx.cfg["port"])
    if ctx.cfg["access_policy"] == "allowlist" and not ctx.cfg["allow"]:
        log("WARNING: allow list empty - every POST /msg will be rejected")
    names = [None] if not ctx.agents and ctx.cfg.get("default_agent") is None else \
        ([ctx.cfg["default_agent"]] if ctx.cfg.get("default_agent") not in (None, *ctx.agents) else []) + list(ctx.agents)
    for n in names:
        try:
            desc = describe_adapter(ctx.agent_adapter(n))
        except AdapterError as e:
            desc = "MISCONFIGURED, wakes will fail: %s" % e
        tag = "" if n is None else " @%s%s" % (n, " (default)" if n == ctx.cfg.get("default_agent") else "")
        log("wake adapter%s:" % tag, desc)
    if ctx.cfg.get("self_agent"):
        log("self_agent (sent as from_agent):", ctx.cfg["self_agent"])
    srv = NumericHTTPServer((bind, port), make_handler(ctx, me["node"]))
    srv.daemon_threads = True
    # Request lifecycle, response delivery and attachment cleanup run as supervised workers; the serving loop
    # restarts only a worker whose thread has ended (see Maintenance).
    ctx.maintenance = Maintenance(ctx)
    srv.service_actions = ctx.maintenance.service_actions
    ctx.maintenance.start()
    log("tailmsg v%d serving %s on %s:%d  home=%s" % (VERSION, me["node"], bind, port, ctx.home))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ctx.maintenance.stop.set()


# ---------------------------------------------------------------- client
class TransportError(RuntimeError):
    """No complete HTTP response was received; a write may already have reached the peer."""


def _expire_exchange(sock, expired):
    expired.set()
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def transport_diagnostic(peer, ip, port, operation, error):
    """Describe the attempted route, without guessing the failed network layer or exposing URLs."""
    endpoint = "[%s]:%s" % (ip, port) if ":" in ip else "%s:%s" % (ip, port)
    # BadStatusLine can contain arbitrary peer output, including an echoed attachment grant URL.
    reason = "invalid or incomplete HTTP response" if isinstance(error, http.client.HTTPException) else str(error)
    reason = "".join(" " if unicodedata.category(c).startswith("C") else c for c in reason[:200])
    return ("%s to %s (%s) failed: %s: %s. Check that the receiver is running on port %s; "
            "check the Tailscale path with `tailscale ping --c 3 --timeout 3s %s` "
            "(a successful ping does not verify the receiver or its access policy)." %
            (operation, peer, endpoint, type(error).__name__, reason, port, shlex.quote(peer)))


def http_json(ip, port, method, path, body=None, host=None, timeout=15, deadline=None, limit=None):
    """`timeout` limits each socket operation, not the exchange: a peer that trickles bytes can hold it longer.
    With `deadline`, the connected socket is shut down that many seconds after connecting and the exchange
    fails even if a usable prefix arrived. With `limit`, a body larger than `limit` bytes, or one that does not
    match its Content-Length, fails: a truncated response is never treated as a complete reply."""
    c = http.client.HTTPConnection(ip, port, timeout=timeout)
    hdrs = {"Host": "%s:%d" % (host or ip, port)}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    timer, expired = None, threading.Event()
    try:
        if deadline is not None:
            c.connect()                       # bounded by `timeout`
            sock = c.sock                     # kept: a `Connection: close` reply detaches c.sock while reading
            timer = threading.Timer(deadline, _expire_exchange, args=(sock, expired))
            timer.daemon = True
            timer.start()
        c.request(method, path, body=data, headers=hdrs)
        r = c.getresponse()
        length = None if r.chunked else r.length      # the declared Content-Length, before reading
        raw = r.read() if limit is None else r.read(limit + 1)
        if expired.is_set():
            raise socket.timeout("exchange deadline exceeded")
        if limit is not None and (len(raw) > limit or (length is not None and len(raw) != length)):
            raise http.client.IncompleteRead(raw[:0], None)
    except (OSError, http.client.HTTPException) as e:
        detail = transport_diagnostic(host or ip, ip, port, "%s %s" % (method, path), e)
        if method == "POST" and path == "/msg":
            detail += (" Delivery is unknown for message %s; the receiver may already have stored it. "
                       "Check its inbox for this ID before sending again; a new send creates a new ID." %
                       (body or {}).get("id", "(unknown)"))
        raise TransportError(detail) from e
    finally:
        if timer is not None:
            timer.cancel()
        c.close()
    try:
        return r.status, json.loads(raw)
    except Exception:
        return r.status, {"raw": raw[:200].decode("utf-8", "replace")}


def split_addr(addr):
    """'host[:port][/agent]' -> ('host[:port]', agent or None)"""
    if "/" in addr:
        host, agent = addr.split("/", 1)
        return host, (agent or None)
    return addr, None


def split_peer(peer, default_port):
    if peer.count(":") == 1:
        h, p = peer.split(":")
        return h, int(p)
    return peer, default_port


def prepare_attachments(ctx, a, env, to_fqdn, persistent_request=None):
    me, msg_id = env["from"], env["id"]
    if a.file:
        if persistent_request and a.ttl_hours is not None:
            raise ValueError("pull result attachments are persistent; --ttl-hours is not supported")
        if persistent_request and (len(a.file) > 100 or
                sum(Path(f).stat().st_size for f in a.file) > ctx.cfg["max_fetch_bytes"]):
            raise ValueError("result attachments exceed count or max_fetch_bytes limit")
        ttl = a.ttl_hours if a.ttl_hours is not None else ctx.cfg["outbox_ttl_hours"]
        expires = iso(now_utc() + dt.timedelta(seconds=int(ttl * 3600)))
        token = secrets.token_urlsafe(32)
        d = ctx.outbox / token
        private_mkdir(d / "files")
        try:
            items = stage_attachment_files(ctx, a, d, me, msg_id, to_fqdn, token, expires, persistent_request)
        except BaseException:
            shutil.rmtree(d, ignore_errors=True)       # nothing refers to a copy that was never completed
            raise
        env["fetch"] = items
        return d
    return None


def stage_attachment_files(ctx, a, d, me, msg_id, to_fqdn, token, expires, persistent_request):
    """Snapshot each file under outbox/<token>/files and write meta.json -> the fetch entries."""
    meta = {"id": msg_id, "to": to_fqdn, "expires": expires, "files": {}}
    if persistent_request:
        meta["persistent_request"] = persistent_request
    items = []
    copied_bytes = 0
    for f in a.file:
        src = Path(f)
        if not src.is_file():
            die("not a file: %s" % f)
        name = safe_name(src.name)
        while name in meta["files"]:
            name = "_" + name
        dst = d / "files" / name
        if persistent_request:
            with open(src, "rb") as reader, private_open(dst) as writer:
                while True:
                    chunk = reader.read(1 << 20)
                    if not chunk:
                        break
                    copied_bytes += len(chunk)
                    if copied_bytes > ctx.cfg["max_fetch_bytes"]:
                        raise ValueError("result attachments exceed max_fetch_bytes")
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
        else:
            with open(src, "rb") as reader, private_open(dst) as writer:
                shutil.copyfileobj(reader, writer)  # snapshot: hash stays valid even if the original changes
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        item = {"name": name, "size": dst.stat().st_size, "sha256": sha256_file(dst), "mime": mime,
                "url": "http://%s:%d/f/%s/%s" % (me, int(ctx.cfg["port"]), token, urllib.parse.quote(name)),
                "expires": expires}
        if persistent_request:
            del item["expires"]
        meta["files"][name] = item
        items.append(item)
    if persistent_request:
        sync_directory(d / "files")
    (durable_write if persistent_request else atomic_write)(
        d / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"))
    return items


def prepare_pull_attachments(ctx, a, env):
    """Snapshot the attachments in requests/sending/<id>, a new directory that is returned (None without files).
    Any failure removes it again: no ledger row, printed ID or resume path refers to it yet."""
    if a.ttl_hours is not None:
        raise ValueError("pull requests use their request deadline for uploads; --ttl-hours is not supported")
    if len(a.file) > 100:
        raise ValueError("at most 100 request attachments are allowed")
    directory = ctx.home / "requests" / "sending" / env["id"]
    names, items, total = set(), [], 0
    env["request"]["uploads"] = items
    if not a.file:
        return None
    private_mkdir(directory.parent)
    try:
        directory.mkdir(mode=0o700)          # an exclusive claim: never adopt (or later remove) another attempt's
    except FileExistsError:
        raise ValueError("request attachment directory already exists: " + env["id"])
    try:
        for filename in a.file:
            source = Path(filename)
            if not source.is_file():
                raise ValueError("not a file: " + str(filename))
            name = safe_name(source.name)
            while name in names:
                name = "_" + name
                if len(name) > 200:
                    name = secrets.token_hex(4) + name[-192:]
            names.add(name)
            token = secrets.token_urlsafe(32)
            target = directory / token
            digest, size = hashlib.sha256(), 0
            with open(source, "rb") as reader, private_open(target) as writer:
                while True:
                    chunk = reader.read(1 << 20)
                    if not chunk:
                        break
                    size += len(chunk)
                    if total + size > ctx.cfg["max_fetch_bytes"]:
                        raise ValueError("request attachments exceed max_fetch_bytes")
                    digest.update(chunk)
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            total += size
            items.append({"token": token, "name": name, "sha256": digest.hexdigest(), "size": size,
                          "mime": mimetypes.guess_type(name)[0] or "application/octet-stream"})
        sync_directory(directory)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return directory


def discard_unsaved_pull_snapshots(ctx, mid, staged, saving):
    """Remove the new attachment snapshots of a pull request that was not saved. Before the save is attempted
    nothing durable can refer to them. After a failed save, only a readable ledger without the outgoing row
    proves that; a saved request keeps them for resume, and a missing or unreadable ledger keeps them too."""
    if staged is None:
        return
    if saving:
        try:
            path = Path(ctx.home) / "requests" / "ledger.sqlite3"
            db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                saved = db.execute("SELECT 1 FROM requests WHERE direction='outgoing' AND id=?", (mid,)).fetchone()
            finally:
                db.close()
        except Exception as exc:
            log("kept request attachment snapshots; could not check the request ledger:", type(exc).__name__)
            return
        if saved:
            return
    shutil.rmtree(staged, ignore_errors=True)


def upload_request_file(ctx, env, item, ip, port, peer):
    source = ctx.home / "requests" / "sending" / env["id"] / item["token"]
    if (not source.is_file() or source.is_symlink() or source.stat().st_size != item["size"] or
            sha256_file(source) != item["sha256"]):
        raise ValueError("saved request attachment is missing or changed: " + item["name"])
    # Hashing a large snapshot takes time: check the pinned destination again right before connecting, so a
    # private input never goes to whoever holds the address now.
    verify_pull_destination(ctx, env, peer, ip)
    connection = http.client.HTTPConnection(ip, port, timeout=30)
    try:
        with open(source, "rb") as stream:
            connection.request("PUT", "/requests/%s/uploads/%s" % (env["id"], item["token"]), stream,
                               headers={"Host": "%s:%d" % (peer, port), "Content-Type": "application/octet-stream",
                                        "Content-Length": str(item["size"])})
            response = connection.getresponse()
            raw = response.read(64 * 1024)
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if (response.status not in (200, 201) or not isinstance(body, dict) or body.get("ok") is not True or
                body.get("id") != env["id"] or body.get("token") != item["token"] or
                body.get("sha256") != item["sha256"] or body.get("size") != item["size"]):
            raise RuntimeError("request attachment upload not confirmed (HTTP %s)" % response.status)
    except (OSError, http.client.HTTPException) as exc:
        raise TransportError(transport_diagnostic(peer, ip, port, "request attachment upload", exc)) from exc
    finally:
        connection.close()


def verify_pull_destination(ctx, env, peer, ip):
    """The destination pinned when the request was saved, checked again before each new connection: the
    address must still belong to that stable Tailscale identity and local policy must still allow it, and
    this machine must still be the request's sender. Each upload and the final commit opens a new
    connection, and the identity behind the address can change between them."""
    identity = fresh_whois(ctx, ip)
    if not identity or identity.get("node") != peer or not ctx.access_decision(identity)[0]:
        raise RuntimeError("request destination denied by local identity/access policy")
    with request_db(ctx) as db:
        saved = request_row(db, "outgoing", env["id"])
        pinned = json.loads(saved["record"] or "{}").get("peer_identity") if saved else None
    if pinned != peer_identity(identity):
        raise RuntimeError("request destination stable identity changed")
    if stable_peer_id(ctx.self_node().get("stable_id")) != env["from"]:
        raise RuntimeError("request sender stable identity changed")


def submit_pull_request(ctx, env, peer, ip, port):
    verify_pull_destination(ctx, env, peer, ip)
    try:
        status, prepared = http_json(ip, port, "POST", "/requests/prepare", env, host=peer)
        if (status not in (200, 201) or not isinstance(prepared, dict) or prepared.get("ok") is not True or
                prepared.get("id") != env["id"]):
            raise RuntimeError("request preparation not confirmed (HTTP %s): %s" %
                               (status, prepared.get("error", "invalid response") if isinstance(prepared, dict) else "invalid response"))
        if not prepared.get("committed"):
            for item in env["request"].get("uploads", []):
                upload_request_file(ctx, env, item, ip, port, peer)   # checks the destination before connecting
        verify_pull_destination(ctx, env, peer, ip)
        status, body = http_json(ip, port, "POST", "/msg", env, host=peer)
    except (RuntimeError, OSError, ValueError) as exc:
        with request_db(ctx) as db:
            db.execute("UPDATE requests SET submission='unknown' WHERE direction='outgoing' AND id=?", (env["id"],))
        raise RuntimeError("pull request %s remains resumable with `request resume %s`: %s" %
                           (env["id"], env["id"], exc)) from exc
    with request_db(ctx) as db:
        accepted = status in (200, 202) and isinstance(body, dict) and body.get("ok") is True and body.get("id") == env["id"]
        db.execute("UPDATE requests SET submission=? WHERE direction='outgoing' AND id=?",
                   ("accepted" if accepted else "unknown" if status in (200, 202) else "rejected", env["id"]))
    return status, body


def cmd_send(ctx, a, reply_to=None, peer=None, agent=None):
    addr, addr_agent = split_addr(peer or a.peer)
    opt_agent = getattr(a, "agent", None)
    if addr_agent and opt_agent and norm_agent(addr_agent) != norm_agent(opt_agent):
        die("conflicting agent: %r in address vs --agent %r" % (addr_agent, opt_agent))
    agent = opt_agent or addr_agent or agent
    from_agent = getattr(a, "as_agent", None)
    implicit_from = from_agent is None
    if implicit_from:
        from_agent = ctx.cfg.get("self_agent")
    for k, v in (("agent", agent), ("from_agent", from_agent)):
        if v is not None and agent_name_error(v):
            die("bad %s %r: %s" % (k, v, agent_name_error(v)))
    self_node = ctx.self_node()
    me = self_node["node"]
    host, port = split_peer(addr, int(ctx.cfg["port"]))
    to_fqdn, to_ip = ctx.resolve(host)
    tracked = a.kind == "request" and not getattr(a, "legacy_request", False)
    pull = getattr(a, "transport", "push") == "pull"
    if pull and not tracked:
        die("--transport pull requires a tracked request; it cannot be combined with --legacy-request")
    if tracked:
        try:
            status, info = http_json(to_ip, port, "GET", "/info", host=to_fqdn, timeout=5,
                                     deadline=5, limit=64 * 1024)
        except TransportError as exc:
            die("request preflight transport failure; no request created: %s" % exc)
        if status in (401, 403):
            die("request preflight access denied (HTTP %d); check authenticated identity and receiver policy; no request created" % status)
        if status == 503 and isinstance(info, dict) and info.get("code") in (
                "identity_unavailable", "receiver_identity_unavailable", "receiver_identity_changed"):
            die("request preflight %s; repair the receiver identity before retrying; no request created" % info["code"])
        if status != 200:
            die("request preflight HTTP %d from /info; receiver capability is unverified; no request created" % status)
        if not isinstance(info, dict) or not isinstance(info.get("node"), str) or not info["node"]:
            die("request preflight malformed /info response (missing node); no request created")
        if info["node"] != to_fqdn:
            die("request preflight receiver identity mismatch; /info does not match the resolved target; restart/reconcile the receiver; no request created")
        if not isinstance(info.get("capabilities"), list) or not all(isinstance(c, str) for c in info["capabilities"]):
            die("request preflight malformed /info capabilities; no request created")
        if REQUEST_CAPABILITY not in info["capabilities"]:
            die("peer does not advertise request-lifecycle-v1; upgrade it or explicitly use --legacy-request (no receipt/result guarantee)")
        if pull and REQUEST_PULL_CAPABILITY not in info["capabilities"]:
            die("peer does not advertise request-pull-v1; upgrade it before using --transport pull")
        timeout_seconds = getattr(a, "timeout_seconds", 900)
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 604800:
            die("--timeout-seconds must be 1..604800")
    if pull:
        me = stable_peer_id(self_node.get("stable_id"))
        if not me:
            die("Tailscale did not return this machine's StableID; pull requests cannot authenticate safely")
    msg_id = secrets.token_hex(8)  # hex: never starts with "-"
    env = {"v": VERSION, "id": msg_id, "from": me, "to": to_fqdn, "ts": iso(now_utc()),
           "kind": a.kind, "text": a.text}
    if tracked:
        env["request"] = {"timeout_seconds": timeout_seconds, "reply_port": int(ctx.cfg["port"])}
        if pull:
            env["request"]["transport"] = "pull"
    if reply_to or a.reply_to:
        env["reply_to"] = reply_to or a.reply_to
    if a.wake:  # omitted unless given, so receivers older than the wake field still accept it
        env["wake"] = a.wake
    if agent is not None:  # omitted unless addressed: older receivers reject unknown fields
        env["agent"] = norm_agent(agent)
    if from_agent is not None:
        env["from_agent"] = norm_agent(from_agent)
    staged, saving = None, False
    if pull:
        staged = prepare_pull_attachments(ctx, a, env)
    else:
        prepare_attachments(ctx, a, env, to_fqdn)
    try:
        error = validate_env(env)
        if error:
            die(error)
        if tracked:
            if staged is not None:
                request_db(ctx).close()      # exists before the save, so a failed save can be told from a commit
            saving = True
            save_outgoing_request(ctx, env, to_fqdn, port)
    except BaseException:
        discard_unsaved_pull_snapshots(ctx, msg_id, staged, saving)
        raise
    try:
        status, resp = submit_pull_request(ctx, env, to_fqdn, to_ip, port) if pull else \
            http_json(to_ip, port, "POST", "/msg", env, host=to_fqdn)
    except (RuntimeError, OSError):
        if tracked:
            with request_db(ctx) as db:
                db.execute("UPDATE requests SET submission='unknown' WHERE direction='outgoing' AND id=?", (msg_id,))
        raise
    if tracked:
        with request_db(ctx) as db:
            db.execute("UPDATE requests SET submission=? WHERE direction='outgoing' AND id=?",
                       ("accepted" if status in (200, 202) and isinstance(resp, dict) and resp.get("ok") is True and resp.get("id") == msg_id
                        else "unknown" if status in (200, 202) else "rejected", msg_id))
    if status == 400 and not tracked and implicit_from and "from_agent" in env and "from_agent" in str(resp.get("error", "")):
        # receiver predates @agent: from_agent only came from config, so drop it and resend (same id;
        # the rejected attempt was not stored). An explicit --agent / --as is never dropped silently.
        print("note: %s does not understand from_agent (old tailmsg); resent without it" % to_fqdn, file=sys.stderr)
        del env["from_agent"]
        status, resp = http_json(to_ip, port, "POST", "/msg", env, host=to_fqdn)
    out = {"status": status, "response": resp, "id": msg_id, "to": to_fqdn,
           "files": len(env["request"].get("uploads", [])) if pull else len(env.get("fetch", []))}
    if pull:
        out["transport"] = "pull"
    if "agent" in env:
        out["agent"] = env["agent"]
    if tracked:
        out["submission"] = request_snapshot(ctx.home, msg_id, "outgoing")["submission"]
    print(json.dumps(out, ensure_ascii=True))
    if status not in (200, 202) or (tracked and out["submission"] != "accepted"):
        sys.exit(2)


def load_msg(ctx, mid):
    # ids (or unique id prefixes) only: never a path or glob that could reach outside the inbox
    if not MSG_REF_RE.match(mid):
        die("bad message id %r (expected letters, digits, '_' or '-')" % mid)
    p = ctx.inbox / (mid + ".json")
    if not p.is_file():
        hits = [x for x in ctx.inbox.glob(mid + "*.json")]
        if len(hits) != 1:
            ledger = ctx.home / "requests" / "ledger.sqlite3"
            if ledger.is_file():
                db = sqlite3.connect(ledger.resolve().as_uri() + "?mode=ro", uri=True)
                db.row_factory = sqlite3.Row
                try:
                    rows = db.execute("SELECT e.* FROM response_events e WHERE substr(e.id,1,?)=?", (len(mid), mid)).fetchall()
                finally:
                    db.close()
                if len(rows) == 1:
                    env = json.loads(rows[0]["envelope"])
                    return p, {"envelope": env, "received": {"at": rows[0]["received_at"],
                        "peer": {"node": env["from"]}, "agent": env.get("agent"),
                        "wake": {"requested": "none", "effective": "none", "reason": "protocol response"}}}
            die("no unique message matching %r" % mid)
        p = hits[0]
    return p, json.loads(p.read_text(encoding="utf-8"))


def eff_wake(r):
    return ((r.get("received") or {}).get("wake") or {}).get("effective", WAKE_DEFAULT)


def cmd_inbox(ctx, a):
    rows = []
    want = norm_agent(a.agent) if getattr(a, "agent", None) else None
    dflt = ctx.cfg.get("default_agent")
    for p in ctx.inbox.glob("*.json"):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        w, acked = eff_wake(r), bool(r.get("acked_at"))
        if a.pending and (w == "none" or acked):
            continue
        ag = rec_agent(r, dflt)
        if want is not None and ag != want:
            continue
        if (r.get("received") or {}).get("agent_status") == "unknown":
            ag = "%s?" % ag
        e = r["envelope"]
        attachments = e["request"].get("uploads", []) if is_pull_request(e) else e.get("fetch", [])
        nf = len(attachments)
        got = sum((ctx.inbox / e["id"] / "files" / safe_name(f.get("name") or "file")).is_file() for f in attachments)
        wd = r.get("wake_delivery") or {}
        mark = ("" if acked else "*") + ("!" if wd and not wd.get("ok") else "")
        frm = e["from"].split(".")[0] + ("/" + e["from_agent"] if e.get("from_agent") else "")
        if getattr(a, "json", False):
            # the same rows as the table, unabridged where it truncates; text stays the sender's data
            rows.append((r["received"]["at"], e["id"], {
                "id": e["id"], "received_at": r["received"]["at"], "from": e["from"],
                "from_agent": e.get("from_agent"), "agent": rec_agent(r, dflt),
                "agent_known": (r.get("received") or {}).get("agent_status") != "unknown",
                "kind": e.get("kind"), "tracked_request": "request" in e, "wake": w, "acked": acked,
                "wake_failed": bool(wd) and not wd.get("ok"), "files": nf, "files_fetched": got,
                "reply_to": e.get("reply_to"), "text_preview": (e.get("text") or "")[:INBOX_PREVIEW]}))
            continue
        rows.append((r["received"]["at"], e["id"], frm, "@" + ag if ag else "-", w + mark, nf, got,
                     e.get("reply_to") or "", (e.get("text") or "").replace("\n", " ")[:60]))
    if getattr(a, "json", False):
        print(json.dumps([row[2] for row in sorted(rows, key=lambda row: row[:2])], ensure_ascii=True, indent=2))
        return
    for at, mid, frm, ag, w, nf, got, rt, txt in sorted(rows):
        files = "%d/%d files" % (got, nf) if nf else ""
        local = parse_iso(at).astimezone().strftime("%m-%d %H:%M %Z")
        print("%s  %-16s %-20s %-10s %-6s %-10s %s%s" % (local, mid, frm, ag, w, files,
                                                        ("re:" + rt + " ") if rt else "", txt))
    if not rows:
        print("(no pending messages)" if a.pending else "(inbox empty)")
    elif not a.pending:
        print("(* = not acked; ! = wake adapter failed; wake column is the effective level; @x? = unknown agent)")


def cmd_ack(ctx, a):
    for mid in a.id:
        p, r = load_msg(ctx, mid)
        if "response" in r["envelope"]:
            die("protocol receipts/results are already recorded; ack is not applicable")
        if "request" in r["envelope"]:
            state = request_snapshot(ctx.home, r["envelope"]["id"])
            if not state or state["last_state"] not in REQUEST_FINAL + ("timed_out",):
                die("tracked request needs a durable final result before ack; use request respond or reply")
        if not r.get("acked_at"):
            r["acked_at"] = iso(now_utc())
            atomic_write(p, json.dumps(r, ensure_ascii=False, indent=2).encode("utf-8"))
        print("acked", r["envelope"]["id"])


def cmd_wake(ctx, a):
    """re-run the wake adapter for a stored message (manual retry / adapter test), in the foreground."""
    p, r = load_msg(ctx, a.id)
    if "response" in r["envelope"]:
        die("protocol receipts/results must not wake an agent")
    agent = rec_agent(r, ctx.cfg.get("default_agent"))
    if not ctx.agent_adapter(agent):
        die("no wake adapter configured for @%s" % agent if agent is not None else "no wake_adapter configured")
    if not deliver_wake(ctx, p):
        sys.exit(2)
    print("woke", p.stem)


def cmd_show(ctx, a):
    _, r = load_msg(ctx, a.id)
    print(json.dumps(r, ensure_ascii=True, indent=2))


def cmd_fetch(ctx, a):
    _, r = load_msg(ctx, a.id)
    env, sender = r["envelope"], r["received"]["peer"]["node"]
    out = ctx.inbox / env["id"] / "files"
    private_mkdir(out)
    if is_pull_request(env):
        for item in env["request"].get("uploads", []):
            path = out / item["name"]
            if (not path.is_file() or path.is_symlink() or path.stat().st_size != item["size"] or
                    sha256_file(path) != item["sha256"]):
                raise ValueError("received request attachment is missing or changed: " + item["name"])
            print("OK    %s  %d bytes  sha256 verified -> %s" % (item["name"], item["size"], path))
        return
    ok = True
    for f in env.get("fetch", []):
        name = safe_name(f.get("name") or Path(urllib.parse.urlparse(f["url"]).path).name)
        dst = out / name
        u = urllib.parse.urlparse(f["url"])
        try:
            if (u.hostname or "").lower() != sender or u.scheme != "http":
                raise RuntimeError("url host is not the verified sender %s" % sender)
            if f["size"] > ctx.cfg["max_fetch_bytes"]:
                raise RuntimeError("declared size exceeds max_fetch_bytes")
            if dst.is_file() and sha256_file(dst) == f["sha256"]:
                print("OK    %s (already fetched)" % name)
                continue
            _, ip = ctx.resolve(u.hostname)
            if not is_tailnet_ip(ip):
                raise RuntimeError("resolved to non-tailnet ip %s" % ip)
            if env.get("response") and env.get("reply_to"):
                with request_db(ctx) as db:
                    original = request_row(db, "outgoing", env["reply_to"])
                if original and is_pull_request(json.loads(original["envelope"])):
                    ident = fresh_whois(ctx, ip)
                    pinned = json.loads(original["record"] or "{}").get("peer_identity")
                    if not ident or pinned != peer_identity(ident) or not ctx.access_decision(ident)[0]:
                        raise RuntimeError("result attachment destination identity or authorization changed")
            c = http.client.HTTPConnection(ip, u.port or 80, timeout=30)
            try:
                try:
                    c.request("GET", u.path, headers={"Host": u.netloc})
                    resp = c.getresponse()
                    if resp.status != 200:
                        raise RuntimeError("HTTP %d %s" % (resp.status, resp.read(200).decode("utf-8", "replace")))
                except (OSError, http.client.HTTPException) as e:
                    raise TransportError(transport_diagnostic(sender, ip, u.port or 80, "file fetch", e)) from e
                h, n = hashlib.sha256(), 0
                part = dst.with_name(dst.name + ".part")
                with private_open(part, truncate=True) as w:
                    while True:
                        try:
                            chunk = resp.read(1 << 20)
                        except (OSError, http.client.HTTPException) as e:
                            raise TransportError(transport_diagnostic(sender, ip, u.port or 80, "file fetch", e)) from e
                        if not chunk:
                            break
                        n += len(chunk)
                        if n > f["size"]:
                            break
                        h.update(chunk)
                        w.write(chunk)
            finally:
                c.close()
            if n != f["size"] or h.hexdigest() != f["sha256"]:
                part.unlink()
                raise RuntimeError("sha256/size mismatch - discarded")
            os.replace(part, dst)
            print("OK    %s  %d bytes  sha256 verified -> %s" % (name, n, dst))
        except Exception as e:
            ok = False
            print("FAIL  %s  %s" % (name, e))
    if not ok:
        sys.exit(2)


def staged_result_referenced(ctx, request_id, token, since_seq=None):
    """Whether a durable response of this incoming request may refer to the staged outbox token. The ledger is
    opened read-only and nothing is created. Only a readable ledger with the original request's row, whose
    responses all parse and name other copies, proves the copy unused; anything else counts as referenced.
    since_seq is the response sequence seen before staging: any response committed since then may have been
    this attempt's, published and already replaced as latest (pull responses keep no other record)."""
    path = Path(ctx.home) / "requests" / "ledger.sqlite3"
    if not path.is_file():
        return True
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        row = db.execute("SELECT latest,seq FROM requests WHERE direction='incoming' AND id=?",
                         (request_id,)).fetchone()
        if row is None or (since_seq is not None and row[1] != since_seq):
            return True
        packed = ([row[0]] if row[0] else []) + [r[0] for r in db.execute(
            "SELECT envelope FROM response_outbox WHERE request_id=?", (request_id,))]
    finally:
        db.close()
    marker = "/f/%s/" % token
    for value in packed:
        try:
            fetch = json.loads(value).get("fetch", [])
            urls = [item["url"] for item in fetch]
        except (ValueError, TypeError, KeyError, AttributeError):
            return True
        if any(not isinstance(url, str) or marker in url for url in urls):
            return True
    return False


def discard_unreferenced_staging(ctx, request_id, staged, since_seq=None):
    """Remove the copy this attempt staged, but only once the ledger proves that no durable response refers to
    it. A missing, unreadable or malformed ledger keeps the copy: a response may already refer to it."""
    if staged is None:
        return
    try:
        if staged_result_referenced(ctx, request_id, staged.name, since_seq):
            return
    except Exception as exc:
        log("kept staged result files; could not check the request ledger:", type(exc).__name__)
        return
    shutil.rmtree(staged, ignore_errors=True)


def respond_with_files(ctx, rec, a, state):
    original = rec["envelope"]
    snapshot = request_snapshot(ctx.home, original["id"])
    if not snapshot:
        raise ValueError("no tracked incoming request with this ID")
    envelope = {"id": original["id"], "from": snapshot["response"]["from"]}
    staged = prepare_attachments(ctx, a, envelope, rec["received"]["peer"]["node"],
                                 persistent_request=original["id"] if is_pull_request(original) else None)
    try:
        result = respond_request(ctx, original["id"], state, a.text, envelope.get("fetch"))
    except BaseException:
        # A refused or failed response leaves this copy unreferenced, unless the failure came after the durable
        # commit: the ledger decides, not the exception. Once any response was committed after the snapshot, a
        # requester may already have this copy's URLs although a later response replaced them as latest.
        discard_unreferenced_staging(ctx, original["id"], staged, since_seq=snapshot["seq"])
        raise
    if result.get("fetch") != envelope.get("fetch"):
        # An identical retry returned the stored response, which keeps its own copy and URLs.
        discard_unreferenced_staging(ctx, original["id"], staged)
    return result


def cmd_reply(ctx, a):
    _, r = load_msg(ctx, a.id)
    if "response" in r["envelope"]:
        die("protocol receipts/results do not accept replies; send a new request if needed")
    if "request" in r["envelope"]:
        if getattr(a, "agent", None) or getattr(a, "as_agent", None) or a.port:
            die("tracked reply uses original authenticated sender and agent routing; route overrides are not allowed")
        result = respond_with_files(ctx, r, a, "completed")
        detail = {"stored": True, "transport": "pull"} if is_pull_request(r["envelope"]) else {"queued": True}
        print(json.dumps(dict(detail, id=r["envelope"]["id"], response_id=result["id"], state="completed")))
        return
    # route back to the sender's agent (from_agent) unless --agent overrides
    cmd_send(ctx, a, reply_to=r["envelope"]["id"], peer=r["received"]["peer"]["node"] +
             (":%d" % a.port if a.port else ""), agent=r["envelope"].get("from_agent"))


def cmd_request(ctx, a):
    if a.request_cmd == "send":
        a.kind, a.reply_to = "request", None
        return cmd_send(ctx, a)
    if a.request_cmd == "resume":
        if not ID_RE.fullmatch(a.id):
            die("bad request id")
        with request_db(ctx) as db:
            row = request_row(db, "outgoing", a.id)
            if not row:
                die("no outgoing request with this ID")
            env, peer, port = json.loads(row["envelope"]), row["peer"], row["port"]
        if not is_pull_request(env):
            die("request resume is only supported for pull requests")
        resolved, ip = ctx.resolve(peer)
        if resolved != peer:
            die("request destination identity changed")
        status, response = submit_pull_request(ctx, env, peer, ip, port)
        snapshot = request_snapshot(ctx.home, a.id, "outgoing")
        print(json.dumps({"id": a.id, "status": status, "response": response,
                          "submission": snapshot["submission"], "transport": "pull"}))
        if snapshot["submission"] != "accepted":
            sys.exit(2)
        return
    if a.request_cmd == "respond":
        _, rec = load_msg(ctx, a.id)
        mid = rec["envelope"]["id"]
        result = respond_with_files(ctx, rec, a, a.state)
        detail = {"stored": True, "transport": "pull"} if is_pull_request(rec["envelope"]) else {"queued": True}
        print(json.dumps(dict(detail, id=mid, response_id=result["id"], state=a.state)))
        return
    if a.request_cmd == "maintain":
        phases = request_maintenance(ctx)
        failed = sorted(name for name, outcome in phases.items() if outcome.startswith("failed"))
        # busy: the receiver (or another maintain) is delivering responses right now; nothing was sent twice.
        print(json.dumps({"ok": not failed, "complete": all(o == "ok" for o in phases.values()), "phases": phases}))
        if failed:
            sys.exit(1)
        return
    if a.request_cmd == "list":
        output = request_snapshots(ctx.home)
    else:
        output = request_snapshot(ctx.home, a.id, a.direction)
        if not output:
            die("no tracked %s request with this ID" % a.direction)
    if a.request_cmd == "status" and a.refresh:
        if a.direction != "outgoing":
            die("--refresh is only for outgoing requests")
        try:
            peer, ip = ctx.resolve(output["peer"])
            if peer != output["peer"]:
                raise RuntimeError("request destination identity changed")
            identity = fresh_whois(ctx, ip) if output.get("transport") == "pull" else ctx.whois(ip)
            if not identity or identity.get("node") != peer or not ctx.access_decision(identity)[0]:
                raise RuntimeError("request destination denied by local identity/access policy")
            if output.get("transport") == "pull":
                with request_db(ctx) as db:
                    row = request_row(db, "outgoing", a.id)
                    pinned = json.loads(row["record"] or "{}").get("peer_identity")
                if pinned != peer_identity(identity):
                    raise RuntimeError("request destination stable identity changed")
            status, body = http_json(ip, output["port"], "GET", "/requests/" + a.id, host=peer, timeout=5)
            if status != 200 or not isinstance(body, dict):
                raise RuntimeError("receiver status unavailable (HTTP %s); outcome remains unknown" % status)
            remote_request = body.get("request")
            result = remote_request.get("response") if isinstance(remote_request, dict) else None
            if not isinstance(result, dict) or result.get("reply_to") != a.id:
                raise RuntimeError("receiver returned no matching request response")
            accept_response(ctx, result, peer)
            with request_db(ctx) as db:
                output = request_view(request_row(db, "outgoing", a.id))
            output["refresh"] = "ok"
        except (RuntimeError, OSError, ValueError) as exc:
            output["refresh"] = "unavailable"
            output["refresh_error"] = str(exc)
    if a.json:
        print(json.dumps(output, ensure_ascii=True, indent=2))
    else:
        for item in output if isinstance(output, list) else [output]:
            print("%s %s %s state=%s deadline=%s outcome=%s%s" %
                  (item["id"], item["direction"], item["peer"], item["state"], item["deadline"],
                   "known" if item["outcome_known"] else "unknown", " (late result)" if item["late"] else ""))
            if item.get("response"):
                print(item["response"]["text"])
            if item.get("refresh_error"):
                print(item["refresh_error"])


def cmd_peers(ctx, a):
    st = ctx.status()
    port = int(ctx.cfg["port"])
    peers = list((st.get("Peer") or {}).values())
    res = {}

    def probe(n):
        ip = (n.get("TailscaleIPs") or ["?"])[0]
        try:
            s, j = http_json(ip, port, "GET", "/health", host=(n.get("DNSName") or ip).rstrip("."), timeout=3)
            res[ip] = "yes (v%s)" % j.get("v") if s == 200 else "HTTP %d" % s
        except TransportError as e:
            res[ip] = "no response: %s" % e
        except Exception:
            res[ip] = "no"
    ths = [threading.Thread(target=probe, args=(n,)) for n in peers if n.get("Online")]
    [t.start() for t in ths]
    [t.join() for t in ths]
    print("%-16s %-28s %-8s %-8s %s" % ("IP", "NAME", "OS", "ONLINE", "TAILMSG:%d" % port))
    for n in sorted(peers, key=lambda n: (not n.get("Online"), n.get("DNSName", ""))):
        ip = (n.get("TailscaleIPs") or ["?"])[0]
        print("%-16s %-28s %-8s %-8s %s" % (ip, (n.get("DNSName") or "").split(".")[0], n.get("OS", ""),
                                            "online" if n.get("Online") else "offline",
                                            res.get(ip, "-") + ("  " + ",".join(n.get("Tags") or []))))


def main():
    configure_cli_output()
    ap = argparse.ArgumentParser(prog="tailmsg", description="minimal P2P messages over Tailscale")
    ap.add_argument("--home", default=os.environ.get("TAILMSG_HOME", "~/.tailmsg"))
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("serve", help="run receiver + file server on the Tailscale IP")
    for name in ("send", "reply"):
        p = sp.add_parser(name, help="send a message" if name == "send" else "reply to a received message")
        if name == "send":
            p.add_argument("peer", help="hostname | fqdn | tailscale ip, optional :port, optional /agent "
                                        "(e.g. dev-server/codex)")
        else:
            p.add_argument("id")
            p.add_argument("--port", type=int)
        p.add_argument("text")
        p.add_argument("--file", action="append", default=[])
        p.add_argument("--kind", default="text")
        p.add_argument("--ttl-hours", type=float)
        p.add_argument("--wake", choices=WAKE_LEVELS,
                       help="requested wake level (default: omitted = batch); receiver may downgrade")
        p.add_argument("--agent", help="address this agent on the receiver (reply: default = original from_agent)")
        p.add_argument("--as", dest="as_agent", metavar="NAME",
                       help="my agent name, sent as from_agent (default: config self_agent; omitted if unset)")
        if name == "send":
            p.add_argument("--reply-to")
            p.add_argument("--timeout-seconds", type=int, default=900)
            p.add_argument("--legacy-request", action="store_true", help="opt out of request lifecycle for an old peer")
            p.add_argument("--transport", choices=("push", "pull"), default="push")
    pr = sp.add_parser("request", help="tracked requests, receipts, results and timeout status")
    rs = pr.add_subparsers(dest="request_cmd", required=True)
    rq = rs.add_parser("send")
    rq.add_argument("peer")
    rq.add_argument("text")
    rq.add_argument("--timeout-seconds", type=int, default=900)
    rq.add_argument("--legacy-request", action="store_true")
    rq.add_argument("--transport", choices=("push", "pull"), default="push",
                    help="pull sends files and polls results without any reverse connections")
    rq.add_argument("--file", action="append", default=[])
    rq.add_argument("--ttl-hours", type=float)
    rq.add_argument("--wake", choices=WAKE_LEVELS)
    rq.add_argument("--agent")
    rq.add_argument("--as", dest="as_agent")
    rs.add_parser("resume", help="retry the same saved pull request and immutable attachments").add_argument("id")
    rq = rs.add_parser("status")
    rq.add_argument("id")
    rq.add_argument("--direction", choices=("incoming", "outgoing"), default="outgoing")
    rq.add_argument("--refresh", action="store_true")
    rq.add_argument("--json", action="store_true")
    rs.add_parser("list").add_argument("--json", action="store_true")
    rq = rs.add_parser("respond")
    rq.add_argument("id")
    rq.add_argument("text")
    rq.add_argument("--state", choices=("running",) + REQUEST_FINAL, default="completed")
    rq.add_argument("--file", action="append", default=[])
    rq.add_argument("--ttl-hours", type=float)
    rs.add_parser("maintain", help="one pass: inbox recovery, request expiry and response delivery "
                                   "(each attempted; delivery reports busy while the receiver delivers)")
    pi = sp.add_parser("inbox", help="list received messages")
    pi.add_argument("--pending", action="store_true", help="only un-acked messages with effective wake now/batch")
    pi.add_argument("--agent", help="only messages for this agent (no `agent` in message = default_agent)")
    pi.add_argument("--json", action="store_true", help="one JSON array, oldest first (text cut to %d characters)"
                    % INBOX_PREVIEW)
    sp.add_parser("ack", help="mark message(s) as processed").add_argument("id", nargs="+")
    for name in ("show", "fetch"):
        sp.add_parser(name).add_argument("id")
    sp.add_parser("wake", help="re-run the wake adapter for a message (manual retry)").add_argument("id")
    sp.add_parser("peers", help="online peers and whether they run tailmsg")
    a = ap.parse_args()
    if a.cmd == "reply":
        a.reply_to = None
    try:
        if a.cmd == "request" and a.request_cmd in ("status", "list") and not getattr(a, "refresh", False):
            ctx = object.__new__(Ctx)
            ctx.home = Path(a.home).expanduser()
        else:
            ctx = Ctx(a.home)
        {"serve": cmd_serve, "send": cmd_send, "reply": cmd_reply, "inbox": cmd_inbox, "show": cmd_show,
         "fetch": cmd_fetch, "peers": cmd_peers, "ack": cmd_ack, "wake": cmd_wake, "request": cmd_request}[a.cmd](ctx, a)
    except (RuntimeError, OSError, AdapterError, ValueError) as e:
        die(str(e))


if __name__ == "__main__":
    main()
