"""Safety contracts for the versioned, manually installed gateway services."""
import configparser
import datetime
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def load_report():
    spec = importlib.util.spec_from_file_location("gateway_report", str(REPO / "deploy/yobiru-gateway-report.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LitestreamServiceTests(unittest.TestCase):
    def test_gateway_stop_waits_for_litestream_before_database_replacement(self):
        unit = configparser.ConfigParser(interpolation=None)
        unit.read_string((REPO / "deploy/yobiru-litestream.service").read_text())
        gateway = "yobiru-gateway.service"

        # Deployment stops the gateway before backing up or replacing its database.
        # PartOf propagates that stop, but only After makes the gateway's stop job
        # wait for Litestream to release its WAL reader (shutdown reverses startup).
        self.assertIn(gateway, unit["Unit"].get("PartOf", "").split())
        self.assertIn(gateway, unit["Unit"].get("After", "").split())
        self.assertNotIn(gateway, unit["Unit"].get("Before", "").split())
        self.assertIn(gateway, unit["Install"].get("WantedBy", "").split())



class CapacityDropInTests(unittest.TestCase):
    def test_gateway_caps_glibc_malloc_arenas(self):
        # A thread per request with glibc's default of 8 arenas per core grew each request process without bound.
        unit = configparser.ConfigParser(interpolation=None, strict=False)
        unit.read_string((REPO / "deploy/yobiru-gateway-capacity.conf").read_text())
        self.assertEqual(unit["Service"]["Environment"], "MALLOC_ARENA_MAX=2")
        self.assertEqual(unit["Service"]["MemoryHigh"], "8G")


class GatewayReportTests(unittest.TestCase):
    """deploy/yobiru-gateway-report.py: aggregate counts only, nothing written to the database."""

    @classmethod
    def setUpClass(cls):
        cls.report = load_report()

    def test_upload_signature_matches_the_gateway_blob_store(self):
        sys.path.insert(0, str(REPO / "public_bots"))
        try:
            import bot_blobs
        finally:
            sys.path.pop(0)
        store = bot_blobs.S3Store("https://acct.r2.cloudflarestorage.com", "bucket", "AKID", "secret")
        now = datetime.datetime(2026, 11, 2, 12, 0, 0, tzinfo=datetime.timezone.utc)
        path, body = "/bucket/ops/gateway-report/latest.json", b'{"v":1}'
        mine = self.report.sign_put({"endpoint": "https://acct.r2.cloudflarestorage.com", "bucket": "bucket",
                                     "region": "auto", "key_id": "AKID", "secret": "secret"}, path, body, now=now)
        self.assertEqual(mine, store.sign("PUT", path, body, now=now))

    def test_journal_counts_requests_and_masks_error_lines(self):
        lines = [json.dumps({"MESSAGE": m, "_SYSTEMD_UNIT": u}) for m, u in (
            ("2026-11-02 10:00:00 127.0.0.1 GET connector_jobs 200", "yobiru-gateway.service"),
            ("2026-11-02 10:00:01 127.0.0.1 POST enroll_email 429", "yobiru-gateway.service"),
            ("2026-11-02 10:00:02 127.0.0.1 GET share_card 500", "yobiru-gateway.service"),
            ("2026-11-02 10:00:02 127.0.0.1 POST mcp tools/call yobiru_send 200", "yobiru-gateway.service"),
            ("2026-11-02 10:00:02 127.0.0.1 POST mcp X-Secret/Thing 200", "yobiru-gateway.service"),
            ("2026-11-02 10:00:02 127.0.0.1 GET Some-Odd-Path 404", "yobiru-gateway.service"),
            ("2026-11-02 10:00:03 internal error: KeyError for someone@example.com @guoyu token "
             "abcdefghijklmnopqrstuvwxyz0123456789 from 203.0.113.9", "yobiru-gateway.service"),
            ("2026-11-02 10:00:03 request aborted: BrokenPipeError", "yobiru-gateway.service"),
            ("2026-11-02 10:00:04 mail relay: sent 2, failed 0", "yobiru-gateway-mail.service"),
            ("2026-11-02 10:00:05 mail relay: sent 0, failed 1", "yobiru-gateway-mail.service"),
            ("2026-11-02 10:00:06 yobiru-bot-gateway serving on 127.0.0.1:7780 home=/x processes=8",
             "yobiru-gateway.service"),
        )] + ["not json"]
        out = self.report.count_journal(lines)
        self.assertEqual(out["requests"]["total"], 6)
        self.assertEqual((out["requests"]["2xx"], out["requests"]["4xx"], out["requests"]["5xx"]), (3, 2, 1))
        self.assertEqual(out["routes"], {"connector_jobs": 1, "enroll_email": 1, "share_card": 1, "mcp": 2,
                                         "other": 1})
        self.assertEqual(out["errors_5xx"], {"share_card 500": 1})
        self.assertEqual(out["events"], {"yobiru-gateway:internal_error": 1, "yobiru-gateway-mail:mail": 1,
                                         "yobiru-gateway:aborted": 1, "yobiru-gateway:start": 1})
        self.assertEqual(len(out["samples"]), 2)                 # the internal error and the mail failure
        text = " ".join(out["samples"])
        self.assertNotIn("someone@example.com", text)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", text)
        self.assertNotIn("guoyu", text)
        self.assertNotIn("203.0.113.9", text)
        self.assertNotIn("serving on", text)

    def test_nginx_counts_only_new_lines_and_follows_rotation(self):
        with tempfile.TemporaryDirectory() as d:
            state, log = Path(d) / "state", Path(d) / "access.log"
            state.mkdir()
            line = '1.2.3.4 - - [02/Nov/2026:10:00:00 +0900] "GET /v1/health HTTP/1.1" %s 12 "-" "ua"\n'
            log.write_text(line % 200)
            self.assertEqual(self.report.nginx_counts(state, str(log))["status"], {})   # starts at the end
            with open(log, "a") as f:
                f.write(line % 502 + line % 200)
            self.assertEqual(self.report.nginx_counts(state, str(log))["status"], {"502": 1, "200": 1})
            self.assertEqual(self.report.nginx_counts(state, str(log))["status"], {})
            os.rename(log, str(log) + ".1")
            log.write_text(line % 499)
            self.assertEqual(self.report.nginx_counts(state, str(log))["status"], {"499": 1})

    def test_database_counts_on_a_new_gateway_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "gw"
            subprocess.run([sys.executable, str(REPO / "public_bots/bot_gateway.py"), "--gateway-home", str(home),
                            "init"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            db = sqlite3.connect(str(home / "gateway.sqlite3"))
            now = int(time.time())
            for i, age in enumerate((100, 2 * 86400, 10 * 86400)):
                db.execute("INSERT INTO owners(id, label, created_ts, kind, email_hash) VALUES(?,?,?,?,?)",
                           ("o%d" % i, "x", now - age, "self_enrolled", "h%d" % i))
            db.commit()
            db.close()
            before = (home / "gateway.sqlite3").read_bytes()
            out = self.report.db_stats(home / "gateway.sqlite3", now)
            self.assertEqual(out["owners"], {"admin": 1, "self_enrolled": 3})
            self.assertEqual({k: v["owners"] for k, v in out["totals_at"].items()},
                             {"now": 3, "12h": 2, "1d": 2, "7d": 1, "30d": 0})
            self.assertEqual(out["active"]["24h"]["messages"], 0)
            self.assertEqual((home / "gateway.sqlite3").read_bytes(), before)

    def test_windows_peaks_and_rates(self):
        now = 1_800_000_000
        samples = [{"ts": now - 600 + 120 * i, "conn": c, "cpu_ns": 10 ** 9 * i, "restarts": 0,
                    "health_ms": 2.0, "probe_ok": True,
                    "journal": {"requests": {"total": 240, "5xx": i}, "routes": {"connector_jobs": 200}}}
                   for i, c in enumerate((3, 9, 4, 5, 6))]
        w = self.report.windows(samples, now)["1h"]
        self.assertEqual(w["conn"]["max"], 9)
        self.assertEqual(w["requests"], {"total": 1200, "5xx": 10})
        self.assertEqual(w["peak_requests_per_min"], 120.0)
        self.assertAlmostEqual(w["cpu_cores_avg"], 4 / 480, places=3)
        self.assertEqual(w["gateway_restarts"], 0)

    def test_report_unit_runs_as_the_gateway_user_with_read_access_only(self):
        unit = configparser.ConfigParser(interpolation=None, strict=False)
        unit.read_string((REPO / "deploy/yobiru-gateway-report.service").read_text())
        service = unit["Service"]
        self.assertEqual(service["User"], "yobiru-gw")
        self.assertEqual(service["ProtectSystem"], "strict")
        self.assertEqual(service["NoNewPrivileges"], "true")
        self.assertEqual(set(service["SupplementaryGroups"].split()), {"adm", "systemd-journal"})
        self.assertEqual(service["ExecStart"], "/usr/local/sbin/yobiru-gateway-report")
        self.assertIn("deploy/yobiru-gateway-report.py /usr/local/sbin/yobiru-gateway-report",
                      (REPO / "deploy/yobiru-gateway-report.py").read_text())


if __name__ == "__main__":
    unittest.main()
