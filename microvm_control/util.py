"""Small helpers: process running, hashing, sizes, atomic JSON."""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .errors import MvError


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def now_epoch() -> float:
    return time.time()


def run(cmd, *, input: str | None = None, timeout: float | None = None, cwd=None):
    return subprocess.run(
        [str(c) for c in cmd],
        input=input,
        text=True,
        capture_output=True,
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
    )


def which(name: str) -> str | None:
    for p in os.environ.get("PATH", "").split(os.pathsep):
        cand = Path(p) / name
        try:
            if cand.is_file() and os.access(cand, os.X_OK):
                return str(cand)
        except OSError:
            # PATH entries can vanish or be unreadable; keep looking
            continue
    return None


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    try:
        f = open(path, "rb")
    except OSError as exc:
        raise MvError(
            "read-failed", f"could not read {path}: {exc.strerror or exc}"
        ) from exc
    with f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def atomic_write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def du_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def free_bytes(path: Path) -> int:
    try:
        st = os.statvfs(str(path))
    except OSError as exc:
        raise MvError("io-failed", f"cannot stat the filesystem for {path}",
                      detail=str(exc)) from exc
    return st.f_bavail * st.f_frsize


def invoking_identity() -> tuple[int, int] | None:
    """uid/gid of the user who ran sudo, when running as root on their behalf."""
    sudo_uid = os.environ.get("SUDO_UID")
    sudo_gid = os.environ.get("SUDO_GID")
    if sudo_uid and sudo_gid:
        try:
            return int(sudo_uid), int(sudo_gid)
        except ValueError:
            return None
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        try:
            pw = pwd.getpwnam(sudo_user)
        except KeyError:
            return None
        return pw.pw_uid, pw.pw_gid
    return None


def adopt_ownership(paths: list[Path], owner: tuple[int, int] | None) -> None:
    """Best-effort chown of state written by a privileged verb."""
    if owner is None:
        return
    uid, gid = owner
    for path in paths:
        try:
            if path.exists():
                os.chown(path, uid, gid)
        except OSError:
            continue


def ensure_space(target: Path, need: int, *, label: str) -> None:
    """Refuse when the filesystem behind `target` cannot hold `need` bytes."""
    probe = target
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    free = free_bytes(probe)
    if free < need:
        raise MvError("disk-full", f"not enough free space on {probe} for {label}",
                      detail={"free_bytes": free, "needed_bytes": need})


def copy_file(src: Path, dst: Path) -> str:
    """Copy a file, preferring a btrfs/XFS reflink (instant COW)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    cp = run(["cp", "-p", "--reflink=always", "--sparse=always", str(src), str(dst)])
    if cp.returncode == 0:
        return "reflink"
    cp = run(["cp", "-p", "--reflink=auto", "--sparse=always", str(src), str(dst)])
    if cp.returncode != 0:
        raise MvError(
            "copy-failed",
            f"could not copy {src} to {dst}",
            detail=(cp.stderr or "").strip()[-500:],
        )
    return "copy"
