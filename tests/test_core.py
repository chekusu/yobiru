"""Hardening in the canonical tailmsg.py that the plugin relies on."""
import importlib.util
import json
import os
import tempfile
import unittest

from support.pkg import REPO

spec = importlib.util.spec_from_file_location("tailmsg_core_under_test", str(REPO / "tailmsg.py"))
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)

ENV = {"v": 1, "id": "abcdef0123456789", "from": "a.example.ts.net", "to": "b.example.ts.net",
       "ts": "2026-10-10T00:00:00Z"}


class CoreTests(unittest.TestCase):
    def test_parse_config_is_pure_and_keeps_error_messages(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.getcwd()
            os.chdir(d)
            try:
                cfg, agents = core.parse_config({"allow": ["x"], "agents": {"café": None}})
                self.assertEqual([], os.listdir(d))
            finally:
                os.chdir(old)
        self.assertEqual(7766, cfg["port"])
        self.assertEqual({"café": {}}, agents)
        with self.assertRaisesRegex(RuntimeError, r"config agents\['a/b'\]"):
            core.parse_config({"agents": {"a/b": {}}})
        with self.assertRaisesRegex(RuntimeError, "JSON object"):
            core.parse_config(["not", "an", "object"])

    def test_ids_with_trailing_newline_or_paths_are_rejected(self):
        self.assertIsNone(core.validate_env(dict(ENV)))
        self.assertEqual("bad id", core.validate_env(dict(ENV, id="abcdef0123456789\n")))
        self.assertEqual("bad reply_to", core.validate_env(dict(ENV, reply_to="abcdef01\n")))
        for ref in ("../config", "*", "", "a\nb", "a/b", "x" * 65):
            self.assertIsNone(core.MSG_REF_RE.match(ref), repr(ref))
        self.assertTrue(core.MSG_REF_RE.match("abcd"))

    def test_attachment_names_stay_inside_the_inbox(self):
        for raw, want in (("../../etc/passwd", "passwd"), ("..", "file"), ("a\\..\\b.txt", "b.txt"),
                          (".hidden", "hidden"), ("my report.pdf", "my report.pdf")):
            self.assertEqual(want, core.safe_name(raw))


class WebhookDiagnosticsTests(unittest.TestCase):
    """A webhook can echo anything it received. Only its HTTP status, one standardized error code that repeats
    nothing that was sent to it, and a numeric Retry-After may reach the receiver log or the inbox record."""
    TOKEN = "PRIVATE_PATH_TOKEN_12345678"
    QUERY = "QUERY_TOKEN_87654321"
    SECRET = "SYNTHETIC_SECRET_abcdefgh"
    TEXT = "SYNTHETIC_PRIVATE_MESSAGE_12345678"

    def serve(self, status, reply, headers=None, raw=None):
        import http.server
        import threading

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                if raw is not None:                    # a broken status line that echoes the request path
                    self.wfile.write(raw(self.path))
                    return
                body = json.dumps(reply(self.path, payload, dict(self.headers))).encode()
                self.send_response(status)
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        os.environ["YOBIRU_T_URL"] = "http://127.0.0.1:%d/webhooks/%s?key=%s" % (srv.server_address[1], self.TOKEN,
                                                                               self.QUERY)
        os.environ["YOBIRU_T_SECRET"] = self.SECRET
        self.addCleanup(os.environ.pop, "YOBIRU_T_URL", None)
        self.addCleanup(os.environ.pop, "YOBIRU_T_SECRET", None)
        return {"type": "webhook", "url_env": "YOBIRU_T_URL", "secret_env": "YOBIRU_T_SECRET"}

    def record(self):
        return {"envelope": dict(ENV, text=self.TEXT), "received": {"peer": {"node": ENV["from"]}, "at": ENV["ts"]}}

    def error(self, adapter):
        with self.assertRaises(core.AdapterError) as caught:
            core.call_webhook(adapter, self.record(), 5)
        return str(caught.exception)

    def assert_no_leak(self, text):
        for leak in (self.TOKEN, self.QUERY, self.SECRET, self.TEXT, "Bearer"):
            self.assertNotIn(leak, text)

    def test_echoed_path_message_and_credentials_never_reach_the_error(self):
        adapter = self.serve(400, lambda path, payload, headers: {
            "error": "invalid hook", "path": path, "received": payload["text_preview"],
            "auth": headers.get("Authorization"), "detail": "token %s" % self.SECRET})
        message = self.error(adapter)
        self.assertEqual("HTTP 400", message)
        self.assert_no_leak(message)

    def test_a_standard_code_and_retry_after_are_kept(self):
        adapter = self.serve(400, lambda path, payload, headers: {"code": "invalid_hook", "path": path})
        self.assertEqual("HTTP 400 (code invalid_hook)", self.error(adapter))
        adapter = self.serve(429, lambda *a: {"error": {"code": "rate_limited", "message": self.TEXT}},
                             headers={"Retry-After": "30"})
        self.assertEqual("HTTP 429 (code rate_limited, retry after 30 s)", self.error(adapter))

    def test_a_code_that_repeats_what_was_sent_or_free_text_is_dropped(self):
        for code in (self.TOKEN, self.QUERY.lower(), self.TEXT, self.SECRET, "webhooks", "not an identifier",
                     "x" * 80):
            with self.subTest(code=code):
                adapter = self.serve(403, lambda *a: {"code": code})
                message = self.error(adapter)
                self.assertEqual("HTTP 403", message)
                self.assert_no_leak(message)
        adapter = self.serve(502, lambda *a: {"code": "bad_gateway"}, headers={"Retry-After": "soon"})
        self.assertEqual("HTTP 502 (code bad_gateway)", self.error(adapter))

    def test_a_broken_answer_records_only_the_exception_type(self):
        # Exception text can echo what was sent in any case or encoding, so none of it is kept.
        for echo in ("{path} {text}", "{lower}", "{escaped}"):
            with self.subTest(echo=echo):
                adapter = self.serve(None, None, raw=lambda path: ("BOGUS %s\r\n\r\n" % echo.format(
                    path=path, text=self.TEXT, lower=self.SECRET.lower(), escaped=json.dumps(self.TEXT + "\n"))).encode())
                message = self.error(adapter)
                self.assertEqual("BadStatusLine", message)

    def test_deliver_wake_records_and_logs_only_the_safe_detail(self):
        import contextlib
        import io
        from pathlib import Path
        adapter = self.serve(400, lambda path, payload, headers: {
            "error": "invalid hook", "path": path, "received": payload["text_preview"],
            "auth": headers.get("Authorization")})
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stderr(io.StringIO()) as log:
            ctx = core.Ctx(Path(d))
            ctx.cfg.update(wake_attempts=2, wake_backoff_s=0, wake_adapter=adapter)
            path = ctx.inbox / (ENV["id"] + ".json")
            path.write_text(json.dumps(self.record()), encoding="utf-8")
            self.assertFalse(core.deliver_wake(ctx, path))
            stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((False, 2, "HTTP 400"), (stored["wake_delivery"]["ok"], stored["wake_delivery"]["attempts"],
                                                  stored["wake_delivery"]["error"]))
        self.assert_no_leak(json.dumps(stored["wake_delivery"]))
        self.assert_no_leak(log.getvalue())
        self.assertIn("HTTP 400", log.getvalue())


    def test_short_secrets_and_message_values_are_redacted_too(self):
        import contextlib
        import io
        from pathlib import Path
        # (field, value, echoed code): a one-character value hides inside an otherwise valid code.
        for field, value, code in (("secret", "Q7", "Q7"), ("secret", "Ab3", "Ab3"), ("secret", "x", "bad_x"),
                                   ("text", "Q7", "Q7"), ("text", "x", "bad_x"), ("from_agent", "Z9", "Z9")):
            with self.subTest(field=field, value=value):
                adapter = self.serve(400, lambda *a: {"code": code})
                if field == "secret":
                    os.environ["YOBIRU_T_SECRET"] = value
                record = self.record()
                if field != "secret":
                    record["envelope"][field] = value
                with self.assertRaises(core.AdapterError) as caught:
                    core.call_webhook(adapter, record, 5)
                self.assertEqual("HTTP 400", str(caught.exception))
                with tempfile.TemporaryDirectory() as d, contextlib.redirect_stderr(io.StringIO()) as log:
                    ctx = core.Ctx(Path(d))
                    ctx.cfg.update(wake_attempts=1, wake_backoff_s=0, wake_adapter=adapter)
                    path = ctx.inbox / (ENV["id"] + ".json")
                    path.write_text(json.dumps(record), encoding="utf-8")
                    self.assertFalse(core.deliver_wake(ctx, path))
                    stored = json.loads(path.read_text(encoding="utf-8"))["wake_delivery"]["error"]
                self.assertEqual("HTTP 400", stored)
                self.assertIn("HTTP 400", log.getvalue())
                self.assertNotIn("code " + code, log.getvalue())
        adapter = self.serve(None, None, raw=lambda path: b"BOGUS Q7 Q7\r\n\r\n")    # a broken answer echoes it
        os.environ["YOBIRU_T_SECRET"] = "Q7"
        self.assertEqual("BadStatusLine", self.error(adapter))


    def test_a_retry_after_that_repeats_what_was_sent_is_dropped(self):
        import contextlib
        import io
        from pathlib import Path
        # (what carries the value, its value, the Retry-After answered), compared as written and without zeros.
        cases = (("secret", "57391", "57391"), ("secret", "37", "37"), ("secret", "7", "7"), ("text", "57391", "57391"),
                 ("path", "57391", "57391"), ("query", "57391", "57391"), ("query", "%35%37%33%39%31", "57391"),
                 ("secret", "00573", "00573"), ("secret", "573", "00573"))
        for field, value, retry in cases:
            with self.subTest(field=field, value=value, retry=retry):
                adapter = self.serve(429, lambda *a: {"code": "rate_limited"}, headers={"Retry-After": retry})
                if field == "secret":
                    os.environ["YOBIRU_T_SECRET"] = value
                elif field in ("path", "query"):
                    os.environ["YOBIRU_T_URL"] = os.environ["YOBIRU_T_URL"].replace(
                        self.TOKEN if field == "path" else self.QUERY, value)
                record = self.record()
                if field == "text":
                    record["envelope"]["text"] = value
                with self.assertRaises(core.AdapterError) as caught:
                    core.call_webhook(adapter, record, 5)
                self.assertEqual("HTTP 429 (code rate_limited)", str(caught.exception))
                with tempfile.TemporaryDirectory() as d, contextlib.redirect_stderr(io.StringIO()) as log:
                    ctx = core.Ctx(Path(d))
                    ctx.cfg.update(wake_attempts=1, wake_backoff_s=0, wake_adapter=adapter)
                    path = ctx.inbox / (ENV["id"] + ".json")
                    path.write_text(json.dumps(record), encoding="utf-8")
                    self.assertFalse(core.deliver_wake(ctx, path))
                    stored = json.loads(path.read_text(encoding="utf-8"))["wake_delivery"]["error"]
                self.assertEqual("HTTP 429 (code rate_limited)", stored)
                self.assertIn(stored, log.getvalue())
                self.assertNotIn("retry after", log.getvalue())


if __name__ == "__main__":
    unittest.main()
