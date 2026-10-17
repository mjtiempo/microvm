"""Fakes for the browser subsystem tests: a scriptable WebSocket/HTTP server.

Server frames are unmasked (RFC 6455), client frames must be masked; the
parser asserts that, so a client that forgets to mask fails the echo.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import socket
import struct
import threading

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0, 1, 2, 8, 9, 10


def _accept(key: str) -> str:  # pi-lens-ignore: python-weak-hash -- RFC 6455 §4.1 mandates SHA-1
    # RFC 6455 mandates SHA-1 for the accept token (a correlation value,
    # not a security primitive).  # nosemgrep
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()  # noqa: S324  # pi-lens-ignore: python-weak-hash -- RFC 6455 §4.1


class FakeWsServer:
    """One-connection WebSocket server on 127.0.0.1; `handler(self)` drives it."""

    def __init__(self, handler=None, http_handler=None):
        self.http_handler = http_handler  # callable(path, headers) -> (status, body bytes)
        self.handler = handler
        self.received: list[tuple[int, bytes]] = []
        self.pongs: list[bytes] = []
        self.handshake_key = ""
        self.handshake_seen = False
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self.conn: socket.socket | None = None
        self.error: BaseException | None = None
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # -- server side ------------------------------------------------------ #
    def _run(self) -> None:
        """Serve connections in turn: HTTP `/json` first, then one websocket."""
        try:
            while True:
                try:
                    conn, _ = self._sock.accept()
                except OSError:
                    return
                self.conn = conn
                self._buf = b""
                try:
                    self._serve_conn(conn)
                except BaseException as exc:  # the test asserts on this
                    self.error = exc
                    return
        finally:
            self._done.set()

    def _serve_conn(self, conn: socket.socket) -> None:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        first = head.split(b"\r\n", 1)[0].decode(errors="replace")
        lines = head.decode(errors="replace").split("\r\n")
        lowered = head.decode(errors="replace").lower()
        if "upgrade" not in lowered or "websocket" not in lowered:
            path = first.split(" ")[1] if " " in first else "/"
            if self.http_handler is None:
                status, body = "404 Not Found", b"{}"
            else:
                status, body = self.http_handler(path, lines)  # type: ignore[misc]
            conn.sendall(
                f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\n"
                f"Content-Type: application/json\r\n\r\n".encode() + body)
            with contextlib.suppress(OSError):
                conn.close()
            return
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()
        self.handshake_key = headers.get("sec-websocket-key", "")
        self.handshake_seen = True
        conn.sendall(
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {_accept(self.handshake_key)}\r\n\r\n".encode())
        self._buf = rest
        if self.handler:
            self.handler(self)

    @property
    def _buffer(self) -> bytes:
        return getattr(self, "_buf", b"")

    @_buffer.setter
    def _buffer(self, value: bytes) -> None:
        self._buf = value

    def _read_exact(self, n: int) -> bytes:
        assert self.conn is not None
        while len(self._buffer) < n:
            chunk = self.conn.recv(65536)
            if not chunk:
                raise ConnectionError("client closed")
            self._buffer += chunk
        out, self._buffer = self._buffer[:n], self._buffer[n:]
        return out

    def recv_frame(self) -> tuple[int, bytes, bool]:
        assert self.conn is not None
        b1, b2 = self._read_exact(2)
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        if not b2 & 0x80:
            raise AssertionError("client frame is not masked")
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        mask = self._read_exact(4)
        payload = self._read_exact(length)
        payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        if opcode == OP_PONG:
            self.pongs.append(payload)
        return opcode, payload, fin

    def recv_message(self) -> tuple[int, bytes]:
        opcode, payload, fin = self.recv_frame()
        while not fin:
            _, more, fin = self.recv_frame()
            payload += more
        self.received.append((opcode, payload))
        return opcode, payload

    def send_frame(self, opcode: int, payload: bytes = b"", fin: bool = True) -> None:
        assert self.conn is not None
        b1 = (0x80 if fin else 0) | opcode
        if len(payload) < 126:
            header = bytes([b1, len(payload)])
        elif len(payload) < 1 << 16:
            header = bytes([b1, 126]) + struct.pack("!H", len(payload))
        else:
            header = bytes([b1, 127]) + struct.pack("!Q", len(payload))
        self.conn.sendall(header + payload)

    def send_text(self, text: str) -> None:
        self.send_frame(OP_TEXT, text.encode())

    def send_fragments(self, text: str, parts: int = 2) -> None:
        data = text.encode()
        size = max(1, len(data) // parts)
        chunks = [data[i:i + size] for i in range(0, len(data), size)] or [b""]
        for i, chunk in enumerate(chunks):
            self.send_frame(OP_TEXT if i == 0 else OP_CONT, chunk,
                            fin=i == len(chunks) - 1)

    def send_ping(self, payload: bytes = b"ping") -> None:
        self.send_frame(OP_PING, payload)

    def send_close(self, code: int = 1000) -> None:
        self.send_frame(OP_CLOSE, struct.pack("!H", code))

    def stop(self) -> None:
        for sock in (self.conn, self._sock):
            if sock is None:
                continue
            try:
                sock.close()
            except OSError:
                pass

    def wait(self, timeout: float = 5.0) -> None:
        self._done.wait(timeout)


class FakeCdp:
    """An in-process stand-in for `microvm_control.browser.cdp.Cdp`.

    Records calls; answers from `results` (method → result | callable(params))
    and `evals` (expression → value | callable(expression)).
    """

    def __init__(self, results=None, evals=None, events=None, eval_queue=None,
                 errors=None):
        self.results = results or {}
        self.evals = evals or {}
        self.eval_queue = list(eval_queue or [])
        self.errors = errors or {}
        self.calls: list[tuple[str, dict, str]] = []
        self.evaluate_calls: list[str] = []
        self.events = events or []
        self.closed = False

    def call(self, method, params=None, session_id="", timeout=None):
        params = params or {}
        self.calls.append((method, params, session_id))
        if method in self.errors:
            from microvm_control.errors import MvError
            error = self.errors[method]
            raise MvError("cdp-error", str(error.get("message") or method),
                          detail=error)
        answer = self.results.get(method, {})
        if callable(answer):
            answer = answer(params)
        return answer

    def evaluate(self, expression, session_id="", timeout=None):
        self.evaluate_calls.append(expression)
        if self.eval_queue:
            value = self.eval_queue.pop(0)
            if isinstance(value, BaseException):
                raise value
            return value(expression) if callable(value) else value
        for needle, value in self.evals.items():
            if needle in expression:
                return value(expression) if callable(value) else value
        raise AssertionError(f"no fake eval for {expression!r}")

    def attach(self, target_id):
        return f"sid-{target_id}"

    def pump(self, wait=0.2):
        return list(self.events)

    def close(self):
        self.closed = True


class FakeCdpServer(FakeWsServer):
    """A fake Chromium DevTools endpoint: /json over HTTP, CDP over the socket.

    `results` maps a CDP method to its result (or a callable(params)).
    `errors` maps a method to an error envelope; `respond=False` never answers
    (for deadline tests); `events` are pushed before the first reply.
    """

    def __init__(self, results=None, errors=None, tabs=None, version=None,
                 events=None, respond=True, **kw):
        self.results = results or {}
        self.errors = errors or {}
        self.tabs = tabs if tabs is not None else [
            {"id": "TAB1", "type": "page", "url": "https://one.example/",
             "title": "One", "webSocketDebuggerUrl":
             "ws://127.0.0.1:0/devtools/page/TAB1"},
            {"id": "TAB2", "type": "page", "url": "https://two.example/",
             "title": "Two", "webSocketDebuggerUrl":
             "ws://127.0.0.1:0/devtools/page/TAB2"},
            {"id": "SW1", "type": "service_worker", "url": "https://one.example/sw.js",
             "title": "sw"},
        ]
        self.version = version
        self.events = events or []
        self.respond = respond
        self.cdp_calls: list[dict] = []
        super().__init__(handler=self._cdp_handler, http_handler=self._http, **kw)

    def _version_payload(self) -> dict:
        if self.version is not None:
            return dict(self.version)
        return {
            "Browser": "HeadlessChrome/151.0.0.0",
            "Protocol-Version": "1.3",
            "User-Agent": "Mozilla/5.0 (X11) HeadlessChrome/151.0.0.0",
            "V8-Version": "15.1",
            "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/browser/FAKE",
        }

    def _http(self, path: str, headers: list[str]):
        if path.startswith("/json/version"):
            return "200 OK", json.dumps(self._version_payload()).encode()
        if path.startswith("/json/list"):
            return "200 OK", json.dumps(self.tabs).encode()
        return "404 Not Found", b"{}"

    def _cdp_handler(self, server: FakeWsServer) -> None:
        for event in self.events:
            server.send_text(json.dumps(event))
        while True:
            try:
                _, payload = server.recv_message()
            except (AssertionError, ConnectionError, OSError):
                return
            message = json.loads(payload.decode())
            self.cdp_calls.append(message)
            if "id" not in message:
                continue
            if not self.respond:
                continue
            method = message.get("method")
            if method in self.errors:
                reply = {"id": message["id"], "error": self.errors[method]}
            else:
                result = self.results.get(method, {})
                if callable(result):
                    result = result(message.get("params") or {})
                reply = {"id": message["id"], "result": result}
            if message.get("sessionId"):
                reply["sessionId"] = message["sessionId"]
            try:
                server.send_text(json.dumps(reply))
            except (ConnectionError, OSError):
                return
