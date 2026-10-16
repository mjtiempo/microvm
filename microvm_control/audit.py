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

    The file is created 0600 (it records argv, which may contain secrets).
    Set MICROVM_AUDIT_ARGS=0 to keep argv out of the log entirely.
    """
    try:
        home.mkdir(parents=True, exist_ok=True)
        if os.environ.get("MICROVM_AUDIT_ARGS", "1") == "0":
            entry = {**entry, "argv": None, "argv_redacted": True}
        path = home / AUDIT_NAME
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "a") as handle:
            handle.write(json.dumps(entry, separators=(",", ":")) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
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
