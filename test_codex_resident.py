"""Offline resident regression: real temporary Unix sockets, no Codex account or model."""
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from codex_queue_client import CodexAppClient
from codex_resident import ResidentDispatcher, ResidentError, UnixWebSocket, resident_status


def eventually(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition did not become true")


class FakeServer:
    """Models the reported lifecycle: accepted queue items run only while resumed and connected."""
    def __init__(self, root, thread_id, mode="normal"):
        self.path = str(root / "app.sock")
        self.thread_id, self.mode = thread_id, mode
        self.stop = threading.Event()
        self.requests, self.processed = [], []
        self.pending = []
        self.resumes, self.loaded = 0, False
        self.pings = 0
        self.connection = None
        self.error = None
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(self.path)
        self.listener.listen(8)
        self.listener.settimeout(0.1)
        self.worker = threading.Thread(target=self.run, daemon=True)
        self.worker.start()

    @staticmethod
    def send(conn, value, opcode=1, final=True):
        data = json.dumps(value).encode() if opcode == 1 else value
        size = bytes([len(data)]) if len(data) < 126 else b"\x7e" + struct.pack("!H", len(data))
        conn.sendall(bytes([(0x80 if final else 0) | opcode]) + size + data)

    def read(self, conn, n):
        data = b""
        while len(data) < n and not self.stop.is_set():
            try:
                chunk = conn.recv(n - len(data))
            except socket.timeout:
                if self.loaded and self.pending:
                    self.processed.extend(self.pending)
                    self.pending.clear()
                continue
            if not chunk:
                raise EOFError()
            data += chunk
        if len(data) != n:
            raise EOFError()
        return data

    def session(self, conn):
        raw = b""
        while b"\r\n\r\n" not in raw:
            raw += self.read(conn, 1)
        headers = dict(line.split(": ", 1) for line in raw.decode().split("\r\n")[1:] if ": " in line)
        key = headers["Sec-WebSocket-Key"]
        accept = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if self.mode == "bad_handshake":
            accept = "not-the-accept-key"
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      "Sec-WebSocket-Accept: " + accept + "\r\n\r\n").encode())
        while not self.stop.is_set():
            first, second = self.read(conn, 2)
            assert second & 128, "client frames must be masked"
            size = second & 127
            if size == 126:
                size = struct.unpack("!H", self.read(conn, 2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self.read(conn, 8))[0]
            mask = self.read(conn, 4)
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(self.read(conn, size)))
            opcode = first & 15
            if opcode == 9:
                self.pings += 1
                self.send(conn, payload, 10)
                continue
            if opcode == 10:
                continue
            value = json.loads(payload)
            self.requests.append(value)
            if value.get("method") == "initialize":
                self.send(conn, {"id": value["id"], "result": {"userAgent": "fake"}})
            elif value.get("method") == "thread/resume":
                self.resumes += 1
                if self.mode == "reject":
                    self.send(conn, {"id": value["id"], "error": {"code": -1, "message": "PRIVATE BODY"}})
                    continue
                thread = self.thread_id if self.mode != "wrong_thread" else str(uuid.uuid4())
                self.send(conn, {"id": value["id"], "result": {"thread": {"id": thread}}})
                self.loaded = thread == self.thread_id
                # Requests and notifications may include private content. Never answer or persist them.
                self.send(conn, {"id": "approval", "method": "item/commandExecution/requestApproval",
                                 "params": {"command": "PRIVATE BODY"}})
                self.send(conn, {"method": "item/agentMessage/delta", "params": {"delta": "PRIVATE BODY"}})
                self.send(conn, b"probe", opcode=9)

    def run(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.connection = conn
            conn.settimeout(0.05)
            try:
                self.session(conn)
            except (OSError, EOFError):
                pass
            except Exception as exc:
                self.error = exc
            finally:
                self.loaded = False
                conn.close()
                self.connection = None

    def disconnect(self):
        if self.connection:
            self.connection.shutdown(socket.SHUT_RDWR)

    def close(self):
        self.stop.set()
        self.listener.close()
        self.worker.join(timeout=2)


class ResidentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="yobiru-resident-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.thread_id = str(uuid.uuid4())
        self.config = {"dispatcher_thread_id": self.thread_id, "codex_path": sys.executable,
                       "codex_remote": "unix://" + str(self.root / "app.sock"),
                       "codex_home": str(self.root), "bridge_home": str(self.root), "timeout": 2}

    def start(self, mode="normal"):
        server = FakeServer(self.root, self.thread_id, mode)
        self.addCleanup(server.close)
        resident = ResidentDispatcher(self.config)
        self.addCleanup(resident.close)
        resident.start()
        return server, resident

    def test_queue_acceptance_needs_resident_and_reconnect_does_not_requeue(self):
        server = FakeServer(self.root, self.thread_id)
        self.addCleanup(server.close)
        client = CodexAppClient(self.config)
        with patch("codex_queue_client.subprocess.run") as queue:
            queue.return_value.returncode = 0
            queue.return_value.stdout = "Queued message message_0001 for thread %s.\n" % self.thread_id
            result = client.call_tool("send_message_to_thread", {"threadId": self.thread_id, "prompt": "notification"})
        server.pending.append(result["queueId"])
        self.assertEqual("enqueued", result["status"])
        time.sleep(0.1)
        self.assertEqual([], server.processed)  # reproduces the queue-only gap without a real turn
        resident = ResidentDispatcher(self.config)
        self.addCleanup(resident.close)
        resident.start()
        eventually(lambda: server.processed == ["message_0001"])
        self.assertEqual("connected", resident_status(self.root, self.thread_id)["state"])
        server.disconnect()
        eventually(lambda: not server.loaded)
        server.pending.append("message_0002")
        eventually(lambda: server.resumes >= 2 and server.processed == ["message_0001", "message_0002"])
        self.assertIsNone(server.error)
        self.assertEqual({"initialize", "initialized", "thread/resume"}, {r["method"] for r in server.requests})
        for request in server.requests:
            if request["method"] == "thread/resume":
                self.assertEqual({"threadId": self.thread_id, "excludeTurns": True}, request["params"])
        receipt = (self.root / "resident.json").read_text()
        self.assertNotIn("PRIVATE BODY", receipt)
        self.assertEqual(0o600, (self.root / "resident.json").stat().st_mode & 0o777)
        resident.close()
        self.assertEqual("stopped", resident_status(self.root, self.thread_id)["state"])

    def test_resume_rejected_has_no_connected_receipt_or_private_errors(self):
        server, _ = self.start("reject")
        eventually(lambda: resident_status(self.root, self.thread_id).get("reason") == "resume_rejected")
        self.assertFalse(server.loaded)
        self.assertNotIn("PRIVATE BODY", (self.root / "resident.json").read_text())

    def test_mismatched_thread_never_reports_connected(self):
        _, _ = self.start("wrong_thread")
        eventually(lambda: resident_status(self.root, self.thread_id).get("reason") == "thread_mismatch")

    def test_invalid_upgrade_never_sends_rpc(self):
        server, _ = self.start("bad_handshake")
        eventually(lambda: resident_status(self.root, self.thread_id).get("reason") == "handshake_invalid")
        self.assertEqual([], server.requests)

    def test_missing_socket_and_shutdown_during_backoff(self):
        resident = ResidentDispatcher(self.config)
        resident.start()
        self.addCleanup(resident.close)
        eventually(lambda: resident_status(self.root, self.thread_id).get("reason") == "socket_missing")
        begin = time.monotonic()
        resident.close()
        self.assertLess(time.monotonic() - begin, 1)

    def test_stale_or_different_identity_receipt_does_not_claim_loaded(self):
        resident = ResidentDispatcher(self.config)
        resident._status("connected")
        self.assertEqual("unknown", resident_status(self.root, str(uuid.uuid4()))["state"])
        with patch("codex_resident.time.time", return_value=time.time() + 20):
            self.assertEqual("stale", resident_status(self.root, self.thread_id)["state"])

    def test_idle_connection_heartbeats_and_pings_without_model_rpc(self):
        server, _ = self.start()
        eventually(lambda: server.loaded)
        first = json.loads((self.root / "resident.json").read_text())["updated_at"]
        eventually(lambda: server.pings >= 1, timeout=12)
        last = json.loads((self.root / "resident.json").read_text())["updated_at"]
        self.assertGreater(last, first + 4)
        self.assertEqual("connected", resident_status(self.root, self.thread_id)["state"])
        self.assertEqual(1, server.resumes)

    def test_retry_delays_double_and_cap_without_busy_loop(self):
        resident = ResidentDispatcher(self.config)
        delays = []
        class Stop:
            def is_set(self):
                return False
            def wait(self, seconds):
                delays.append(seconds)
                return len(delays) == 8
        resident.stop = Stop()
        resident._run()  # the configured socket is absent, so no connection or real wait is made
        self.assertEqual([1, 2, 4, 8, 16, 32, 60, 60], delays)

    def test_plugin_status_reads_resident_receipt_without_resuming(self):
        resident = ResidentDispatcher(self.config)
        resident._status("connected")
        (self.root / "codex-bridge.json").write_text(json.dumps(self.config))
        script = Path(__file__).parent / "plugins/yobiru/scripts/yobiru.py"
        result = subprocess.run([sys.executable, str(script), "--home", str(self.root), "codex-bridge", "status"],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("connected", json.loads(result.stdout)["resident"]["state"])

    def test_daemon_connects_only_once_and_sigterm_releases_lock(self):
        server = FakeServer(self.root, self.thread_id)
        self.addCleanup(server.close)
        self.config.update(tailmsg_home=str(self.root), poll_seconds=0.05)
        config_path = self.root / "bridge-config.json"
        config_path.write_text(json.dumps(self.config))
        command = [sys.executable, str(Path(__file__).with_name("codex_bridge.py")), "--config", str(config_path), "daemon"]
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        def stop():
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)
        self.addCleanup(stop)
        eventually(lambda: server.loaded)
        second = subprocess.run(command, capture_output=True, timeout=3)
        self.assertEqual(1, second.returncode)
        self.assertIn(b"daemon_already_running", second.stderr)
        process.terminate()
        _, error = process.communicate(timeout=5)
        self.assertEqual(0, process.returncode, error)
        eventually(lambda: not server.loaded)
        self.assertEqual("stopped", resident_status(self.root, self.thread_id)["state"])
        self.assertEqual(1, server.resumes)
        restarted = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            eventually(lambda: server.resumes == 2 and server.loaded)
        finally:
            restarted.terminate()
            restarted.wait(timeout=5)

    def test_daemon_and_status_expand_tilde_bridge_home(self):
        server = FakeServer(self.root, self.thread_id)
        self.addCleanup(server.close)
        self.config.update(bridge_home="~/bridge state", tailmsg_home=str(self.root), poll_seconds=0.05)
        config_path = self.root / "codex-bridge.json"
        config_path.write_text(json.dumps(self.config))
        environment = dict(os.environ, HOME=str(self.root))
        command = [sys.executable, str(Path(__file__).with_name("codex_bridge.py")),
                   "--config", str(config_path), "daemon"]
        process = subprocess.Popen(command, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            eventually(lambda: server.loaded)
            state = self.root / "bridge state"
            eventually(lambda: resident_status(state, self.thread_id)["state"] == "connected")
            self.assertTrue((state / "bridge.sqlite3").is_file())
            self.assertEqual("connected", resident_status(state, self.thread_id)["state"])
            script = Path(__file__).parent / "plugins/yobiru/scripts/yobiru.py"
            status = subprocess.run([sys.executable, str(script), "--home", str(self.root),
                                     "codex-bridge", "status"], env=environment,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(0, status.returncode, status.stderr)
            self.assertEqual("connected", json.loads(status.stdout)["resident"]["state"])
        finally:
            process.terminate()
            _, error = process.communicate(timeout=5)
        self.assertEqual(0, process.returncode, error)

    def test_fragmented_json_ping_and_frame_size_limit(self):
        client, peer = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(peer.close)
        ws = UnixWebSocket.__new__(UnixWebSocket)
        ws.socket, ws.buffer, ws.fragments = client, bytearray(), bytearray()
        ws.fragmented, ws.stop = False, threading.Event()
        peer.sendall(b'\x01\x06{"id":' + b'\x80\x021}')
        self.assertEqual({"id": 1}, ws.receive(time.monotonic() + 1))
        peer.sendall(b"\x81\x7f" + struct.pack("!Q", 8 * 1024 * 1024 + 1))
        with self.assertRaisesRegex(ResidentError, "response_too_large"):
            ws.receive(time.monotonic() + 1)
        peer.sendall(b'\x81\x02\xff\xfe')
        with self.assertRaisesRegex(ResidentError, "protocol_error"):
            ws.receive(time.monotonic() + 1)


if __name__ == "__main__":
    unittest.main()
