#!/usr/bin/env bash
# Boot the microVM. The guest serial console is attached to this terminal:
#   - log in as root with the password set at build time (default: microvm)
#   - `reboot` exits the VM cleanly, Ctrl-C kills it hard
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

[ -x images/firecracker ] || { echo "run ./build.sh first" >&2; exit 1; }
[ -e images/vmlinux ] || { echo "run ./build.sh first (kernel missing)" >&2; exit 1; }
if ! ip link show fc-tap 2>/dev/null | grep -q '<[^>]*UP'; then
  echo "tap 'fc-tap' is missing or down - run: sudo ./net.sh up" >&2
  exit 1
fi

mkdir -p run
rm -f run/firecracker.sock
echo "booting microVM (serial console follows; ./ssh.sh in another terminal for ssh)"
exec ./images/firecracker --api-sock run/firecracker.sock --config-file vm.json
