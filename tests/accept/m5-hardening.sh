#!/usr/bin/env bash
# M5 hardening acceptance, rootless: image-set repair, verified downloads,
# hostile-archive refusal, audit hardening, and own-VM-only cleanup.
# Usage: tests/accept/m5-hardening.sh
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/../.."
. "$PWD/tests/accept/lib.sh"

MC="$PWD/microvm-control"
WORK=$(mktemp -d "$HOME/.cache/microvm-accept-m5.XXXXXX")
export MICROVM_HOME="$WORK/home" MC WORK
mkdir -p "$MICROVM_HOME"

cleanup() {
  kill_own_vms "$MICROVM_HOME" 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

printf '\n== image build repairs a damaged image set (I8)\n'
"$MC" image build | jq -e '.ok' >/dev/null
rm -f "$MICROVM_HOME/images/base/data.ext4"
"$MC" image build | jq -e '.ok and .data.up_to_date != true' >/dev/null || fail "up_to_date claimed"
[ -f "$MICROVM_HOME/images/base/data.ext4" ] || fail "data image not restored"

printf '\n== fetch-verified helper (I4)\n'
echo "payload" > "$WORK/artifact"
sha256sum "$WORK/artifact" | awk '{print $1"  artifact"}' > "$WORK/artifact.sha256"
bash scripts/fetch-verified.sh "file://$WORK/artifact" "file://$WORK/artifact.sha256" "$WORK/out" \
  || fail "verified fetch failed"
cmp -s "$WORK/artifact" "$WORK/out" || fail "fetched file differs"
# a tampered cache is refused too, not just a fresh download
echo "tamper" >> "$WORK/out"
if bash scripts/fetch-verified.sh "file://$WORK/artifact" "file://$WORK/artifact.sha256" "$WORK/out" 2>/dev/null; then
  fail "tampered cache accepted"
fi
echo "tamper" >> "$WORK/artifact"
rm -f "$WORK/out"
if bash scripts/fetch-verified.sh "file://$WORK/artifact" "file://$WORK/artifact.sha256" "$WORK/out" 2>/dev/null; then
  fail "tampered file accepted"
fi
# a pinned release whose checksum file is gone may reuse the cache...
echo "payload" > "$WORK/cached"
bash scripts/fetch-verified.sh "file://$WORK/artifact" "file://$WORK/missing.sha256" "$WORK/cached" 2>/dev/null \
  || fail "cached artifact without a reachable checksum was not reused"
# ...but a fresh download always needs one
if bash scripts/fetch-verified.sh "file://$WORK/artifact" "file://$WORK/missing.sha256" "$WORK/fresh" 2>/dev/null; then
  fail "download without a checksum accepted"
fi

printf '\n== hostile archive is refused (S6)\n'
mkdir -p "$WORK/evil"
echo x > "$WORK/evil/f"
tar --zstd --transform 's|evil/f|../escape|' -cf "$WORK/evil.tar.zst" -C "$WORK" evil/f 2>/dev/null
if "$MC" restore "$WORK/evil.tar.zst" --id evil >"$WORK/evil.json" 2>/dev/null; then
  fail "hostile archive restored"
fi
# the refusal must come from our member scan (message + listed member), not
# from GNU tar failing later with a different error
jq -e '.error.code == "backup-corrupt"
  and (.error.message | test("unsafe member"))
  and (.error.detail.members | index("../escape") != null)' "$WORK/evil.json" >/dev/null \
  || fail "hostile-archive refusal did not come from the member scan"

printf '\n== a manifest cannot point verification at host files (S6b)\n'
mkdir -p "$WORK/hostile/stage"
echo host-secret > "$WORK/hostile/secret"
mkdir -p "$WORK/hostile/archive"
cp "$WORK/hostile/secret" "$WORK/hostile/archive/rootfs.ext4"
cat > "$WORK/hostile/archive/manifest.json" <<EOF
{"schema": 1, "instance": "x", "disk_format": "raw",
 "files": [{"name": "../secret", "size": 12,
            "sha256": "$(sha256sum "$WORK/hostile/secret" | awk '{print $1}')"}]}
EOF
if "$MC" restore "$WORK/hostile/archive" --id hostile >"$WORK/hostile.json" 2>/dev/null; then
  fail "hostile manifest restored"
fi
jq -e '.error.code == "backup-corrupt"
  and (.error.detail.files | map(.reason) | index("unsafe-name") != null)' "$WORK/hostile.json" >/dev/null \
  || fail "host-file verification oracle was not refused as unsafe-name"

printf '\n== audit hardening (I12)\n'
MICROVM_AUDIT_ARGS=0 "$MC" list >/dev/null
"$MC" audit --tail 1 \
  | jq -e '.data.entries[-1].argv == null and .data.entries[-1].argv_redacted == true' >/dev/null \
  || fail "argv not redacted"
[ "$(stat -c '%a' "$MICROVM_HOME/audit.log")" = "600" ] || fail "audit.log is not 0600"

printf '\n== state dir is 0700 even for read-only verbs (L1)\n'
fresh="$WORK/fresh-home"
MICROVM_HOME="$fresh" "$MC" list >/dev/null
[ "$(stat -c '%a' "$fresh")" = "700" ] || fail "read-only verb left the home at $(stat -c '%a' "$fresh")"

printf '\n== cleanup only targets own VMs (I11)\n'
sleep 30 &
decoy=$!
mkdir -p "$MICROVM_HOME/instances/decoy"
echo "{\"pid\": $decoy}" > "$MICROVM_HOME/instances/decoy/meta.json"
[ -z "$(own_vm_pids "$MICROVM_HOME")" ] || fail "own_vm_pids returned a non-firecracker pid"
kill "$decoy" 2>/dev/null || true
wait "$decoy" 2>/dev/null || true

# ...and it does find and kill a process that is a firecracker (negative
# checks alone would pass even if own_vm_pids were an empty stub)
cp "$(command -v sleep)" "$WORK/firecracker"
"$WORK/firecracker" 30 &
fake_fc=$!
for _ in $(seq 1 100); do
  [ "$(cat "/proc/$fake_fc/comm" 2>/dev/null)" = "firecracker" ] && break
  sleep 0.02
done
mkdir -p "$MICROVM_HOME/instances/decoy2"
echo "{\"pid\": $fake_fc}" > "$MICROVM_HOME/instances/decoy2/meta.json"
[ "$(own_vm_pids "$MICROVM_HOME")" = "$fake_fc" ] || fail "own_vm_pids missed a firecracker process"
kill_own_vms "$MICROVM_HOME"
# reap it before asserting: an unreaped zombie still shows comm=firecracker
wait "$fake_fc" 2>/dev/null || true
assert_no_own_vms "$MICROVM_HOME" || fail "kill_own_vms did not stop the firecracker process"

if grep -rqE 'pkill -x firecrack[e]r' tests/accept/*.sh; then
  fail "a suite still kills every firecracker process"
fi

printf '\nM5 HARDENING ACCEPTANCE PASSED\n'
