#!/usr/bin/env bash
# SSH into the running microVM as root. Extra arguments are passed to ssh,
# e.g. ./ssh.sh -L 9222:127.0.0.1:9222  (Chromium DevTools tunnel)
# (Host keys are regenerated on every rebuild, hence the relaxed checks.)
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
ssh=(ssh -i keys/id_ed25519 -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
# options (./ssh.sh -L ...) go before the host, remote commands after it
case "${1:-}" in
  -*) exec "${ssh[@]}" "$@" root@172.16.0.2 ;;
  *)  exec "${ssh[@]}" root@172.16.0.2 "$@" ;;
esac
