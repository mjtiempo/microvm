#!/usr/bin/env bash
# M0 acceptance: full lifecycle, rootless (no sudo).
# Runs a real microVM in a private user+network namespace against the base
# image and checks create/start/exec/cp/status/logs/stop/delete + the JSON
# contract.  Usage: tests/accept/m0.sh
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/../.."
. "$PWD/tests/accept/lib.sh"

MC="$PWD/microvm-control"
WORK=$(mktemp -d "$HOME/.cache/microvm-accept.XXXXXX")
export MICROVM_HOME="$WORK/home"
export MC WORK
mkdir -p "$MICROVM_HOME"

cleanup() {
  kill_own_vms "$MICROVM_HOME" 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

printf '\n== image build (import from repo images)\n'
"$MC" image build | jq -e '.ok == true and (.data.firecracker != null)' >/dev/null

inner="$WORK/inner.sh"
cat > "$inner" <<'EOS'
set -euo pipefail
. "$PWD/tests/accept/lib.sh"
ip link set lo up
ip tuntap add dev fc-m0 mode tap
ip addr add 172.16.0.1/24 dev fc-m0
ip link set fc-m0 up

TAP=fc-m0
say() { printf '\n== %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

say "create"
"$MC" create demo --tap "$TAP" --ip 172.16.0.2 --gw 172.16.0.1 \
  | jq -e '.ok == true and .verified == true and .instance.state == "created"' >/dev/null || fail "create"

say "start (waits for ssh readiness)"
"$MC" start demo | jq -e '.ok and .verified == true and .data.ssh_ready_ms > 0' >/dev/null || fail "start"

say "start on an already-running instance is honest about readiness"
"$MC" start demo --no-wait-ssh \
  | jq -e '.ok and .verified == false and .data.already_running == true' >/dev/null \
  || fail "already-running start must report verified: false"

say "start --no-wait-ssh is honest and still becomes reachable"
"$MC" stop demo | jq -e '.ok' >/dev/null || fail "stop before no-wait start"
"$MC" start demo --no-wait-ssh \
  | jq -e '.ok and (.verified == false) and .effect == "unverifiable" and .rung == "api"' >/dev/null \
  || fail "no-wait-ssh contract"
for _ in $(seq 1 40); do
  if "$MC" status demo --probe 2>/dev/null | jq -e '.data.ssh_reachable == true' >/dev/null 2>&1; then break; fi
  sleep 0.5
done
"$MC" exec demo -- true | jq -e '.data.exit_code == 0' >/dev/null || fail "not reachable after no-wait start"

say "exec"
"$MC" exec demo -- uname -s | jq -e '.data.exit_code == 0 and (.data.stdout | test("Linux"))' >/dev/null || fail "exec"

say "exec failure is data, not a refusal"
"$MC" exec demo -- sh -c 'exit 3' | jq -e '.ok == true and .data.exit_code == 3' >/dev/null || fail "exec exit code"

say "cp round trip"
payload="payload-$(date +%s)"
echo "$payload" > "$WORK/local.txt"
"$MC" cp "$WORK/local.txt" demo:/tmp/copied.txt | jq -e '.ok and .verified == true' >/dev/null || fail "cp in"
"$MC" exec demo -- cat /tmp/copied.txt | jq -e --arg want "$payload" '.data.stdout | test($want)' >/dev/null || fail "cp content"
"$MC" cp demo:/tmp/copied.txt "$WORK/back.txt" | jq -e '.ok' >/dev/null || fail "cp out"
cmp -s "$WORK/local.txt" "$WORK/back.txt" || fail "cp round trip mismatch"

say "status --probe"
"$MC" status demo --probe | jq -e '.instance.state == "running" and .data.ssh_reachable == true' >/dev/null || fail "status"

say "logs (serial console captured)"
"$MC" logs demo --tail 200 | jq -e '.data.count > 0' >/dev/null || fail "logs"

say "stop (graceful)"
"$MC" stop demo | jq -e '.ok and .instance.state == "stopped"' >/dev/null || fail "stop"
if ! assert_no_own_vms "$MICROVM_HOME"; then fail "firecracker still running after stop"; fi

say "delete (moved to trash)"
"$MC" delete demo --yes | jq -e '.ok and .data.trash_path != null' >/dev/null || fail "delete"
[ ! -e "$MICROVM_HOME/instances/demo" ] || fail "instance dir still present"

say "list is empty"
"$MC" list | jq -e '.data.count == 0' >/dev/null || fail "list"

printf '\nM0 ACCEPTANCE PASSED\n'
EOS

unshare -Urn bash "$inner"
