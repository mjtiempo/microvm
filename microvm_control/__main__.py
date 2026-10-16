"""microvm-control entry point: argparse surface and JSON envelope handling.

Global flags come before the verb; everything after the verb belongs to it.
Exit status: 0 ok, 1 refusal (ERR[code] on stderr), 2 usage, 130 interrupt.
Exactly one JSON object is printed on stdout for every non-interactive verb.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback

# The pyright language server in this session does not index modules added
# mid-session; CLI pyright and python3 resolve all of these cleanly.
from microvm_control import __version__, lifecycle  # pyright: ignore[reportAttributeAccessIssue]
from microvm_control import netpool  # pyright: ignore[reportAttributeAccessIssue]
from microvm_control.errors import EXIT_INTERRUPT, EXIT_OK, EXIT_REFUSED, DryRun, MvError
from microvm_control.output import emit, envelope
from microvm_control.store import home_path, sudo_home  # pyright: ignore[reportMissingImports]


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

    return parser


def dispatch(ns) -> dict | tuple[dict, int]:
    home = home_path(ns.home)
    if ns.verb == "net" and ns.net_verb in ("install", "grow", "uninstall"):
        home = sudo_home(ns.home)
    if ns.verb == "image":
        if ns.image_verb == "build":
            return lifecycle.image_build(ns, home)
        raise MvError("bad-args", f"unknown image verb: {ns.image_verb}")
    if ns.verb == "net":
        return netpool.dispatch(ns, home)
    handlers = {
        "create": lifecycle.create,
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
        "caps": lifecycle.caps,
        "doctor": lifecycle.doctor,
    }
    return handlers[ns.verb](ns, home)


def main(argv: list[str] | None = None) -> int:
    ns = build_parser().parse_args(argv)
    try:
        result = dispatch(ns)
    except DryRun as plan:
        emit(envelope(ok=True, data={"dry_run": True, "plan": plan.plan}), ns.quiet)
        return EXIT_OK
    except MvError as exc:
        emit(envelope(ok=False, error=exc.to_json()), ns.quiet)
        sys.stderr.write(f"ERR[{exc.code}]: {exc.message}\n")
        if exc.hint:
            sys.stderr.write(f"  hint: {exc.hint}\n")
        return EXIT_REFUSED
    except KeyboardInterrupt:
        sys.stderr.write("interrupted\n")
        return EXIT_INTERRUPT
    except BrokenPipeError:
        return EXIT_OK
    except Exception as exc:  # last-resort refusal; MICROVM_DEBUG=1 for a traceback
        if os.environ.get("MICROVM_DEBUG") == "1":
            traceback.print_exc()
        emit(envelope(ok=False, error={"code": "internal", "message": str(exc)}), ns.quiet)
        sys.stderr.write(f"ERR[internal]: {exc}\n")
        return EXIT_REFUSED
    code = EXIT_OK
    if isinstance(result, tuple):
        result, code = result
    emit(result, ns.quiet)
    return code


if __name__ == "__main__":
    sys.exit(main())
