"""Best-effort JSONL audit log of every invocation."""

from __future__ import annotations

import json
from pathlib import Path

from microvm_control.errors import MvError
from microvm_control.output import envelope

AUDIT_NAME = "audit.log"


def append(home: Path, entry: dict) -> None:
    """Append one audit line; never let auditing break the command itself."""
    try:
        home.mkdir(parents=True, exist_ok=True)
        with open(home / AUDIT_NAME, "a") as handle:
            handle.write(json.dumps(entry, separators=(",", ":")) + "\n")
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
