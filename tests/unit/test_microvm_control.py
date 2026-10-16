"""Unit tests for microvm-control internals (no KVM, no host networking)."""

from __future__ import annotations

import json
import os
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from microvm_control import audit, backup, fcapi, identity, lifecycle, netpool, store, util  # noqa: E402
from microvm_control.errors import MvError  # noqa: E402


def run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


class TempDirCase(unittest.TestCase):
    def setUp(self):
        cache = Path.home() / ".cache"
        cache.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="mvunit-", dir=str(cache)))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def make_image(self, files: dict | None = None) -> Path:
        stage = self.tmp / "stage"
        (stage / "etc").mkdir(parents=True, exist_ok=True)
        for rel, data in (files or {}).items():
            path = stage / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        image = self.tmp / "img.ext4"
        with open(image, "wb") as handle:
            handle.truncate(32 * 1024**2)
        cp = run(["mke2fs", "-q", "-t", "ext4", "-F", "-m", "0", "-d", str(stage), str(image)])
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return image

    def make_instance(self, home: Path, instance_id: str) -> store.Instance:
        inst = store.Instance(home, instance_id)
        (inst.dir / "run").mkdir(parents=True, exist_ok=True)
        inst.save_meta({"schema": 1, "id": instance_id, "state": "created", "pid": None})
        return inst


class KillProcessTests(TempDirCase):
    def test_never_signals_a_non_firecracker_pid(self):
        proc = subprocess.Popen(["sleep", "30"])
        self.addCleanup(proc.wait)
        try:
            result = lifecycle._kill_process(proc.pid, grace=0.2)
            self.assertTrue(result)
            time.sleep(0.1)
            self.assertIsNone(proc.poll(), "an unrelated process was signalled")
        finally:
            proc.terminate()
            proc.wait()


class IdentityTests(TempDirCase):
    def test_read_file_preserves_debugfs_like_content(self):
        image = self.make_image({"etc/x": b"debugfs 9.9.9\nkeep\n"})
        self.assertEqual(identity.read_file(image, "/etc/x"), b"debugfs 9.9.9\nkeep\n")

    def test_read_file_returns_stdout_exactly(self):
        fake = subprocess.CompletedProcess([], 0, "debugfs 1.2.3\nreal\n", "")
        with mock.patch.object(identity, "run", return_value=fake):
            self.assertEqual(identity.read_file(Path("x"), "/etc/x"),
                             b"debugfs 1.2.3\nreal\n")

    def test_write_file_verifies_content_and_inode(self):
        image = self.make_image()
        identity.write_file(image, "/etc/x", b"data", "0100600")
        fields = identity._inode_fields(image, "/etc/x")
        self.assertEqual(fields.get("Mode"), "0600")
        self.assertEqual(fields.get("User"), "0")
        self.assertEqual(fields.get("Group"), "0")
        with self.assertRaises(MvError) as ctx:
            identity.verify_inode(image, "/etc/x", 0o644)
        self.assertEqual(ctx.exception.code, "image-write-failed")


class LockingTests(TempDirCase):
    def test_source_paused_holds_the_instance_lock(self):
        home = self.tmp / "home"
        inst = self.make_instance(home, "demo")
        held = threading.Event()
        release = threading.Event()

        def holder():
            with inst.lock():
                held.set()
                release.wait(3)

        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(held.wait(2), "lock holder did not start")
        started = time.monotonic()
        threading.Timer(0.3, release.set).start()
        with lifecycle._source_paused(inst, {"net": {}, "pid": None}):
            pass
        elapsed = time.monotonic() - started
        thread.join(3)
        self.assertGreaterEqual(elapsed, 0.25, "source lock was not respected")


class QuotaAndLimitsTests(TempDirCase):
    def test_quota_enforced(self):
        home = self.tmp / "home"
        home.mkdir()
        self.make_instance(home, "one")
        (home / "config.json").write_text('{"max_instances": 1}')
        with self.assertRaises(MvError) as ctx:
            store.enforce_instance_quota(home)
        self.assertEqual(ctx.exception.code, "quota-exceeded")
        (home / "config.json").write_text('{"max_instances": 2}')
        store.enforce_instance_quota(home)

    def test_validate_sizes(self):
        lifecycle._validate_sizes(2, 2048)
        for vcpu, mem in ((0, 1024), (2, 1), (999, 1024)):
            with self.assertRaises(MvError):
                lifecycle._validate_sizes(vcpu, mem)


