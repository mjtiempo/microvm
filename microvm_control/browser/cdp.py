"""cdp — HTTP `/json` reads and one synchronous CDP connection over `ws`.

The vendored core from `mark-browser-control` (MIT), adapted to a synchronous
stdlib transport. Rules kept from upstream:

* reads retry nowhere — one deadline per call, and a mutation is never
  replayed (the caller decides what a retry would mean);
* a CDP error envelope is a refusal (`cdp-error`) carrying the protocol's own
  error object in `detail`;
* an `exceptionDetails` from `Runtime.evaluate` is `js-error`, not a value;
* the websocket URL from `/json/version` is host-checked to loopback — the
  tunnel makes everything the client speaks to 127.0.0.1 by construction, so
  anything else is an endpoint that is not ours.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.parse

from ..errors import MvError
from . import ws as ws_mod

__all__ = ["Cdp", "http_json", "page_rows", "version", "websocket_url"]

JSON_CAP = 8 * 1024 * 1024
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def http_json(host: str, port: int, path: str, timeout: float = 5.0):
    """One bounded GET whose answer must be JSON; failures are refusals."""
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        body = response.read(JSON_CAP)
    except (OSError, http.client.HTTPException) as exc:
        raise MvError(
            "cdp-unreachable",
            f"the CDP endpoint on 127.0.0.1:{port} did not answer: {exc}",
        ) from exc
    finally:
        conn.close()
    if response.status != 200:
        raise MvError("cdp-error", f"{path} answered HTTP {response.status}")
    try:
        return json.loads(body)
    except ValueError as exc:
        raise MvError("cdp-error", f"{path} answered no JSON: {exc}") from exc


def version(port: int, timeout: float = 5.0) -> dict:
    """`GET /json/version` — the browser's own identity card."""
    data = http_json("127.0.0.1", port, "/json/version", timeout)
    if not isinstance(data, dict):
        raise MvError("cdp-error", "/json/version answered something that is not an object")
    return data


def page_rows(port: int, timeout: float = 5.0) -> list[dict]:
    """The PAGE targets from `/json/list`; other target types are dropped."""
    data = http_json("127.0.0.1", port, "/json/list", timeout)
    if not isinstance(data, list):
        raise MvError("cdp-error", "/json/list answered something that is not a list")
    return [row for row in data
            if isinstance(row, dict) and row.get("type") == "page" and row.get("id")]


def websocket_url(port: int, timeout: float = 5.0) -> str:
    """The browser-level websocket URL, with its host checked."""
    return _ws_url_from(version(port, timeout))


def _ws_url_from(info: dict) -> str:
    url = str(info.get("webSocketDebuggerUrl") or "")
    if not url:
        raise MvError("cdp-error", "/json/version names no webSocketDebuggerUrl")
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError as exc:
        raise MvError("cdp-not-local", f"{url!r} is not a usable endpoint ({exc})") from exc
    if host not in LOOPBACK_HOSTS:
        raise MvError(
            "cdp-not-local",
            f"the endpoint names a websocket on {host!r} — refusing to send "
            "page traffic off the loopback CDP endpoint",
        )
    return url


