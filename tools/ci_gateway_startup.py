"""Bounded CI gateway startup probe with a traceback from a stalled Python child."""
import platform
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from support.public_harness import Gateway, PUBLIC


# The gateway child normally writes readiness immediately after binding. If it
# stalls before that point, preserve its stack in the harness's stderr log.
CHILD = """
import faulthandler
import runpy
import sys
from pathlib import Path
faulthandler.enable()
faulthandler.dump_traceback_later(10)
script = sys.argv[1]
sys.argv = sys.argv[1:]
sys.path[0] = str(Path(script).resolve().parent)
runpy.run_path(script, run_name="__main__")
"""


def main():
    print("Gateway startup:", platform.platform(), sys.version, "SQLite", sqlite3.sqlite_version, flush=True)
    with tempfile.TemporaryDirectory(prefix="yobiru ci startup ") as tmp:
        gateway = Gateway(Path(tmp), prefix=[sys.executable, "-u", "-c", CHILD, str(PUBLIC / "bot_gateway.py")])
        try:
            gateway.start()
            print("Gateway readiness received:", gateway.url, flush=True)
        finally:
            gateway.stop()


if __name__ == "__main__":
    main()
