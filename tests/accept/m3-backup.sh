#!/usr/bin/env bash
# M3 acceptance, rootless: backup a running instance, verify manifests, restore
# with hash verification and --verify boot, corrupt-archive refusal, --no-data,
# and the qcow2 disk encoding.
# Usage: tests/accept/m3-backup.sh
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/../.."
. "$PWD/tests/accept/lib.sh"

MC="$PWD/microvm-control"
WORK=$(mktemp -d "$HOME/.cache/microvm-accept-m3.XXXXXX")
export MICROVM_HOME="$WORK/home" MC WORK
mkdir -p "$MICROVM_HOME"

cleanup() {
  kill_own_vms "$MICROVM_HOME" 2>/dev/null || true
  kill_own_vms "$WORK/home2" 2>/dev/null || true
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
python3 -c "from microvm_control import netpool; print(netpool._render_script(5, 'root'))" \
  > "$WORK/net-setup.sh"
sh "$WORK/net-setup.sh" up

say() { printf '\n== %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

say "net init (5 taps)"
"$MC" net init --taps 5 >/dev/null

say "seed a running instance"
"$MC" create src >/dev/null
"$MC" start src | jq -e '.verified == true' >/dev/null || fail "start src"
"$MC" exec src -- sh -c 'echo hello-backup > /persist/work/marker.txt' >/dev/null

say "backup a running instance (tar.zst, pause + sha256 manifest)"
"$MC" backup src --name b1 \
  | jq -e '.ok and .verified and .data.bytes > 0 and (.data.files | length == 3)' >/dev/null \
  || fail "backup"
archive=$(ls "$MICROVM_HOME"/backups/src-b1*.tar.zst | head -1)
[ -f "$archive" ] || fail "archive missing"

say "restore with --verify boot"
"$MC" restore "$archive" --id src2 --verify \
  | jq -e '.ok and .verified and .data.boot_verified == true and .data.files_verified == 3' >/dev/null \
  || fail "restore"
"$MC" exec src2 -- hostname | jq -e '(.data.stdout | sub("\\n$"; "")) == "src2"' >/dev/null || fail "restored hostname"
"$MC" exec src2 -- cat /persist/work/marker.txt | jq -e '.data.stdout | test("hello-backup")' >/dev/null \
  || fail "restored /persist data"
"$MC" exec src -- true | jq -e '.data.exit_code == 0' >/dev/null || fail "source unusable after backup"

say "a corrupt archive is refused"
cp "$archive" "$WORK/corrupt.tar.zst"
python3 - "$WORK/corrupt.tar.zst" <<'PY'
import sys
path = sys.argv[1]
data = bytearray(open(path, "rb").read())
data[len(data) // 2] ^= 0xFF
open(path, "wb").write(data)
PY
if "$MC" restore "$WORK/corrupt.tar.zst" --id bad >"$WORK/corrupt.json" 2>/dev/null; then
  fail "corrupt archive restored"
fi
jq -e '.error.code == "backup-corrupt"' "$WORK/corrupt.json" >/dev/null || fail "wrong corrupt-archive code"

say "backup --no-data + restore starts with a fresh /persist"
"$MC" backup src --name nodata --no-data | jq -e '.ok and (.data.files | length == 2)' >/dev/null \
  || fail "no-data backup"
nodata=$(ls "$MICROVM_HOME"/backups/src-nodata*.tar.zst | head -1)
"$MC" restore "$nodata" --id src3 | jq -e '.ok and .verified' >/dev/null || fail "restore no-data"
"$MC" start src3 | jq -e '.verified == true' >/dev/null || fail "start src3"
"$MC" exec src3 -- sh -c 'test ! -e /persist/work/marker.txt && echo fresh' \
  | jq -e '.data.stdout | test("fresh")' >/dev/null || fail "/persist not fresh"

say "qcow2 disk encoding round trip"
"$MC" backup src --name qc --format qcow2 \
  | jq -e '.ok and .data.disk_format == "qcow2"' >/dev/null || fail "qcow2 backup"
qc=$(ls "$MICROVM_HOME"/backups/src-qc*.tar.zst | head -1)
"$MC" restore "$qc" --id src4 | jq -e '.ok and .data.disk_format == "qcow2"' >/dev/null || fail "qcow2 restore"
"$MC" start src4 | jq -e '.verified == true' >/dev/null || fail "start src4"
"$MC" exec src4 -- cat /persist/work/marker.txt | jq -e '.data.stdout | test("hello-backup")' >/dev/null \
  || fail "qcow2 restored data"

say "restore into a second home boots from that home's image set"
second="$WORK/home2"
MICROVM_HOME="$second" "$MC" image build | jq -e '.ok' >/dev/null || fail "second image build"
MICROVM_HOME="$second" "$MC" restore "$archive" --id xh \
  --tap mvtap5 --ip 172.16.20.2 --gw 172.16.20.1 --prefix 30 \
  | jq -e '.ok and .verified' >/dev/null || fail "cross-home restore"
# the archive's config pointed at the first home: the restored meta must
# resolve onto the second home's image set, or `start` would run a foreign path
image_dir=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['config']['image_dir'])" \
  "$second/instances/xh/meta.json")
case "$image_dir" in
  "$second"/images/*) ;;
  *) fail "restored image_dir was not rewritten: $image_dir" ;;
esac
MICROVM_HOME="$second" "$MC" start xh | jq -e '.verified == true' >/dev/null || fail "start xh"
MICROVM_HOME="$second" "$MC" exec xh -- cat /persist/work/marker.txt \
  | jq -e '.data.stdout | test("hello-backup")' >/dev/null || fail "cross-home restored data"
MICROVM_HOME="$second" "$MC" stop xh >/dev/null
MICROVM_HOME="$second" "$MC" delete xh --yes >/dev/null
MICROVM_HOME="$second" "$MC" list | jq -e '.data.count == 0' >/dev/null || fail "second home cleanup"

say "gc removes trash entries"
"$MC" --dry-run gc | jq -e '.data.dry_run == true' >/dev/null || fail "gc dry-run"

say "cleanup"
for i in src src2 src3 src4; do
  "$MC" stop "$i" >/dev/null 2>&1 || true
  "$MC" delete "$i" --yes >/dev/null
done
"$MC" list | jq -e '.data.count == 0' >/dev/null || fail "cleanup"

printf '\nM3 BACKUP/RESTORE ACCEPTANCE PASSED\n'
EOS

unshare -Urn bash "$inner"
