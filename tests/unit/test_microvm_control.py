"""Unit tests for microvm-control internals (no KVM, no host networking)."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import pwd
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

    def _fake_firecracker(self, sock: Path) -> subprocess.Popen:
        """A process named firecracker with `sock` in its argv (tail -f)."""
        binary = self.tmp / "firecracker"
        if not binary.exists():
            shutil.copy(shutil.which("tail") or "/usr/bin/tail", binary)
        sock.touch()
        proc = subprocess.Popen([str(binary), "-f", str(sock)])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        for _ in range(200):
            if store.pid_matches(proc.pid, sock):
                break
            time.sleep(0.02)
        else:
            self.fail("the fake firecracker did not come up")
        return proc

    def test_another_instances_firecracker_is_not_signalled(self):
        # two VMs, two sockets: B's pid must not be killed for A's instance
        ours = self.tmp / "ours.sock"
        theirs = self.tmp / "theirs.sock"
        victim = self._fake_firecracker(theirs)
        self.assertTrue(store.pid_matches(victim.pid, theirs))
        self.assertFalse(store.pid_matches(victim.pid, ours))
        self.assertTrue(lifecycle._kill_process(victim.pid, grace=0.2, sock=ours))
        time.sleep(0.1)
        self.assertIsNone(victim.poll(), "another instance's firecracker was signalled")

    def test_matching_instance_sock_is_signalled(self):
        sock = self.tmp / "ours.sock"
        proc = self._fake_firecracker(sock)
        self.assertTrue(lifecycle._kill_process(proc.pid, grace=2.0, sock=sock))
        for _ in range(200):
            if proc.poll() is not None:
                break
            time.sleep(0.01)
        self.assertIsNotNone(proc.poll(), "the matching firecracker was not signalled")


class IdentityTests(TempDirCase):
    def test_read_file_preserves_debugfs_like_content(self):
        image = self.make_image({"etc/x": b"debugfs 9.9.9\nkeep\n"})
        self.assertEqual(identity.read_file(image, "/etc/x"), b"debugfs 9.9.9\nkeep\n")

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


class SourcePausedTests(TempDirCase):
    def _instance_with_api(self, resume_error: MvError | None):
        home = self.tmp / "home"
        inst = self.make_instance(home, "demo")
        api = mock.Mock()
        api.resume.side_effect = resume_error
        return inst, api

    def test_failed_resume_raises_resume_failed(self):
        inst, api = self._instance_with_api(MvError("api-error", "frozen"))
        with mock.patch.object(lifecycle.Instance, "pid_alive", return_value=True), \
                mock.patch.object(lifecycle.fcapi, "FcApi", return_value=api):
            with self.assertRaises(MvError) as ctx:
                with lifecycle._source_paused(inst, {}):
                    pass
        self.assertEqual(ctx.exception.code, "resume-failed")
        self.assertIn("restart", ctx.exception.hint or "")
        api.pause.assert_called_once()

    def test_body_failure_keeps_error_and_flags_left_paused(self):
        inst, api = self._instance_with_api(MvError("api-error", "frozen"))
        with mock.patch.object(lifecycle.Instance, "pid_alive", return_value=True), \
                mock.patch.object(lifecycle.fcapi, "FcApi", return_value=api):
            with self.assertRaises(MvError) as ctx:
                with lifecycle._source_paused(inst, {}):
                    raise MvError("copy-failed", "disk gone")
        self.assertEqual(ctx.exception.code, "copy-failed")
        self.assertEqual(ctx.exception.detail["resume_error"], "frozen")
        self.assertTrue(ctx.exception.detail["left_paused"])
        self.assertIn("restart", ctx.exception.hint or "")

    def test_clean_pause_and_resume_yields_running_state(self):
        inst, api = self._instance_with_api(None)
        with mock.patch.object(lifecycle.Instance, "pid_alive", return_value=True), \
                mock.patch.object(lifecycle.fcapi, "FcApi", return_value=api):
            with lifecycle._source_paused(inst, {}) as state:
                self.assertTrue(state["running"])
                self.assertTrue(state["paused"])
        api.pause.assert_called_once()
        api.resume.assert_called_once()

    def test_stopped_source_is_not_paused(self):
        home = self.tmp / "home"
        inst = self.make_instance(home, "demo")
        with mock.patch.object(lifecycle.fcapi, "FcApi") as api:
            with lifecycle._source_paused(inst, {}) as state:
                self.assertFalse(state["running"])
                self.assertFalse(state["paused"])
        api.assert_not_called()


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

    def test_audit_never_follows_a_symlink_and_forces_home_to_0700(self):
        home = self.tmp / "home"
        home.mkdir(mode=0o755)
        victim = self.tmp / "victim"
        victim.write_text("keep")
        victim.chmod(0o644)
        (home / "audit.log").symlink_to(victim)
        audit.append(home, {"verb": "list"})
        self.assertEqual(victim.read_text(), "keep")
        self.assertEqual(oct(victim.stat().st_mode & 0o777), "0o644")
        self.assertEqual(oct(home.stat().st_mode & 0o777), "0o700")
        (home / "audit.log").unlink()
        audit.append(home, {"verb": "list"})
        self.assertEqual(audit.read(home, 1)[0]["verb"], "list")


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

    def test_manifest_member_names_are_validated(self):
        staging = self.tmp / "stage"
        staging.mkdir()
        victim = self.tmp / "victim"
        victim.write_text("host")
        (staging / "rootfs.ext4").write_text("disk")
        manifest = {"files": [
            {"name": "rootfs.ext4", "size": 4,
             "sha256": util.sha256_file(staging / "rootfs.ext4")},
            {"name": "../victim", "size": 4, "sha256": util.sha256_file(victim)},
            {"name": "/etc/passwd", "size": 1, "sha256": "x"},
            {"name": "sub/x", "size": 1, "sha256": "x"},
            {"name": "..", "size": 1, "sha256": "x"},
        ]}
        bad = backup._validate_staging_files(staging, manifest)
        self.assertEqual({item["name"]: item["reason"] for item in bad},
                         {"../victim": "unsafe-name", "/etc/passwd": "unsafe-name",
                          "sub/x": "unsafe-name", "..": "unsafe-name"})
        for good in ("rootfs.ext4", "data.ext4", "meta.json"):
            self.assertTrue(backup._safe_member_name(good))
        for bad_name in (None, "", ".", "..", "/x", "a/b", "a\\b"):
            self.assertFalse(backup._safe_member_name(bad_name))

    def test_symlinked_member_is_refused(self):
        staging = self.tmp / "stage2"
        staging.mkdir()
        victim = self.tmp / "victim2"
        victim.write_text("host")
        (staging / "rootfs.ext4").symlink_to(victim)
        manifest = {"files": [{"name": "rootfs.ext4", "size": victim.stat().st_size,
                               "sha256": util.sha256_file(victim)}]}
        self.assertEqual(backup._validate_staging_files(staging, manifest),
                         [{"name": "rootfs.ext4", "reason": "symlink"}])

    def test_manifest_bytes_uses_uncompressed_sizes(self):
        manifest = {"files": [{"size": 3}, {"size": 4}, {"nope": 1}, {"size": "x"},
                              "junk", {"size": None}]}
        self.assertEqual(backup._manifest_bytes(manifest), 7)


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

        def record(*args, **kwargs):
            calls.append((args, kwargs))

        with mock.patch.object(util.os, "chown", side_effect=record):
            util.adopt_ownership([target], (123, 456))
        self.assertEqual(len(calls), 1)
        args, kwargs = calls[0]
        self.assertEqual(args[0], target)
        self.assertEqual(args[1:], (123, 456))
        self.assertEqual(kwargs, {"follow_symlinks": False})
        # a pre-planted symlink must not redirect the handover
        link = self.tmp / "link"
        link.symlink_to(target)
        util.adopt_ownership([link], (123, 456))
        util.adopt_ownership([self.tmp / "missing"], (123, 456))
        self.assertEqual(len(calls), 1)
        util.adopt_ownership([target], None)
        self.assertEqual(len(calls), 1)


class SudoHomeTests(TempDirCase):
    def _pw(self, user_dir: Path):
        return pwd.struct_passwd(("someone", "x", 1000, 1000, "", str(user_dir), "/bin/sh"))

    def test_privileged_home_must_stay_inside_the_invoking_users_home(self):
        user_dir = self.tmp / "userhome"
        user_dir.mkdir()
        env = {"SUDO_USER": "someone", "SUDO_UID": "1000", "SUDO_GID": "1000"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(util.pwd, "getpwnam", return_value=self._pw(user_dir)):
            self.assertEqual(store.sudo_home(),
                             user_dir / ".local" / "share" / "microvm")
            # a symlink that escapes the user's home is refused
            (user_dir / ".local").mkdir()
            (user_dir / ".local" / "share").symlink_to(self.tmp / "outside")
            (self.tmp / "outside").mkdir()
            with self.assertRaises(MvError) as ctx:
                store.sudo_home()
            self.assertEqual(ctx.exception.code, "bad-state")
            # --home outside the user's home is refused too
            with self.assertRaises(MvError) as ctx:
                store.sudo_home(str(self.tmp / "elsewhere"))
            self.assertEqual(ctx.exception.code, "bad-state")
            # a home inside the user's directory is accepted
            (user_dir / ".local" / "share").unlink()
            self.assertEqual(store.sudo_home(str(user_dir / "custom")).parent, user_dir)

    def test_without_sudo_the_home_is_used_as_given(self):
        home = self.tmp / "plain"
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(store.sudo_home(str(home)), home.resolve())


class RuntimeTests(TempDirCase):
    def test_resolve_runtime_refuses_foreign_and_escaping_paths(self):
        home = self.tmp / "home"
        images = home / "images" / "base"
        images.mkdir(parents=True)
        # a complete runtime outside <home>/images is refused: restore used to
        # bake an archive path in here and execute it from the next start
        bogus = self.tmp / "no-runtime"
        bogus.mkdir()
        (bogus / "firecracker").write_text("x")
        (bogus / "vmlinux-x").write_text("x")
        meta = {"id": "demo", "config": {"image_dir": str(bogus), "kernel": "vmlinux-x"}}
        with self.assertRaises(MvError) as ctx:
            lifecycle._resolve_runtime(home, meta)
        self.assertEqual(ctx.exception.code, "image-missing")
        # a kernel path that escapes the image dir is refused too
        (images / "firecracker").write_text("x")
        (images / "vmlinux-x").write_text("x")
        meta = {"id": "demo",
                "config": {"image_dir": str(images), "kernel": "../../etc/passwd"}}
        with self.assertRaises(MvError) as ctx:
            lifecycle._resolve_runtime(home, meta)
        self.assertEqual(ctx.exception.code, "image-missing")
        # the local runtime resolves
        meta = {"id": "demo", "config": {"image_dir": str(images), "kernel": "vmlinux-x"}}
        firecracker, kernel = lifecycle._resolve_runtime(home, meta)
        self.assertEqual(firecracker, images / "firecracker")
        self.assertEqual(kernel, images / "vmlinux-x")
        # no recorded runtime falls back to the current base dir
        with self.assertRaises(MvError) as ctx:
            lifecycle._resolve_runtime(home, {"id": "demo", "config": {}})
        self.assertEqual(ctx.exception.code, "image-missing")


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
    def _pw(self, user_dir: Path):
        return pwd.struct_passwd(("someone", "x", 1000, 1000, "", str(user_dir), "/bin/sh"))

    def _run_privileged(self, home: Path, user_dir: Path):
        import io
        from microvm_control import __main__ as cli
        env = {"SUDO_UID": "1000", "SUDO_GID": "1000", "SUDO_USER": "someone"}
        with mock.patch.object(cli.os, "geteuid", return_value=0), \
                mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(util.pwd, "getpwnam", return_value=self._pw(user_dir)), \
                mock.patch.object(netpool, "net_install",
                                  return_value=cli.envelope(ok=True)) as install, \
                mock.patch.object(cli, "adopt_ownership") as adopted, \
                mock.patch("sys.stdout", new=io.StringIO()), \
                mock.patch("sys.stderr", new=io.StringIO()):
            rc = cli.main(["--home", str(home), "net", "install"])
        return rc, adopted, install

    def test_privileged_verb_adopts_ownership_inside_user_home(self):
        user_dir = self.tmp / "userhome"
        home = user_dir / ".local" / "share" / "microvm"
        home.mkdir(parents=True)
        rc, adopted, install = self._run_privileged(home, user_dir)
        self.assertEqual(rc, 0)
        install.assert_called_once()
        adopted.assert_called_once()
        paths, owner = adopted.call_args[0]
        self.assertIn(home, paths)
        self.assertIn(home / "net.json", paths)
        self.assertIn(home / "audit.log", paths)
        self.assertEqual(owner, (1000, 1000))

    def test_privileged_verb_outside_user_home_is_refused_without_adoption(self):
        user_dir = self.tmp / "userhome"
        user_dir.mkdir()
        home = self.tmp / "elsewhere"
        home.mkdir()
        rc, adopted, install = self._run_privileged(home, user_dir)
        self.assertEqual(rc, 1, "an escaping state dir must be refused")
        adopted.assert_not_called()
        install.assert_not_called()
        # the audit write must not go through the refused path as root either
        self.assertFalse((home / "audit.log").exists())
        self.assertTrue((user_dir / ".local" / "share" / "microvm" / "audit.log").exists())


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


class VerifyApkTests(TempDirCase):
    def test_control_segment_checksum(self):
        script = ROOT / "scripts" / "verify-apk.py"
        control = gzip.compress(b".PKGINFO\x00" + b"pkg metadata")
        data = gzip.compress(b"payload")
        signature = gzip.compress(b".SIGN.RSA.alpine-devel\x00" + b"\0" * 32)
        expected = "Q1" + base64.b64encode(hashlib.sha1(control).digest()).decode()
        # signed layout: signature member first, control member second
        signed = self.tmp / "signed.apk"
        signed.write_bytes(signature + control + data)
        cp = run(["python3", str(script), expected, str(signed)])
        self.assertEqual(cp.returncode, 0, cp.stderr)
        # unsigned layout: the control member comes first
        unsigned = self.tmp / "unsigned.apk"
        unsigned.write_bytes(control + data)
        self.assertEqual(run(["python3", str(script), expected, str(unsigned)]).returncode, 0)
        # a tampered package is refused
        tampered = self.tmp / "tampered.apk"
        tampered.write_bytes(signature + gzip.compress(b".PKGINFO\x00EVIL") + data)
        cp = run(["python3", str(script), expected, str(tampered)])
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("mismatch", cp.stderr)
        # garbage that carries no .PKGINFO member is refused
        garbage = self.tmp / "garbage.apk"
        garbage.write_bytes(b"not a gzip stream")
        cp = run(["python3", str(script), expected, str(garbage)])
        self.assertNotEqual(cp.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
