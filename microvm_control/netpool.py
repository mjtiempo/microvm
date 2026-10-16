"""Tap pool and NAT management: pool state, allocation, systemd install.

Pool layout (index 0 is reserved for the legacy shared `fc-tap`):

    mvtap<i>  host 172.16.<4i>.1/30  guest 172.16.<4i>.2/30

`net init` defines the pool in `$MICROVM_HOME/net.json` (no root). `net
install` (root) writes the generated setup script and systemd unit, starts the
pool and records it. Instance creation allocates a free index; deletion
releases it.
"""

from __future__ import annotations

import fcntl
import json
import os
import pwd
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path

from microvm_control import __version__
from microvm_control.errors import DryRun, MvError
from microvm_control.output import envelope
from microvm_control.store import home_path  # pyright: ignore[reportMissingImports]
from microvm_control.util import (  # pyright: ignore[reportMissingImports]
    atomic_write_json,
    exists_quiet as _exists,
    mkdir_guarded,
    now_iso,
    run,
    unlink_guarded as _unlink,
    which,
)

BASE = "172.16"
INDEX_START = 1
MAX_TAPS = 64
DEFAULT_TAPS = 8
SERVICE = "microvm-net.service"
UNIT_PATH = Path("/etc/systemd/system") / SERVICE
SCRIPT_PATH = Path("/usr/local/lib/microvm/net-setup.sh")
STATE_NAME = "net.json"
LOCK_NAME = "net.lock"


# --------------------------------------------------------------------------- #
# pool geometry
# --------------------------------------------------------------------------- #

def tap_name(index: int) -> str:
    return f"mvtap{index}"


def host_ip(index: int) -> str:
    return f"{BASE}.{4 * index}.1"


def guest_ip(index: int) -> str:
    return f"{BASE}.{4 * index}.2"


def pool_prefix() -> int:
    return 30


def _target(index: int) -> dict:
    return {"index": index, "tap": tap_name(index), "ip": guest_ip(index),
            "gw": host_ip(index), "prefix": pool_prefix()}


# --------------------------------------------------------------------------- #
# guarded filesystem helpers
# --------------------------------------------------------------------------- #

def _write_file(path: Path, text: str, mode: int) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        os.chmod(path, mode)
    except OSError as exc:
        raise MvError("io-failed", f"cannot write {path}", detail=str(exc)) from exc


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #

def state_path(home: Path) -> Path:
    return home / STATE_NAME


def load_state(home: Path) -> dict | None:
    path = state_path(home)
    if not _exists(path):
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise MvError("bad-state", "net.json is unreadable", detail=str(exc)) from exc


def save_state(home: Path, state: dict) -> None:
    try:
        atomic_write_json(state_path(home), state)
    except OSError as exc:
        raise MvError("io-failed", f"cannot write {state_path(home)}",
                      detail=str(exc)) from exc


@contextmanager
def _lock(home: Path):
    try:
        (home / "locks").mkdir(parents=True, exist_ok=True)
        handle = open(home / "locks" / LOCK_NAME, "w")
    except OSError as exc:
        raise MvError("io-failed", "cannot open the net lock", detail=str(exc)) from exc
    with handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _instance_exists(home: Path, instance_id: str) -> bool:
    try:
        return (home / "instances" / instance_id / "meta.json").is_file()
    except OSError:
        return False


def _reconcile(state: dict, home: Path) -> dict:
    allocations = state.get("allocations") or {}
    state["allocations"] = {k: v for k, v in allocations.items() if _instance_exists(home, v)}
    return state


def _allocations(state: dict) -> dict[int, str]:
    out: dict[int, str] = {}
    for key, value in (state.get("allocations") or {}).items():
        try:
            out[int(key)] = value
        except (TypeError, ValueError):
            continue
    return out


def _first_free(state: dict, instance_id: str) -> int:
    allocations = _allocations(state)
    for index, owner in allocations.items():
        if owner == instance_id:
            return index
    used = set(allocations)
    taps = int(state.get("taps", 0))
    for index in range(INDEX_START, taps + 1):
        if index not in used:
            return index
    raise MvError(
        "no-tap-available",
        f"the tap pool ({taps} taps) is fully allocated",
        hint="sudo microvm-control net grow --taps 12, or delete an instance",
    )


def configured(home: Path) -> bool:
    return load_state(home) is not None


def plan(home: Path, instance_id: str) -> dict | None:
    """Read-only allocation preview (used by --dry-run)."""
    state = load_state(home)
    if state is None:
        return None
    return _target(_first_free(_reconcile(state, home), instance_id))


