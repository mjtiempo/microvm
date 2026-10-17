"""Unit tests for browser.json state and the pure staleness verdict (TDD).

Covers the file round-trip (missing/malformed read as None, atomic save,
idempotent clear) and the pure verdicts the service reconciles with: the
guest `/proc/<pid>/cmdline` check and the `/json/version` fingerprint.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from microvm_control import store  # noqa: E402
from microvm_control.browser import state  # noqa: E402

PROFILE = "/persist/chromium"
FINGERPRINT = {"browser": "HeadlessChrome/151.0.0.0", "protocol": "1.3",
               "user_agent": "Mozilla/5.0 HeadlessChrome/151.0.0.0",
               "v8": "15.1"}
CMDLINE = ("chromium --headless --no-sandbox --remote-debugging-port=9222 "
           f"--user-data-dir={PROFILE} about:blank")


class StateFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.inst = store.Instance(self.home, "demo")

    def test_roundtrip(self):
        record = state.new_state(pid=4711, port=9222, port_requested=9222,
                                 profile=PROFILE, log_path="/persist/work/b.log",
                                 fingerprint=FINGERPRINT)
        state.save(self.inst, record)
        loaded = state.load(self.inst)
        assert loaded is not None
        self.assertEqual(loaded["pid"], 4711)
        self.assertEqual(loaded["schema"], state.SCHEMA)
        self.assertEqual(loaded["fingerprint"]["protocol"], "1.3")

    def test_missing_file_is_none(self):
        self.assertIsNone(state.load(self.inst))

    def test_malformed_file_is_none(self):
        path = state.path(self.inst)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        self.assertIsNone(state.load(self.inst))

    def test_clear_removes_the_file(self):
        state.save(self.inst, state.new_state(pid=1, port=2, port_requested=2,
                                              profile=PROFILE, log_path="",
                                              fingerprint=FINGERPRINT))
        state.clear(self.inst)
        self.assertIsNone(state.load(self.inst))
        state.clear(self.inst)  # idempotent

    def test_save_is_atomic(self):
        state.save(self.inst, state.new_state(pid=1, port=2, port_requested=2,
                                              profile=PROFILE, log_path="",
                                              fingerprint=FINGERPRINT))
        leftovers = [p.name for p in state.path(self.inst).parent.iterdir()
                     if p.name != "browser.json"]
        self.assertEqual(leftovers, [])
        self.assertEqual(json.loads(state.path(self.inst).read_text())["pid"], 1)


class CmdlineVerdictTests(unittest.TestCase):
    def test_ours_is_true(self):
        self.assertTrue(state.cmdline_is_ours(CMDLINE, PROFILE, 9222))

    def test_wrong_port_is_false(self):
        self.assertFalse(state.cmdline_is_ours(CMDLINE, PROFILE, 9223))

    def test_wrong_profile_is_false(self):
        self.assertFalse(state.cmdline_is_ours(CMDLINE, "/persist/other", 9222))

    def test_no_profile_flag_is_false(self):
        self.assertFalse(state.cmdline_is_ours(
            "chromium --headless --remote-debugging-port=9222", PROFILE, 9222))

    def test_empty_cmdline_is_false(self):
        self.assertFalse(state.cmdline_is_ours("", PROFILE, 9222))

    def test_stale_reason_names_the_cause(self):
        self.assertIn("gone", state.stale_reason(None, PROFILE, 9222) or "")
        self.assertIn("another", (state.stale_reason(
            "chromium --user-data-dir=/other --remote-debugging-port=9222",
            PROFILE, 9222) or ""))
        self.assertIsNone(state.stale_reason(CMDLINE, PROFILE, 9222))


class FingerprintTests(unittest.TestCase):
    def test_identical_matches(self):
        self.assertTrue(state.fingerprint_matches(FINGERPRINT, dict(FINGERPRINT)))

    def test_changed_field_is_refused(self):
        changed = dict(FINGERPRINT, browser="HeadlessChrome/152.0.0.0")
        self.assertFalse(state.fingerprint_matches(FINGERPRINT, changed))

    def test_missing_current_field_is_refused(self):
        self.assertFalse(state.fingerprint_matches(
            FINGERPRINT, {"browser": FINGERPRINT["browser"]}))

    def test_empty_stored_fingerprint_is_refused(self):
        self.assertFalse(state.fingerprint_matches({}, dict(FINGERPRINT)))

    def test_changed_fields_names_both_values(self):
        changed = dict(FINGERPRINT, browser="HeadlessChrome/152.0.0.0")
        pairs = state.changed_fields(FINGERPRINT, changed)
        self.assertEqual(len(pairs), 1)
        name, was, now = pairs[0]
        self.assertEqual(name, "browser")
        self.assertEqual((was, now), (FINGERPRINT["browser"], changed["browser"]))


if __name__ == "__main__":
    unittest.main()


class DeleteClearsStateTests(unittest.TestCase):
    """`delete` clears browser.json with the instance (a VM `stop` does not)."""

    def test_delete_clears_browser_json(self):
        import tempfile
        from microvm_control import lifecycle
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            inst = store.Instance(home, "demo")
            inst.save_meta({"schema": 1, "id": "demo", "state": "stopped",
                            "pid": None})
            state.save(inst, state.new_state(
                pid=4711, port=9222, port_requested=9222, profile=PROFILE,
                log_path="", fingerprint=FINGERPRINT))
            data = lifecycle._delete_instance(inst, home, purge=False,
                                              force=False, timeout=5.0)
            self.assertFalse((inst.dir / "browser.json").exists())
            trash = Path(data["trash_path"])
            self.assertTrue(trash.is_dir())
            self.assertFalse((trash / "browser.json").exists())
