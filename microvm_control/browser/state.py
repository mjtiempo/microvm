"""state — `browser.json`: the handoff between one CLI process and the next.

One `microvm-control` invocation is one process, so a later `browser text` has
no memory of the `browser start` before it. This file is that memory: the
guest pid, the RESOLVED DevTools port, the profile, and the fingerprint the
endpoint reported about itself. Like `meta.json` it is a *lead*, never a
fact — `load` reads anything unusable as "no state" (the fail-closed
direction), and the service reconciles the record against the guest
(`/proc/<pid>/cmdline` plus a live `/json/version`) before driving anything.

The verdict helpers (`cmdline_is_ours`, `stale_reason`, `fingerprint_matches`,
`changed_fields`) are pure so the hermetic suite can pin them without a VM.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from .. import util
from ..errors import MvError

__all__ = [
    "FINGERPRINT_FIELDS", "SCHEMA",
    "changed_fields", "clear", "cmdline_is_ours", "fingerprint_matches",
    "fingerprint_of", "load", "new_state", "path", "save", "stale_reason",
]

SCHEMA = 1
#: The fields of `/json/version` that make up the endpoint's identity card.
FINGERPRINT_FIELDS = ("browser", "protocol", "user_agent", "v8")

FILE_NAME = "browser.json"


def path(inst) -> Path:
    """Where an instance's browser state lives."""
    return inst.dir / FILE_NAME


def new_state(*, pid: int, port: int, port_requested: int, profile: str,
              log_path: str, fingerprint: dict) -> dict:
    """The record `browser start` writes; `port` is the RESOLVED one."""
    return {
        "schema": SCHEMA,
        "pid": pid,
        "port": port,
        "port_requested": port_requested,
        "profile": profile,
        "log_path": log_path,
        "started_at": util.now_iso(),
        "fingerprint": dict(fingerprint),
    }


def load(inst) -> dict | None:
    """The record, or None for missing/unreadable/foreign-shaped state."""
    target = path(inst)
    try:
        if not target.is_file():
            return None
        data = json.loads(target.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return None
    if not isinstance(data.get("pid"), int) or not isinstance(data.get("port"), int):
        return None
    return data


def save(inst, record: dict) -> None:
    target = path(inst)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        util.atomic_write_json(target, record)
    except OSError as exc:
        raise MvError("io-failed", f"cannot write {target}", detail=str(exc)) from exc


def clear(inst) -> None:
    """Remove the record; missing is fine, any other failure is not."""
    try:
        path(inst).unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise MvError("io-failed", f"cannot remove {path(inst)}",
                      detail=str(exc)) from exc


def _flags(cmdline: str | None) -> tuple[str, str]:
    """`(--user-data-dir, --remote-debugging-port)` out of a guest cmdline."""
    if not cmdline:
        return "", ""
    try:
        parts = shlex.split(cmdline)
    except ValueError:
        parts = cmdline.split()
    profile = port = ""
    for index, part in enumerate(parts):
        if part == "--user-data-dir" and index + 1 < len(parts):
            profile = parts[index + 1]
        elif part.startswith("--user-data-dir="):
            profile = part.split("=", 1)[1]
        elif part == "--remote-debugging-port" and index + 1 < len(parts):
            port = parts[index + 1]
        elif part.startswith("--remote-debugging-port="):
            port = part.split("=", 1)[1]
    return profile, port


def cmdline_is_ours(cmdline: str | None, profile: str, port: int) -> bool:
    """Is that process still THIS browser (our profile, our port)?"""
    found_profile, found_port = _flags(cmdline)
    return found_profile == profile and found_port == str(port)


def stale_reason(cmdline: str | None, profile: str, port: int) -> str | None:
    """Why the recorded pid is no longer our browser, or None when it is."""
    if not cmdline:
        return "the recorded pid is gone"
    found_profile, found_port = _flags(cmdline)
    if found_profile != profile:
        return (f"the recorded pid now runs with another --user-data-dir "
                f"({found_profile or 'none'}); expected {profile}")
    if found_port != str(port):
        return (f"the recorded pid now runs on another --remote-debugging-port "
                f"({found_port or 'none'}); expected {port}")
    return None


def fingerprint_of(version: dict) -> dict:
    """The identity card out of a `/json/version` payload."""
    return {
        "browser": str(version.get("Browser") or ""),
        "protocol": str(version.get("Protocol-Version") or ""),
        "user_agent": str(version.get("User-Agent") or ""),
        "v8": str(version.get("V8-Version") or ""),
    }


def changed_fields(stored: dict, current: dict) -> list[tuple[str, str, str]]:
    """The `(field, was, now)` differences, for the refusal's detail."""
    diffs = []
    for field in FINGERPRINT_FIELDS:
        was = str((stored or {}).get(field) or "")
        now = str((current or {}).get(field) or "")
        if was != now:
            diffs.append((field, was, now))
    return diffs


def fingerprint_matches(stored: dict, current: dict) -> bool:
    """Does the endpoint still report the stored identity? (fail closed)"""
    if not isinstance(stored, dict) or not stored:
        return False
    if not isinstance(current, dict):
        return False
    return all(stored.get(field) == current.get(field)
               for field in FINGERPRINT_FIELDS)
