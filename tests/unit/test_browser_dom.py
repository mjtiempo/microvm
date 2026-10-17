"""Unit tests for the vendored page verbs (tests first, TDD).

Every test drives `dom` against `FakeCdp`: the eval queue is the page's answers
in order, `results` are the CDP methods, and the assertions are the verdicts.
"""

from __future__ import annotations

import base64
import hashlib
import struct
import sys
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from microvm_control.errors import MvError  # noqa: E402
from microvm_control.browser import dom  # noqa: E402
from tests.unit.browser_fakes import FakeCdp  # noqa: E402

SID = "sid-1"


def png_bytes(width: int, height: int) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    chunk = (struct.pack(">I", 13) + b"IHDR" + ihdr
             + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr)))
    return signature + chunk


def resolve_ok(text="Go", hit=True, found=True):
    return {"found": found, "total": 1, "tag": "a", "text": text,
            "box": {"x": 10, "y": 20, "w": 100, "h": 40},
            "center": {"x": 60, "y": 40}, "in_viewport": True, "hit": hit,
            "disabled": False}


SNAP1 = {"url": "https://one.example/", "title": "One", "scroll": 0}
SNAP2 = {"url": "https://two.example/", "title": "Two", "scroll": 300}


class TextTests(unittest.TestCase):
    def test_text_truncation_comes_from_the_page(self):
        cdp = FakeCdp(eval_queue=[{"text": "hello", "length": 11}])
        data = dom.text(cdp, SID, chars=20)
        self.assertEqual(data["text"], "hello")
        self.assertEqual(data["length"], 11)
        self.assertTrue(data["truncated"])
        self.assertIn("slice(0, 20)", cdp.evaluate_calls[0])

    def test_text_selector_prefixes_the_answer(self):
        cdp = FakeCdp(eval_queue=[{"text": "hi", "length": 2}])
        data = dom.text(cdp, SID, selector="main", chars=100)
        self.assertEqual(data["selector"], "main")
        self.assertIn("main", cdp.evaluate_calls[0])
        self.assertFalse(data["truncated"])


class FindTests(unittest.TestCase):
    def test_find_returns_matches_and_totals(self):
        cdp = FakeCdp(eval_queue=[{
            "matches": [{"tag": "button", "text": "Sign in", "href": "",
                         "box": {}, "center": {}, "in_viewport": True, "hit": True}],
            "total": 3}])
        data = dom.find(cdp, SID, text="Sign in", cap=10)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["total"], 3)
        self.assertIn("10", cdp.evaluate_calls[0])


class ExtractTests(unittest.TestCase):
    def test_extract_returns_records_and_bounds(self):
        cdp = FakeCdp(eval_queue=[{
            "records": [{"text": "a", "url": "/a"}], "total": 5, "truncated": True}])
        data = dom.extract(cdp, SID, each="article",
                           fields=["text=h2", "url=a@href"], cap=1, chars=50)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["total"], 5)
        self.assertTrue(data["truncated"])

    def test_extract_requires_a_field(self):
        cdp = FakeCdp()
        with self.assertRaises(MvError) as caught:
            dom.extract(cdp, SID, each="article", fields=[], cap=10, chars=50)
        self.assertEqual(caught.exception.code, "bad-args")


class ClickTests(unittest.TestCase):
    def test_click_dispatches_and_reports_changed(self):
        cdp = FakeCdp(eval_queue=[resolve_ok(), SNAP1, SNAP2])
        data = dom.click(cdp, SID, text="Go")
        self.assertTrue(data["clicked"])
        self.assertTrue(data["changed"])
        self.assertEqual(data["before"]["scroll"], 0)
        self.assertEqual(data["after"]["scroll"], 300)
        kinds = [p.get("type") for m, p, _ in cdp.calls
                 if m == "Input.dispatchMouseEvent"]
        self.assertEqual(kinds, ["mousePressed", "mouseReleased"])

    def test_click_refuses_when_occluded(self):
        cdp = FakeCdp(eval_queue=[resolve_ok(hit=False)])
        with self.assertRaises(MvError) as caught:
            dom.click(cdp, SID, selector="#buy")
        self.assertEqual(caught.exception.code, "occluded")
        self.assertFalse([m for m, _, _ in cdp.calls if m == "Input.dispatchMouseEvent"])

    def test_click_refuses_when_nothing_matches(self):
        cdp = FakeCdp(eval_queue=[resolve_ok(found=False)])
        with self.assertRaises(MvError) as caught:
            dom.click(cdp, SID, text="missing")
        self.assertEqual(caught.exception.code, "no-match")

    def test_click_at_point_reports_what_it_reached(self):
        cdp = FakeCdp(eval_queue=[SNAP1, {"tag": "button", "text": "Buy", "summary": "button"},
                                  SNAP2])
        data = dom.click(cdp, SID, at="60,40")
        self.assertFalse(data["verified"])
        self.assertEqual(data["under"]["tag"], "button")
        self.assertTrue(data["clicked"])


