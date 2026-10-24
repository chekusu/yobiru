"""Retry durable wake results across repeated gateway failures without repeating a host wake."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support.public_harness import PUBLIC

import bot_client as P


BOT = "bot_aaaaaaaaaaaaaaaa"
LEDGER = "connector-ledger.json"


class StopPolling(Exception):
    """Stop a continuous connector after a fixed number of successful listing polls."""


class GatewayReplies:
    def __init__(self, polls=5, jobs=(), failures=None, duplicate=False, intent_errors=()):
        self.max_polls, self.polls = polls, 0
        self.jobs, self.failures = list(jobs), dict(failures or {})
        self.duplicate, self.intents, self.results = duplicate, [], []
        self.intent_errors = list(intent_errors)

    def __call__(self, base, method, path, body, token, timeout=15):
        if method == "GET":
            if self.polls == self.max_polls:
                raise StopPolling()
            self.polls += 1
            jobs, self.jobs = self.jobs, []
            return {"ok": True, "jobs": jobs, "checks": [], "replies": []}
        ref = path.split("/")[-2]
        if body["step"] == "intent":
            self.intents.append(dict(body))
            if self.intent_errors:
                return self.intent_errors.pop(0)
            return {"ok": True, "duplicate": self.duplicate}
        self.results.append((ref, dict(body)))
        if self.failures.get(ref, 0):
            self.failures[ref] -= 1
            return {"ok": False, "error": "busy", "http_status": 503}
        return {"ok": True}


class ConnectorRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="yobiru connector recovery ")
        self.addCleanup(tmp.cleanup)
        self.st = P.State(Path(tmp.name))

    def entry(self, ref, step="woken", kind="job"):
        entry = {"kind": kind, "ref": ref, "phase": "ready" if kind == "job" else None,
                 "wake_id": "wake-" + ref, "step": step}
        if step == "woken":
            entry.update(outcome="refused", detail="synthetic host unavailable")
        return entry

    def save(self, *entries):
        self.st.save(LEDGER, {"v": 1, "wakes": {BOT + ":" + e["ref"]: e for e in entries}})

    def run_connector(self, replies):
        args = SimpleNamespace(once=False, wait_seconds=25, idle_exit=None, poll_seconds=0.1)
        with patch.object(P, "request", side_effect=replies), patch.object(P.time, "sleep"), \
                patch.object(P, "perform_wake", return_value=("refused", "synthetic host unavailable")) as wake:
            with self.assertRaises(StopPolling):
                P.connector_run(self.st, args, "http://127.0.0.1:1", BOT,
                                {"token": "synthetic", "handle": "@test"}, {"type": "manual"})
        return wake

    def test_running_connector_retries_a_result_after_two_failures(self):
        ref = "msg_aaaaaaaaaaaaaaaa"
        gateway = GatewayReplies(jobs=[{"job_id": ref, "phase": "ready"}], failures={ref: 2})
        wake = self.run_connector(gateway)
        self.assertEqual(5, gateway.polls)
        self.assertEqual(3, len(gateway.results), "retry until the gateway accepts the result, then stop")
        self.assertEqual(1, len(gateway.intents))
        self.assertEqual(1, wake.call_count, "retry the result, never the host wake")
        self.assertEqual(1, len({body["wake_id"] for _, body in gateway.results}))
        self.assertEqual(["refused"] * 3, [body["outcome"] for _, body in gateway.results])
        self.assertEqual({}, self.st.load(LEDGER)["wakes"])

    def test_failed_startup_recovery_keeps_retrying_jobs_and_checks(self):
        for kind, ref in (("job", "msg_aaaaaaaaaaaaaaaa"), ("binding_check", "chk_aaaaaaaaaaaaaaaa")):
            with self.subTest(kind=kind):
                self.save(self.entry(ref, kind=kind))
                gateway = GatewayReplies(failures={ref: 2})
                wake = self.run_connector(gateway)
                self.assertEqual(3, len(gateway.results))
                self.assertEqual([], gateway.intents)
                wake.assert_not_called()
                self.assertEqual({}, self.st.load(LEDGER)["wakes"])

    def test_every_pending_result_is_retried_until_all_are_settled(self):
        first, second = "msg_aaaaaaaaaaaaaaaa", "msg_bbbbbbbbbbbbbbbb"
        self.save(self.entry(first), self.entry(second))
        gateway = GatewayReplies(failures={first: 3, second: 1})
        self.run_connector(gateway).assert_not_called()
        self.assertEqual([first, second, first, second, first, first], [ref for ref, _ in gateway.results])
        self.assertEqual({}, self.st.load(LEDGER)["wakes"])

    def test_recovered_outcome_survives_another_restart_after_a_failed_result(self):
        ref = "msg_aaaaaaaaaaaaaaaa"
        for step, duplicate, outcome in (("intended", False, "uncertain"),
                                          ("planned", True, "uncertain"),
                                          ("planned", False, "refused")):
            with self.subTest(step=step, duplicate=duplicate):
                self.save(self.entry(ref, step=step))
                gateway = GatewayReplies(polls=0, failures={ref: 1}, duplicate=duplicate)
                self.run_connector(gateway).assert_not_called()
                saved = self.st.load(LEDGER)["wakes"][BOT + ":" + ref]
                self.assertEqual(("woken", outcome), (saved["step"], saved["outcome"]))
                restarted = GatewayReplies()
                self.run_connector(restarted).assert_not_called()
                self.assertEqual([], restarted.intents, "the recovered result is durable before it is sent")
                self.assertEqual([outcome], [body["outcome"] for _, body in restarted.results])
                self.assertEqual(gateway.results[0], restarted.results[0])
                self.assertEqual({}, self.st.load(LEDGER)["wakes"])

    def test_a_transient_failure_to_recover_intent_keeps_the_original_wake_id(self):
        ref = "msg_aaaaaaaaaaaaaaaa"
        for status in (None, 429, 503):
            with self.subTest(status=status):
                entry = self.entry(ref, step="planned")
                self.save(entry)
                gateway = GatewayReplies(polls=0, intent_errors=[{"ok": False, "http_status": status}])
                self.run_connector(gateway).assert_not_called()
                self.assertEqual({BOT + ":" + ref: entry}, self.st.load(LEDGER)["wakes"])
                restarted = GatewayReplies(duplicate=True)
                self.run_connector(restarted).assert_not_called()
                self.assertEqual(gateway.intents, restarted.intents)
                self.assertEqual(["uncertain"], [body["outcome"] for _, body in restarted.results])
                self.assertEqual({}, self.st.load(LEDGER)["wakes"])

    def test_a_listed_job_does_not_replace_an_intent_still_being_recovered(self):
        ref = "msg_aaaaaaaaaaaaaaaa"
        entry = self.entry(ref, step="planned")
        self.save(entry)
        gateway = GatewayReplies(polls=1, jobs=[{"job_id": ref, "phase": "ready"}],
                                 intent_errors=[{"ok": False, "http_status": 503}] * 2)
        self.run_connector(gateway).assert_not_called()
        self.assertEqual([entry["wake_id"]] * 2, [body["wake_id"] for body in gateway.intents])
        self.assertEqual([], gateway.results)
        self.assertEqual({BOT + ":" + ref: entry}, self.st.load(LEDGER)["wakes"])


if __name__ == "__main__":
    unittest.main()
