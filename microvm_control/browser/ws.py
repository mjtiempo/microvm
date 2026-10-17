"""ws — a minimal RFC 6455 WebSocket client on the standard library.

microvm-control runs from a bare checkout (no pip step, no third-party
imports), so the CDP transport cannot depend on `websockets`. This is the
client the plan calls for: handshake, masked client frames, fragmentation
reassembly, ping/pong, close, a hard message cap and deadlines. There is no
TLS — the peer is always 127.0.0.1, reached through an ephemeral SSH forward
into the guest.

Only what CDP needs is implemented: text messages, one connection, no
extensions, no compression. Failures are typed so `cdp` can map them onto
refusal codes.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import time

__all__ = [
    "OP_CLOSE", "OP_CONT", "OP_PING", "OP_PONG", "OP_TEXT",
    "DEFAULT_MAX_MESSAGE", "WebSocket",
    "WsClosed", "WsError", "WsHandshakeError", "WsProtocolError", "WsTimeout",
]

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

HANDSHAKE_CAP = 64 * 1024
DEFAULT_MAX_MESSAGE = 64 * 1024 * 1024


class WsError(Exception):
    """Base class for every websocket failure."""


class WsHandshakeError(WsError):
    """The peer did not complete an RFC 6455 handshake."""


class WsProtocolError(WsError):
    """The peer sent something that is not valid RFC 6455."""


class WsClosed(WsError):
    """The connection is gone (or was closed by the peer)."""


class WsTimeout(WsError):
    """No frame arrived within the deadline."""


def _accept_value(key: str) -> str:  # pi-lens-ignore: python-weak-hash -- RFC 6455 §4.1 mandates SHA-1
    # RFC 6455 §4.1 DEFINES Sec-WebSocket-Accept as SHA-1(key + GUID). The
    # digest is a handshake correlation token, not a security primitive; the
    # protocol gives no stronger choice.
    # pi-lens-ignore: python-weak-hash -- RFC 6455 §4.1 mandates SHA-1
    digest = hashlib.sha1((key + GUID).encode("ascii")).digest()  # noqa: S324  # nosemgrep: python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1
    return base64.b64encode(digest).decode("ascii")


class WebSocket:
    """One client connection; `recv_text` is the only reader."""

    def __init__(self, sock: socket.socket, buffer: bytes = b"",
                 max_message: int = DEFAULT_MAX_MESSAGE):
        self._sock = sock
        self._buf = buffer
        self._max = max_message
        self._closed = False

    # -- connect ---------------------------------------------------------- #
    @classmethod
    def connect(cls, host: str, port: int, path: str = "/",
                timeout: float = 10.0,
                max_message: int = DEFAULT_MAX_MESSAGE) -> "WebSocket":
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise WsError(f"cannot connect to {host}:{port}: {exc}") from exc
        sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path or '/'} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        try:
            sock.sendall(request.encode("ascii"))
        except OSError as exc:
            sock.close()
            raise WsError(f"handshake write failed: {exc}") from exc
        deadline = time.monotonic() + timeout
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                sock.close()
                raise WsTimeout("the websocket handshake did not answer in time")
            sock.settimeout(remaining)
            try:
                chunk = sock.recv(4096)
            except TimeoutError as exc:
                sock.close()
                raise WsTimeout("the websocket handshake did not answer in time") from exc
            except OSError as exc:
                sock.close()
                raise WsHandshakeError(f"handshake read failed: {exc}") from exc
            if not chunk:
                sock.close()
                raise WsHandshakeError("the peer closed during the handshake")
            buffer += chunk
            if len(buffer) > HANDSHAKE_CAP:
                sock.close()
                raise WsHandshakeError("the handshake answer is oversized")
        head, _, rest = buffer.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        status = lines[0].decode("latin-1", errors="replace")
        if " 101 " not in f" {status} ":
            sock.close()
            raise WsHandshakeError(f"the peer refused the upgrade: {status}")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, _, value = line.partition(b":")
            headers[name.decode("latin-1").strip().lower()] = (
                value.decode("latin-1").strip())
        if headers.get("sec-websocket-accept") != _accept_value(key):
            sock.close()
            raise WsHandshakeError("the peer's Sec-WebSocket-Accept does not match")
        if headers.get("upgrade", "").lower() != "websocket":
            sock.close()
            raise WsHandshakeError("the peer did not upgrade to websocket")
        return cls(sock, rest, max_message)

    # -- reading ---------------------------------------------------------- #
    def _read_exact(self, count: int, deadline: float | None) -> bytes:
        while len(self._buf) < count:
            if deadline is None:
                timeout = None
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WsTimeout("timed out reading from the websocket")
                timeout = remaining
            self._sock.settimeout(timeout)
            try:
                chunk = self._sock.recv(65536)
            except TimeoutError as exc:
                raise WsTimeout("timed out reading from the websocket") from exc
            except OSError as exc:
                self._closed = True
                raise WsClosed(f"the websocket failed: {exc}") from exc
            if not chunk:
                self._closed = True
                raise WsClosed("the websocket closed")
            self._buf += chunk
        out, self._buf = self._buf[:count], self._buf[count:]
        return out

    def _read_frame(self, deadline: float | None) -> tuple[bool, int, bytes]:
        first, second = self._read_exact(2, deadline)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        if opcode & 0x8 and opcode not in (OP_CLOSE, OP_PING, OP_PONG):
            raise WsProtocolError(f"reserved opcode 0x{opcode:x}")
        if second & 0x80:
            raise WsProtocolError("a server frame must not be masked")
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2, deadline))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8, deadline))[0]
        if length > self._max:
            raise WsProtocolError(f"frame of {length} bytes exceeds the cap")
        payload = self._read_exact(length, deadline) if length else b""
        return fin, opcode, payload

    def recv_text(self, timeout: float | None = None) -> str:
        """The next complete text message; answers pings on the way."""
        if self._closed:
            raise WsClosed("the websocket is closed")
        deadline = None if timeout is None else time.monotonic() + timeout
        opcode: int | None = None
        message = bytearray()
        while True:
            fin, op, payload = self._read_frame(deadline)
            if op == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if op == OP_PONG:
                continue
            if op == OP_CLOSE:
                with _suppress_any():
                    self._send_frame(OP_CLOSE, payload[:2])
                self._closed = True
                raise WsClosed("the peer closed the websocket")
            if op in (OP_TEXT, OP_BIN):
                if opcode is not None:
                    raise WsProtocolError("a new data frame interrupted a fragment")
                opcode = op
                message = bytearray(payload)
            elif op == OP_CONT:
                if opcode is None:
                    raise WsProtocolError("a continuation frame with nothing to continue")
                message += payload
            else:
                raise WsProtocolError(f"unsupported opcode 0x{op:x}")
            if len(message) > self._max:
                raise WsProtocolError("the message exceeds the cap")
            if fin:
                break
        if opcode != OP_TEXT:
            raise WsProtocolError("a binary message where text was expected")
        try:
            return message.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WsProtocolError(f"invalid UTF-8 in a text message: {exc}") from exc

    # -- writing ---------------------------------------------------------- #
    def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        if self._closed:
            raise WsClosed("the websocket is closed")
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        length = len(payload)
        header = bytes([0x80 | opcode])
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 1 << 16:
            header += bytes([0x80 | 126]) + struct.pack("!H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", length)
        try:
            self._sock.sendall(header + mask + masked)
        except OSError as exc:
            self._closed = True
            raise WsClosed(f"the websocket failed: {exc}") from exc

    def send_text(self, text: str) -> None:
        if self._closed:
            raise WsClosed("the websocket is closed")
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def send_ping(self, payload: bytes = b"") -> None:
        self._send_frame(OP_PING, payload[:125])

    def close(self) -> None:
        """Best-effort close; never raises."""
        if self._closed:
            return
        with _suppress_any():
            self._send_frame(OP_CLOSE, struct.pack("!H", 1000))
        self._closed = True
        with _suppress_os():
            self._sock.shutdown(socket.SHUT_RDWR)
        with _suppress_os():
            self._sock.close()


class _suppress_os:
    """`contextlib.suppress(OSError)` without importing contextlib for it."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None and issubclass(exc_type, OSError)


class _suppress_any:
    """Suppress any websocket error on a best-effort close path."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return True
