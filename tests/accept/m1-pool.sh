#!/usr/bin/env bash
# M1 acceptance: tap pool + multi-instance lifecycle, rootless (no sudo).
#
# The privileged part of `net install` (systemd + real host taps) needs root,
# so this test exercises: `net install --dry-run` as a user, the needs-root
# refusal, `net init` + `net status`, pool auto-allocation, two microVMs
# running concurrently on separate /30 taps, exhaustion, and release on
# delete. The taps themselves are created inside a private network namespace.
# Usage: tests/accept/m1-pool.sh
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/../.."
. "$PWD/tests/accept/lib.sh"

MC="$PWD/microvm-control"
WORK=$(mktemp -d "$HOME/.cache/microvm-accept-m1.XXXXXX")
export MICROVM_HOME="$WORK/home"
export MC WORK
mkdir -p "$MICROVM_HOME"

cleanup() {
  kill_own_vms "$MICROVM_HOME" 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

printf '\n== image build (import from repo images)\n'
"$MC" image build | jq -e '.ok == true' >/dev/null

printf '\n== net install --dry-run plans the systemd unit\n'
"$MC" --dry-run net install --taps 2 \
  | jq -e '.data.plan | tostring | test("microvm-net.service")' >/dev/null \
  || fail "dry-run plan"

printf '\n== net install refuses without root\n'
if "$MC" net install --taps 2 >/dev/null 2>"$WORK/net.err"; then
  fail "net install should refuse as a normal user"
fi
grep -q 'ERR\[needs-root\]' "$WORK/net.err" || fail "expected ERR[needs-root]"

inner="$WORK/inner.sh"
cat > "$inner" <<'EOS'
set -euo pipefail
ip link set lo up
# run the real generated net-setup.sh inside this private netns
python3 -c "from microvm_control import netpool; print(netpool._render_script(2, 'root'))" \
  > "$WORK/net-setup.sh"
sh -n "$WORK/net-setup.sh"
sh "$WORK/net-setup.sh" up
for i in 1 2; do
  ip -o addr show "mvtap$i" | grep -q "172.16.$((i * 4)).1/30" \
    || { echo "FAIL: mvtap$i missing its /30 address"; exit 1; }
done

say() { printf '\n== %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

say "net init (writes net.json, no root)"
"$MC" net init --taps 2 | jq -e '.ok and .data.pool.taps == 2' >/dev/null || fail "net init"

say "net status"
"$MC" net status | jq -e '
  .data.configured == true
  and .data.pool.taps == 2
  and (.data.taps | length) == 2
  and .data.taps[0].tap == "mvtap1"
  and .data.taps[0].guest_ip == "172.16.4.2"
  and .data.taps[0].up == true
  and .data.free == 2
' >/dev/null || fail "net status"

say "create a and b (pool allocation)"
"$MC" create a | jq -e '(.instance.tap == "mvtap1") and (.instance.ip == "172.16.4.2")' >/dev/null || fail "create a"
"$MC" create b | jq -e '(.instance.tap == "mvtap2") and (.instance.ip == "172.16.8.2")' >/dev/null || fail "create b"

say "pool exhaustion is a clear refusal"
if "$MC" create c >"$WORK/c.json" 2>"$WORK/c.err"; then fail "create c should fail"; fi
jq -e '.error.code == "no-tap-available"' "$WORK/c.json" >/dev/null || fail "wrong code for exhausted pool"

say "both instances run concurrently"
"$MC" start a | jq -e '.ok and .verified == true' >/dev/null || fail "start a"
"$MC" start b | jq -e '.ok and .verified == true' >/dev/null || fail "start b"
"$MC" exec a -- hostname | jq -e '(.data.stdout | sub("\\n$"; "")) == "a"' >/dev/null || fail "exec a"
"$MC" exec b -- hostname | jq -e '(.data.stdout | sub("\\n$"; "")) == "b"' >/dev/null || fail "exec b"
"$MC" exec a -- ip -4 -o addr show eth0 | jq -e '.data.stdout | test("172.16.4.2/30")' >/dev/null || fail "a ip"
"$MC" exec b -- ip -4 -o addr show eth0 | jq -e '.data.stdout | test("172.16.8.2/30")' >/dev/null || fail "b ip"

say "net status shows the allocations"
"$MC" net status | jq -e '
  .data.free == 0
  and (.data.taps | map(.allocated_to) | sort) == ["a", "b"]
' >/dev/null || fail "allocations"

say "delete releases the allocation"
"$MC" stop a | jq -e '.ok' >/dev/null || fail "stop a"
"$MC" stop b | jq -e '.ok' >/dev/null || fail "stop b"
"$MC" delete a --yes | jq -e '.ok' >/dev/null || fail "delete a"
"$MC" net status | jq -e '.data.free == 1' >/dev/null || fail "free after delete a"
"$MC" delete b --yes | jq -e '.ok' >/dev/null || fail "delete b"
"$MC" net status | jq -e '.data.free == 2' >/dev/null || fail "free after delete b"

say "generated net-setup.sh down removes the taps"
sh "$WORK/net-setup.sh" down
if ip link show mvtap1 >/dev/null 2>&1; then fail "mvtap1 still exists after down"; fi

printf '\nM1 POOL ACCEPTANCE PASSED\n'
EOS

unshare -Urn bash "$inner"
