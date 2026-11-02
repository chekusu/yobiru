"""Execute the sender-local example from GROK_BOT.md with synthetic HTTP and time.

No provider, Tailscale, real user home, credential or external network is used. The
one real HTTP test binds only a random loopback port to check redirect handling.
"""
import ast
import datetime
import io
import json
from pathlib import Path
import re
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


REPO = Path(__file__).resolve().parents[1]
DOC = REPO / "docs" / "GROK_BOT.md"
HEALTH = {"v": 1, "ok": True, "node": "receiver.example.ts.net"}
URL = "https://routine.example.invalid/private-route"
KEY = "synthetic-bearer-key"


def recipe():
    text = DOC.read_text(encoding="utf-8")
    match = re.search(r"<!-- grok-prewake-example:start -->\s*```python\n(.*?)\n```", text, re.S)
    if not match:
        raise AssertionError("sender pre-wake example is missing")
    source = match.group(1)
    module = types.ModuleType("grok_prewake_recipe_under_test")
    exec(compile(source, str(DOC), "exec"), module.__dict__)
    # Execute the example's unchanged CLI error boundary separately, after installing fakes.
    entrypoint = ast.Module(body=[ast.parse(source).body[-1]], type_ignores=[])
    return module, compile(entrypoint, str(DOC), "exec")