class Cdp:
    """One CDP connection to the browser-level endpoint of a running browser."""

    def __init__(self, port: int, timeout: float = 60.0,
                 version_info: dict | None = None):
        self.port = port
        self.timeout = timeout
        info = version_info if version_info is not None else version(
            port, timeout=min(timeout, 10.0))
        url = _ws_url_from(info)
        parts = urllib.parse.urlparse(url)
        host = parts.hostname or "127.0.0.1"
        ws_port = parts.port or 80
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        try:
            self._ws = ws_mod.WebSocket.connect(
                host, ws_port, path, timeout=min(timeout, 10.0))
        except ws_mod.WsError as exc:
            raise MvError(
                "cdp-unreachable",
                f"the CDP websocket on 127.0.0.1:{port} did not connect: {exc}",
            ) from exc
        self._next_id = 0
        self.events: list[dict] = []

    # -- calls ------------------------------------------------------------ #
    def call(self, method: str, params: dict | None = None,
             session_id: str = "", timeout: float | None = None) -> dict:
        """One request → one reply, under one deadline; never replayed."""
        self._next_id += 1
        request_id = self._next_id
        message: dict = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        if session_id:
            message["sessionId"] = session_id
        try:
            self._ws.send_text(json.dumps(message))
        except ws_mod.WsError as exc:
            raise MvError("cdp-unreachable",
                          f"the CDP connection failed: {exc}") from exc
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self._deadline(method, timeout)
            try:
                raw = self._ws.recv_text(timeout=remaining)
            except ws_mod.WsTimeout as exc:
                raise self._deadline(method, timeout) from exc
            except ws_mod.WsClosed as exc:
                raise MvError("cdp-unreachable",
                              f"the CDP connection closed during {method}: {exc}") from exc
            except ws_mod.WsError as exc:
                raise MvError("cdp-error", f"{method}: {exc}") from exc
            try:
                data = json.loads(raw)
            except ValueError as exc:
                raise MvError("cdp-error", f"{method}: CDP sent no JSON: {exc}") from exc
            if not isinstance(data, dict):
                raise MvError("cdp-error", f"{method}: CDP sent a non-object message")
            if "id" not in data:
                self.events.append(data)
                if len(self.events) > 500:
                    del self.events[:-500]
                continue
            if data["id"] != request_id:
                raise MvError(
                    "cdp-error",
                    f"{method}: reply id {data['id']} does not match request {request_id}",
                )
            if "error" in data:
                error = data["error"] if isinstance(data["error"], dict) else {}
                raise MvError(
                    "cdp-error",
                    f"{method}: {error.get('message') or 'the protocol refused it'}",
                    detail=error,
                )
            result = data.get("result")
            return result if isinstance(result, dict) else {}

    def _deadline(self, method: str, timeout: float | None) -> MvError:
        seconds = timeout if timeout is not None else self.timeout
        if method == "Runtime.evaluate":
            return MvError("eval-timeout",
                           f"the page did not answer within {seconds:.0f}s "
                           "(a renderer can be parked)")
        return MvError("cdp-error", f"{method} did not answer within {seconds:.0f}s")

    def evaluate(self, expression: str, session_id: str = "",
                 timeout: float | None = None, await_promise: bool = True):
        """`Runtime.evaluate` by value; a page exception is `js-error`."""
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True,
             "awaitPromise": await_promise},
            session_id=session_id, timeout=timeout,
        )
        if result.get("exceptionDetails"):
            details = result["exceptionDetails"]
            reason = ((details.get("exception") or {}).get("description")
                      or details.get("text") or "the expression threw")
            raise MvError("js-error", str(reason), detail=details)
        return (result.get("result") or {}).get("value")

    # -- sessions --------------------------------------------------------- #
    def attach(self, target_id: str) -> str:
        """Attach a FLAT session to one tab; the id scopes every later call."""
        result = self.call("Target.attachToTarget",
                           {"targetId": target_id, "flatten": True})
        session_id = str(result.get("sessionId") or "")
        if not session_id:
            raise MvError("cdp-error", f"could not attach to tab {target_id}",
                          detail=result)
        return session_id

    def pump(self, wait: float = 0.25) -> list[dict]:
        """Drain events that arrived within `wait`; returns what was seen."""
        seen_start = len(self.events)
        deadline = time.monotonic() + wait
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                raw = self._ws.recv_text(timeout=remaining)
            except ws_mod.WsTimeout:
                break
            except ws_mod.WsError:
                break
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if isinstance(data, dict) and "id" not in data:
                self.events.append(data)
        return self.events[seen_start:]

    def close(self) -> None:
        self._ws.close()