def allocate(home: Path, instance_id: str) -> dict:
    with _lock(home):
        state = load_state(home)
        if state is None:
            raise MvError("net-not-configured", "no tap pool is defined",
                          hint="microvm-control net init --taps 8")
        state = _reconcile(state, home)
        index = _first_free(state, instance_id)
        state.setdefault("allocations", {})[str(index)] = instance_id
        save_state(home, state)
        return _target(index)


def release(home: Path, instance_id: str) -> None:
    with _lock(home):
        state = load_state(home)
        if state is None:
            return
        allocations = state.get("allocations") or {}
        state["allocations"] = {k: v for k, v in allocations.items() if v != instance_id}
        save_state(home, state)


def pool_summary(home: Path) -> dict | None:
    state = load_state(home)
    if state is None:
        return None
    state = _reconcile(state, home)
    allocations = _allocations(state)
    taps = int(state.get("taps", 0))
    return {"taps": taps, "allocated": len(allocations), "free": max(0, taps - len(allocations)),
            "user": state.get("user")}


# --------------------------------------------------------------------------- #
# live state
# --------------------------------------------------------------------------- #

def tap_state(name: str) -> dict:
    cp = run(["ip", "link", "show", name])
    if cp.returncode != 0:
        return {"exists": False, "up": False}
    first_line = (cp.stdout or "").splitlines()[0] if cp.stdout else ""
    match = re.search(r"<([^>]*)>", first_line)
    up = bool(match and "UP" in match.group(1).split(","))
    return {"exists": True, "up": up}


def _default_iface() -> str | None:
    cp = run(["ip", "route", "list", "default"])
    if cp.returncode != 0:
        return None
    for line in (cp.stdout or "").splitlines():
        parts = line.split()
        if parts and parts[0] == "default" and len(parts) >= 5:
            return parts[4]
    return None


def _rule_present(argv: list[str]) -> bool:
    return run(argv).returncode == 0