class InsertTests(unittest.TestCase):
    def test_insert_verifies_length_growth(self):
        cdp = FakeCdp(eval_queue=[2, 7], results={"Input.insertText": {}})
        data = dom.insert(cdp, SID, "hello")
        self.assertEqual(data["length_before"], 2)
        self.assertEqual(data["length_after"], 7)
        self.assertTrue(data["verified"])
        self.assertEqual(cdp.calls[0][1]["text"], "hello")

    def test_insert_refuses_without_focus(self):
        cdp = FakeCdp(eval_queue=[-1])
        with self.assertRaises(MvError) as caught:
            dom.insert(cdp, SID, "hello")
        self.assertEqual(caught.exception.code, "no-focus")

    def test_type_sends_real_keys_and_verifies(self):
        cdp = FakeCdp(eval_queue=[0, 3], results={"Input.dispatchKeyEvent": {}})
        data = dom.type_text(cdp, SID, "abc")
        self.assertEqual(data["length_after"], 3)
        self.assertTrue(data["verified"])
        keys = [p for m, p, _ in cdp.calls if m == "Input.dispatchKeyEvent"]
        self.assertEqual(len(keys), 6)

    def test_type_paces_keystrokes_when_asked(self):
        sleeps: list[float] = []
        cdp = FakeCdp(eval_queue=[0, 3], results={"Input.dispatchKeyEvent": {}})
        data = dom.type_text(cdp, SID, "abc", delay=0.05, _sleep=sleeps.append)
        self.assertTrue(data["verified"])
        self.assertEqual(data["delay_s"], 0.05)
        self.assertEqual(sleeps, [0.05, 0.05])   # between the three keys

    def test_type_without_delay_never_sleeps(self):
        sleeps: list[float] = []
        cdp = FakeCdp(eval_queue=[0, 1], results={"Input.dispatchKeyEvent": {}})
        dom.type_text(cdp, SID, "a", _sleep=sleeps.append)
        self.assertEqual(sleeps, [])

    def test_type_delay_is_bounded(self):
        cdp = FakeCdp()
        with self.assertRaises(MvError) as caught:
            dom.type_text(cdp, SID, "a", delay=9)
        self.assertEqual(caught.exception.code, "bad-args")


class PressTests(unittest.TestCase):
    def test_press_is_honest_about_verification(self):
        cdp = FakeCdp(results={"Input.dispatchKeyEvent": {}})
        data = dom.press(cdp, SID, "enter")
        self.assertFalse(data["verified"])
        self.assertTrue(data["dispatched"])
        keys = [p for m, p, _ in cdp.calls if m == "Input.dispatchKeyEvent"]
        self.assertEqual([k["type"] for k in keys], ["keyDown", "keyUp"])

    def test_unknown_key_is_bad_args(self):
        cdp = FakeCdp()
        with self.assertRaises(MvError) as caught:
            dom.press(cdp, SID, "banana")
        self.assertEqual(caught.exception.code, "bad-args")


class CheckSelectTests(unittest.TestCase):
    def test_check_already_checked_changes_nothing(self):
        cdp = FakeCdp(eval_queue=[resolve_ok(), True])
        data = dom.check(cdp, SID, selector="#agree")
        self.assertTrue(data["checked"])
        self.assertFalse(data["changed"])

    def test_check_clicks_and_verifies(self):
        cdp = FakeCdp(eval_queue=[resolve_ok(), False, True],
                      results={"Input.dispatchMouseEvent": {}})
        data = dom.check(cdp, SID, selector="#agree")
        self.assertTrue(data["changed"])
        self.assertTrue(data["checked"])

    def test_select_by_label(self):
        cdp = FakeCdp(eval_queue=[resolve_ok(), {"selected": True, "value": "b",
                                                 "by": "label"}])
        data = dom.select(cdp, SID, selector="#color", value="Blue")
        self.assertEqual(data["by"], "label")
        self.assertEqual(data["value"], "b")

    def test_select_without_match_lists_options(self):
        cdp = FakeCdp(eval_queue=[resolve_ok(), {"selected": False,
                                                 "options": ["Red", "Blue"]}])
        with self.assertRaises(MvError) as caught:
            dom.select(cdp, SID, selector="#color", value="Green")
        self.assertEqual(caught.exception.code, "no-match")
        self.assertEqual((caught.exception.detail or {}).get("options"), ["Red", "Blue"])