class Clock:
    def __init__(self):
        self.now = 0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Response(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(json.dumps(body).encode() if isinstance(body, (dict, list)) else body)
        self.status = status


class HTTP:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def open(self, request, timeout):
        if isinstance(request, str):
            request = urllib.request.Request(request)
        self.calls.append((request, timeout))
        if not self.outcomes:
            raise AssertionError("unexpected HTTP request")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return Response(*outcome)


class PrewakeRecipeTests(unittest.TestCase):
    def setUp(self):
        self.module, self.entrypoint = recipe()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        directory = Path(self.tmp.name)
        self.module.URL_FILE = directory / "routine-url"
        self.module.KEY_FILE = directory / "routine-key"
        self.module.HEALTH_URL = "http://receiver.example.ts.net:7766/health"
        self.module.SENDER_NODE = "sender.example.ts.net"
        self.module.AGENT = "grok"
        self.clock = self.module.time = Clock()

    def secrets(self, url=URL):
        for path, value in ((self.module.URL_FILE, url), (self.module.KEY_FILE, KEY)):
            path.write_text(value + "\n", encoding="utf-8")
            path.chmod(0o600)

    def cli_failure(self):
        self.module.__name__ = "__main__"
        with self.assertRaises(SystemExit) as failure:
            exec(self.entrypoint, self.module.__dict__)
        self.assertEqual("Pre-wake failed; no message was sent.", str(failure.exception))
        self.assertNotIn(URL, str(failure.exception))
        self.assertNotIn(KEY, str(failure.exception))

    def methods(self):
        return [request.get_method() for request, _ in self.module.HTTP.calls]

    def test_ready_receiver_does_not_need_or_read_secret_files(self):
        self.assertFalse(self.module.URL_FILE.exists())
        self.assertFalse(self.module.KEY_FILE.exists())
        self.module.HTTP = HTTP((200, HEALTH))
        self.module.main()
        self.assertEqual(["GET"], self.methods())
        self.assertEqual([], self.clock.sleeps)

    def test_single_wake_then_wait_for_valid_receiver_health(self):
        self.secrets()
        self.module.HTTP = HTTP(OSError("asleep or unreachable"), (202, b""),
                                OSError("still unavailable"), (200, HEALTH))
        self.module.main()
        self.assertEqual(["GET", "POST", "GET", "GET"], self.methods())
        self.assertEqual([5], self.clock.sleeps)
        request, timeout = self.module.HTTP.calls[1]
        self.assertEqual(URL, request.full_url)
        self.assertEqual("Bearer " + KEY, request.get_header("Authorization"))
        self.assertEqual("application/json", request.get_header("Content-type"))
        self.assertGreater(timeout, 0)
        payload = json.loads(request.data)
        self.assertEqual({"v", "event", "id", "from", "kind", "agent", "text_preview", "received_at"},
                         set(payload))
        self.assertEqual((1, "tailmsg.wake", "sender.example.ts.net", "notice", "grok"),
                         (payload["v"], payload["event"], payload["from"], payload["kind"], payload["agent"]))
        self.assertRegex(payload["id"], r"^wake-[0-9a-f]{32}$")
        self.assertEqual("Wake receiver; no request is attached.", payload["text_preview"])
        self.assertIsNotNone(datetime.datetime.fromisoformat(payload["received_at"]).tzinfo)
        for request, _ in self.module.HTTP.calls:
            if request.get_method() == "GET":
                self.assertEqual(self.module.HEALTH_URL, request.full_url)
                self.assertIsNone(request.get_header("Authorization"))

    def test_timeout_returns_failure_after_one_wake_without_submitting_message(self):
        self.secrets()
        self.module.WAIT_SECONDS = 11
        self.module.HTTP = HTTP(OSError(), (200, b""), OSError(), OSError(), OSError())
        self.cli_failure()
        self.assertEqual(11, self.clock.now)
        self.assertEqual([5, 5, 1], self.clock.sleeps)
        self.assertEqual(["GET", "POST", "GET", "GET", "GET"], self.methods())
        self.assertEqual(1, self.module.HTTP.calls[-1][1])

    def test_webhook_rejection_or_transport_failure_is_not_retried(self):
        for outcome in (urllib.error.HTTPError(URL, 429, "private upstream details " + KEY, {}, None),
                        OSError(URL + KEY)):
            with self.subTest(outcome=type(outcome).__name__):
                self.secrets()
                self.module.HTTP = HTTP(OSError(), outcome)
                self.cli_failure()
                self.assertEqual(["GET", "POST"], self.methods())
                self.assertEqual([], self.clock.sleeps)

    def test_health_arriving_after_deadline_never_allows_send(self):
        self.secrets()
        clock = self.clock

        class SlowHealth(HTTP):
            def open(self, request, timeout):
                response = super().open(request, timeout)
                if isinstance(request, str):
                    clock.now += 2
                return response

        for outcomes, expected_methods in (
                ([(200, HEALTH)], ["GET"]),
                ([OSError(), (202, b""), (200, HEALTH)], ["GET", "POST", "GET"])):
            with self.subTest(methods=expected_methods):
                self.clock.now = 0
                self.module.WAIT_SECONDS = 1
                self.module.HTTP = SlowHealth(*outcomes)
                self.cli_failure()
                self.assertEqual(expected_methods, self.methods())
                self.assertEqual(2, self.clock.now)

    def test_http_errors_malformed_or_degraded_health_fail_before_secret_access(self):
        outcomes = [urllib.error.HTTPError(self.module.HEALTH_URL, 403, "denied", {}, None),
                    (200, {"v": 1, "ok": False, "status": "degraded"}),
                    (200, {"status": "healthy"}), (200, b"not JSON"),
                    (200, b"x" * 65537), (200, [True]), (503, HEALTH)]
        for outcome in outcomes:
            with self.subTest(outcome=repr(outcome)[:80]):
                self.module.HTTP = HTTP(outcome)
                self.cli_failure()
                self.assertEqual(["GET"], self.methods())
                self.assertFalse(self.module.KEY_FILE.exists())

    def test_private_file_permissions_are_required_before_webhook(self):
        self.secrets()
        self.module.KEY_FILE.chmod(0o644)
        self.module.HTTP = HTTP(OSError())
        self.cli_failure()
        self.assertEqual(["GET"], self.methods())

    def test_insecure_or_credentialed_webhook_urls_are_not_called(self):
        for url in ("http://routine.example.invalid/wake", "https://user:pass@routine.example.invalid/wake",
                    "https://routine.example.invalid/wake#secret", "file:///private/wake"):
            with self.subTest(url=url):
                self.secrets(url)
                self.module.HTTP = HTTP(OSError())
                self.cli_failure()
                self.assertEqual(["GET"], self.methods())

    def test_redirect_does_not_forward_bearer_header(self):
        calls = []

        class RedirectHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                calls.append((self.path, self.headers.get("Authorization")))
                self.send_response(302)
                self.send_header("Location", "/redirect-target")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                calls.append((self.path, self.headers.get("Authorization")))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        request = urllib.request.Request("http://127.0.0.1:%d/start" % server.server_port,
                                         data=b"{}", headers={"Authorization": "Bearer " + KEY})
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.module.HTTP.open(request, timeout=2)
        self.assertEqual(302, failure.exception.code)
        failure.exception.close()
        self.assertEqual([("/start", "Bearer " + KEY)], calls)


if __name__ == "__main__":
    unittest.main()
