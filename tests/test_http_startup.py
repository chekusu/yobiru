"""Numeric listeners must become usable without consulting reverse DNS."""
import http.client
import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler
from unittest import mock

from support.public_harness import C
import bot_gateway as G
import tailmsg as T


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        data = json.dumps({"address": self.server.server_name, "port": self.server.server_port}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class NumericListenerTests(unittest.TestCase):
    def check_listener(self, host, family):
        class ServiceServer(C.NumericHTTPServer):
            address_family = family

        class ReceiverServer(T.NumericHTTPServer):
            address_family = family

        for factory in (lambda: ServiceServer((host, 0), Handler),
                        lambda: ReceiverServer((host, 0), Handler),
                        lambda: G.BoundedServer((host, 0), Handler, 2, family)):
            with self.subTest(host=host, factory=factory):
                with mock.patch("socket.getfqdn", side_effect=AssertionError("reverse DNS must not delay startup")):
                    server = factory()
                    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
                    worker.start()
                    try:
                        port = server.server_address[1]
                        conn = http.client.HTTPConnection(host, port, timeout=2)
                        try:
                            conn.request("GET", "/")
                            reply = conn.getresponse()
                            self.assertEqual(200, reply.status)
                            self.assertEqual({"address": host, "port": port}, json.loads(reply.read()))
                        finally:
                            conn.close()
                    finally:
                        server.shutdown()
                        server.server_close()
                        worker.join(2)
                    self.assertFalse(worker.is_alive())

    def test_ipv4_listener_serves_when_reverse_dns_is_unavailable(self):
        self.check_listener("127.0.0.1", socket.AF_INET)

    @unittest.skipUnless(socket.has_ipv6, "IPv6 sockets are unavailable")
    def test_ipv6_listener_serves_when_reverse_dns_is_unavailable(self):
        self.check_listener("::1", socket.AF_INET6)
