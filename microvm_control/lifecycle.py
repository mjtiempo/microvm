"""Verb implementations for microvm-control (M0 slice).

Covered: image build, create, start, stop, restart, status, list, exec, cp,
ssh, logs, delete, caps, doctor. Networking is the single shared tap mode
(`fc-tap`, 172.16.0.2/24); the tap pool arrives in M1.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import IO, Any

# The pyright language server in this session does not index modules added
# mid-session; CLI pyright and python3 resolve all of these cleanly.
from microvm_control import fcapi  # pyright: ignore[reportAttributeAccessIssue]
from microvm_control import identity  # pyright: ignore[reportAttributeAccessIssue]
from microvm_control import netpool  # pyright: ignore[reportAttributeAccessIssue]
from microvm_control import sshx  # pyright: ignore[reportAttributeAccessIssue]
from microvm_control.errors import DryRun, MvError
from microvm_control.output import envelope
from microvm_control.store import Instance, ensure_home, pid_is_firecracker, validate_id  # pyright: ignore[reportMissingImports]
from microvm_control.util import (
    atomic_write_json,
    copy_file,
    du_bytes,
    free_bytes,
    now_epoch,
    now_iso,
    run,
    sha256_file,
    which,
)

AGENT_DEFAULT_TTL_S = 1800
INLINE_LIMIT = 16384
DEFAULT_BOOT_ARGS = "console=ttyS0 reboot=k panic=1 root=/dev/vda rw"
DEFAULT_DATA_SIZE = "4G"
DEFAULT_TAP = "fc-tap"
DEFAULT_IP = "172.16.0.2"
DEFAULT_GW = "172.16.0.1"
DEFAULT_PREFIX = 24


# --------------------------------------------------------------------------- #
# guarded filesystem helpers (each turns OSError into a refusal)
# --------------------------------------------------------------------------- #

def _rmtree(path: Path) -> None:
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def _mkdir(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MvError("io-failed", f"cannot create {path}", detail=str(exc)) from exc


def _open(path: Path, mode: str) -> IO[Any]:
    try:
        return open(path, mode)
    except OSError as exc:
        raise MvError("io-failed", f"cannot open {path}", detail=str(exc)) from exc


def _write_text(path: Path, text: str) -> None:
    try:
        path.write_text(text)
    except OSError as exc:
        raise MvError("io-failed", f"cannot write {path}", detail=str(exc)) from exc


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError as exc:
        raise MvError("io-failed", f"cannot chmod {path}", detail=str(exc)) from exc


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError as exc:
        raise MvError("io-failed", f"cannot stat {path}", detail=str(exc)) from exc


def _exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def _kvm_ok() -> bool:
    try:
        return os.path.exists("/dev/kvm") and os.access("/dev/kvm", os.R_OK | os.W_OK)
    except OSError:
        return False


def _replace(src: Path, dst: Path) -> None:
    try:
        os.replace(src, dst)
    except OSError as exc:
        raise MvError("io-failed", f"cannot move {src} to {dst}", detail=str(exc)) from exc


def _human(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024:
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


# --------------------------------------------------------------------------- #
# domain helpers
# --------------------------------------------------------------------------- #

def base_dir(home: Path) -> Path:
    return home / "images" / "base"


def load_base(home: Path) -> dict:
    base = base_dir(home)
    version_path = base / "version.json"
    if not _exists(version_path):
        raise MvError("image-missing", "base image is not built",
                      hint="microvm-control image build")
    try:
        version = json.loads(version_path.read_text())
    except (OSError, ValueError) as exc:
        raise MvError("image-missing", "base image version.json is unreadable",
                      detail=str(exc)) from exc
    filenames = {
        "firecracker": "firecracker",
        "kernel": version.get("kernel"),
        "rootfs": version.get("rootfs"),
        "data": version.get("data"),
        "key": version.get("key"),
    }
    missing = []
    for name, rel in filenames.items():
        if not rel or not _exists(base / rel):
            missing.append(str(base / rel) if rel else name)
    if missing:
        raise MvError("image-missing", "base image is incomplete",
                      hint="microvm-control image build --force", detail={"missing": missing})
    return {"dir": base, **version}


def mac_for(ip: str) -> str:
    octets = ip.split(".")
    if len(octets) != 4:
        raise MvError("bad-args", f"invalid ip: {ip}")
    try:
        third, fourth = int(octets[2]), int(octets[3])
    except ValueError as exc:
        raise MvError("bad-args", f"invalid ip: {ip}") from exc
    return f"06:00:AC:10:{third:02X}:{fourth:02X}"


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _tail_lines(path: Path, count: int) -> list[str]:
    try:
        with open(path, "r", errors="replace") as f:
            content = f.read()
    except OSError:
        return []
    return content.splitlines()[-count:]


def _alpine_version(repo: Path) -> str | None:
    for p in sorted((repo / "build").glob("alpine-minirootfs-*.tar.gz")):
        return p.name.split("alpine-minirootfs-")[1].split("-x86_64")[0]
    return None


def _reflink_ok(home: Path) -> bool:
    probe = home / ".reflink-probe"
    try:
        probe.write_bytes(b"probe")
        cp = run(["cp", "--reflink=always", str(probe), str(probe) + ".2"])
        return cp.returncode == 0
    except OSError:
        return False
    finally:
        for p in (probe, Path(str(probe) + ".2")):
            try:
                p.unlink()
            except OSError:
                pass


def make_data_image(dst: Path, size: str) -> None:
    stage = dst.parent / ".data-stage"
    _rmtree(stage)
    for sub in ("w3m", "work", "chromium"):
        _mkdir(stage / sub)
    tmp = dst.with_name(dst.name + ".tmp")
    try:
        tmp.unlink()
    except OSError:
        pass
    cp = run(["truncate", "-s", size, str(tmp)])
    if cp.returncode != 0:
        raise MvError("image-build-failed", "could not create the data image",
                      detail=(cp.stderr or "")[-300:])
    cp = run(["unshare", "-Ur", "mke2fs", "-q", "-t", "ext4", "-F", "-m", "0",
              "-L", "persist", "-d", str(stage), str(tmp)])
    if cp.returncode != 0:
        cp = run(["mke2fs", "-q", "-t", "ext4", "-F", "-m", "0",
                  "-L", "persist", "-d", str(stage), str(tmp)])
        if cp.returncode != 0:
            raise MvError("image-build-failed", "mke2fs failed for the data image",
                          detail=(cp.stderr or cp.stdout)[-300:])
    _replace(tmp, dst)
    _rmtree(stage)


def _kill_process(pid, grace: float = 3.0) -> bool:
    if not pid:
        return True
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 2.0)):
        try:
            os.kill(int(pid), sig)
        except (ProcessLookupError, PermissionError, ValueError):
            return True
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not pid_is_firecracker(pid):
                return True
            time.sleep(0.05)
    return not pid_is_firecracker(pid)


def _net_conflict(home: Path, tap: str, ip: str, exclude: str) -> None:
    for other in Instance.all(home):
        if other.id == exclude:
            continue
        try:
            meta = other.reconcile()
        except MvError:
            continue
        if meta.get("state") in ("starting", "running", "paused"):
            net = meta.get("net") or {}
            if net.get("tap") == tap or net.get("ip") == ip:
                raise MvError(
                    "net-conflict",
                    f"'{other.id}' is running on {net.get('tap')} ({net.get('ip')})",
                    hint="M0 shares one tap; stop the other instance or wait for the M1 tap pool",
                )


def _stop_instance(inst: Instance, meta: dict, timeout: float):
    if not inst.pid_alive(meta):
        meta["state"] = "stopped"
        meta["pid"] = None
        meta["stopped_at"] = now_iso()
        inst.save_meta(meta)
        return meta, None
    graceful = True
    try:
        fcapi.FcApi(inst.sock).ctrl_alt_del()
    except MvError:
        graceful = False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and inst.pid_alive(meta):
        time.sleep(0.1)
    if inst.pid_alive(meta):
        graceful = False
        _kill_process(meta["pid"], grace=5)
    meta["state"] = "stopped"
    meta["pid"] = None
    meta["stopped_at"] = now_iso()
    inst.save_meta(meta)
    return meta, graceful


# --------------------------------------------------------------------------- #
# verbs
# --------------------------------------------------------------------------- #

def image_build(ns, home: Path):
    ensure_home(home)
    base = base_dir(home)
    version_path = base / "version.json"
    if _exists(version_path) and not ns.force:
        version = {}
        try:
            version = json.loads(version_path.read_text())
        except (OSError, ValueError):
            pass
        return envelope(data={"base_dir": str(base), "up_to_date": True, **version})
    if ns.dry_run:
        raise DryRun([f"build base image into {base}"])

    repo = Path(__file__).resolve().parent.parent
    repo_images = repo / "images"
    log_path = home / "logs" / "image-build.log"
    _mkdir(log_path.parent)
    source = "repo-import"
    if not _exists(repo_images / "firecracker"):
        source = "build"
        with _open(log_path, "w") as log_file:
            cp = subprocess.run(["bash", "build.sh"], cwd=str(repo),
                                stdout=log_file, stderr=subprocess.STDOUT)
        if cp.returncode != 0:
            raise MvError("image-build-failed", "build.sh failed",
                          hint=f"see {log_path}",
                          detail="\n".join(_tail_lines(log_path, 15)))

    _mkdir(base)
    copy_file(repo_images / "firecracker", base / "firecracker")
    _chmod(base / "firecracker", 0o755)
    if _exists(repo_images / "jailer"):
        copy_file(repo_images / "jailer", base / "jailer")
        _chmod(base / "jailer", 0o755)
    kernel_link = repo_images / "vmlinux"
    if not _exists(kernel_link):
        raise MvError("image-build-failed", "no kernel found in the repo images")
    kernel_name = kernel_link.resolve().name
    copy_file(repo_images / kernel_name, base / kernel_name)
    copy_file(repo_images / "rootfs.ext4", base / "rootfs.ext4")
    make_data_image(base / "data.ext4", os.environ.get("MICROVM_DATA_SIZE", DEFAULT_DATA_SIZE))
    keys = base / "keys"
    _mkdir(keys)
    for name in ("id_ed25519", "id_ed25519.pub"):
        if not _exists(repo / "keys" / name):
            raise MvError("image-build-failed", f"missing control key {name}")
        copy_file(repo / "keys" / name, keys / name)
    _chmod(keys / "id_ed25519", 0o600)

    version = {
        "schema": 1,
        "source": source,
        "created_at": now_iso(),
        "firecracker": _read_text(repo / "build" / "fc.version") or "unknown",
        "kernel": kernel_name,
        "alpine": _alpine_version(repo) or "unknown",
        "rootfs": "rootfs.ext4",
        "data": "data.ext4",
        "key": "keys/id_ed25519",
    }
    atomic_write_json(version_path, version)
    load_base(home)  # raises if anything is missing
    return envelope(verified=True, effect="confirmed", rung=f"fs ({source})", data={
        "base_dir": str(base), **version,
        "rootfs_bytes": _size(base / "rootfs.ext4"),
        "data_bytes": _size(base / "data.ext4"),
    })


def create(ns, home: Path):
    validate_id(ns.id)
    ensure_home(home)
    base = load_base(home)
    ttl = ns.ttl
    if ttl is None and os.environ.get("MICROVM_AGENT") == "1":
        ttl = AGENT_DEFAULT_TTL_S
    inst = Instance(home, ns.id)
    explicit = any(value is not None for value in (ns.tap, ns.ip, ns.gw))
    if explicit and not (ns.tap and ns.ip):
        raise MvError("bad-args", "--tap and --ip must be given together",
                      hint="e.g. --tap fc-tap --ip 172.16.0.2 --gw 172.16.0.1")
    legacy = {"tap": DEFAULT_TAP, "ip": DEFAULT_IP, "gw": DEFAULT_GW,
              "prefix": DEFAULT_PREFIX, "index": None}
    if explicit:
        target = {"tap": ns.tap, "ip": ns.ip, "gw": ns.gw or DEFAULT_GW,
                  "prefix": ns.prefix or DEFAULT_PREFIX, "index": None}
    elif ns.dry_run:
        target = netpool.plan(home, ns.id) or legacy
    else:
        target = netpool.allocate(home, ns.id) if netpool.configured(home) else legacy
    allocated = target.get("index") is not None and not explicit

    plan = [
        f"reserve {inst.dir}",
        f"copy {base['dir']}/{base['rootfs']} -> {inst.dir}/disks/rootfs.ext4 (reflink)",
        f"{'fresh' if ns.fresh_data else 'copy'} data -> {inst.dir}/disks/data.ext4",
        f"network {target['tap']} guest {target['ip']}/{target['prefix']} gw {target['gw']}"
        + (f" (pool index {target['index']})" if target.get("index") else ""),
        f"inject identity hostname={ns.id} + fresh ssh host keys",
        f"write {inst.meta_path}",
    ]
    if ns.dry_run:
        raise DryRun(plan)
    try:
        inst.dir.mkdir(parents=True)
    except FileExistsError as exc:
        raise MvError("already-exists", f"instance '{ns.id}' already exists",
                      hint=f"microvm-control delete {ns.id} --yes, then retry") from exc
    except OSError as exc:
        raise MvError("io-failed", f"cannot create {inst.dir}", detail=str(exc)) from exc
    try:
        for sub in ("disks", "keys", "ssh", "run", "artifacts"):
            _mkdir(inst.dir / sub)
        rootfs = inst.dir / "disks" / "rootfs.ext4"
        data = inst.dir / "disks" / "data.ext4"
        mode = copy_file(base["dir"] / base["rootfs"], rootfs)
        if ns.fresh_data:
            make_data_image(data, os.environ.get("MICROVM_DATA_SIZE", DEFAULT_DATA_SIZE))
        else:
            copy_file(base["dir"] / base["data"], data)
        copy_file(base["dir"] / base["key"], inst.key)
        _chmod(inst.key, 0o600)
        copy_file(base["dir"] / (base["key"] + ".pub"), inst.dir / "keys" / "id_ed25519.pub")

        ident = identity.inject_identity(
            rootfs, instance=ns.id, hostname=ns.id, ip=target["ip"], gw=target["gw"],
            prefix=target["prefix"], workdir=inst.dir / "tmp")
        _rmtree(inst.dir / "tmp")
        _write_text(inst.known_hosts, "\n".join(ident["known_hosts_lines"]) + "\n")

        meta = {
            "schema": 1,
            "id": ns.id,
            "state": "created",
            "created_at": now_iso(),
            "started_at": None,
            "stopped_at": None,
            "expires_at": (now_epoch() + ttl) if ttl else None,
            "config": {
                "vcpu": ns.vcpu,
                "mem_mib": ns.mem_mib,
                "kernel": base["kernel"],
                "firecracker": base["firecracker"],
                "image_dir": str(base["dir"]),
                "boot_args": DEFAULT_BOOT_ARGS,
                "track_dirty_pages": True,
            },
            "net": {"tap": target["tap"], "ip": target["ip"], "gw": target["gw"],
                    "prefix": target["prefix"], "pool_index": target.get("index"),
                    "mac": mac_for(target["ip"])},
            "disks": {
                "rootfs": {"path": "disks/rootfs.ext4", "size_bytes": _size(rootfs)},
                "data": {"path": "disks/data.ext4", "size_bytes": _size(data)},
            },
            "ssh": {"key": "keys/id_ed25519", "known_hosts": "ssh/known_hosts"},
            "pid": None,
        }
        inst.save_meta(meta)

        back = inst.load_meta()
        hostname = identity.read_file(rootfs, "/etc/hostname").decode(errors="replace").strip()
        if back["net"]["ip"] != target["ip"] or hostname != ns.id:
            raise MvError("identity-failed", "identity read-back did not match",
                          detail={"meta_ip": back["net"]["ip"], "guest_hostname": hostname})
        return envelope(instance=inst.summary(back), verified=True, effect="confirmed",
                        rung=f"fs ({mode}) + debugfs", data={
                            "disks": meta["disks"],
                            "net": meta["net"],
                            "identity": {"hostname": ident["hostname"], "ip": ident["ip"],
                                         "gw": ident["gw"],
                                         "host_key_sha256": ident["host_key_sha256"]},
                        })
    except BaseException:
        if allocated:
            netpool.release(home, ns.id)
        _rmtree(inst.dir)
        raise


def start(ns, home: Path):
    inst = Instance(home, ns.id)
    meta = inst.reconcile()
    wait_ssh = not ns.no_wait_ssh
    timeout = ns.timeout_verb or ns.timeout
    if meta.get("state") == "running" and inst.pid_alive(meta):
        ssh_ready = sshx.probe(inst.key, inst.known_hosts, meta["net"]["ip"]) if wait_ssh else None
        return envelope(instance=inst.summary(meta), verified=ssh_ready, effect="unverifiable",
                        rung="api", data={"already_running": True, "ssh_ready": ssh_ready})
    base = load_base(home)
    tap = meta["net"]["tap"]
    tap_info = netpool.tap_state(tap)
    if not tap_info["exists"]:
        raise MvError("tap-missing", f"tap '{tap}' does not exist",
                      hint="sudo microvm-control net install (pool), or sudo ./net.sh up (shared tap)")
    if not tap_info["up"]:
        raise MvError("tap-down", f"tap '{tap}' is not up",
                      hint=f"sudo ip link set {tap} up")
    _net_conflict(home, meta["net"]["tap"], meta["net"]["ip"], exclude=ns.id)
    plan = [
        f"launch {base['dir']}/firecracker --api-sock {inst.sock} (detached)",
        f"configure vcpu={meta['config']['vcpu']} mem={meta['config']['mem_mib']}MiB "
        f"tap={meta['net']['tap']} mac={meta['net']['mac']}",
        "InstanceStart",
        "wait for ssh" if wait_ssh else "return without waiting for ssh",
    ]
    if ns.dry_run:
        raise DryRun(plan)
    with inst.lock():
        meta = inst.reconcile()
        try:
            inst.sock.unlink()
        except OSError:
            pass
        console = _open(inst.console_log, "ab")
        try:
            fc_log = _open(inst.fc_log, "ab")
        except MvError:
            console.close()
            raise
        try:
            proc = subprocess.Popen(
                [str(base["dir"] / "firecracker"), "--api-sock", str(inst.sock)],
                cwd=str(inst.dir), stdin=subprocess.DEVNULL,
                stdout=console, stderr=fc_log,
                start_new_session=True, close_fds=True,
            )
        finally:
            console.close()
            fc_log.close()
        meta["pid"] = proc.pid
        meta["state"] = "starting"
        meta["started_at"] = now_iso()
        inst.save_meta(meta)

        boot_start = time.monotonic()
        api = fcapi.FcApi(inst.sock, timeout=5)
        try:
            api.wait_socket(10)
            drives = [
                {"drive_id": "rootfs", "path_on_host": str(inst.dir / "disks" / "rootfs.ext4"),
                 "is_root_device": True, "is_read_only": False},
                {"drive_id": "data", "path_on_host": str(inst.dir / "disks" / "data.ext4"),
                 "is_root_device": False, "is_read_only": False},
            ]
            iface = {"iface_id": "eth0", "host_dev_name": meta["net"]["tap"],
                     "guest_mac": meta["net"]["mac"]}
            api.configure(kernel=base["dir"] / meta["config"]["kernel"],
                          boot_args=meta["config"]["boot_args"],
                          vcpu=meta["config"]["vcpu"], mem_mib=meta["config"]["mem_mib"],
                          track_dirty_pages=True, drives=drives, iface=iface,
                          log_path=inst.fc_log)
            api.start_instance()
            boot_ms = (time.monotonic() - boot_start) * 1000.0
            ssh_ms = None
            if wait_ssh:
                ssh_ms = sshx.wait_ready(inst.key, inst.known_hosts, meta["net"]["ip"],
                                         timeout=timeout,
                                         cancel=lambda: not inst.pid_alive(meta))
        except MvError as exc:
            _kill_process(meta["pid"], grace=3)
            meta["state"] = "dead"
            meta["pid"] = None
            inst.save_meta(meta)
            raise MvError(exc.code, exc.message, hint=exc.hint,
                          detail={"api": exc.detail,
                                  "console_tail": _tail_lines(inst.console_log, 15)}) from exc
        meta["state"] = "running"
        meta["pid"] = proc.pid
        inst.save_meta(meta)
        return envelope(instance=inst.summary(meta), verified=True, effect="confirmed",
                        rung="api+ssh" if wait_ssh else "api", data={
                            "pid": proc.pid,
                            "boot_ms": round(boot_ms, 1),
                            "ssh_ready_ms": round(ssh_ms, 1) if ssh_ms is not None else None,
                            "console_log": str(inst.console_log),
                        })


def stop(ns, home: Path):
    inst = Instance(home, ns.id)
    meta = inst.reconcile()
    timeout = ns.timeout_verb or ns.timeout
    if not inst.pid_alive(meta):
        if meta.get("state") != "stopped":
            meta["state"] = "stopped"
            meta["pid"] = None
            meta["stopped_at"] = now_iso()
            inst.save_meta(meta)
        return envelope(instance=inst.summary(meta), verified=False, effect="unverifiable",
                        rung="fs", data={"already_stopped": True})
    if ns.dry_run:
        raise DryRun([f"SendCtrlAltDel to {ns.id} (pid {meta['pid']})",
                      "wait for exit, then SIGTERM/SIGKILL if needed"])
    with inst.lock():
        meta = inst.reconcile()
        meta, graceful = _stop_instance(inst, meta, timeout)
    return envelope(instance=inst.summary(meta), verified=True,
                    effect="confirmed" if graceful else "unconfirmed",
                    rung="api" if graceful else "signal",
                    data={"graceful": bool(graceful)})


def restart(ns, home: Path):
    if ns.dry_run:
        raise DryRun([f"stop {ns.id} if running", f"start {ns.id}"])
    stop(ns, home)
    return start(ns, home)


def status(ns, home: Path):
    inst = Instance(home, ns.id)
    meta = inst.reconcile()
    running = inst.pid_alive(meta)
    data = {"running": running,
            "console_log": str(inst.console_log),
            "disks": meta.get("disks")}
    if ns.probe and running:
        data["ssh_reachable"] = sshx.probe(inst.key, inst.known_hosts, meta["net"]["ip"])
        data["ssh_probe"] = "publickey + pinned known_hosts"
    return envelope(instance=inst.summary(meta), data=data)


def list_instances(ns, home: Path):
    items = []
    for inst in Instance.all(home):
        try:
            items.append(inst.summary(inst.reconcile()))
        except MvError:
            continue
    return envelope(data={"instances": items, "count": len(items)})


def exec_cmd(ns, home: Path):
    inst = Instance(home, ns.id)
    meta = inst.reconcile()
    if not inst.pid_alive(meta):
        raise MvError("not-running", f"instance '{ns.id}' is not running",
                      hint=f"microvm-control start {ns.id}")
    cmd = list(ns.command or [])
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        raise MvError("bad-args", "no command given",
                      hint=f"microvm-control exec {ns.id} -- uname -a")
    timeout = ns.timeout_verb or ns.timeout
    res = sshx.run_cmd(inst.key, inst.known_hosts, meta["net"]["ip"], cmd, timeout=timeout)
    stdout = res["stdout"]
    stdout_bytes = len(stdout.encode())
    out_path = None
    stdout_inline = stdout
    truncated = False
    if stdout_bytes > INLINE_LIMIT:
        truncated = True
        target = Path(ns.out).expanduser() if ns.out else (
            inst.dir / "artifacts" / f"exec-{int(now_epoch())}.out")
        _mkdir(target.parent)
        _write_text(target, stdout)
        out_path = str(target)
        stdout_inline = None
    stderr = res["stderr"]
    stderr_truncated = len(stderr.encode()) > INLINE_LIMIT
    if stderr_truncated:
        stderr = stderr.encode()[:INLINE_LIMIT].decode(errors="replace")
    return envelope(instance=inst.summary(meta), verified=True, effect="confirmed", rung="ssh",
                    data={
                        "exit_code": res["exit_code"],
                        "stdout": stdout_inline,
                        "stdout_bytes": stdout_bytes,
                        "stdout_sha256": sha256_file(Path(out_path)) if out_path else None,
                        "stdout_truncated": truncated,
                        "out_path": out_path,
                        "stderr": stderr,
                        "stderr_truncated": stderr_truncated,
                        "duration_ms": round(res["duration_ms"], 1),
                    })


_REMOTE_RE = re.compile(r"^([a-z0-9][a-z0-9-]{0,30}):(.*)$")


def cp(ns, home: Path):
    src_match = _REMOTE_RE.match(ns.src)
    dst_match = _REMOTE_RE.match(ns.dst)
    if src_match and dst_match:
        raise MvError("bad-args", "both sides name a guest; one side must be local",
                      hint="microvm-control cp <id>:/path /local/path")
    match = src_match or dst_match
    if match is None:
        raise MvError("bad-args", "neither side names an instance",
                      hint="microvm-control cp <id>:/path /local/path")
    inst = Instance(home, match.group(1))
    meta = inst.reconcile()
    if not inst.pid_alive(meta):
        raise MvError("not-running", f"instance '{inst.id}' is not running",
                      hint=f"microvm-control start {inst.id}")
    ip = meta["net"]["ip"]

    def convert(value: str, remote) -> str:
        if remote:
            return f"root@{ip}:{remote.group(2)}"
        return str(Path(value).expanduser())

    src, dst = convert(ns.src, src_match), convert(ns.dst, dst_match)
    sshx.scp(inst.key, inst.known_hosts, src, dst)
    return envelope(instance=inst.summary(meta), verified=True, effect="confirmed", rung="scp",
                    data={"src": src, "dst": dst})


def ssh_shell(ns, home: Path):
    inst = Instance(home, ns.id)
    meta = inst.reconcile()
    if not inst.pid_alive(meta):
        raise MvError("not-running", f"instance '{ns.id}' is not running",
                      hint=f"microvm-control start {ns.id}")
    sshx.interactive(inst.key, inst.known_hosts, meta["net"]["ip"])
    return envelope(data={"interactive": True})  # unreachable; exec replaced the process


def logs(ns, home: Path):
    inst = Instance(home, ns.id)
    inst.reconcile()
    path = inst.fc_log if ns.firecracker else inst.console_log
    lines = _tail_lines(path, ns.tail) if _exists(path) else []
    return envelope(data={"path": str(path), "lines": lines, "count": len(lines)})


def delete(ns, home: Path):
    inst = Instance(home, ns.id)
    meta = inst.reconcile()
    if ns.dry_run:
        raise DryRun([f"stop {ns.id}" if inst.pid_alive(meta) else f"{ns.id} is already stopped",
                      f"purge {inst.dir}" if ns.purge else f"move {inst.dir} to trash"])
    if not ns.yes and not sys.stdin.isatty():
        raise MvError("destructive-needs-yes", "delete requires --yes when not interactive",
                      hint=f"microvm-control delete {ns.id} --yes")
    with inst.lock():
        meta = inst.reconcile()
        if inst.pid_alive(meta):
            if ns.force:
                _kill_process(meta["pid"], grace=5)
            else:
                _stop_instance(inst, meta, ns.timeout_verb or ns.timeout)
    freed = du_bytes(inst.dir)
    if ns.purge:
        _rmtree(inst.dir)
        trash_path = None
    else:
        trash = home / "trash" / f"{ns.id}.{int(now_epoch())}"
        _mkdir(trash.parent)
        try:
            os.rename(inst.dir, trash)
        except OSError as exc:
            raise MvError("delete-failed", f"could not move {inst.dir} to trash",
                          detail=str(exc)) from exc
        trash_path = str(trash)
    if _exists(inst.dir):
        raise MvError("delete-failed", f"{inst.dir} still exists after delete")
    if netpool.configured(home):
        netpool.release(home, ns.id)
    return envelope(verified=True, effect="confirmed", rung="fs",
                    data={"id": ns.id, "purged": bool(ns.purge), "trash_path": trash_path,
                          "freed_bytes": freed})


def net_info(home: Path) -> dict:
    try:
        pool = netpool.pool_summary(home)
    except MvError:
        pool = None
    if pool is not None:
        return {"mode": "pool", "pool": pool,
                "state_path": str(netpool.state_path(home))}
    return {"mode": "shared-tap", "tap": DEFAULT_TAP, "ip_default": DEFAULT_IP,
            "note": "run 'microvm-control net init --taps 8' (then 'sudo microvm-control net install') for a pool"}


def caps(ns, home: Path):
    ensure_home(home)
    try:
        base = load_base(home)
        base_info = {
            "dir": str(base["dir"]), "firecracker": base["firecracker"],
            "kernel": base["kernel"], "alpine": base.get("alpine"),
            "rootfs_bytes": _size(base["dir"] / base["rootfs"]),
        }
    except MvError as exc:
        base_info = {"error": exc.code, "message": exc.message}

    instances = Instance.all(home)
    running = 0
    for inst in instances:
        try:
            if inst.pid_alive(inst.reconcile()):
                running += 1
        except MvError:
            continue
    free = free_bytes(home)
    return envelope(data={
        "home": str(home),
        "base": base_info,
        "instances": {"total": len(instances), "running": running},
        "limits": {"max_instances": 8, "agent_default_ttl_s": AGENT_DEFAULT_TTL_S},
        "disk": {"free_bytes": free, "free_human": _human(free)},
        "host": {"kvm_rw": _kvm_ok(),
                 "debugfs": which("debugfs"), "unshare": which("unshare"),
                 "reflink": _reflink_ok(home)},
        "net": net_info(home),
    })


def doctor(ns, home: Path):
    ensure_home(home)
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str, required: bool = True) -> None:
        checks.append({"name": name, "ok": bool(ok), "required": required, "detail": detail})

    check("kvm", _kvm_ok(), "/dev/kvm readable+writable")
    for tool in ("debugfs", "ssh", "ssh-keygen", "scp", "mke2fs", "unshare"):
        found = which(tool)
        check(f"tool:{tool}", bool(found), found or "missing from PATH")
    try:
        base = load_base(home)
        check("base-image", True,
              f"{base['dir']} (fc {base['firecracker']}, kernel {base['kernel']})")
    except MvError as exc:
        check("base-image", False, exc.message, required=True)
    check("disk-space", free_bytes(home) > 2 * 1024**3, _human(free_bytes(home)) + " free")
    try:
        probe = home / ".doctor-probe"
        probe.write_text("x")
        probe.unlink()
        check("home-writable", True, str(home))
    except OSError as exc:
        check("home-writable", False, str(exc))
    tap = subprocess.run(["ip", "link", "show", DEFAULT_TAP], capture_output=True, text=True)
    tap_up = tap.returncode == 0 and "UP" in tap.stdout.split("\n")[0]
    check("tap", tap_up,
          f"{DEFAULT_TAP} is {'up' if tap_up else 'missing or down'} "
          f"(run 'sudo ./net.sh up' on the host)", required=False)

    ok = all(c["ok"] for c in checks if c["required"])
    return envelope(ok=ok, data={"checks": checks}), (0 if ok else 1)