class FocusTests(unittest.TestCase):
    def test_focus_reports_the_pages_own_answer(self):
        cdp = FakeCdp(eval_queue=[dict(resolve_ok(), focused=True)])
        data = dom.focus(cdp, SID, selector="#name")
        self.assertTrue(data["focused"])
        self.assertIn("el.focus()", cdp.evaluate_calls[0])

    def test_focus_not_landing_is_a_refusal(self):
        cdp = FakeCdp(eval_queue=[dict(resolve_ok(), focused=False)])
        with self.assertRaises(MvError) as caught:
            dom.focus(cdp, SID, selector="#name")
        self.assertEqual(caught.exception.code, "focus-not-verified")


class ScrollTests(unittest.TestCase):
    def test_scroll_by_verifies_movement(self):
        cdp = FakeCdp(eval_queue=[0, 300], results={"Input.dispatchMouseEvent": {}})
        data = dom.scroll(cdp, SID, by=300)
        self.assertTrue(data["moved"])
        self.assertEqual((data["before"], data["after"]), (0, 300))

    def test_scroll_to_top_verifies_movement(self):
        cdp = FakeCdp(eval_queue=[500, 0], results={"Input.dispatchMouseEvent": {}})
        data = dom.scroll(cdp, SID, edge="top")
        self.assertEqual((data["before"], data["after"]), (500, 0))


class WaitTests(unittest.TestCase):
    def test_wait_for_element_polls_to_success(self):
        cdp = FakeCdp(eval_queue=[False, True])
        data = dom.wait(cdp, SID, for_="element", selector="#x",
                        timeout=1.0, poll_interval=0.01)
        self.assertEqual(data["for"], "element")
        self.assertTrue(data["waited_s"] >= 0)

    def test_wait_timeout_names_what_it_waited_for(self):
        cdp = FakeCdp(evals={"querySelector": False})
        with self.assertRaises(MvError) as caught:
            dom.wait(cdp, SID, for_="element", selector="#x",
                     timeout=0.2, poll_interval=0.01)
        self.assertEqual(caught.exception.code, "wait-timeout")
        self.assertIn("#x", caught.exception.message)

    def test_wait_for_js_uses_the_page_expression(self):
        cdp = FakeCdp(evals={"window.ready": lambda e: True})
        data = dom.wait(cdp, SID, for_="js", expr="window.ready",
                        timeout=1.0, poll_interval=0.01)
        self.assertEqual(data["for"], "js")

    def test_wait_bad_kind_is_bad_args(self):
        cdp = FakeCdp()
        with self.assertRaises(MvError) as caught:
            dom.wait(cdp, SID, for_="banana", timeout=0.2)
        self.assertEqual(caught.exception.code, "bad-args")


