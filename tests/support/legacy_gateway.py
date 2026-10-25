"""Run a frozen gateway fixture with only its numeric listener metadata adapted.

The archived source and its protocol/storage behavior stay intact. Its inherited
HTTPServer bind otherwise makes migration tests depend on host reverse DNS.
"""
import importlib.util
import sys
from pathlib import Path

from numeric_http import NumericBind

script = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(script.parent))
spec = importlib.util.spec_from_file_location("legacy_gateway_fixture", str(script))
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)
gateway.BoundedServer.server_bind = NumericBind.server_bind
sys.exit(gateway.main(sys.argv[2:]))
