"""Unit tests for the vendored WebSocket client (tests first, TDD)."""

from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.unit.browser_fakes import OP_PING, OP_TEXT, FakeWsServer  # noqa: E402


class WsClientTests(unittest.TestCase):
    def connect(self, server: FakeWsServer, **kw):
        from microvm_control.browser.ws import WebSocket
        return WebSocket.connect("127.0.0.1", server.port, "/devtools/x", timeout=5.0, **kw)

    def test_handshake_and_echo(self):
        def handler(server: FakeWsServer):
            _, payload = server.recv_message()
            server.send_text(payload.decode())

        server = FakeWsServer(handler)
        ws = self.connect(server)
        ws.send_text("hello")
        self.assertEqual(ws.recv_text(timeout=5.0), "hello")
        ws.close()
        server.stop()
        self.assertIsNone(server.error)

    def test_client_frames_are_masked(self):
        def handler(server: FakeWsServer):
            _, payload = server.recv_message()   # asserts mask bit
            server.send_text("ok")

        server = FakeWsServer(handler)
        ws = self.connect(server)
        ws.send_text("abc")
        self.assertEqual(ws.recv_text(timeout=5.0), "ok")
        ws.close()
        server.stop()
        self.assertIsNone(server.error)

    def test_fragmented_message_is_reassembled(self):
        def handler(server: FakeWsServer):
            server.send_fragments("fragment-me-please", parts=3)

        server = FakeWsServer(handler)
        ws = self.connect(server)
        self.assertEqual(ws.recv_text(timeout=5.0), "fragment-me-please")
        ws.close()
        server.stop()

    def test_ping_is_answered_and_message_continues(self):
        def handler(server: FakeWsServer):
            server.send_text("first")
            server.send_ping(b"are-you-there")
            server.send_text("second")
            server.recv_frame()   # the client's pong

        server = FakeWsServer(handler)
        ws = self.connect(server)
        self.assertEqual(ws.recv_text(timeout=5.0), "first")
        self.assertEqual(ws.recv_text(timeout=5.0), "second")
        deadline = time.monotonic() + 3
        while not server.pongs and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(server.pongs, [b"are-you-there"])
        ws.close()
        server.stop()

    def test_close_frame_raises_ws_closed(self):
        def handler(server: FakeWsServer):
            server.send_close()

        server = FakeWsServer(handler)
        ws = self.connect(server)
        from microvm_control.browser.ws import WsClosed
        with self.assertRaises(WsClosed):
            ws.recv_text(timeout=5.0)
        ws.close()
        server.stop()

    def test_oversized_message_is_refused(self):
        def handler(server: FakeWsServer):
            server.send_text("x" * 100)
            time.sleep(3)

        server = FakeWsServer(handler)
        from microvm_control.browser.ws import WsProtocolError
        ws = self.connect(server, max_message=64)
        with self.assertRaises(WsProtocolError):
            ws.recv_text(timeout=5.0)
        ws.close()
        server.stop()

    def test_bad_accept_header_is_refused(self):
        # raw server: answers 101 with a wrong Sec-WebSocket-Accept
        import socket
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]

        def raw():
            conn, _ = sock.accept()
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\n"
                         b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                         b"Sec-WebSocket-Accept: bogus\r\n\r\n")
            time.sleep(2)
            conn.close()

        import threading
        threading.Thread(target=raw, daemon=True).start()
        from microvm_control.browser.ws import WsHandshakeError, WebSocket
        with self.assertRaises(WsHandshakeError):
            WebSocket.connect("127.0.0.1", port, "/", timeout=5.0)
        sock.close()

    def test_non_101_refused(self):
        import socket
        import threading
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]

        def raw():
            conn, _ = sock.accept()
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            conn.close()

        threading.Thread(target=raw, daemon=True).start()
        from microvm_control.browser.ws import WsHandshakeError, WebSocket
        with self.assertRaises(WsHandshakeError):
            WebSocket.connect("127.0.0.1", port, "/", timeout=5.0)
        sock.close()

    def test_timeout_raises_ws_timeout(self):
        def handler(server: FakeWsServer):
            time.sleep(3)

        server = FakeWsServer(handler)
        from microvm_control.browser.ws import WsTimeout
        ws = self.connect(server)
        with self.assertRaises(WsTimeout):
            ws.recv_text(timeout=0.2)
        ws.close()
        server.stop()

    def test_send_after_close_raises(self):
        def handler(server: FakeWsServer):
            time.sleep(1)

        server = FakeWsServer(handler)
        from microvm_control.browser.ws import WsClosed
        ws = self.connect(server)
        ws.close()
        with self.assertRaises(WsClosed):
            ws.send_text("late")
        server.stop()


if __name__ == "__main__":
    unittest.main()
