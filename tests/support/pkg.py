"""Build the real release archive, unpack it outside the source checkout and drive it in isolation."""
import hashlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SUPPORT = Path(__file__).resolve().parent


def python_env(values):
    """Keep isolated test settings and the selected Linux interpreter's loader path."""
    env = dict(values)
    if sys.platform.startswith("linux") and os.environ.get("LD_LIBRARY_PATH"):
        env.setdefault("LD_LIBRARY_PATH", os.environ["LD_LIBRARY_PATH"])
    return env


def load_builder():
    spec = importlib.util.spec_from_file_location("build_plugin", str(REPO / "tools" / "build_plugin.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert port != 7766
    return port


def snapshot(path, ignore_macos_caches=False):
    """{relative path: sha256 or 'dir'} under path (missing path -> None).

    Fake HOME snapshots may ignore macOS framework caches created by Python
    itself. Keep other Library entries and all package snapshots visible.
    """
    path = Path(path)
    if not path.exists():
        return None
    out = {}
    for p in sorted(path.rglob("*")):
        rel = p.relative_to(path).as_posix()
        if ignore_macos_caches and sys.platform == "darwin":
            # The Library directory is scaffolding; only its Caches subtree is
            # runtime noise. Files or symlinks at either name remain visible.
            if rel in ("Library", "Library/Caches") and p.is_dir() and not p.is_symlink():
                continue
            if rel.startswith("Library/Caches/"):
                continue
        out[rel] = "dir" if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest()
    return out


class Package(object):
    """The plugin archive unpacked into '<tmp>/Plugin Cache/with spaces/yobiru', far from the checkout."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="yobiru test ")
        self.root = Path(self._tmp.name).resolve()
        builder = load_builder()
        self.product = builder.load_product()
        name = "%s-%s-plugin.zip" % (self.product["id"], self.product["version"])
        self.zip_bytes = builder.archives(self.product)[name]
        self.zip_path = self.root / name
        self.zip_path.write_bytes(self.zip_bytes)
        self.cache = self.root / "Plugin Cache" / "with spaces"
        with zipfile.ZipFile(str(self.zip_path)) as z:      # like most unzip tools: no file modes
            z.extractall(str(self.cache))
        self.plugin = self.cache / self.product["id"]
        self.launcher = self.plugin / "scripts" / "yobiru"
        self.entry = self.plugin / "scripts" / "yobiru.py"
        self.core = self.plugin / "scripts" / "tailmsg.py"
        self.bin = self.root / "bin"                          # only what the launcher needs
        self.bin.mkdir()
        for tool in ("sh", "dirname", "readlink"):
            os.symlink(shutil.which(tool), str(self.bin / tool))
        os.symlink(sys.executable, str(self.bin / "python3"))
        self.fakebin = self.root / "fakebin"                  # a fake tailscale, only when asked for
        self.fakebin.mkdir()
        fake = self.fakebin / "tailscale"
        fake.write_text("#!%s\nimport runpy, sys\nsys.argv[0] = %r\nrunpy.run_path(%r, run_name='__main__')\n"
                        % (sys.executable, str(SUPPORT / "fake_tailscale.py"), str(SUPPORT / "fake_tailscale.py")))
        fake.chmod(0o755)
        self.work = self.root / "unrelated cwd"
        self.work.mkdir()

    def cleanup(self):
        self._tmp.cleanup()

    def new_home(self, name="user home"):
        d = self.root / name
        d.mkdir()
        return d

    def env(self, user_home, tailscale=False, extra=None):
        path = str(self.bin) + (os.pathsep + str(self.fakebin) if tailscale else "")
        e = python_env({"HOME": str(user_home), "PATH": path, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
        e.update(extra or {})
        return e

    def run(self, args, user_home, tailscale=False, extra=None, cwd=None, launcher=None, timeout=60):
        cmd = ["/bin/sh", str(launcher or self.launcher)] + list(args)
        return subprocess.run(cmd, env=self.env(user_home, tailscale, extra), cwd=str(cwd or self.work),
                              capture_output=True, text=True, timeout=timeout)

    def run_json(self, args, user_home, **kw):
        r = self.run(args, user_home, **kw)
        try:
            return r, json.loads(r.stdout)
        except ValueError:
            raise AssertionError("not JSON (exit %s):\n%s\n%s" % (r.returncode, r.stdout, r.stderr))

    def fake_state(self, name, **state):
        p = self.root / ("ts-%s.json" % name)
        p.write_text(json.dumps(state))
        return p
