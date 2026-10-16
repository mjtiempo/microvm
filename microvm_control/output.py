"""The JSON envelope: exactly one object per invocation."""

from __future__ import annotations

import json
import sys

SCHEMA = 1


def envelope(
    *,
    ok: bool = True,
    instance: dict | None = None,
    verified: bool | None = None,
    effect: str | None = None,
    rung: str | None = None,
    data: dict | None = None,
    error: dict | None = None,
) -> dict:
    doc: dict = {"schema": SCHEMA, "ok": ok}
    if instance is not None:
        doc["instance"] = instance
    if verified is not None:
        doc["verified"] = verified
    if effect is not None:
        doc["effect"] = effect
    if rung is not None:
        doc["rung"] = rung
    if error is not None:
        doc["error"] = error
    if data is not None:
        doc["data"] = data
    return doc


def emit(doc: dict, quiet: bool = False) -> None:
    if quiet:
        return
    json.dump(doc, sys.stdout, indent=2)
    sys.stdout.write("\n")
    sys.stdout.flush()
