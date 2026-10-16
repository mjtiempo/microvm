#!/usr/bin/env python3
"""Verify an Alpine .apk against the checksum published in APKINDEX.

Usage: verify-apk.py EXPECTED_QX APK_FILE

APKINDEX indexes packages by the control segment of the .apk: the gzip member
that carries `.PKGINFO` (the second member of a signed package, the first of an
unsigned one). It is recorded as `C:Q1<base64 sha1>` or `C:Q2<hex sha256>`.
Exit status: 0 match, 1 mismatch/unreadable (reason on stderr).
"""

from __future__ import annotations

import base64
import hashlib
import sys
import zlib


def control_segment(raw: bytes) -> bytes | None:
    """Bytes of the gzip member whose tar stream contains .PKGINFO."""
    off = 0
    while off < len(raw):
        dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            out = dec.decompress(raw[off:]) + dec.flush()
        except zlib.error:
            return None
        used = len(raw) - off - len(dec.unused_data)
        if used <= 0:
            return None
        if b".PKGINFO" in out:
            return raw[off:off + used]
        off += used
    return None


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        sys.stderr.write(__doc__.splitlines()[2].strip() + "\n")
        return 1
    expected, path = argv[1], argv[2]
    try:
        raw = open(path, "rb").read()
    except OSError as exc:
        sys.stderr.write(f"error: cannot read {path}: {exc}\n")
        return 1
    segment = control_segment(raw)
    if segment is None:
        sys.stderr.write(f"error: no .PKGINFO control segment found in {path}\n")
        return 1
    if expected.startswith("Q1"):
        actual = "Q1" + base64.b64encode(hashlib.sha1(segment).digest()).decode()
    elif expected.startswith("Q2"):
        actual = "Q2" + hashlib.sha256(segment).hexdigest()
    else:
        sys.stderr.write(f"error: unsupported checksum type: {expected[:8]}...\n")
        return 1
    if actual != expected:
        sys.stderr.write(f"error: checksum mismatch for {path}\n"
                         f"  expected: {expected}\n  actual:   {actual}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
