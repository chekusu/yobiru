"""Hosted wake routes cannot alter HTTP framing or use translated private destinations.

No external requests are made: DNS replies are synthetic and registration uses a loopback gateway.
"""
import os
import socket
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from support.public_harness import C, FriendsGateway
import bot_gateway as G


class HostedWakeHeaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="yobiru hosted header test ")
        cls.gw = FriendsGateway(Path(cls._tmp.name), limits={"hosted_wake_private_hosts": 1}).start()
        cls.bot = cls.gw.bot("synthetic-wake-headers")

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls._tmp.cleanup()

    def register(self, **extra):
        body = dict({"url": "http://127.0.0.1:1/synthetic", "secret": "synthetic-header-secret"}, **extra)
        return self.gw.call("PUT", "/v1/owner/bots/%s/wake" % self.bot, body, self.gw.owner_token)

    def test_transport_headers_cannot_be_used_for_authentication(self):
        for header in ("Content-Length", "content-length", "TRANSFER-ENCODING", "Host", "Connection",
                       "Keep-Alive", "Proxy-Authenticate", "Proxy-Authorization", "TE", "Trailer", "Upgrade",
                       "Expect", "Content-Type", "User-Agent"):
            with self.subTest(header=header):
                status, body = self.register(header=header)
                self.assertEqual((400, "invalid_wake_header"), (status, body.get("error")))

    def test_header_and_scheme_must_match_the_entire_value(self):
        for extra in ({"header": "X-Wake-Token\n"}, {"scheme": "Bearer\n"}):
            with self.subTest(extra=extra):
                status, body = self.register(**extra)
                self.assertEqual((400, "invalid_wake_header"), (status, body.get("error")))

    def test_custom_authentication_headers_still_register(self):
        for extra in ({}, {"header": "X-Wake-Token", "scheme": None}):
            with self.subTest(extra=extra):
                status, body = self.register(**extra)
                self.assertEqual(200, status, body)

    def test_secret_must_be_printable_ascii(self):
        for suffix in ("\x00", "\x1b", "\x7f", "\u0100", "\U0001f511"):
            with self.subTest(codepoint=ord(suffix)):
                status, body = self.register(secret="synthetic-secret-" + suffix)
                self.assertEqual((400, "invalid_wake_secret"), (status, body.get("error")))


class HostedWakeAddressTests(unittest.TestCase):
    def resolve(self, *addresses):
        return patch.object(C.socket, "getaddrinfo", return_value=[
            (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
            for address in addresses])

    def test_nat64_does_not_make_private_ipv4_addresses_public(self):
        for address in ("64:ff9b::7f00:1", "64:ff9b::a00:1", "64:ff9b::a9fe:a9fe", "64:ff9b::c0a8:1",
                        "64:ff9b:1::7f00:1", "64:ff9b:1::a00:1"):
            with self.subTest(address=address), self.resolve(address):
                with self.assertRaises(ValueError):
                    C.public_address("synthetic.invalid", 443)

    def test_one_translated_private_answer_rejects_the_entire_host(self):
        with self.resolve("2606:4700:4700::1111", "64:ff9b::7f00:1"):
            with self.assertRaises(ValueError):
                C.public_address("synthetic.invalid", 443)

    def test_translation_prefixes_are_refused_even_with_public_embedded_ipv4(self):
        for address in ("64:ff9b::808:808", "64:ff9b:1::808:808"):
            with self.subTest(address=address), self.resolve(address):
                with self.assertRaises(ValueError):
                    C.public_address("synthetic.invalid", 443)

    def test_ordinary_public_addresses_remain_usable(self):
        for address in ("8.8.8.8", "2606:4700:4700::1111"):
            with self.subTest(address=address), self.resolve(address):
                self.assertEqual(address, C.public_address("synthetic.invalid", 443))


class HostedWakeKeyTests(unittest.TestCase):
    def test_concurrent_first_use_publishes_only_the_complete_key(self):
        with tempfile.TemporaryDirectory(prefix="yobiru hosted key test ") as home:
            created, resume = threading.Event(), threading.Event()
            real_open, results, errors = os.open, [], []
            first = [True]
            guard = threading.Lock()

            def pause_first_create(path, flags, *args, **kwargs):
                fd = real_open(path, flags, *args, **kwargs)
                with guard:
                    pause = bool(flags & os.O_CREAT) and first[0]
                    if pause:
                        first[0] = False
                if pause:
                    created.set()
                    if not resume.wait(5):
                        os.close(fd)
                        raise RuntimeError("synthetic key writer was not released")
                return fd

            def creator():
                try:
                    results.append(G.wake_key(home))
                except Exception as exc:
                    errors.append(type(exc).__name__)

            with patch.object(G.os, "open", side_effect=pause_first_create):
                writer = threading.Thread(target=creator)
                writer.start()
                try:
                    self.assertTrue(created.wait(5), "first key creation did not begin")
                    concurrent = G.wake_key(home)
                finally:
                    resume.set()
                    writer.join(5)
            self.assertFalse(writer.is_alive())
            self.assertEqual([], errors)
            self.assertEqual(32, len(concurrent))
            self.assertEqual([32], [len(key) for key in results])
            self.assertTrue(results[0] == concurrent == (Path(home) / "wake.key").read_bytes())
            if os.name == "posix":
                self.assertEqual(0o600, stat.S_IMODE((Path(home) / "wake.key").stat().st_mode))


if __name__ == "__main__":
    unittest.main()
