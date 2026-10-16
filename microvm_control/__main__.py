"""microvm-control entry point: argparse surface and JSON envelope handling.

Global flags come before the verb; everything after the verb belongs to it.
Exit status: 0 ok, 1 refusal (ERR[code] on stderr), 2 usage, 130 interrupt.
Exactly one JSON object is printed on stdout for every non-interactive verb.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from pathlib import Path

# The pyright language server in this session does not index modules added
# mid-session; CLI pyright and python3 resolve all of these cleanly.
from microvm_control import __version__, lifecycle  # pyright: ignore[reportAttributeAccessIssue]
from microvm_control import audit  # pyright: ignore[reportAttributeAccessIssue]
from microvm_control import backup  # pyright: ignore[reportAttributeAccessIssue]
from microvm_control import netpool  # pyright: ignore[reportAttributeAccessIssue]
from microvm_control.errors import EXIT_INTERRUPT, EXIT_OK, EXIT_REFUSED, DryRun, MvError
from microvm_control.output import emit, envelope
from microvm_control.store import home_path, sudo_home  # pyright: ignore[reportMissingImports]
from microvm_control.util import adopt_ownership, invoking_identity, now_iso  # pyright: ignore[reportMissingImports]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="microvm-control",
        description="On-demand Firecracker microVMs (one JSON object per call).",
    )
    parser.add_argument("--version", action="version", version=f"microvm-control {__version__}")
    parser.add_argument("--home", metavar="DIR",
                        help="state directory (default ~/.local/share/microvm, env MICROVM_HOME)")
    parser.add_argument("--quiet", action="store_true", help="suppress the JSON reply on stdout")
    parser.add_argument("--dry-run", action="store_true",
                        help="show the plan of a mutating verb without doing it")
    parser.add_argument("--timeout", type=float, default=60.0, metavar="SEC",
                        help="default timeout for waits and ssh (default 60)")
    sub = parser.add_subparsers(dest="verb", required=True)

    image = sub.add_parser("image", help="base image management")
    image_sub = image.add_subparsers(dest="image_verb", required=True)
    image_build_parser = image_sub.add_parser("build", help="build/import the base image set")
    image_build_parser.add_argument("--force", action="store_true",
                                    help="rebuild even when the base image is present")

    p_net = sub.add_parser("net", help="tap pool and NAT")
    net_sub = p_net.add_subparsers(dest="net_verb", required=True)
    p_net_init = net_sub.add_parser("init", help="define the pool in state (no root)")
    p_net_init.add_argument("--taps", type=int, default=netpool.DEFAULT_TAPS)
    net_sub.add_parser("status", help="pool, taps, service and NAT state")
    p_net_install = net_sub.add_parser("install", help="apply taps + systemd unit (root)")
    p_net_install.add_argument("--taps", type=int, default=None)
    p_net_install.add_argument("--user", default=None,
                               help="tap owner (default: SUDO_USER)")
    p_net_grow = net_sub.add_parser("grow", help="resize the pool (root)")
    p_net_grow.add_argument("--taps", type=int, required=True)
    net_sub.add_parser("uninstall", help="remove the unit, taps and pool state (root)")

    p_clone = sub.add_parser("clone", help="copy an instance (a running source is paused for the copy)")
    p_clone.add_argument("source")
    p_clone.add_argument("id")
    p_clone.add_argument("--fresh-data", action="store_true",
                          help="start the clone with an empty /persist")
    p_clone.add_argument("--vcpu", type=int, default=None)
    p_clone.add_argument("--mem", type=int, default=None, metavar="MIB", dest="mem_mib")
    p_clone.add_argument("--ttl", type=int, metavar="SEC")
    p_clone.add_argument("--tap", default=None)
    p_clone.add_argument("--ip", default=None)
    p_clone.add_argument("--gw", default=None)
    p_clone.add_argument("--prefix", type=int, default=None)

    p_snap = sub.add_parser("snapshot", help="memory snapshots (pause/snapshot/resume)")
    snap_sub = p_snap.add_subparsers(dest="snapshot_verb", required=True)
    ps_create = snap_sub.add_parser("create", help="snapshot a running instance")
    ps_create.add_argument("id")
    ps_create.add_argument("--name", required=True)
    ps_list = snap_sub.add_parser("list", help="list snapshots of an instance")
    ps_list.add_argument("id")
    ps_delete = snap_sub.add_parser("delete", help="delete a snapshot")
    ps_delete.add_argument("id")
    ps_delete.add_argument("--name", required=True)
    ps_load = snap_sub.add_parser("load", help="restore an instance from a snapshot")
    ps_load.add_argument("id")
    ps_load.add_argument("--name", required=True)
    ps_load.add_argument("--force", action="store_true",
                         help="load even when a disk image changed size since the snapshot")
    ps_load.add_argument("--timeout", type=float, default=None, dest="timeout_verb", metavar="SEC")

    p_reap = sub.add_parser("reap", help="delete expired instances (TTL)")
    p_reap.add_argument("--older-than", type=float, default=None, metavar="SEC",
                        help="also reap instances stopped longer than SEC")
    p_reap.add_argument("--timeout", type=float, default=None, dest="timeout_verb", metavar="SEC")

    p_gc = sub.add_parser("gc", help="remove old trash entries, exec artifacts and snapshots")
    p_gc.add_argument("--trash-days", type=int, default=None)
    p_gc.add_argument("--artifact-days", type=int, default=None)
    p_gc.add_argument("--snapshot-days", type=int, default=None,
                      help="prune snapshots older than N days (disabled by default)")

    p_browser = sub.add_parser("browser", help="headless Chromium in the guest (dom/screenshot/pdf)")
    p_browser.add_argument("id")
    browser_action = p_browser.add_mutually_exclusive_group(required=True)
    browser_action.add_argument("--dom", metavar="URL", help="print the rendered DOM")
    browser_action.add_argument("--screenshot", metavar="URL", help="save a PNG")
    browser_action.add_argument("--pdf", metavar="URL", help="save a PDF")
    p_browser.add_argument("--out", default=None, metavar="FILE")
    p_browser.add_argument("--window-size", default="1280,800", metavar="W,H")
    p_browser.add_argument("--timeout", type=float, default=None, dest="timeout_verb", metavar="SEC")

    p_backup = sub.add_parser("backup", help="archive an instance (disks + meta + sha256 manifest)")
    p_backup.add_argument("id")
    p_backup.add_argument("--out", default=None, metavar="DIR")
    p_backup.add_argument("--name", default=None)
    p_backup.add_argument("--no-data", action="store_true", help="exclude /persist")
    p_backup.add_argument("--no-pause", action="store_true",
                          help="copy a running instance without pausing it first")
    p_backup.add_argument("--format", choices=backup.BACKUP_FORMATS, default="tar.zst")

    p_restore = sub.add_parser("restore", help="restore an instance from a backup archive")
    p_restore.add_argument("archive")
    p_restore.add_argument("--id", default=None,
                           help="new instance id (default: <original>-r<ts>)")
    p_restore.add_argument("--verify", dest="verify_boot", action="store_true",
                           help="boot the restored instance and wait for ssh")
    p_restore.add_argument("--fresh-data", action="store_true")
    p_restore.add_argument("--ttl", type=int, metavar="SEC")
    p_restore.add_argument("--tap", default=None)
    p_restore.add_argument("--ip", default=None)
    p_restore.add_argument("--gw", default=None)
    p_restore.add_argument("--prefix", type=int, default=None)
    p_restore.add_argument("--timeout", type=float, default=None, dest="timeout_verb", metavar="SEC")

    p_create = sub.add_parser("create", help="create an instance (disk copies + identity)")
    p_create.add_argument("id")
    p_create.add_argument("--vcpu", type=int, default=2)
    p_create.add_argument("--mem", type=int, default=2048, metavar="MIB", dest="mem_mib")
    p_create.add_argument("--ttl", type=int, metavar="SEC", help="expire after SEC seconds")
    p_create.add_argument("--tap", default=None,
                          help="explicit tap (default: allocate from the pool, else fc-tap)")
    p_create.add_argument("--ip", default=None, help="explicit guest ip (pair with --tap)")
    p_create.add_argument("--gw", default=None)
    p_create.add_argument("--prefix", type=int, default=None)
    p_create.add_argument("--fresh-data", action="store_true",
                          help="start with an empty /persist instead of the base data image")

    p_start = sub.add_parser("start", help="boot an instance and wait for ssh")
    p_start.add_argument("id")
    p_start.add_argument("--no-wait-ssh", action="store_true")
    p_start.add_argument("--timeout", type=float, default=None, dest="timeout_verb", metavar="SEC")

    p_stop = sub.add_parser("stop", help="stop gracefully (CtrlAltDel via the API)")
    p_stop.add_argument("id")
    p_stop.add_argument("--timeout", type=float, default=None, dest="timeout_verb", metavar="SEC")

    p_restart = sub.add_parser("restart", help="stop then start")
    p_restart.add_argument("id")
    p_restart.add_argument("--no-wait-ssh", action="store_true")
    p_restart.add_argument("--timeout", type=float, default=None, dest="timeout_verb", metavar="SEC")

    p_status = sub.add_parser("status", help="instance state (never lies about a dead pid)")
    p_status.add_argument("id")
    p_status.add_argument("--probe", action="store_true", help="also check ssh reachability")

    sub.add_parser("list", help="all instances")

    p_exec = sub.add_parser("exec", help="run a command in the guest over ssh")
    p_exec.add_argument("id")
    p_exec.add_argument("--out", metavar="FILE", help="write stdout here when it is large")
    p_exec.add_argument("--timeout", type=float, default=None, dest="timeout_verb", metavar="SEC")
    p_exec.add_argument("command", nargs=argparse.REMAINDER)

    p_cp = sub.add_parser("cp", help="copy files: ID:/guest/path <-> /local/path")
    p_cp.add_argument("src")
    p_cp.add_argument("dst")

    p_ssh = sub.add_parser("ssh", help="interactive shell (replaces this process)")
    p_ssh.add_argument("id")

    p_logs = sub.add_parser("logs", help="tail the serial console or the firecracker log")
    p_logs.add_argument("id")
    p_logs.add_argument("--firecracker", action="store_true",
                        help="show the firecracker log instead of the console")
    p_logs.add_argument("--tail", type=int, default=40)

    p_delete = sub.add_parser("delete", help="stop (if needed) and remove an instance")
    p_delete.add_argument("id")
    p_delete.add_argument("--yes", action="store_true", help="required when not interactive")
    p_delete.add_argument("--purge", action="store_true", help="remove instead of moving to trash")
    p_delete.add_argument("--force", action="store_true",
                          help="SIGTERM/SIGKILL instead of a graceful stop")
    p_delete.add_argument("--timeout", type=float, default=None, dest="timeout_verb", metavar="SEC")

    sub.add_parser("caps", help="what this host/install can do")
    sub.add_parser("doctor", help="preflight checks (exit 1 when a required check fails)")

    p_audit = sub.add_parser("audit", help="recent invocations (JSONL)")
    p_audit.add_argument("--tail", type=int, default=50)

    return parser


def resolve_home(ns) -> Path:
    home = home_path(ns.home)
    if ns.verb == "net" and ns.net_verb in ("install", "grow", "uninstall"):
        home = sudo_home(ns.home)
    return home


def dispatch(ns, home: Path) -> dict | tuple[dict, int]:
    if ns.verb == "image":
        if ns.image_verb == "build":
            return lifecycle.image_build(ns, home)
        raise MvError("bad-args", f"unknown image verb: {ns.image_verb}")
    if ns.verb == "net":
        return netpool.dispatch(ns, home)
    if ns.verb == "snapshot":
        snapshot_handlers = {
            "create": lifecycle.snapshot_create,
            "list": lifecycle.snapshot_list,
            "delete": lifecycle.snapshot_delete,
            "load": lifecycle.snapshot_load,
        }
        return snapshot_handlers[ns.snapshot_verb](ns, home)
    handlers = {
        "create": lifecycle.create,
        "clone": lifecycle.clone,
        "reap": lifecycle.reap,
        "gc": lifecycle.gc,
        "backup": backup.backup,
        "restore": backup.restore,
        "start": lifecycle.start,
        "stop": lifecycle.stop,
        "restart": lifecycle.restart,
        "status": lifecycle.status,
        "list": lifecycle.list_instances,
        "exec": lifecycle.exec_cmd,
        "cp": lifecycle.cp,
        "ssh": lifecycle.ssh_shell,
        "logs": lifecycle.logs,
        "delete": lifecycle.delete,
        "browser": lifecycle.browser,
        "caps": lifecycle.caps,
        "doctor": lifecycle.doctor,
        "audit": audit.verb,
    }
    return handlers[ns.verb](ns, home)


def main(argv: list[str] | None = None) -> int:
    ns = build_parser().parse_args(argv)
    home = resolve_home(ns)
    privileged = ns.verb == "net" and ns.net_verb in ("install", "grow", "uninstall")
    started = time.monotonic()
    ok = False
    code = None
    exit_code = EXIT_OK
    try:
        result = dispatch(ns, home)
    except DryRun as plan:
        emit(envelope(ok=True, data={"dry_run": True, "plan": plan.plan}), ns.quiet)
        ok = True
        code = "dry-run"
    except MvError as exc:
        emit(envelope(ok=False, error=exc.to_json()), ns.quiet)
        sys.stderr.write(f"ERR[{exc.code}]: {exc.message}\n")
        if exc.hint:
            sys.stderr.write(f"  hint: {exc.hint}\n")
        code = exc.code
        exit_code = EXIT_REFUSED
    except KeyboardInterrupt:
        sys.stderr.write("interrupted\n")
        code = "interrupt"
        exit_code = EXIT_INTERRUPT
    except BrokenPipeError:
        code = "broken-pipe"
        return EXIT_OK
    except Exception as exc:  # last-resort refusal; MICROVM_DEBUG=1 for a traceback
        if os.environ.get("MICROVM_DEBUG") == "1":
            traceback.print_exc()
        emit(envelope(ok=False, error={"code": "internal", "message": str(exc)}), ns.quiet)
        sys.stderr.write(f"ERR[internal]: {exc}\n")
        code = "internal"
        exit_code = EXIT_REFUSED
    else:
        if isinstance(result, tuple):
            result, exit_code = result
        emit(result, ns.quiet)
        ok = exit_code == EXIT_OK
        if not ok:
            code = "doctor-failed" if ns.verb == "doctor" else "failed"
    finally:
        audit.append(home, {
            "ts": now_iso(),
            "verb": ns.verb,
            "sub": getattr(ns, f"{ns.verb}_verb", None),
            "instance": getattr(ns, "id", None) or getattr(ns, "source", None),
            "argv": sys.argv[1:],
            "ok": ok,
            "code": code,
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
        })
        if privileged and os.geteuid() == 0:
            # sudo wrote state as root; hand it back to the invoking user so
            # every later unprivileged verb can read it (see C1 in the review)
            adopt_ownership([home, home / netpool.STATE_NAME, home / audit.AUDIT_NAME],
                            invoking_identity())
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
