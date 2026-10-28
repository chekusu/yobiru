#!/usr/bin/env python3
"""Prepare a visible Claude bridge; preview by default, no service activation."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import sys
import time
import uuid
import unicodedata

sys.dont_write_bytecode = True
from claude_bridge import (BridgeError, Bridge, PERMISSIONS, atomic_json, empty_state,
                           read_json, validate_config, writer_lock)

SOURCE = Path(__file__).resolve().parent


def package_files(plugin):
    files = {}
    for path in sorted(plugin.rglob("*")):
        if path.is_symlink():
            raise BridgeError("plugin contains a symlink")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in (".pyc", ".pyo") and path.name != ".DS_Store":
            files[path.relative_to(plugin).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    for required in ("plugin.json", "scripts/yobiru.py", "scripts/tailmsg.py", "scripts/claude_bridge/claude_bridge.py"):
        if required not in files:
            raise BridgeError("complete packaged plugin required: " + required)
    return files


def adapter(cfg):
    return {"type": "exec", "argv": [cfg["python"], "-B", cfg["entry"], "--config",
                                     str(Path(cfg["bridge_home"]) / "config.json"), "wake"]}


def service_templates(cfg):
    root = Path(cfg["bridge_home"])
    label = "dev.yobiru.claude-bridge." + cfg["installation_id"][:8]
    command = [cfg["python"], "-B", cfg["entry"], "--config", str(root / "config.json"), "daemon"]
    plist = plistlib.dumps({"Label": label, "ProgramArguments": command, "WorkingDirectory": cfg["workdir"],
                           "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 60,
                           "StandardOutPath": str(root / "logs" / "stdout.log"),
                           "StandardErrorPath": str(root / "logs" / "stderr.log")})
    def quote(value):
        return '"' + value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'
    unit = ("[Unit]\nDescription=Yobiru visible Claude Remote Control dispatcher\n\n[Service]\nType=simple\n"
            "WorkingDirectory=" + quote(cfg["workdir"]) + "\nExecStart=" + " ".join(quote(x).replace("$", "$$") for x in command) +
            "\nRestart=on-failure\nRestartSec=60\n\n[Install]\nWantedBy=default.target\n")
    return {label + ".plist": plist, label + ".service": unit.encode("utf-8")}


def install(args):
    if os.name != "posix":
        raise BridgeError("Claude visible bridge currently supports macOS and Linux only")
    source = Path(args.plugin_root).expanduser().resolve()
    home = Path(args.tailmsg_home).expanduser().resolve()
    target = Path(args.bridge_home).expanduser().resolve() if args.bridge_home else home / "claude-visible"
    if target == source or source in target.parents or home == source or source in home.parents:
        raise BridgeError("data/bridge home must be outside the plugin package")
    for name in ("versions", "backups", "wake", "logs", "services"):
        if (target / name).is_symlink():
            raise BridgeError("managed bridge directories must not be symlinks")
    original = read_json(home / "config.json")
    cfg_path = target / "config.json"
    existing = read_json(cfg_path) if cfg_path.exists() or cfg_path.is_symlink() else None
    if existing is not None:
        validate_config(existing)
        Bridge(existing).load_state()  # missing/corrupt ownership cannot be reset by reinstalling
        if Path(existing["bridge_home"]) != target or Path(existing["tailmsg_home"]) != home:
            raise BridgeError("existing bridge identity cannot be moved by reinstalling")
    elif (target / "state.json").exists():
        raise BridgeError("orphaned bridge state exists; reconcile it before installation")
    agent = unicodedata.normalize("NFC", args.agent or (existing or {}).get("agent", "claude"))
    claude = args.claude_path or (existing or {}).get("claude_path")
    workdir = args.workdir or (existing or {}).get("workdir")
    if not claude or not workdir:
        raise BridgeError("first install needs --claude-path and --workdir")
    claude, workdir = Path(claude).expanduser().resolve(), Path(workdir).expanduser().resolve()
    if not claude.is_file() or not os.access(str(claude), os.X_OK) or not workdir.is_dir():
        raise BridgeError("Claude executable and trusted working directory must exist")
    if existing and any(existing[key] != value for key, value in
                        (("agent", agent), ("claude_path", str(claude)), ("workdir", str(workdir)))):
        raise BridgeError("existing agent/executable/workdir binding differs; review instead of replacing it")
    files = package_files(source)
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    release = target / "versions" / digest
    installed_plugin = release / "yobiru"
    if release.exists() and package_files(installed_plugin) != files:
        raise BridgeError("installed immutable package differs; preserve it for inspection")
    install_id = existing["installation_id"] if existing else str(uuid.uuid4())
    cfg = dict(existing or {})
    cfg.update(version=1, installation_id=install_id, bridge_home=str(target), tailmsg_home=str(home),
               agent=agent, claude_path=str(claude), workdir=str(workdir), plugin_root=str(installed_plugin),
               instructions=(existing or {}).get("instructions", str(target / "CLAUDE_DISPATCHER.md")), python=str(Path(sys.executable).resolve()),
               entry=str(installed_plugin / "scripts" / "claude_bridge" / "claude_bridge.py"))
    cfg["permission_mode"] = args.permission_mode or cfg.get("permission_mode", "manual")
    if args.model is not None:
        cfg["model"] = args.model
    validate_config(cfg)
    changed = copy.deepcopy(original)
    agents = changed.setdefault("agents", {})
    if not isinstance(agents, dict):
        raise BridgeError("invalid Yobiru agents configuration")
    matches = [name for name in agents if isinstance(name, str) and unicodedata.normalize("NFC", name) == agent]
    if len(matches) > 1:
        raise BridgeError("duplicate normalized agent names; review configuration first")
    agent_key = matches[0] if matches else agent
    current = agents.setdefault(agent_key, {})
    if current is None:
        current = agents[agent_key] = {}
    if not isinstance(current, dict):
        raise BridgeError("invalid Yobiru agent specification")
    previous_adapter = current.get("wake_adapter")
    default_agent = original.get("default_agent")
    if isinstance(default_agent, str):
        default_agent = unicodedata.normalize("NFC", default_agent)
    if not previous_adapter and agent == default_agent:
        previous_adapter = original.get("wake_adapter")
        if not previous_adapter and original.get("on_message"):
            raise BridgeError("an inherited legacy wake adapter is already configured; it was preserved")
    if previous_adapter and (not existing or previous_adapter != adapter(existing)):
        raise BridgeError("a different wake adapter is already configured; it was preserved")
    current["wake_adapter"] = adapter(cfg)
    templates = service_templates(cfg)
    plan = {"applied": False, "starts_service": False, "launches_claude": False,
            "bridge_home": str(target), "agent": agent, "permission_mode": cfg["permission_mode"],
            "model": cfg.get("model"),
            "package_sha256": digest, "package_files": len(files), "keeps_state": bool(existing),
            "instructions": "preserve existing local instructions; create template only when absent",
            "services": [str(target / "services" / name) for name in templates]}
    if not args.apply:
        return plan
    os.umask(0o077)
    target.mkdir(parents=True, mode=0o700, exist_ok=True)
    if target.stat().st_mode & 0o077:
        raise BridgeError("bridge home must be private (mode 0700)")
    with writer_lock(target):
        # Re-check source configuration under the writer lock before changing it.
        if read_json(home / "config.json") != original:
            raise BridgeError("Yobiru configuration changed during preview; retry after reviewing it")
        if existing and read_json(cfg_path) != existing:
            raise BridgeError("bridge configuration changed during preview")
        if not existing and (cfg_path.exists() or (target / "state.json").exists()):
            raise BridgeError("another installation appeared; review it first")
        backup = target / "backups" / (time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])
        backup.mkdir(parents=True, mode=0o700)
        shutil.copy2(str(home / "config.json"), str(backup / "tailmsg-config.json"))
        if existing:
            shutil.copy2(str(cfg_path), str(backup / "bridge-config.json"))
        for path in backup.iterdir():
            path.chmod(0o600)
        if not release.exists():
            release.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            staging = release.with_name("." + digest + ".staging-" + uuid.uuid4().hex)
            staging.mkdir(mode=0o700)
            staged_plugin = staging / "yobiru"
            # Copy only reviewed regular package files. Publish the complete directory atomically.
            staged_plugin.mkdir(mode=0o700)
            for name in files:
                destination = staged_plugin / name
                destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                shutil.copy2(str(source / name), str(destination))
            if package_files(staged_plugin) != files:
                raise BridgeError("package changed while copied; configuration not switched")
            os.rename(str(staging), str(release))
        for directory in ("wake", "logs", "services"):
            (target / directory).mkdir(mode=0o700, exist_ok=True)
        instructions = target / "CLAUDE_DISPATCHER.md"
        if instructions.is_symlink():
            raise BridgeError("instructions must not be a symlink")
        if not instructions.exists():
            instructions.write_bytes((source / "scripts" / "claude_bridge" / "CLAUDE_DISPATCHER.md").read_bytes())
            instructions.chmod(0o600)
        for name, data in templates.items():
            path = target / "services" / name
            if path.is_symlink():
                raise BridgeError("service template must not be a symlink")
            path.write_bytes(data)
            path.chmod(0o600)
        if not existing:
            atomic_json(target / "state.json", empty_state(install_id))
        atomic_json(cfg_path, cfg)
        atomic_json(home / "config.json", changed)
        plan.update(applied=True, backup=str(backup))
        return plan


def main(argv=None):
    default_plugin = SOURCE / "plugins" / "yobiru" if (SOURCE / "plugins" / "yobiru").is_dir() else SOURCE.parent.parent
    ap = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    ap.add_argument("--tailmsg-home", required=True)
    ap.add_argument("--plugin-root", default=str(default_plugin))
    ap.add_argument("--bridge-home")
    ap.add_argument("--agent")
    ap.add_argument("--claude-path")
    ap.add_argument("--workdir")
    ap.add_argument("--permission-mode", choices=PERMISSIONS)
    ap.add_argument("--model", metavar="ID", help="session model alias or full ID; omitted upgrades preserve the saved selection")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    try:
        print(json.dumps(install(args), ensure_ascii=True, indent=2))
        return 0
    except (BridgeError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc), "starts_service": False}), file=sys.stderr)
        return 2

if __name__ == "__main__":
    sys.exit(main())
