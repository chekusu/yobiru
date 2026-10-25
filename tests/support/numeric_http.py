"""Loopback HTTP fixtures that do not depend on the host's reverse DNS resolver."""
from http.server import HTTPServer as BaseHTTPServer, ThreadingHTTPServer as BaseThreadingHTTPServer
from socketserver import TCPServer


class NumericBind:
    def server_bind(self):
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class HTTPServer(NumericBind, BaseHTTPServer):
    pass


class ThreadingHTTPServer(NumericBind, BaseThreadingHTTPServer):
    pass
