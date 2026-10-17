"""Unit tests for the vendored CDP layer and tab resolution (tests first)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from microvm_control.errors import MvError  # noqa: E402
from tests.unit.browser_fakes import FakeCdpServer  # noqa: E402


class EndpointTests(unittest.TestCase):
    def test_version_and_page_rows(self):
        from microvm_control.browser import cdp
        server = FakeCdpServer()
        try:
            version = cdp.version(server.port, timeout=5.0)
            self.assertEqual(version["Browser"], "HeadlessChrome/151.0.0.0")
            rows = cdp.page_rows(server.port, timeout=5.0)
            self.assertEqual([r["id"] for r in rows], ["TAB1", "TAB2"])
        finally:
            server.stop()

    def test_version_refuses_unreachable_port(self):
        import socket
        quiet = socket.socket()
        quiet.bind(("127.0.0.1", 0))
        port = quiet.getsockname()[1]
        quiet.close()
        from microvm_control.browser import cdp
        with self.assertRaises(MvError) as caught:
            cdp.version(port, timeout=1.0)
        self.assertEqual(caught.exception.code, "cdp-unreachable")

    def test_foreign_websocket_host_refused(self):
        from microvm_control.browser import cdp
        server = FakeCdpServer(version={
            "Browser": "HeadlessChrome/151.0.0.0",
            "webSocketDebuggerUrl": "ws://evil.example:9222/devtools/browser/x",
        })
        try:
            with self.assertRaises(MvError) as caught:
                cdp.websocket_url(server.port, timeout=5.0)
            self.assertEqual(caught.exception.code, "cdp-not-local")
        finally:
            server.stop()


class CdpTests(unittest.TestCase):
    def cdp_for(self, server: FakeCdpServer, **kw):
        from microvm_control.browser.cdp import Cdp
        return Cdp(server.port, timeout=5.0, **kw)

    def test_call_roundtrip(self):
        server = FakeCdpServer(results={"Browser.getVersion": {"product": "HeadlessChrome"}})
        cdp = self.cdp_for(server)
        try:
            result = cdp.call("Browser.getVersion")
            self.assertEqual(result["product"], "HeadlessChrome")
            self.assertTrue(server.handshake_seen)
            self.assertEqual(server.cdp_calls[0]["method"], "Browser.getVersion")
        finally:
            cdp.close()
            server.stop()

    def test_error_envelope_is_a_refusal(self):
        server = FakeCdpServer(errors={"Page.navigate": {"code": -32000, "message": "no target"}})
        cdp = self.cdp_for(server)
        try:
            with self.assertRaises(MvError) as caught:
                cdp.call("Page.navigate", {"url": "https://x/"})
            self.assertEqual(caught.exception.code, "cdp-error")
            self.assertIn("no target", caught.exception.message)
            self.assertEqual((caught.exception.detail or {}).get("code"), -32000)
        finally:
            cdp.close()
            server.stop()

    def test_evaluate_exception_is_js_error(self):
        server = FakeCdpServer(results={
            "Runtime.evaluate": {"exceptionDetails": {
                "text": "Uncaught", "exception": {"description": "ReferenceError: nope"}}},
        })
        cdp = self.cdp_for(server)
        try:
            with self.assertRaises(MvError) as caught:
                cdp.evaluate("nope()")
            self.assertEqual(caught.exception.code, "js-error")
            self.assertIn("ReferenceError", caught.exception.message)
        finally:
            cdp.close()
            server.stop()

    def test_evaluate_returns_value(self):
        server = FakeCdpServer(results={
            "Runtime.evaluate": {"result": {"type": "string", "value": "hello"}}})
        cdp = self.cdp_for(server)
        try:
            self.assertEqual(cdp.evaluate("'hello'"), "hello")
        finally:
            cdp.close()
            server.stop()

    def test_events_are_collected_not_mistaken_for_replies(self):
        server = FakeCdpServer(results={"Page.enable": {}}, events=[
            {"method": "Page.javascriptDialogOpening", "params": {"type": "alert"}}])
        cdp = self.cdp_for(server)
        try:
            cdp.call("Page.enable", {})
            cdp.pump(wait=0.5)
            self.assertEqual(cdp.events[0]["method"], "Page.javascriptDialogOpening")
        finally:
            cdp.close()
            server.stop()

    def test_session_id_is_forwarded(self):
        server = FakeCdpServer(results={"Runtime.evaluate": {"result": {"value": 1}}})
        cdp = self.cdp_for(server)
        try:
            cdp.call("Runtime.evaluate", {"expression": "1"}, session_id="sid-42")
            self.assertEqual(server.cdp_calls[0]["sessionId"], "sid-42")
        finally:
            cdp.close()
            server.stop()

    def test_attach_returns_session(self):
        server = FakeCdpServer(results={"Target.attachToTarget": {"sessionId": "S1"}})
        cdp = self.cdp_for(server)
        try:
            self.assertEqual(cdp.attach("TAB1"), "S1")
            self.assertEqual(server.cdp_calls[0]["params"],
                             {"targetId": "TAB1", "flatten": True})
        finally:
            cdp.close()
            server.stop()

    def test_deadline_is_a_refusal(self):
        server = FakeCdpServer(respond=False)
        cdp = self.cdp_for(server)
        try:
            with self.assertRaises(MvError) as caught:
                cdp.call("Browser.getVersion", timeout=0.3)
            self.assertEqual(caught.exception.code, "cdp-error")
            self.assertIn("did not answer", caught.exception.message)
        finally:
            cdp.close()
            server.stop()

    def test_evaluate_deadline_is_eval_timeout(self):
        server = FakeCdpServer(respond=False)
        cdp = self.cdp_for(server)
        try:
            with self.assertRaises(MvError) as caught:
                cdp.evaluate("while(true){}", timeout=0.3)
            self.assertEqual(caught.exception.code, "eval-timeout")
        finally:
            cdp.close()
            server.stop()


class TargetTests(unittest.TestCase):
    TABS = [
        {"id": "AAA111", "type": "page", "title": "Alpha", "url": "https://alpha.example/"},
        {"id": "BBB222", "type": "page", "title": "Beta", "url": "https://beta.example/x"},
    ]

    def test_id_prefix(self):
        from microvm_control.browser import targets
        self.assertEqual(targets.resolve(self.TABS, "id:AAA")["id"], "AAA111")

    def test_title_substring(self):
        from microvm_control.browser import targets
        self.assertEqual(targets.resolve(self.TABS, "beta")["id"], "BBB222")

    def test_url_substring(self):
        from microvm_control.browser import targets
        self.assertEqual(targets.resolve(self.TABS, "alpha.example")["id"], "AAA111")

    def test_no_match_names_the_tabs(self):
        from microvm_control.browser import targets
        with self.assertRaises(MvError) as caught:
            targets.resolve(self.TABS, "nope")
        self.assertEqual(caught.exception.code, "no-page-tab")
        self.assertEqual(len((caught.exception.detail or {}).get("tabs") or []), 2)

    def test_ambiguous_spec_refuses(self):
        from microvm_control.browser import targets
        with self.assertRaises(MvError) as caught:
            targets.resolve(self.TABS, "example")
        self.assertEqual(caught.exception.code, "tab-ambiguous")

    def test_default_needs_exactly_one(self):
        from microvm_control.browser import targets
        self.assertEqual(targets.resolve(self.TABS[:1], "")["id"], "AAA111")
        with self.assertRaises(MvError) as caught:
            targets.resolve(self.TABS, "")
        self.assertEqual(caught.exception.code, "tab-ambiguous")
        with self.assertRaises(MvError) as caught:
            targets.resolve([], "")
        self.assertEqual(caught.exception.code, "no-page-tab")


if __name__ == "__main__":
    unittest.main()
