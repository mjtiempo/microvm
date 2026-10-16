"""Instance registry: on-disk state, locking, reconciliation."""

from __future__ import annotations

import fcntl
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path

from .errors import MvError
from .util import atomic_write_json, now_epoch

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
META_SCHEMA = 1
LIVE_STATES = ("starting", "running", "paused")


def home_path(cli_home: str | None = None) -> Path:
    if cli_home:
        return Path(cli_home).expanduser().resolve()
    env = os.environ.get("MICROVM_HOME")
    if env:
        return Path(env).expanduser().resolve()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return (base / "microvm").resolve()


def ensure_home(home: Path) -> Path:
    for sub in ("instances", "images", "trash", "locks", "logs"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    return home


def validate_id(instance_id: str) -> None:
    if not ID_RE.match(instance_id or ""):
        raise MvError(
            "invalid-id",
            f"invalid instance id: {instance_id!r}",
            hint="ids match [a-z0-9][a-z0-9-]{0,30}",
        )


def pid_is_firecracker(pid) -> bool:
    try:
        with open(f"/proc/{int(pid)}/comm") as f:
            comm = f.read().strip()
    except (OSError, ValueError, TypeError):
        return False
    return comm.startswith("firecracker")


class Instance:
    def __init__(self, home: Path, instance_id: str):
        validate_id(instance_id)
        self.home = home
        self.id = instance_id
        self.dir = home / "instances" / instance_id
        self.meta_path = self.dir / "meta.json"
        self.sock = self.dir / "run" / "firecracker.sock"
        self.console_log = self.dir / "run" / "console.log"
        self.fc_log = self.dir / "run" / "firecracker.log"
        self.key = self.dir / "keys" / "id_ed25519"
        self.known_hosts = self.dir / "ssh" / "known_hosts"

    def exists(self) -> bool:
        return self.meta_path.is_file()

    def load_meta(self) -> dict:
        if not self.meta_path.is_file():
            raise MvError("not-found", f"instance '{self.id}' does not exist",
                          hint="microvm-control list")
        try:
            return json.loads(self.meta_path.read_text())
        except (OSError, ValueError) as exc:
            raise MvError("bad-state", f"meta.json for '{self.id}' is unreadable",
                          detail=str(exc)) from exc

    def save_meta(self, meta: dict) -> None:
        atomic_write_json(self.meta_path, meta)

    @contextmanager
    def lock(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            handle = open(self.dir / "lock", "w")
        except OSError as exc:
            raise MvError("locked", f"cannot open the lock file for '{self.id}'",
                          detail=str(exc)) from exc
        with handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def pid_alive(self, meta: dict | None = None) -> bool:
        m = meta if meta is not None else (self.load_meta() if self.exists() else {})
        return pid_is_firecracker(m.get("pid"))

    def reconcile(self) -> dict:
        """Never report a live state that the process table contradicts."""
        meta = self.load_meta()
        state = meta.get("state")
        alive = pid_is_firecracker(meta.get("pid"))
        if state in LIVE_STATES and not alive:
            meta["state"] = "dead"
            meta["pid"] = None
            self.save_meta(meta)
        elif state in ("created", "stopped") and alive:
            meta["state"] = "running"
            self.save_meta(meta)
        return meta

    def summary(self, meta: dict | None = None) -> dict:
        m = meta if meta is not None else self.load_meta()
        net = m.get("net") or {}
        cfg = m.get("config") or {}
        expires = m.get("expires_at")
        return {
            "id": self.id,
            "state": m.get("state"),
            "pid": m.get("pid"),
            "ip": net.get("ip"),
            "tap": net.get("tap"),
            "vcpu": cfg.get("vcpu"),
            "mem_mib": cfg.get("mem_mib"),
            "created_at": m.get("created_at"),
            "started_at": m.get("started_at"),
            "expires_at": expires,
            "ttl_remaining_s": max(0, int(expires - now_epoch())) if expires else None,
        }

    @classmethod
    def all(cls, home: Path) -> list["Instance"]:
        root = home / "instances"
        if not root.is_dir():
            return []
        out = []
        for child in sorted(root.iterdir()):
            if child.is_dir() and (child / "meta.json").is_file():
                try:
                    out.append(cls(home, child.name))
                except MvError:
                    continue
        return out
