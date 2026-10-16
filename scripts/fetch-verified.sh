#!/usr/bin/env bash
# Download a file and verify its sha256 before it is used.
# Usage: fetch-verified.sh URL SHA256_URL OUTPUT
#
# A fresh download always requires a published sha256. An existing OUTPUT is
# re-verified against the published checksum when that checksum can be fetched;
# when it cannot (pinned old releases drop their .sha256 from the CDN), the
# cached file is reused with a warning instead of failing the whole build.
set -euo pipefail

url="${1:?usage: fetch-verified.sh URL SHA256_URL OUTPUT}"
sha_url="${2:?usage: fetch-verified.sh URL SHA256_URL OUTPUT}"
out="${3:?usage: fetch-verified.sh URL SHA256_URL OUTPUT}"

expected=""
if raw=$(curl -fsSL --retry 3 "$sha_url" 2>/dev/null); then
  expected=$(printf '%s\n' "$raw" | awk 'NF{print $1; exit}')
fi
if ! printf '%s' "$expected" | grep -Eq '^[0-9a-fA-F]{64}$'; then
  if [ -f "$out" ]; then
    echo "warning: no sha256 checksum at $sha_url; reusing the cached $out unverified" >&2
    exit 0
  fi
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