def _systemctl_is_active() -> str:
    try:
        cp = subprocess.run(["systemctl", "is-active", SERVICE],
                            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return (cp.stdout or "").strip() or "unknown"


# --------------------------------------------------------------------------- #
# generated script + unit
# --------------------------------------------------------------------------- #

SCRIPT_TEMPLATE = """#!/bin/sh
# generated by microvm-control %(version)s on %(created)s
# tap pool + NAT for microvm-control; manual edits will be overwritten
set -eu

TAPS=%(taps)d
TAP_USER='%(user)s'
BASE="%(base)s"

WAN="$(ip route list default 2>/dev/null | awk '/^default/{print $5; exit}')"

up() {
  i=1
  while [ "$i" -le "$TAPS" ]; do
    name="mvtap$i"
    host="$BASE.$((i * 4)).1"
    if ! ip link show "$name" >/dev/null 2>&1; then
      ip tuntap add dev "$name" mode tap user "$TAP_USER"
    fi
    if ! ip addr show dev "$name" | grep -q "$host"; then
      ip addr add "$host/30" dev "$name"
    fi
    ip link set "$name" up
    i=$((i + 1))
  done
  # drop pool taps beyond the configured size
  i=$((TAPS + 1))
  while [ "$i" -le 64 ]; do
    ip link del "mvtap$i" >/dev/null 2>&1 || true
    i=$((i + 1))
  done
  sysctl -qw net.ipv4.ip_forward=1 || true
  [ -n "$WAN" ] || return 0
  iptables -t nat -C POSTROUTING -s "$BASE.0.0/12" -o "$WAN" -j MASQUERADE 2>/dev/null \\
    || iptables -t nat -A POSTROUTING -s "$BASE.0.0/12" -o "$WAN" -j MASQUERADE
  iptables -C FORWARD -i 'mvtap+' -o "$WAN" -j ACCEPT 2>/dev/null \\
    || iptables -A FORWARD -i 'mvtap+' -o "$WAN" -j ACCEPT
  iptables -C FORWARD -i "$WAN" -o 'mvtap+' -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null \\
    || iptables -A FORWARD -i "$WAN" -o 'mvtap+' -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
}

down() {
  if [ -n "$WAN" ]; then
    iptables -t nat -D POSTROUTING -s "$BASE.0.0/12" -o "$WAN" -j MASQUERADE 2>/dev/null || true
    iptables -D FORWARD -i 'mvtap+' -o "$WAN" -j ACCEPT 2>/dev/null || true
    iptables -D FORWARD -i "$WAN" -o 'mvtap+' -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || true
  fi
  i=1
  while [ "$i" -le 64 ]; do
    ip link del "mvtap$i" >/dev/null 2>&1 || true
    i=$((i + 1))
  done
}

case "${1:-up}" in
  up) up ;;
  down) down ;;
  *) echo "usage: $0 up|down" >&2; exit 2 ;;
esac
"""

UNIT_TEMPLATE = """[Unit]
Description=microvm-control tap pool and NAT
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=%(script)s up
ExecStop=%(script)s down

[Install]
WantedBy=multi-user.target
"""


def _render_script(taps: int, user: str) -> str:
    return SCRIPT_TEMPLATE % {"version": __version__, "created": now_iso(),
                              "taps": taps, "user": user, "base": BASE}


def _render_unit() -> str:
    return UNIT_TEMPLATE % {"script": SCRIPT_PATH}


# --------------------------------------------------------------------------- #
# verbs
# --------------------------------------------------------------------------- #

def _current_user() -> str:
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        return sudo_user
    return pwd.getpwuid(os.getuid()).pw_name


def _validate_taps(taps: int) -> int:
    if not 1 <= taps <= MAX_TAPS:
        raise MvError("bad-args", f"--taps must be between 1 and {MAX_TAPS}")
    return taps


TAP_USER_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def validate_tap_user(user: str) -> str:
    if not TAP_USER_RE.match(user or ""):
        raise MvError("bad-args", f"invalid tap owner name: {user!r}",
                      hint="usernames must match [A-Za-z0-9._-]+")
    return user


def _new_state(home: Path, taps: int, user: str | None) -> dict:
    state = _reconcile(load_state(home) or {}, home)
    return {
        "schema": 1,
        "taps": taps,
        "user": user or state.get("user") or _current_user(),
        "base": BASE,
        "index_start": INDEX_START,
        "created_at": state.get("created_at") or now_iso(),
        "updated_at": now_iso(),
        "allocations": state.get("allocations") or {},
    }


def _check_shrink(state: dict | None, taps: int) -> None:
    if not state:
        return
    beyond = sorted(i for i in _allocations(state) if i > taps)
    if beyond:
        raise MvError("pool-shrink",
                      f"{len(beyond)} instance(s) are allocated to taps above {taps}",
                      detail={"indices": beyond,
                              "hint": "delete those instances or keep the pool size"})


def net_init(ns, home: Path):
    taps = _validate_taps(ns.taps or DEFAULT_TAPS)
    state = load_state(home)
    _check_shrink(state, taps)
    if ns.dry_run:
        raise DryRun([f"define a {taps}-tap pool in {state_path(home)}",
                      f"taps mvtap{INDEX_START}..mvtap{taps} "
                      f"({host_ip(INDEX_START)}/30 .. {host_ip(taps)}/30)"])
    new_state = _new_state(home, taps, _current_user())
    save_state(home, new_state)
    return envelope(verified=True, effect="confirmed", rung="fs", data={
        "state_path": str(state_path(home)),
        "pool": {"taps": taps, "user": new_state["user"],
                 "index_start": INDEX_START, "base": BASE},
    })


def net_install(ns, home: Path):
    state = load_state(home)
    taps = _validate_taps(ns.taps or (state or {}).get("taps") or DEFAULT_TAPS)
    _check_shrink(state, taps)
    user = getattr(ns, "user", None) or _current_user()
    plan = [
        f"write {SCRIPT_PATH} (pool of {taps} taps, tap owner {user})",
        f"write {UNIT_PATH}",
        "systemctl daemon-reload",
        f"systemctl enable --now {SERVICE}",
        f"verify mvtap{INDEX_START}..mvtap{taps} exist and are up + NAT via the default interface",
    ]
    if ns.dry_run:
        raise DryRun(plan)
    if os.geteuid() != 0:
        raise MvError("needs-root", "net install requires root",
                      hint=f"sudo microvm-control net install --taps {taps}")
    if not which("iptables"):
        raise MvError("missing-tool", "iptables is required for NAT",
                      hint="install iptables, or use --no-nat once supported")
    try:
        pwd.getpwnam(user)
    except KeyError as exc:
        raise MvError("bad-args", f"user {user!r} does not exist") from exc
    validate_tap_user(user)

    # create the state dir 0700 before writing net.json: root's mkdir would
    # otherwise leave it at the umask default until the ownership handover
    mkdir_guarded(home, 0o700)
    _write_file(SCRIPT_PATH, _render_script(taps, user), 0o755)
    _write_file(UNIT_PATH, _render_unit(), 0o644)
    save_state(home, _new_state(home, taps, user))

    for cmd in (["systemctl", "daemon-reload"], ["systemctl", "enable", "--now", SERVICE]):
        cp = run(cmd)
        if cp.returncode != 0:
            raise MvError("net-install-failed", f"{' '.join(cmd)} failed",
                          detail={"stderr": (cp.stderr or cp.stdout).strip()[-400:],
                                  "hint": f"journalctl -u {SERVICE}"})

    details = []
    for index in range(INDEX_START, taps + 1):
        info = tap_state(tap_name(index))
        details.append({"index": index, "tap": tap_name(index), "host_ip": host_ip(index),
                        "guest_ip": guest_ip(index), **info})
    if not all(d["exists"] and d["up"] for d in details):
        raise MvError("net-install-failed", "the service ran but some taps are missing or down",
                      hint=f"journalctl -u {SERVICE}", detail={"taps": details})
    return envelope(verified=True, effect="confirmed", rung="systemd", data={
        "pool": {"taps": taps, "user": user}, "taps": details,
        "unit": str(UNIT_PATH), "script": str(SCRIPT_PATH),
        "service": _systemctl_is_active(),
    })


def net_uninstall(ns, home: Path):
    plan = [
        f"systemctl disable --now {SERVICE}",
        f"remove {UNIT_PATH}",
        f"remove {SCRIPT_PATH}",
        f"remove {state_path(home)}",
    ]
    if ns.dry_run:
        raise DryRun(plan)
    if os.geteuid() != 0:
        raise MvError("needs-root", "net uninstall requires root",
                      hint="sudo microvm-control net uninstall")
    run(["systemctl", "disable", "--now", SERVICE])
    _unlink(UNIT_PATH)
    _unlink(SCRIPT_PATH)
    _unlink(state_path(home))
    run(["systemctl", "daemon-reload"])
    verified = not _exists(UNIT_PATH) and not _exists(state_path(home))
    return envelope(verified=verified, effect="confirmed", rung="systemd", data={
        "unit_removed": not _exists(UNIT_PATH),
        "state_removed": not _exists(state_path(home)),
    })


def net_status(ns, home: Path):
    state = load_state(home)
    if state is None:
        return envelope(data={
            "configured": False,
            "installed": _exists(UNIT_PATH) and _exists(SCRIPT_PATH),
            "service": _systemctl_is_active(),
            "hint": "microvm-control net init --taps 8, or sudo microvm-control net install",
        })
    state = _reconcile(state, home)
    taps = int(state.get("taps", 0))
    allocations = _allocations(state)
    entries = []
    for index in range(INDEX_START, taps + 1):
        info = tap_state(tap_name(index))
        entries.append({
            "index": index, "tap": tap_name(index),
            "host_ip": host_ip(index), "guest_ip": guest_ip(index),
            "prefix": pool_prefix(), "allocated_to": allocations.get(index),
            "exists": info["exists"], "up": info["up"],
        })
    free = sum(1 for entry in entries if entry["allocated_to"] is None)
    nat: dict = {"checked": False}
    if os.geteuid() == 0:
        wan = _default_iface()
        nat = {"checked": True, "wan": wan}
        if wan:
            nat["masquerade"] = _rule_present([
                "iptables", "-t", "nat", "-C", "POSTROUTING",
                "-s", f"{BASE}.0.0/12", "-o", wan, "-j", "MASQUERADE"])
            nat["forward"] = _rule_present([
                "iptables", "-C", "FORWARD", "-i", "mvtap+", "-o", wan, "-j", "ACCEPT"])
    return envelope(data={
        "configured": True,
        "state_path": str(state_path(home)),
        "installed": _exists(UNIT_PATH) and _exists(SCRIPT_PATH),
        "service": _systemctl_is_active(),
        "pool": {"taps": taps, "user": state.get("user"),
                 "index_start": INDEX_START, "base": BASE},
        "taps": entries,
        "free": free,
        "nat": nat,
    })


def dispatch(ns, home: Path):
    handlers = {
        "init": net_init,
        "install": net_install,
        "grow": net_install,
        "uninstall": net_uninstall,
        "status": net_status,
    }
    handler = handlers.get(ns.net_verb)
    if handler is None:
        raise MvError("bad-args", f"unknown net verb: {ns.net_verb}")
    return handler(ns, home)
