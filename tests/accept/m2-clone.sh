#!/usr/bin/env bash
# M2 acceptance, rootless (no sudo): per-instance ssh keys, live snapshots,
# snapshot load (memory restore), cold + live clone, snapshot delete, TTL reap.
# Runs two/three real microVMs in a private user+network namespace.
# Usage: tests/accept/m2-clone.sh
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/../.."
. "$PWD/tests/accept/lib.sh"

MC="$PWD/microvm-control"
WORK=$(mktemp -d "$HOME/.cache/microvm-accept-m2.XXXXXX")
export MICROVM_HOME="$WORK/home" MC WORK
mkdir -p "$MICROVM_HOME"

cleanup() {
  kill_own_vms "$MICROVM_HOME" 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

printf '\n== image build\n'
"$MC" image build | jq -e '.ok == true' >/dev/null

inner="$WORK/inner.sh"
cat > "$inner" <<'EOS'
set -euo pipefail
ip link set lo up
python3 -c "from microvm_control import netpool; print(netpool._render_script(4, 'root'))" \
  > "$WORK/net-setup.sh"
sh "$WORK/net-setup.sh" up

say() { printf '\n== %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

say "net init (4 taps)"
"$MC" net init --taps 4 | jq -e '.data.pool.taps == 4' >/dev/null || fail "net init"

say "create + start src"
"$MC" create src | jq -e '.instance.tap == "mvtap1" and .instance.ip == "172.16.4.2"' >/dev/null || fail "create src"
"$MC" start src | jq -e '.verified == true' >/dev/null || fail "start src"

say "per-instance ssh key replaces the shared control key"
[ -f "$MICROVM_HOME/instances/src/keys/id_ed25519" ] || fail "instance key missing"
if cmp -s "$MICROVM_HOME/instances/src/keys/id_ed25519" "$PWD/keys/id_ed25519"; then
  fail "instance key is still the shared control key"
fi
"$MC" exec src -- cat /root/.ssh/authorized_keys | jq -jr '.data.stdout' > "$WORK/guest-ak"
cmp -s "$WORK/guest-ak" "$MICROVM_HOME/instances/src/keys/id_ed25519.pub" \
  || fail "guest authorized_keys does not match the instance key"
debugfs -R 'stat /root/.ssh/authorized_keys' "$MICROVM_HOME/instances/src/disks/rootfs.ext4" 2>/dev/null \
  | grep -Eq 'Mode: +0600' || fail "authorized_keys mode is not 0600"
debugfs -R 'stat /root/.ssh' "$MICROVM_HOME/instances/src/disks/rootfs.ext4" 2>/dev/null \
  | grep -Eq 'Mode: +0700' || fail ".ssh directory mode is not 0700"
if ssh -F /dev/null -i "$PWD/keys/id_ed25519" -o IdentitiesOnly=yes -o BatchMode=yes \
     -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=3 \
     root@172.16.4.2 true >/dev/null 2>&1; then
  fail "the shared control key is still accepted"
fi

say "browser helper (dom + verified screenshot)"
mkdir -p "$WORK/http"
echo '<html><body>hello-browser</body></html>' > "$WORK/http/index.html"
python3 -m http.server 8000 --bind 172.16.4.1 --directory "$WORK/http" \
  </dev/null >"$WORK/http.log" 2>&1 &
HTTP=$!
trap 'kill ${HTTP:-} 2>/dev/null || true' EXIT
"$MC" browser src --dom http://172.16.4.1:8000/ \
  | jq -e '.ok and .verified and (.data.dom | test("hello-browser"))' >/dev/null || fail "browser dom"
"$MC" browser src --screenshot http://172.16.4.1:8000/ --out "$WORK/shot.png" \
  | jq -e '.ok and .verified and .data.magic_ok == true and .data.bytes > 0' >/dev/null || fail "browser screenshot"
head -c4 "$WORK/shot.png" | od -An -tx1 | grep -q '89 50 4e 47' || fail "screenshot is not a PNG"

say "snapshot create (live, source keeps running)"
"$MC" exec src -- sh -c 'echo before > /dev/shm/marker' >/dev/null
"$MC" snapshot create src --name snap1 \
  | jq -e '.ok and .verified and .data.snapshot.mem_bytes > 0 and .data.snapshot.vmstate_bytes > 0' >/dev/null \
  || fail "snapshot create"
"$MC" snapshot list src | jq -e '.data.count == 1 and .data.snapshots[0].name == "snap1"' >/dev/null \
  || fail "snapshot list"

say "snapshot load restores the memory state"
"$MC" exec src -- rm -f /dev/shm/marker >/dev/null
"$MC" stop src | jq -e '.ok' >/dev/null || fail "stop before load"
"$MC" snapshot load src --name snap1 | jq -e '.ok and .verified' >/dev/null || fail "snapshot load"
"$MC" exec src -- cat /dev/shm/marker | jq -e '.data.stdout | test("before")' >/dev/null \
  || fail "memory state was not restored"

say "cold clone of a stopped instance"
"$MC" stop src >/dev/null
"$MC" clone src src2 \
  | jq -e '.ok and .verified and .instance.tap == "mvtap2" and .instance.ip == "172.16.8.2"' >/dev/null \
  || fail "cold clone"
"$MC" start src2 | jq -e '.verified == true' >/dev/null || fail "start src2"
"$MC" exec src2 -- hostname | jq -e '(.data.stdout | sub("\\n$"; "")) == "src2"' >/dev/null \
  || fail "clone hostname"

say "live clone (source is paused only for the copy)"
"$MC" start src >/dev/null
t0=$(date +%s%N)
"$MC" clone src src3 | jq -e '.ok and .verified and .instance.tap == "mvtap3"' >/dev/null || fail "live clone"
elapsed_ms=$(( ($(date +%s%N) - t0) / 1000000 ))
printf 'live clone took %s ms\n' "$elapsed_ms"
[ "$elapsed_ms" -lt 2000 ] || fail "live clone too slow (${elapsed_ms}ms)"
"$MC" exec src -- true | jq -e '.data.exit_code == 0' >/dev/null || fail "source unusable after live clone"
"$MC" start src3 | jq -e '.verified == true' >/dev/null || fail "start src3"
"$MC" exec src3 -- hostname | jq -e '(.data.stdout | sub("\\n$"; "")) == "src3"' >/dev/null \
  || fail "clone3 hostname"

say "snapshot delete"
"$MC" snapshot delete src --name snap1 | jq -e '.ok and .verified' >/dev/null || fail "snapshot delete"
"$MC" snapshot list src | jq -e '.data.count == 0' >/dev/null || fail "snapshot list after delete"

say "gc prunes old snapshots when asked"
"$MC" snapshot create src --name gcme >/dev/null
"$MC" gc --snapshot-days 0 \
  | jq -e '.data.removed >= 1 and (.data.items | length >= 1) and .data.items[0].kind == "snapshot"' >/dev/null \
  || fail "gc snapshots"
[ ! -e "$MICROVM_HOME/instances/src/snapshots/gcme" ] || fail "snapshot not pruned"

say "reap deletes TTL-expired instances"
"$MC" create temp --ttl 1 >/dev/null
sleep 1.3
"$MC" reap | jq -e '.data.reaped | index("temp") != null' >/dev/null || fail "reap"
[ ! -e "$MICROVM_HOME/instances/temp" ] || fail "temp still present after reap"

say "cleanup"
for i in src src2 src3; do
  "$MC" stop "$i" >/dev/null 2>&1 || true
  "$MC" delete "$i" --yes >/dev/null
done
"$MC" list | jq -e '.data.count == 0' >/dev/null || fail "cleanup"

printf '\nM2 CLONE/SNAPSHOT ACCEPTANCE PASSED\n'
EOS

unshare -Urn bash "$inner"
