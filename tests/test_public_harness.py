"""The isolated subprocess harness must keep the interpreter's own shared runtime."""
import json
import subprocess
import sys
import tempfile
import unittest

from support.public_harness import plain_env


class IsolatedPythonTests(unittest.TestCase):
    def test_child_uses_current_python_runtime(self):
        # setup-python's Linux executable can otherwise load the distro libpython
        # with its own newer stdlib, including incompatible JSON exception code.
        code = """
import json
import sys
try:
    json.loads('{not json')
except ValueError as error:
    print(json.dumps({'version': list(sys.version_info), 'error': type(error).__name__}))
"""
        with tempfile.TemporaryDirectory(prefix="yobiru python runtime ") as home:
            result = subprocess.run([sys.executable, "-c", code], env=plain_env(home),
                                    capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual({"version": list(sys.version_info), "error": "JSONDecodeError"},
                         json.loads(result.stdout))
