#!/usr/bin/env bash
# Rootless smoke test: boots the microVM inside a user + network namespace
# (no sudo), then checks SSH, sshd, w3m (including a real fetch from a tiny
# HTTP server on the host side of the tap), /persist and persistence across a
# reboot. The normal flow is net.sh + run.sh; this just verifies the images.
#
# The inner script lives in a temp file instead of a heredoc-on-stdin: ssh and
# firecracker inherit fd 0 and would otherwise consume the script bytes.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

[ -x images/firecracker ] || { echo "run ./build.sh first" >&2; exit 1; }
[ "$(id -u)" -ne 0 ] || { echo "run as a normal user (this script uses unshare -Urn)" >&2; exit 1; }

inner=$(mktemp "${TMPDIR:-/tmp}/microvm-test.XXXXXX")
trap 'rm -f "$inner"' EXIT
cat > "$inner" <<'EOS'
set -euo pipefail
ip link set lo up
ip tuntap add dev fc-tap mode tap
ip addr add 172.16.0.1/24 dev fc-tap
ip link set fc-tap up

# ssh runs inside the namespace, where the host's /etc/ssh/ssh_config.d is
# owned by an unmapped uid, so skip the system config (-F /dev/null).
SSH=(ssh -F /dev/null -i keys/id_ed25519 -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -o LogLevel=ERROR -o ConnectTimeout=2)

boot() {
  rm -f run/firecracker.sock
  ./images/firecracker --api-sock run/firecracker.sock --config-file vm.json \
    </dev/null >"run/console.test-$1.log" 2>&1 &
  FC=$!
  for _ in $(seq 1 40); do
    "${SSH[@]}" root@172.16.0.2 true </dev/null 2>/dev/null && return 0
    sleep 1
  done
  echo "FAIL: guest did not come up"; tail -20 "run/console.test-$1.log"; return 1
}

stop() {
  "${SSH[@]}" root@172.16.0.2 'sync; reboot' </dev/null >/dev/null 2>&1 || true
  for _ in $(seq 1 15); do kill -0 "$FC" 2>/dev/null || break; sleep 1; done
  kill "$FC" 2>/dev/null || true
  wait "$FC" 2>/dev/null || true
}

mkdir -p run/http
echo "hello from host" > run/http/index.html
HTTP=""
if command -v python3 >/dev/null; then
  python3 -m http.server 8000 --bind 172.16.0.1 --directory run/http \
    </dev/null >run/http.log 2>&1 &
  HTTP=$!
fi
trap 'kill ${FC:-} ${HTTP:-} 2>/dev/null || true' EXIT

boot 1
"${SSH[@]}" root@172.16.0.2 '
  echo "guest:       $(uname -r) (Alpine $(cat /etc/alpine-release))"
  mount | grep -q " /persist " && echo "persist:     mounted at /persist"
  /etc/init.d/sshd status >/dev/null && echo "sshd:        running"
  w3m -version >/dev/null && echo "w3m:         present"
  chromium --version >/dev/null 2>&1 && echo "chromium:    $(chromium --version 2>/dev/null)"
  echo "test-payload-$(date +%s)" > /persist/work/persist-probe.txt
' </dev/null
if [ -n "$HTTP" ]; then
  "${SSH[@]}" root@172.16.0.2 'chromium --headless --no-sandbox --dump-dom http://172.16.0.1:8000/ 2>/dev/null | grep -q "hello from host"' \
    </dev/null && echo "browser:     chromium --dump-dom fetched http://172.16.0.1:8000/ ok"
else
  echo "browser:     skipped (python3 not available for the test server)"
fi
stop
boot 2
probe=$("${SSH[@]}" root@172.16.0.2 'cat /persist/work/persist-probe.txt' </dev/null)
[ -n "$probe" ] && echo "persistence: ok after reboot ($probe)"
stop
grep -qa "Welcome to Alpine" run/console.test-1.log && echo "console:     serial boot log captured"
echo "ALL CHECKS PASSED"
EOS

unshare -Urn bash "$inner"
