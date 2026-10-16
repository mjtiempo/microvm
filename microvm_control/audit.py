"""Best-effort JSONL audit log of every invocation."""

from __future__ import annotations

import json
import os
from pathlib import Path

from microvm_control.errors import MvError
from microvm_control.output import envelope

AUDIT_NAME = "audit.log"


def append(home: Path, entry: dict) -> None:
    """Append one audit line; never let auditing break the command itself.

    The file is created 0600 (it records argv, which may contain secrets) and
    opened with O_NOFOLLOW so a pre-planted symlink cannot redirect a root
    write; the mode is applied through the opened fd (no second lookup to
    race with). Set MICROVM_AUDIT_ARGS=0 to keep argv out of the log entirely.
    """
    try:
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(home, 0o700)
        except OSError:
            pass
        if os.environ.get("MICROVM_AUDIT_ARGS", "1") == "0":
            entry = {**entry, "argv": None, "argv_redacted": True}
        line = json.dumps(entry, separators=(",", ":")) + "\n"
        path = home / AUDIT_NAME
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(line)
    except OSError:
        pass


def read(home: Path, tail: int) -> list[dict]:
    path = home / AUDIT_NAME
    if not path.is_file():
        return []
    try:
        with open(path, "r", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError as exc:
        raise MvError("io-failed", f"cannot read {path}", detail=str(exc)) from exc
    entries = []
    for line in lines[-max(0, tail):]:
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


def verb(ns, home: Path):
    entries = read(home, ns.tail)
    return envelope(data={"path": str(home / AUDIT_NAME), "entries": entries,
                          "count": len(entries)})
