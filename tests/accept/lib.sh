#!/usr/bin/env bash
# Shared helpers for the acceptance suites: identify only the VMs that belong
# to a given MICROVM_HOME, so cleanup never touches the user's own instances.

# Print pids recorded in $1's instance metas that are still Firecracker.
own_vm_pids() {
  local home="$1" meta pid
  for meta in "$home"/instances/*/meta.json; do
    [ -f "$meta" ] || continue
    pid=$(jq -r '.pid // empty' "$meta" 2>/dev/null)
    [ -n "$pid" ] || continue
    [ "$(cat "/proc/$pid/comm" 2>/dev/null)" = "firecracker" ] && printf '%s\n' "$pid"
  done
}

# Kill only this run's VMs.
kill_own_vms() {
  local pid
  for pid in $(own_vm_pids "$1"); do
    kill "$pid" 2>/dev/null || true
  done
}

# True when no VM from $1 is alive.
assert_no_own_vms() {
  [ -z "$(own_vm_pids "$1")" ]
}
