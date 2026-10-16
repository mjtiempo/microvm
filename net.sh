#!/usr/bin/env bash
# Host networking for the microVM: tap device + NAT for outbound traffic.
# Usage: sudo ./net.sh up | sudo ./net.sh down
set -euo pipefail

TAP="${TAP:-fc-tap}"
HOST_IP="${HOST_IP:-172.16.0.1}"

[ "$(id -u)" -eq 0 ] || exec sudo -- "$0" "$@"

host_iface() { ip route list default | awk '/^default/{print $5; exit}'; }

up() {
  HOST_IFACE=$(host_iface)
  [ -n "$HOST_IFACE" ] || { echo "error: no default route found" >&2; exit 1; }

  if ! ip link show "$TAP" >/dev/null 2>&1; then
    ip tuntap add dev "$TAP" mode tap user "${SUDO_USER:-root}"
  fi
  ip addr show dev "$TAP" | grep -q "$HOST_IP" || ip addr add "$HOST_IP/24" dev "$TAP"
  ip link set "$TAP" up

  sysctl -qw net.ipv4.ip_forward=1
  iptables -t nat -C POSTROUTING -s 172.16.0.0/12 -o "$HOST_IFACE" -j MASQUERADE 2>/dev/null || \
    iptables -t nat -A POSTROUTING -s 172.16.0.0/12 -o "$HOST_IFACE" -j MASQUERADE
  iptables -C FORWARD -i "$TAP" -o "$HOST_IFACE" -j ACCEPT 2>/dev/null || \
    iptables -I FORWARD -i "$TAP" -o "$HOST_IFACE" -j ACCEPT
  iptables -C FORWARD -i "$HOST_IFACE" -o "$TAP" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || \
    iptables -I FORWARD -i "$HOST_IFACE" -o "$TAP" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT

  echo "tap $TAP ($HOST_IP/24) up, guest NAT via $HOST_IFACE"
}

down() {
  HOST_IFACE=$(host_iface)
  [ -n "$HOST_IFACE" ] && {
    iptables -D FORWARD -i "$HOST_IFACE" -o "$TAP" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || true
    iptables -D FORWARD -i "$TAP" -o "$HOST_IFACE" -j ACCEPT 2>/dev/null || true
    iptables -t nat -D POSTROUTING -s 172.16.0.0/12 -o "$HOST_IFACE" -j MASQUERADE 2>/dev/null || true
  }
  ip link del "$TAP" 2>/dev/null || true
  echo "tap $TAP removed, NAT rules dropped"
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  *) echo "usage: sudo $0 up | sudo $0 down" >&2; exit 2 ;;
esac
