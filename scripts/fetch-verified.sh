#!/usr/bin/env bash
# Download a file and verify its sha256 before it is used.
# Usage: fetch-verified.sh URL SHA256_URL OUTPUT
# An existing OUTPUT is re-verified against the published checksum (no download),
# so a tampered or stale cache is refused too.
set -euo pipefail

url="${1:?usage: fetch-verified.sh URL SHA256_URL OUTPUT}"
sha_url="${2:?usage: fetch-verified.sh URL SHA256_URL OUTPUT}"
out="${3:?usage: fetch-verified.sh URL SHA256_URL OUTPUT}"

expected=$(curl -fsSL --retry 3 "$sha_url" | awk 'NF{print $1; exit}')
if ! printf '%s' "$expected" | grep -Eq '^[0-9a-fA-F]{64}$'; then
  echo "error: no sha256 checksum found at $sha_url" >&2
  exit 1
fi

if [ ! -f "$out" ]; then
  curl -fL --retry 3 "$url" -o "$out.part"
  mv "$out.part" "$out"
fi

actual=$(sha256sum "$out" | awk '{print $1}')
if [ "${actual,,}" != "${expected,,}" ]; then
  echo "error: checksum mismatch for $url" >&2
  echo "  expected: $expected" >&2
  echo "  actual:   $actual" >&2
  rm -f "$out"
  exit 1
fi