class ScreenshotTests(unittest.TestCase):
    VIEWPORT = {"dpr": 2, "iw": 800, "ih": 600, "sw": 800, "sh": 600}

    def screenshot(self, cdp, out: Path, **kw):
        return dom.screenshot(cdp, SID, str(out), **kw)

    def test_viewport_screenshot_verifies_png_dimensions(self):
        payload = base64.b64encode(png_bytes(1600, 1200)).decode()
        cdp = FakeCdp(eval_queue=[self.VIEWPORT],
                      results={"Page.captureScreenshot": {"data": payload}})
        with self.tempfile() as out:
            data = self.screenshot(cdp, out)
            self.assertTrue(data["verified"])
            self.assertEqual((data["width"], data["height"]), (1600, 1200))
            self.assertEqual(data["mode"], "viewport")
            self.assertEqual(hashlib.sha256(out.read_bytes()).hexdigest(), data["sha256"])

    def test_dimension_mismatch_writes_nothing(self):
        payload = base64.b64encode(png_bytes(1600, 1100)).decode()
        cdp = FakeCdp(eval_queue=[self.VIEWPORT],
                      results={"Page.captureScreenshot": {"data": payload}})
        with self.tempfile() as out:
            with self.assertRaises(MvError) as caught:
                self.screenshot(cdp, out)
            self.assertEqual(caught.exception.code, "screenshot-not-verified")
            self.assertFalse(out.exists())

    def test_existing_file_refuses_without_force(self):
        payload = base64.b64encode(png_bytes(1600, 1200)).decode()
        cdp = FakeCdp(eval_queue=[self.VIEWPORT],
                      results={"Page.captureScreenshot": {"data": payload}})
        with self.tempfile() as out:
            out.write_bytes(b"old")
            with self.assertRaises(MvError) as caught:
                self.screenshot(cdp, out)
            self.assertEqual(caught.exception.code, "file-exists")
            self.screenshot(cdp, out, force=True)
            self.assertTrue(out.read_bytes().startswith(b"\x89PNG"))

    def test_selector_mode_clips_to_the_element(self):
        payload = base64.b64encode(png_bytes(200, 100)).decode()
        cdp = FakeCdp(eval_queue=[self.VIEWPORT,
                                  {"x": 10, "y": 20, "w": 100, "h": 50}],
                      results={"Page.captureScreenshot": {"data": payload}})
        with self.tempfile() as out:
            data = self.screenshot(cdp, out, selector="#hero")
            self.assertEqual(data["mode"], "element")
            clip = [p["clip"] for m, p, _ in cdp.calls
                    if m == "Page.captureScreenshot"][0]
            self.assertEqual((clip["x"], clip["y"], clip["width"], clip["height"]),
                             (10.0, 20.0, 100.0, 50.0))

    def test_bad_clip_is_bad_args(self):
        cdp = FakeCdp(eval_queue=[self.VIEWPORT])
        with self.tempfile() as out:
            with self.assertRaises(MvError) as caught:
                self.screenshot(cdp, out, clip="1,2,0,4")
            self.assertEqual(caught.exception.code, "bad-args")

    def tempfile(self):
        import tempfile
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        path = Path(holder.name) / "shot.png"
        return _TempPath(path, holder)


class _TempPath:
    def __init__(self, path: Path, holder):
        self._path = path
        self._holder = holder

    def __enter__(self) -> Path:
        return self._path

    def __exit__(self, *exc) -> None:
        self._holder.cleanup()


class PdfTests(unittest.TestCase):
    def test_pdf_writes_and_verifies_magic(self):
        payload = base64.b64encode(b"%PDF-1.4 fake body").decode()
        cdp = FakeCdp(results={"Page.printToPDF": {"data": payload}})
        with _Tmp() as out:
            data = dom.pdf(cdp, SID, str(out))
            self.assertTrue(data["verified"])
            self.assertTrue(out.read_bytes().startswith(b"%PDF"))

    def test_pdf_refuses_a_non_pdf_answer(self):
        payload = base64.b64encode(b"not a pdf").decode()
        cdp = FakeCdp(results={"Page.printToPDF": {"data": payload}})
        with _Tmp() as out:
            with self.assertRaises(MvError) as caught:
                dom.pdf(cdp, SID, str(out))
            self.assertEqual(caught.exception.code, "browser-verify-failed")


class JsTests(unittest.TestCase):
    def test_js_is_an_unverified_escape_hatch(self):
        cdp = FakeCdp(eval_queue=[{"a": 1}])
        data = dom.js(cdp, SID, "({a: 1})")
        self.assertEqual(data["result"], {"a": 1})
        self.assertFalse(data["verified"])

    def test_js_result_cap(self):
        cdp = FakeCdp(eval_queue=["x" * 50])
        with self.assertRaises(MvError) as caught:
            dom.js(cdp, SID, "'x'", max_result=10)
        self.assertEqual(caught.exception.code, "result-too-large")


