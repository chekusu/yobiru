"""Wake delivery retries use isolated inboxes and simulated adapters only."""
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support.pkg import REPO

spec = importlib.util.spec_from_file_location("wake_delivery_core", REPO / "tailmsg.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


class WakeDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with contextlib.redirect_stderr(io.StringIO()):
            self.ctx = core.Ctx(Path(self.temp.name))
        self.ctx.cfg.update(wake_attempts=3, wake_backoff_s=0)
        self.path = self.ctx.inbox / "wake-test-001.json"
        self.record = {
            "envelope": {"id": "wake-test-001", "from": "sender.example", "kind": "notification"},
            "received": {"peer": {"node": "sender.example"}},
        }

    def deliver(self, adapter_type, adapter_results=None, write_errors=None):
        self.path.write_text(json.dumps(self.record), encoding="utf-8")
        self.ctx.cfg["wake_adapter"] = (
            {"type": "exec", "argv": ["fake-adapter"]} if adapter_type == "exec" else
            {"type": "webhook", "url_env": "UNUSED_TEST_WEBHOOK_URL"}
        )
        output = io.StringIO()
        with mock.patch.object(core, "call_exec", return_value="exit 0") as exec_adapter, \
                mock.patch.object(core, "call_webhook", return_value="HTTP 202") as webhook_adapter, \
                mock.patch.object(core, "atomic_write", wraps=core.atomic_write,
                                  side_effect=write_errors) as write, \
                mock.patch.object(core.time, "sleep"), contextlib.redirect_stderr(output):
            adapter = exec_adapter if adapter_type == "exec" else webhook_adapter
            other_adapter = webhook_adapter if adapter_type == "exec" else exec_adapter
            adapter.side_effect = adapter_results
            result = core.deliver_wake(self.ctx, self.path)
            other_adapter.assert_not_called()
        return result, adapter.call_count, write.call_count, output.getvalue()

    def assert_delivered_without_record(self, result, calls, writes, output, expected_calls=1):
        self.assertTrue(result)
        self.assertEqual(expected_calls, calls)
        self.assertEqual(1, writes)
        self.assertEqual(self.record, json.loads(self.path.read_text(encoding="utf-8")))
        self.assertIn("could not record wake_delivery", output)
        self.assertIn("OSError", output)
        self.assertNotIn("private-record-diagnostic", output)

    def test_transient_record_failure_does_not_repeat_successful_adapter(self):
        for adapter_type in ("exec", "webhook"):
            with self.subTest(adapter=adapter_type):
                observed = self.deliver(adapter_type, write_errors=[
                    OSError("private-record-diagnostic"), mock.DEFAULT,
                ])
                self.assert_delivered_without_record(*observed)

    def test_persistent_record_failure_keeps_success_and_inbox(self):
        for adapter_type in ("exec", "webhook"):
            with self.subTest(adapter=adapter_type):
                observed = self.deliver(adapter_type, write_errors=OSError("private-record-diagnostic"))
                self.assert_delivered_without_record(*observed)

    def test_record_failure_after_successful_retry_does_not_send_again(self):
        for adapter_type in ("exec", "webhook"):
            with self.subTest(adapter=adapter_type):
                observed = self.deliver(
                    adapter_type,
                    adapter_results=[core.AdapterError("temporary adapter failure"), "accepted"],
                    write_errors=OSError("private-record-diagnostic"),
                )
                self.assert_delivered_without_record(*observed, expected_calls=2)

    def test_adapter_failure_still_retries_and_records_success(self):
        for adapter_type in ("exec", "webhook"):
            with self.subTest(adapter=adapter_type):
                result, calls, writes, _ = self.deliver(
                    adapter_type, adapter_results=[core.AdapterError("temporary adapter failure"), "accepted"],
                )
                self.assertTrue(result)
                self.assertEqual(2, calls)
                self.assertEqual(1, writes)
                recorded = json.loads(self.path.read_text(encoding="utf-8"))["wake_delivery"]
                self.assertTrue(recorded["ok"])
                self.assertEqual(2, recorded["attempts"])
                self.assertEqual(adapter_type, recorded["adapter"])

    def test_adapter_failure_still_exhausts_retries_and_keeps_message_pending(self):
        for adapter_type in ("exec", "webhook"):
            with self.subTest(adapter=adapter_type):
                result, calls, writes, _ = self.deliver(
                    adapter_type, adapter_results=core.AdapterError("adapter unavailable"),
                )
                self.assertFalse(result)
                self.assertEqual(3, calls)
                self.assertEqual(1, writes)
                record = json.loads(self.path.read_text(encoding="utf-8"))
                self.assertEqual(self.record["envelope"], record["envelope"])
                self.assertNotIn("acked_at", record)
                self.assertFalse(record["wake_delivery"]["ok"])
                self.assertEqual(3, record["wake_delivery"]["attempts"])


if __name__ == "__main__":
    unittest.main()
