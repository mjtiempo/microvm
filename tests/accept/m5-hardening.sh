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
echo "tamper" >> "$WORK/artifact"
rm -f "$WORK/out"
if bash scripts/fetch-verified.sh "file://$WORK/artifact" "file://$WORK/artifact.sha256" "$WORK/out" 2>/dev/null; then
  fail "tampered file accepted"
fi

printf '\n== hostile archive is refused (S6)\n'
mkdir -p "$WORK/evil"
echo x > "$WORK/evil/f"
tar --zstd --transform 's|evil/f|../escape|' -cf "$WORK/evil.tar.zst" -C "$WORK" evil/f
if "$MC" restore "$WORK/evil.tar.zst" --id evil >"$WORK/evil.json" 2>/dev/null; then
  fail "hostile archive restored"
fi
jq -e '.error.code == "backup-corrupt"' "$WORK/evil.json" >/dev/null || fail "wrong hostile-archive code"

printf '\n== audit hardening (I12)\n'
MICROVM_AUDIT_ARGS=0 "$MC" list >/dev/null
"$MC" audit --tail 1 \
  | jq -e '.data.entries[-1].argv == null and .data.entries[-1].argv_redacted == true' >/dev/null \
  || fail "argv not redacted"
[ "$(stat -c '%a' "$MICROVM_HOME/audit.log")" = "600" ] || fail "audit.log is not 0600"

printf '\n== cleanup only targets own VMs (I11)\n'
sleep 30 &
decoy=$!
mkdir -p "$MICROVM_HOME/instances/decoy"
echo "{\"pid\": $decoy}" > "$MICROVM_HOME/instances/decoy/meta.json"
[ -z "$(own_vm_pids "$MICROVM_HOME")" ] || fail "own_vm_pids returned a non-firecracker pid"
kill "$decoy" 2>/dev/null || true
wait "$decoy" 2>/dev/null || true
if grep -rqE 'pkill -x firecrack[e]r' tests/accept/*.sh; then
  fail "a suite still kills every firecracker process"
fi

printf '\nM5 HARDENING ACCEPTANCE PASSED\n'