class StoreHardeningTests(TempDirCase):
    def test_ensure_home_is_private(self):
        home = self.tmp / "home"
        store.ensure_home(home)
        self.assertEqual(oct(home.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct((home / "instances").stat().st_mode & 0o777), "0o700")

    def test_audit_is_private_and_can_redact_argv(self):
        home = self.tmp / "home"
        home.mkdir()
        with mock.patch.dict(os.environ, {"MICROVM_AUDIT_ARGS": "0"}):
            audit.append(home, {"verb": "exec", "argv": ["secret"]})
        entry = audit.read(home, 1)[0]
        self.assertIsNone(entry["argv"])
        self.assertTrue(entry["argv_redacted"])
        self.assertEqual(oct((home / "audit.log").stat().st_mode & 0o777), "0o600")


class BackupHardeningTests(TempDirCase):
    def test_archive_member_scan(self):
        src = self.tmp / "src"
        src.mkdir()
        (src / "f").write_text("x")
        evil = self.tmp / "evil.tar.zst"
        cp = run(["tar", "--zstd", "--transform", "s|src/f|../escape|", "-cf", str(evil),
                  "-C", str(self.tmp), "src/f"])
        self.assertEqual(cp.returncode, 0, cp.stderr)
        with self.assertRaises(MvError) as ctx:
            backup._assert_safe_members(evil)
        self.assertEqual(ctx.exception.code, "backup-corrupt")
        good = self.tmp / "good.tar.zst"
        self.assertEqual(run(["tar", "--zstd", "-cf", str(good), "-C", str(self.tmp), "src/f"]).returncode, 0)
        backup._assert_safe_members(good)

    def test_ensure_space(self):
        util.ensure_space(self.tmp, 1, label="tiny")
        with self.assertRaises(MvError) as ctx:
            util.ensure_space(self.tmp, 10**18, label="huge")
        self.assertEqual(ctx.exception.code, "disk-full")


class NetpoolHardeningTests(TempDirCase):
    def test_tap_user_validation(self):
        self.assertEqual(netpool.validate_tap_user("good.name-1"), "good.name-1")
        for bad in ("bad; rm -rf /", "", "with space", "semi;colon"):
            with self.assertRaises(MvError) as ctx:
                netpool.validate_tap_user(bad)
            self.assertEqual(ctx.exception.code, "bad-args")

    def test_invoking_identity_and_adoption(self):
        with mock.patch.dict(os.environ, {"SUDO_UID": "1234", "SUDO_GID": "5678"}, clear=False):
            self.assertEqual(util.invoking_identity(), (1234, 5678))
        os.environ.pop("SUDO_UID", None)
        os.environ.pop("SUDO_GID", None)
        os.environ.pop("SUDO_USER", None)
        self.assertIsNone(util.invoking_identity())
        calls = []
        target = self.tmp / "file"
        target.write_text("x")
        with mock.patch.object(util.os, "chown", side_effect=lambda *args: calls.append(args)):
            util.adopt_ownership([target], (123, 456))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], target)
        self.assertEqual(calls[0][1:], (123, 456))
        util.adopt_ownership([target], None)
        self.assertEqual(len(calls), 1)


class RuntimeTests(TempDirCase):
    def test_resolve_runtime_refuses_missing(self):
        home = self.tmp / "home"
        bogus = self.tmp / "no-runtime"
        bogus.mkdir()
        meta = {"id": "demo", "config": {"image_dir": str(bogus), "kernel": "vmlinux-x"}}
        with self.assertRaises(MvError) as ctx:
            lifecycle._resolve_runtime(home, meta)
        self.assertEqual(ctx.exception.code, "image-missing")
        (bogus / "firecracker").write_text("x")
        (bogus / "vmlinux-x").write_text("x")
        firecracker, kernel = lifecycle._resolve_runtime(home, meta)
        self.assertEqual(firecracker, bogus / "firecracker")
        self.assertEqual(kernel, bogus / "vmlinux-x")


class _ListBodyHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server API
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"[1, 2, 3]")

    def log_message(self, *args):
        return


class FcApiTests(TempDirCase):
    def test_error_body_that_is_not_an_object(self):
        sock = self.tmp / "api.sock"
        server = socketserver.UnixStreamServer(str(sock), _ListBodyHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        with self.assertRaises(MvError) as ctx:
            fcapi.FcApi(sock).request("GET", "/")
        self.assertEqual(ctx.exception.code, "api-error")


class MainWiringTests(TempDirCase):
    def test_privileged_verb_adopts_state_ownership(self):
        import io
        from microvm_control import __main__ as cli
        home = self.tmp / "home"
        home.mkdir()
        env = {"SUDO_UID": "1000", "SUDO_GID": "1000", "SUDO_USER": "someone"}
        with mock.patch.object(cli.os, "geteuid", return_value=0), \
                mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(netpool, "net_install",
                                  return_value=cli.envelope(ok=True)), \
                mock.patch.object(cli, "adopt_ownership") as adopted, \
                mock.patch("sys.stdout", new=io.StringIO()), \
                mock.patch("sys.stderr", new=io.StringIO()):
            rc = cli.main(["--home", str(home), "net", "install"])
        self.assertEqual(rc, 0)
        adopted.assert_called_once()
        paths, owner = adopted.call_args[0]
        self.assertIn(home / "net.json", paths)
        self.assertIn(home / "audit.log", paths)
        self.assertEqual(owner, (1000, 1000))


class StoreStateTests(TempDirCase):
    def test_net_state_roundtrip(self):
        home = self.tmp / "home"
        home.mkdir()
        state = {"schema": 1, "taps": 2, "allocations": {}}
        netpool.save_state(home, state)
        self.assertEqual(netpool.load_state(home), state)
        self.assertEqual(json.loads(netpool.state_path(home).read_text())["taps"], 2)
        summary = netpool.pool_summary(home) or {}
        self.assertEqual(summary.get("taps"), 2)
        self.assertEqual(summary.get("allocated"), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