class DialogTests(unittest.TestCase):
    def test_dialog_state_reads_the_event(self):
        cdp = FakeCdp(results={"Page.enable": {}},
                      events=[{"method": "Page.javascriptDialogOpening",
                               "params": {"type": "alert"}}])
        data = dom.dialog(cdp, SID, "state")
        self.assertTrue(data["open"])

    def test_dialog_accept_handles_it(self):
        cdp = FakeCdp(results={"Page.enable": {},
                               "Page.handleJavaScriptDialog": {}})
        data = dom.dialog(cdp, SID, "accept", text="answer")
        self.assertTrue(data["handled"])
        params = [p for m, p, _ in cdp.calls if m == "Page.handleJavaScriptDialog"][0]
        self.assertTrue(params["accept"])
        self.assertEqual(params["promptText"], "answer")

    def test_dialog_accept_without_a_dialog_is_no_dialog(self):
        cdp = FakeCdp(errors={"Page.handleJavaScriptDialog":
                              {"code": -32000, "message": "No dialog is showing"}})
        with self.assertRaises(MvError) as caught:
            dom.dialog(cdp, SID, "accept")
        self.assertEqual(caught.exception.code, "no-dialog")


class UploadTests(unittest.TestCase):
    def test_upload_sets_files_and_reads_them_back(self):
        cdp = FakeCdp(eval_queue=[[{"name": "f.txt", "size": 3}]],
                      results={
                          "DOM.getDocument": {"root": {"nodeId": 1}},
                          "DOM.querySelector": {"nodeId": 5},
                          "DOM.setFileInputFiles": {},
                      })
        data = dom.upload(cdp, SID, "/persist/work/f.txt", selector="#file")
        self.assertEqual(data["files"][0]["name"], "f.txt")
        self.assertTrue(data["verified"])
        files = [p["files"] for m, p, _ in cdp.calls if m == "DOM.setFileInputFiles"][0]
        self.assertEqual(files, ["/persist/work/f.txt"])

    def test_upload_without_a_match_is_no_match(self):
        cdp = FakeCdp(results={
            "DOM.getDocument": {"root": {"nodeId": 1}},
            "DOM.querySelector": {"nodeId": 0},
            "DOM.setFileInputFiles": {},
        })
        with self.assertRaises(MvError) as caught:
            dom.upload(cdp, SID, "/persist/work/f.txt", selector="#file")
        self.assertEqual(caught.exception.code, "no-match")


class _Tmp:
    def __enter__(self) -> Path:
        import tempfile
        self._holder = tempfile.TemporaryDirectory()
        return Path(self._holder.name) / "out.pdf"

    def __exit__(self, *exc) -> None:
        self._holder.cleanup()


class NavTests(unittest.TestCase):
    READY = {"url": "https://two.example/", "ready": "complete", "error": ""}

    def test_nav_verifies_the_url(self):
        cdp = FakeCdp(eval_queue=[self.READY],
                      results={"Page.navigate": {"frameId": "F"}})
        data = dom.nav(cdp, SID, "https://two.example/", timeout=1.0)
        self.assertEqual(data["url"], "https://two.example/")
        self.assertTrue(data["verified"])

    def test_nav_refuses_a_non_http_url(self):
        cdp = FakeCdp()
        with self.assertRaises(MvError) as caught:
            dom.nav(cdp, SID, "file:///etc/passwd")
        self.assertEqual(caught.exception.code, "bad-args")

    def test_nav_to_an_error_page_is_nav_failed(self):
        error_page = {"url": "chrome-error://chromewebdata/", "ready": "complete",
                      "error": ""}
        cdp = FakeCdp(evals={"location.href": lambda e: error_page},
                      results={"Page.navigate": {}})
        with self.assertRaises(MvError) as caught:
            dom.nav(cdp, SID, "https://nope.invalid/", timeout=0.3,
                    poll_interval=0.02)
        self.assertEqual(caught.exception.code, "nav-failed")

    def test_reload_verifies_a_new_document(self):
        cdp = FakeCdp(eval_queue=[100.0, 200.0], results={"Page.reload": {}})
        data = dom.reload(cdp, SID, timeout=1.0)
        self.assertTrue(data["verified"])

    def test_history_back_reports_movement(self):
        cdp = FakeCdp(eval_queue=[{"url": "https://two.example/", "ready": "complete"},
                                  {"url": "https://one.example/", "ready": "complete"}],
                      results={"Runtime.evaluate": {"result": {"value": None}}})
        data = dom.history(cdp, SID, "back", timeout=1.0)
        self.assertTrue(data["moved"])
        self.assertEqual(data["url"], "https://one.example/")


if __name__ == "__main__":
    unittest.main()
