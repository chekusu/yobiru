#!/usr/bin/env python3
"""Run the tests/ suite the way CI does, whole or for the client only.

`--suite all` runs every module, as `python3 -m unittest discover -s tests` does. `--suite client` leaves out the
modules that test only the gateway server. The gateway runs on Linux alone (the official one at bots.yobiru.com), so
macOS CI runs the client suite: the CLI, the receiver, the connector and the bridges, including the tests that drive a
loopback gateway through the client. A new module runs in both suites unless it is named in GATEWAY_ONLY.
"""
import argparse
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent.parent / "tests"

# Modules that test only the gateway server: its storage and processes, hosted wakes, the pages it serves, its
# migrations and the rules it enforces between bots. None of them runs client code that a user's machine runs.
GATEWAY_ONLY = frozenset({
    "test_deploy",
    "test_friends_chekusu",
    "test_friends_decisions",
    "test_friends_direct_email",
    "test_friends_direct_messages",
    "test_friends_files_contact",
    "test_friends_gateway",
    "test_friends_hosted_wake",
    "test_friends_hosted_wake_evidence",
    "test_friends_hosted_wake_security",
    "test_friends_hosted_wake_selection",
    "test_friends_hosted_wake_workers",
    "test_friends_ignored_requests",
    "test_friends_inbox_upgrade",
    "test_friends_join_owner",
    "test_friends_loops",
    "test_friends_migration",
    "test_friends_partners",
    "test_friends_read_receipt_wakes",
    "test_gateway_blobs",
    "test_gateway_concurrency",
    "test_gateway_indexes",
    "test_gateway_recycle",
    "test_gateway_write_lock",
    "test_profile_page",
    "test_public_gateway",
    "test_review_pagination",
})


def modules(suite):
    names = sorted(p.stem for p in TESTS.glob("test*.py"))
    missing = sorted(GATEWAY_ONLY - set(names))
    if missing:
        raise SystemExit("ci_tests: GATEWAY_ONLY names a module that does not exist: " + ", ".join(missing))
    return names if suite == "all" else [n for n in names if n not in GATEWAY_ONLY]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--suite", choices=("all", "client"), default="all")
    parser.add_argument("--list", action="store_true", help="print the modules of the suite and exit")
    args = parser.parse_args()
    names = modules(args.suite)
    if args.list:
        print("\n".join(names))
        return
    sys.path.insert(0, str(TESTS))          # as discover does with -s tests: the modules import `support`
    print("ci_tests: %s suite, %d modules" % (args.suite, len(names)), flush=True)
    tests = unittest.defaultTestLoader.loadTestsFromNames(names)
    result = unittest.TextTestRunner(verbosity=2, failfast=True).run(tests)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
