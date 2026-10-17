"""Verb implementations for microvm-control (M0 slice).

Covered: image build, create, start, stop, restart, status, list, exec, cp,
ssh, logs, delete, caps, doctor. Networking is the single shared tap mode
(`fc-tap`, 172.16.0.2/24); the tap pool arrives in M1.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import IO, Any

# The pyright language server in this session does not index modules added
# mid-session; CLI pyright and python3 resolve all of these cleanly.
from microvm_control import fcapi  # pyright: ignore[reportAttributeAccessIssue]
from microvm_control import identity  # pyright: ignore[reportAttributeAccessIssue]
from microvm_control import netpool  # pyright: ignore[reportAttributeAccessIssue]
from microvm_control import sshx  # pyright: ignore[reportAttributeAccessIssue]
from microvm_control.errors import DryRun, MvError
from microvm_control.browser import state as browser_state
from microvm_control.output import envelope
from microvm_control.store import (  # pyright: ignore[reportMissingImports]
    Instance,
    enforce_instance_quota,
    ensure_home,
    limits,
    pid_matches,
    validate_id,
)
from microvm_control.util import (  # pyright: ignore[reportMissingImports]
    atomic_write_json,
    copy_file,
    du_bytes,
    exists_quiet as _exists,
    free_bytes,
    mkdir_guarded as _mkdir,
    now_epoch,
    now_iso,
    rmtree_guarded as _rmtree,
    run,
    sha256_file,
    size_guarded as _size,
    unlink_guarded as _unlink_file,
    which,
    write_json_guarded as _write_json,
    chmod_guarded as _chmod,
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
# local filesystem helpers (the shared guarded set lives in util.py)
# --------------------------------------------------------------------------- #

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


def _resolve_runtime(home: Path, meta: dict) -> tuple[Path, Path]:
    """Firecracker + kernel paths recorded by the instance, or the current base.

    Only paths inside `<home>/images` are trusted: a tampered meta (or a
    hostile backup archive restored by an older version) must not turn
    `start` into executing a binary from an arbitrary directory.
    """
    config = meta.get("config") or {}
    images_root = Path(os.path.realpath(home / "images"))
    image_dir = Path(config.get("image_dir") or base_dir(home))
    kernel = image_dir / str(config.get("kernel") or "")
    for path in (image_dir, kernel):
        real = Path(os.path.realpath(path))
        if real != images_root and not real.is_relative_to(images_root):
            raise MvError("image-missing",
                          f"runtime for '{meta.get('id', '?')}' is outside {images_root}",
                          hint="the instance's recorded runtime is not from this home; "
                               "microvm-control image build, or re-create the instance")
    fc_binary = image_dir / "firecracker"
    if not (fc_binary.is_file() and kernel.is_file()):
        raise MvError("image-missing",
                      f"runtime for '{meta.get('id', '?')}' is incomplete under {image_dir}",
                      hint="microvm-control image build (or rebuild the instance image set)")
    return fc_binary, kernel


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


def _kill_process(pid, grace: float = 3.0, *, sock: Path | str | None = None) -> bool:
    """Signal a process only while it still matches the expected instance.

    With `sock` given, a pid recycled to *another* firecracker (which passes
    the comm check) is not signalled: its cmdline carries a different
    `--api-sock`. The check runs again before each signal and while waiting.
    """
    if not pid:
        return True
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 2.0)):
        if not pid_matches(pid, sock):
            return True
        try:
            os.kill(int(pid), sig)
        except (ProcessLookupError, PermissionError, ValueError):
            return True
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not pid_matches(pid, sock):
                return True
            time.sleep(0.05)
    return not pid_matches(pid, sock)


def _guest_sync(inst: Instance, meta: dict):
    """Best-effort guest sync before pausing for a consistent copy."""
    try:
        res = sshx.run_cmd(inst.key, inst.known_hosts, meta["net"]["ip"], ["sync"], timeout=10)
        return res["exit_code"] == 0
    except MvError:
        return False


def _resume_error(src: Instance) -> MvError | None:
    """Resume the source after a pause; the refusal, when there is one."""
    try:
        fcapi.FcApi(src.sock).resume()
        return None
    except MvError as exc:
        return exc


@contextmanager
def _source_paused(src: Instance, meta: dict, *, sync: bool = False):
    """Hold the source lock and pause it (when running) for a disk copy.

    On success the instance is always resumed; if that resume fails the caller
    gets `resume-failed` (the guest may be frozen: `restart <id>`). When the
    body itself failed, the resume is still attempted and a resume failure is
    attached to the propagating error (`left_paused` in `error.detail`) so the
    operator is not told the copy failed while the VM silently stays frozen.
    """
    with src.lock():
        running = src.pid_alive(meta)
        synced = _guest_sync(src, meta) if (running and sync) else None
        paused = False
        if running:
            fcapi.FcApi(src.sock).pause()
            paused = True
        try:
            yield {"running": running, "paused": paused, "synced": synced}
        except BaseException as exc:
            if paused:
                resume_exc = _resume_error(src)
                if resume_exc is not None and isinstance(exc, MvError):
                    note = {"resume_error": str(resume_exc), "left_paused": True}
                    exc.detail = ({"error": exc.detail, **note} if exc.detail is not None
                                  else note)
                    if exc.hint is None:
                        exc.hint = f"microvm-control restart {src.id}"
            raise
        else:
            if paused:
                resume_exc = _resume_error(src)
                if resume_exc is not None:
                    raise MvError(
                        "resume-failed",
                        f"could not resume '{src.id}' after the pause",
                        hint=f"microvm-control restart {src.id}",
                        detail=str(resume_exc),
                    ) from resume_exc


def _validate_sizes(vcpu, mem_mib) -> None:
    try:
        vcpu_int = int(vcpu)
        mem_int = int(mem_mib)
    except (TypeError, ValueError) as exc:
        raise MvError("bad-args", "vcpu and mem must be integers") from exc
    if not 1 <= vcpu_int <= 64:
        raise MvError("bad-args", "--vcpu must be between 1 and 64")
    if not 128 <= mem_int <= 262144:
        raise MvError("bad-args", "--mem must be between 128 and 262144 MiB")


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
        _kill_process(meta["pid"], grace=5, sock=inst.sock)
    meta["state"] = "stopped"
    meta["pid"] = None
    meta["stopped_at"] = now_iso()
    inst.save_meta(meta)
    return meta, graceful


SNAPSHOT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")


def validate_snapshot_name(name: str) -> None:
    if not SNAPSHOT_NAME_RE.match(name or ""):
        raise MvError("invalid-id", f"invalid snapshot name: {name!r}",
                      hint="names match [a-z0-9][a-z0-9-]{0,30}")


def firecracker_version(binary: Path) -> str:
    cp = run([str(binary), "--version"])
    if cp.returncode != 0:
        raise MvError("firecracker-error", "could not read the firecracker version",
                      detail=(cp.stderr or cp.stdout).strip()[-200:])
    fields = (cp.stdout or "").strip().split()
    return fields[-1] if fields else "unknown"


def _generate_instance_key(inst: Instance) -> Path:
    _mkdir(inst.key.parent)
    try:
        inst.key.unlink()
    except OSError:
        pass
    cp = run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"microvm-{inst.id}",
              "-f", str(inst.key)])
    if cp.returncode != 0:
        raise MvError("identity-failed", "ssh-keygen failed for the instance key",
                      detail=(cp.stderr or "").strip()[-300:])
    _chmod(inst.key, 0o600)
    return inst.key


def _pubkey_bytes(inst: Instance) -> bytes:
    try:
        return Path(str(inst.key) + ".pub").read_bytes()
    except OSError as exc:
        raise MvError("identity-failed", "the instance public key is unreadable",
                      detail=str(exc)) from exc


def _disk_signature(path: Path) -> dict:
    try:
        st = path.stat()
    except OSError as exc:
        raise MvError("io-failed", f"cannot stat {path}", detail=str(exc)) from exc
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _fsck(image: Path, *, journal_only: bool = False) -> None:
    """Replay/clear the ext4 journal so direct debugfs writes survive a mount.

    Images inherited from a previously booted template carry pending journal
    transactions; without this, the guest's first mount replays them and can
    revert identity files written with debugfs.
    """
    argv = ["e2fsck", "-fy"]
    if journal_only:
        argv += ["-E", "journal_only"]
    cp = run(argv + [str(image)])
    if cp.returncode >= 4:
        raise MvError("image-fsck-failed", f"e2fsck failed on {image.name}",
                      detail=(cp.stdout or cp.stderr).strip()[-500:])


def _materialize_instance(home: Path, instance_id: str, *, rootfs_src: Path, data_src: Path,
                          fresh_data: bool, target: dict, config: dict, ttl: int | None,
                          source: dict | None = None):
    """Create an instance dir from source images, with a fresh identity + key."""
    inst = Instance(home, instance_id)
    try:
        inst.dir.mkdir(parents=True)
    except FileExistsError as exc:
        raise MvError("already-exists", f"instance '{instance_id}' already exists",
                      hint=f"microvm-control delete {instance_id} --yes, then retry") from exc
    except OSError as exc:
        raise MvError("io-failed", f"cannot create {inst.dir}", detail=str(exc)) from exc
    try:
        for sub in ("disks", "keys", "ssh", "run", "artifacts"):
            _mkdir(inst.dir / sub)
        rootfs = inst.dir / "disks" / "rootfs.ext4"
        data = inst.dir / "disks" / "data.ext4"
        mode = copy_file(rootfs_src, rootfs)
        if fresh_data:
            make_data_image(data, os.environ.get("MICROVM_DATA_SIZE", DEFAULT_DATA_SIZE))
        else:
            copy_file(data_src, data)
        _fsck(rootfs, journal_only=True)
        _fsck(data, journal_only=True)
        _generate_instance_key(inst)
        ident = identity.inject_identity(
            rootfs, instance=instance_id, hostname=instance_id, ip=target["ip"],
            gw=target["gw"], prefix=target["prefix"], authorized_keys=_pubkey_bytes(inst),
            workdir=inst.dir / "tmp")
        _rmtree(inst.dir / "tmp")
        _write_text(inst.known_hosts, "\n".join(ident["known_hosts_lines"]) + "\n")
        meta = {
            "schema": 1,
            "id": instance_id,
            "state": "created",
            "created_at": now_iso(),
            "started_at": None,
            "stopped_at": None,
            "expires_at": (now_epoch() + ttl) if ttl else None,
            "config": config,
            "net": {"tap": target["tap"], "ip": target["ip"], "gw": target["gw"],
                    "prefix": target["prefix"], "pool_index": target.get("index"),
                    "mac": mac_for(target["ip"])},
            "disks": {
                "rootfs": {"path": "disks/rootfs.ext4", "size_bytes": _size(rootfs)},
                "data": {"path": "disks/data.ext4", "size_bytes": _size(data)},
            },
            "ssh": {"key": "keys/id_ed25519", "known_hosts": "ssh/known_hosts"},
            "source": source,
            "pid": None,
        }
        inst.save_meta(meta)
        back = inst.load_meta()
        hostname = identity.read_file(rootfs, "/etc/hostname").decode(errors="replace").strip()
        if back["net"]["ip"] != target["ip"] or hostname != instance_id:
            raise MvError("identity-failed", "identity read-back did not match",
                          detail={"meta_ip": back["net"]["ip"], "guest_hostname": hostname})
        return inst, back, ident, mode
    except BaseException:
        _rmtree(inst.dir)
        raise


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
            version = {}
        try:
            load_base(home)
            return envelope(data={"base_dir": str(base), "up_to_date": True, **version})
        except MvError:
            pass  # the image set is incomplete: fall through and rebuild it
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
    _fsck(base / "rootfs.ext4")
    make_data_image(base / "data.ext4", os.environ.get("MICROVM_DATA_SIZE", DEFAULT_DATA_SIZE))
    _fsck(base / "data.ext4", journal_only=True)
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
        target = None  # allocated after the dry-run/quota checks
    allocated = False
    planned = target if target is not None else legacy

    plan = [
        f"reserve {inst.dir}",
        f"copy {base['dir']}/{base['rootfs']} -> {inst.dir}/disks/rootfs.ext4 (reflink)",
        f"{'fresh' if ns.fresh_data else 'copy'} data -> {inst.dir}/disks/data.ext4",
        f"network {planned['tap']} guest {planned['ip']}/{planned['prefix']} gw {planned['gw']}"
        + (f" (pool index {planned['index']})" if planned.get("index") else ""),
        f"fresh instance ssh key + host keys, hostname={ns.id}",
        f"write {inst.meta_path}",
    ]
    if ns.dry_run:
        raise DryRun(plan)
    _validate_sizes(ns.vcpu, ns.mem_mib)
    enforce_instance_quota(home)
    if target is None:
        target = netpool.allocate(home, ns.id) if netpool.configured(home) else legacy
    allocated = target.get("index") is not None and not explicit
    config = {
        "vcpu": ns.vcpu,
        "mem_mib": ns.mem_mib,
        "kernel": base["kernel"],
        "firecracker": base["firecracker"],
        "image_dir": str(base["dir"]),
        "boot_args": DEFAULT_BOOT_ARGS,
        "track_dirty_pages": True,
    }
    try:
        inst, meta, ident, mode = _materialize_instance(
            home, ns.id, rootfs_src=base["dir"] / base["rootfs"],
            data_src=base["dir"] / base["data"], fresh_data=ns.fresh_data,
            target=target, config=config, ttl=ttl)
    except BaseException:
        if allocated:
            netpool.release(home, ns.id)
        raise
    return envelope(instance=inst.summary(meta), verified=True, effect="confirmed",
                    rung=f"fs ({mode}) + debugfs", data={
                        "disks": meta["disks"],
                        "net": meta["net"],
                        "identity": {"hostname": ident["hostname"], "ip": ident["ip"],
                                     "gw": ident["gw"],
                                     "host_key_sha256": ident["host_key_sha256"]},
                    })


def start(ns, home: Path):
    inst = Instance(home, ns.id)
    meta = inst.reconcile()
    wait_ssh = not ns.no_wait_ssh
    timeout = ns.timeout_verb or ns.timeout
    if meta.get("state") == "running" and inst.pid_alive(meta):
        # with --no-wait-ssh the reply must still say verified: false, not
        # omit the key: an agent branching on `verified === false` must never
        # see a missing field on a success reply
        ssh_ready = sshx.probe(inst.key, inst.known_hosts, meta["net"]["ip"]) if wait_ssh else False
        return envelope(instance=inst.summary(meta), verified=ssh_ready, effect="unverifiable",
                        rung="api", data={"already_running": True, "ssh_ready": ssh_ready})
    fc_binary, kernel = _resolve_runtime(home, meta)
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
        f"launch {fc_binary} --api-sock {inst.sock} (detached)",
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
                [str(fc_binary), "--api-sock", str(inst.sock)],
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
            api.configure(kernel=kernel,
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
            _kill_process(meta["pid"], grace=3, sock=inst.sock)
            meta["state"] = "dead"
            meta["pid"] = None
            inst.save_meta(meta)
            raise MvError(exc.code, exc.message, hint=exc.hint,
                          detail={"api": exc.detail,
                                  "console_tail": _tail_lines(inst.console_log, 15)}) from exc
        meta["state"] = "running"
        meta["pid"] = proc.pid
        inst.save_meta(meta)
        return envelope(instance=inst.summary(meta),
                        verified=True if wait_ssh else False,
                        effect="confirmed" if wait_ssh else "unverifiable",
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


def _delete_instance(inst: Instance, home: Path, *, purge: bool, force: bool,
                     timeout: float) -> dict:
    with inst.lock():
        meta = inst.reconcile()
        if inst.pid_alive(meta):
            if force:
                _kill_process(meta["pid"], grace=5, sock=inst.sock)
            else:
                _stop_instance(inst, meta, timeout)
    freed = du_bytes(inst.dir)
    # `delete` clears the browser state with the instance: the record is a
    # fact about THIS VM, and a trashed instance must never claim a live
    # browser. (A VM `stop` deliberately leaves it — the guest pid dies with
    # the VM and the next browser verb reconciles it.)
    browser_state.clear(inst)
    if purge:
        _rmtree(inst.dir)
        trash_path = None
    else:
        trash = home / "trash" / f"{inst.id}.{int(now_epoch())}"
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
        netpool.release(home, inst.id)
    return {"id": inst.id, "purged": bool(purge), "trash_path": trash_path,
            "freed_bytes": freed}


def delete(ns, home: Path):
    inst = Instance(home, ns.id)
    meta = inst.reconcile()
    if ns.dry_run:
        raise DryRun([f"stop {ns.id}" if inst.pid_alive(meta) else f"{ns.id} is already stopped",
                      f"purge {inst.dir}" if ns.purge else f"move {inst.dir} to trash"])
    if not ns.yes and not sys.stdin.isatty():
        raise MvError("destructive-needs-yes", "delete requires --yes when not interactive",
                      hint=f"microvm-control delete {ns.id} --yes")
    data = _delete_instance(inst, home, purge=ns.purge, force=ns.force,
                            timeout=ns.timeout_verb or ns.timeout)
    return envelope(verified=True, effect="confirmed", rung="fs", data=data)


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
        "limits": {"max_instances": limits(home)["max_instances"],
                   "agent_default_ttl_s": AGENT_DEFAULT_TTL_S},
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
    for tool in ("debugfs", "e2fsck", "ssh", "ssh-keygen", "scp", "mke2fs", "unshare"):
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
    net_state = netpool.state_path(home)
    if _exists(net_state):
        try:
            readable = os.access(net_state, os.R_OK)
        except OSError:
            readable = False
        check("net-state", readable,
              f"{net_state} is {'readable' if readable else 'NOT readable'} "
              f"(fix: sudo chown \"$(id -u):$(id -g)\" {net_state})")
    tap = subprocess.run(["ip", "link", "show", DEFAULT_TAP], capture_output=True, text=True)
    tap_up = tap.returncode == 0 and "UP" in tap.stdout.split("\n")[0]
    check("tap", tap_up,
          f"{DEFAULT_TAP} is {'up' if tap_up else 'missing or down'} "
          f"(run 'sudo ./net.sh up' on the host)", required=False)

    ok = all(c["ok"] for c in checks if c["required"])
    return envelope(ok=ok, data={"checks": checks}), (0 if ok else 1)


# --------------------------------------------------------------------------- #
# M2: clone + snapshots
# --------------------------------------------------------------------------- #

def clone(ns, home: Path):
    validate_id(ns.id)
    ensure_home(home)
    src = Instance(home, ns.source)
    smeta = src.reconcile()
    if Instance(home, ns.id).exists():
        raise MvError("already-exists", f"instance '{ns.id}' already exists",
                      hint=f"microvm-control delete {ns.id} --yes, then retry")
    explicit = any(value is not None for value in (ns.tap, ns.ip, ns.gw))
    if explicit and not (ns.tap and ns.ip):
        raise MvError("bad-args", "--tap and --ip must be given together")
    inherit = {"tap": smeta["net"]["tap"], "ip": smeta["net"]["ip"],
               "gw": smeta["net"]["gw"], "prefix": smeta["net"]["prefix"], "index": None}
    if explicit:
        target = {"tap": ns.tap, "ip": ns.ip, "gw": ns.gw or smeta["net"]["gw"],
                  "prefix": ns.prefix or smeta["net"]["prefix"], "index": None}
    elif ns.dry_run:
        target = netpool.plan(home, ns.id) or inherit
    else:
        target = None  # allocated only after the dry-run/quota checks
    allocated = False
    planned = target if target is not None else inherit
    # the clone boots on this home's runtime, never on a path inherited from
    # the source meta (which a tampered meta could point anywhere)
    base = load_base(home)
    config = dict(smeta.get("config") or {}) if isinstance(smeta.get("config"), dict) else {}
    config["image_dir"] = str(base["dir"])
    config["kernel"] = base["kernel"]
    if base.get("firecracker"):
        config["firecracker"] = base["firecracker"]
    if ns.vcpu:
        config["vcpu"] = ns.vcpu
    if ns.mem_mib:
        config["mem_mib"] = ns.mem_mib
    running = src.pid_alive(smeta)
    dst = Instance(home, ns.id)
    plan = [
        f"source {src.id} ({'running: pause, copy, resume' if running else 'stopped'})",
        f"copy disks -> {dst.dir}/disks (reflink when possible)",
        f"network {planned['tap']} guest {planned['ip']}/{planned['prefix']} gw {planned['gw']}"
        + (f" (pool index {planned['index']})" if planned.get("index") else ""),
        f"fresh identity for {ns.id} (hostname, ssh key, host keys)",
    ]
    if ns.dry_run:
        raise DryRun(plan)
    _validate_sizes(config.get("vcpu"), config.get("mem_mib"))
    enforce_instance_quota(home)
    if target is None:
        target = netpool.allocate(home, ns.id) if netpool.configured(home) else inherit
    allocated = target.get("index") is not None and not explicit
    result = None
    try:
        with _source_paused(src, smeta):
            result = _materialize_instance(
                home, ns.id,
                rootfs_src=src.dir / "disks" / "rootfs.ext4",
                data_src=src.dir / "disks" / "data.ext4",
                fresh_data=ns.fresh_data, target=target, config=config, ttl=ns.ttl,
                source={"id": src.id, "from": "running" if running else "stopped",
                        "at": now_iso()})
    except BaseException:
        if allocated:
            netpool.release(home, ns.id)
        raise
    if result is None:
        raise MvError("internal", "clone produced no result")
    inst, meta, ident, mode = result
    return envelope(instance=inst.summary(meta), verified=True, effect="confirmed",
                    rung="pause+reflink+debugfs" if running else f"fs ({mode}) + debugfs",
                    data={"source": meta["source"], "copy": mode, "net": meta["net"],
                          "disks": meta["disks"],
                          "identity": {"hostname": ident["hostname"], "ip": ident["ip"],
                                       "host_key_sha256": ident["host_key_sha256"]}})


def _load_snapshot_meta(inst: Instance, name: str) -> tuple[Path, dict]:
    validate_snapshot_name(name)
    snap_dir = inst.snapshots_dir / name
    meta_path = snap_dir / "meta.json"
    if not _exists(meta_path):
        raise MvError("not-found", f"snapshot '{name}' does not exist for '{inst.id}'",
                      hint=f"microvm-control snapshot list {inst.id}")
    try:
        return snap_dir, json.loads(meta_path.read_text())
    except (OSError, ValueError) as exc:
        raise MvError("bad-state", "snapshot meta.json is unreadable", detail=str(exc)) from exc


def snapshot_create(ns, home: Path):
    inst = Instance(home, ns.id)
    meta = inst.reconcile()
    validate_snapshot_name(ns.name)
    if not inst.pid_alive(meta):
        raise MvError("not-running", f"instance '{ns.id}' is not running",
                      hint=f"microvm-control start {ns.id}")
    snap_dir = inst.snapshots_dir / ns.name
    if _exists(snap_dir):
        raise MvError("already-exists", f"snapshot '{ns.name}' already exists for '{ns.id}'",
                      hint=f"microvm-control snapshot delete {ns.id} --name {ns.name}")
    binary, _kernel = _resolve_runtime(home, meta)
    version = firecracker_version(binary)
    vmstate = snap_dir / "vmstate.bin"
    mem = snap_dir / "mem.bin"
    plan = [f"pause {ns.id}", f"snapshot (full) -> {snap_dir}", f"resume {ns.id}"]
    if ns.dry_run:
        raise DryRun(plan)
    _mkdir(snap_dir)
    try:
        with _source_paused(inst, meta):
            fcapi.FcApi(inst.sock).snapshot_create(snapshot_path=vmstate, mem_path=mem)
    except BaseException:
        _rmtree(snap_dir)
        raise
    snap_meta = {
        "schema": 1, "name": ns.name, "created_at": now_iso(), "type": "Full",
        "firecracker": version, "kernel": meta["config"]["kernel"],
        "vcpu": meta["config"]["vcpu"], "mem_mib": meta["config"]["mem_mib"],
        "disks": {key: {"path": value.get("path"), **_disk_signature(inst.dir / value["path"])}
                  for key, value in (meta.get("disks") or {}).items()},
        "vmstate_bytes": _size(vmstate), "mem_bytes": _size(mem),
    }
    if snap_meta["vmstate_bytes"] == 0 or snap_meta["mem_bytes"] == 0:
        _rmtree(snap_dir)
        raise MvError("snapshot-failed", "snapshot files are empty")
    _write_json(snap_dir / "meta.json", snap_meta)
    return envelope(instance=inst.summary(inst.reconcile()), verified=True, effect="confirmed",
                    rung="api (pause/snapshot/resume)", data={"snapshot": snap_meta})


def snapshot_list(ns, home: Path):
    inst = Instance(home, ns.id)
    inst.reconcile()
    entries = []
    if _exists(inst.snapshots_dir):
        for child in sorted(inst.snapshots_dir.iterdir()):
            meta_path = child / "meta.json"
            if child.is_dir() and _exists(meta_path):
                try:
                    entries.append(json.loads(meta_path.read_text()))
                except (OSError, ValueError):
                    continue
    return envelope(data={"snapshots": entries, "count": len(entries)})


def snapshot_delete(ns, home: Path):
    inst = Instance(home, ns.id)
    inst.reconcile()
    validate_snapshot_name(ns.name)
    snap_dir = inst.snapshots_dir / ns.name
    if not _exists(snap_dir):
        raise MvError("not-found", f"snapshot '{ns.name}' does not exist for '{ns.id}'")
    if ns.dry_run:
        raise DryRun([f"remove {snap_dir}"])
    freed = du_bytes(snap_dir)
    _rmtree(snap_dir)
    if _exists(snap_dir):
        raise MvError("delete-failed", f"{snap_dir} still exists after delete")
    return envelope(verified=True, effect="confirmed", rung="fs",
                    data={"snapshot": ns.name, "freed_bytes": freed})


def snapshot_load(ns, home: Path):
    inst = Instance(home, ns.id)
    meta = inst.reconcile()
    snap_dir, snap = _load_snapshot_meta(inst, ns.name)
    binary, _kernel = _resolve_runtime(home, meta)
    live_version = firecracker_version(binary)
    if snap.get("firecracker") not in (None, "unknown", live_version):
        raise MvError("snapshot-version-mismatch",
                      f"snapshot was taken with firecracker {snap.get('firecracker')}, "
                      f"this host runs {live_version}",
                      hint="rebuild the base image or clone from the instance disk instead")
    mismatched = []
    mtime_changed = []
    for name, sig in (snap.get("disks") or {}).items():
        current = _disk_signature(inst.dir / sig["path"])
        if current["size"] != sig["size"]:
            mismatched.append(name)
        elif current["mtime_ns"] != sig["mtime_ns"]:
            mtime_changed.append(name)
    if mismatched and not ns.force:
        raise MvError("disk-modified", "a disk image changed size since this snapshot",
                      hint="use --force to load anyway (the disks will not be reverted)",
                      detail={"disks": mismatched})
    _net_conflict(home, meta["net"]["tap"], meta["net"]["ip"], exclude=ns.id)
    plan = [f"stop {ns.id}" if inst.pid_alive(meta) else f"{ns.id} already stopped",
            f"load snapshot '{ns.name}' ({snap.get('firecracker')})", "wait for ssh"]
    if ns.dry_run:
        raise DryRun(plan)
    timeout = ns.timeout_verb or ns.timeout
    with inst.lock():
        meta = inst.reconcile()
        if inst.pid_alive(meta):
            meta, _ = _stop_instance(inst, meta, timeout)
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
                [str(binary), "--api-sock", str(inst.sock)],
                cwd=str(inst.dir), stdin=subprocess.DEVNULL,
                stdout=console, stderr=fc_log,
                start_new_session=True, close_fds=True,
            )
        finally:
            console.close()
            fc_log.close()
        api = fcapi.FcApi(inst.sock, timeout=5)
        try:
            api.wait_socket(10)
            api.snapshot_load(snapshot_path=snap_dir / "vmstate.bin",
                              mem_path=snap_dir / "mem.bin")
            sshx.wait_ready(inst.key, inst.known_hosts, meta["net"]["ip"],
                            timeout=timeout,
                            cancel=lambda: not pid_matches(proc.pid, inst.sock))
        except MvError as exc:
            _kill_process(proc.pid, grace=3, sock=inst.sock)
            meta["state"] = "dead"
            meta["pid"] = None
            inst.save_meta(meta)
            raise MvError(exc.code, exc.message, hint=exc.hint,
                          detail={"api": exc.detail,
                                  "console_tail": _tail_lines(inst.console_log, 15)}) from exc
        meta["state"] = "running"
        meta["pid"] = proc.pid
        meta["started_at"] = now_iso()
        inst.save_meta(meta)
    return envelope(instance=inst.summary(meta), verified=True, effect="confirmed",
                    rung="api (snapshot/load)",
                    data={"snapshot": ns.name, "pid": proc.pid, "forced": bool(ns.force),
                          "disk_mtime_changed": mtime_changed})


# --------------------------------------------------------------------------- #
# TTL reaping and trash GC
# --------------------------------------------------------------------------- #

def _expired(meta: dict) -> bool:
    expires = meta.get("expires_at")
    return bool(expires) and expires <= now_epoch()


def _stopped_older_than(meta: dict, seconds: float) -> bool:
    if meta.get("state") not in ("stopped", "dead"):
        return False
    stopped_at = meta.get("stopped_at")
    if not stopped_at:
        return False
    try:
        when = datetime.fromisoformat(stopped_at).timestamp()
    except ValueError:
        return False
    return (now_epoch() - when) >= seconds


def reap(ns, home: Path):
    candidates = []
    for inst in Instance.all(home):
        try:
            meta = inst.reconcile()
        except MvError:
            continue
        if _expired(meta) or (ns.older_than and _stopped_older_than(meta, ns.older_than)):
            candidates.append(inst)
    if ns.dry_run:
        raise DryRun([f"reap {i.id}" for i in candidates] or ["nothing expired"])
    reaped = []
    for inst in candidates:
        try:
            _delete_instance(inst, home, purge=False, force=False,
                             timeout=ns.timeout_verb or ns.timeout)
            reaped.append(inst.id)
        except MvError as exc:
            reaped.append({"id": inst.id, "error": exc.code})
    return envelope(verified=True, effect="confirmed", rung="fs",
                    data={"reaped": reaped, "count": len(reaped)})


def gc(ns, home: Path):
    trash_days = ns.trash_days if ns.trash_days is not None else 7
    artifact_days = ns.artifact_days if ns.artifact_days is not None else 7
    now = now_epoch()
    targets: list[dict] = []

    def add(kind: str, path: Path) -> None:
        targets.append({"kind": kind, "path": str(path)})

    trash_root = home / "trash"
    if _exists(trash_root):
        for child in sorted(trash_root.iterdir()):
            try:
                age = now - child.stat().st_mtime
            except OSError:
                continue
            if age >= trash_days * 86400:
                add("trash", child)
    for inst in Instance.all(home):
        artifacts = inst.dir / "artifacts"
        if not _exists(artifacts):
            continue
        for entry in sorted(artifacts.iterdir()):
            try:
                age = now - entry.stat().st_mtime
            except OSError:
                continue
            if entry.is_file() and age >= artifact_days * 86400:
                add("artifact", entry)
    if ns.snapshot_days is not None:
        for inst in Instance.all(home):
            if not _exists(inst.snapshots_dir):
                continue
            for snap in sorted(inst.snapshots_dir.iterdir()):
                try:
                    age = now - snap.stat().st_mtime
                except OSError:
                    continue
                if snap.is_dir() and age >= ns.snapshot_days * 86400:
                    add("snapshot", snap)
    if ns.dry_run:
        raise DryRun([f"remove {t['path']}" for t in targets] or ["nothing to collect"])
    freed = 0
    removed = 0
    for target in targets:
        path = Path(target["path"])
        freed += du_bytes(path)
        if path.is_dir():
            _rmtree(path)
        else:
            _unlink_file(path)
        removed += 1
    return envelope(verified=True, effect="confirmed", rung="fs",
                    data={"removed": removed, "freed_bytes": freed,
                          "freed_bytes_note": "apparent size; reflinked extents may free less",
                          "items": targets})


# --------------------------------------------------------------------------- #
# browser helper (headless chromium inside the guest)
# --------------------------------------------------------------------------- #

BROWSER_TIMEOUT = 120.0


def browser(ns, home: Path):
    inst = Instance(home, ns.id)
    meta = inst.reconcile()
    if not inst.pid_alive(meta):
        raise MvError("not-running", f"instance '{ns.id}' is not running",
                      hint=f"microvm-control start {ns.id}")
    chosen = [(name, value) for name, value in
              (("dom", ns.dom), ("screenshot", ns.screenshot), ("pdf", ns.pdf)) if value]
    if len(chosen) != 1:
        raise MvError("bad-args", "give exactly one of --dom, --screenshot or --pdf")
    action, url = chosen[0]
    timeout = ns.timeout_verb or BROWSER_TIMEOUT
    ip = meta["net"]["ip"]
    base_cmd = ["chromium", "--headless", "--no-sandbox"]
    if action == "dom":
        res = sshx.run_cmd(inst.key, inst.known_hosts, ip,
                           [*base_cmd, "--dump-dom", url], timeout=timeout)
        if res["exit_code"] != 0:
            raise MvError("browser-failed", f"chromium --dump-dom exited {res['exit_code']}",
                          detail=res["stderr"][-400:])
        text = res["stdout"]
        if not text.strip():
            raise MvError("browser-failed", "chromium produced no DOM",
                          detail=res["stderr"][-400:])
        encoded = text.encode()
        truncated = len(encoded) > INLINE_LIMIT
        out_path = None
        if truncated:
            out = Path(ns.out).expanduser() if ns.out else (
                Path.cwd() / f"{inst.id}-browser-{int(now_epoch())}.html")
            _write_text(out, text)
            out_path = str(out)
        return envelope(instance=inst.summary(meta), verified=True, effect="confirmed",
                        rung="guest-chromium",
                        data={"action": "dom", "url": url,
                              "dom": None if truncated else text,
                              "dom_bytes": len(encoded),
                              "dom_truncated": truncated,
                              "out_path": out_path,
                              "out_sha256": sha256_file(Path(out_path)) if out_path else None})

    ext = "png" if action == "screenshot" else "pdf"
    magic = b"\x89PNG\r\n\x1a\n" if ext == "png" else b"%PDF"
    ts = int(now_epoch())
    guest_path = f"/persist/work/browser-{inst.id}-{ts}.{ext}"
    out = Path(ns.out).expanduser() if ns.out else Path.cwd() / f"{inst.id}-browser-{ts}.{ext}"
    if action == "screenshot":
        width, _, height = str(ns.window_size).partition(",")
        extra = [f"--screenshot={guest_path}", f"--window-size={width},{height or '800'}"]
    else:
        extra = [f"--print-to-pdf={guest_path}"]
    res = sshx.run_cmd(inst.key, inst.known_hosts, ip,
                       [*base_cmd, *extra, url], timeout=timeout)
    try:
        sshx.scp(inst.key, inst.known_hosts, f"root@{ip}:{guest_path}", str(out))
    except MvError as exc:
        raise MvError("browser-failed", f"chromium did not produce {guest_path}",
                      detail={"chromium": res["stderr"][-300:], "scp": str(exc)}) from exc
    size = _size(out)
    try:
        head = out.read_bytes()[:8]
    except OSError as exc:
        raise MvError("browser-verify-failed", f"cannot read {out}", detail=str(exc)) from exc
    if size == 0 or not head.startswith(magic):
        raise MvError("browser-verify-failed", f"{out} is not a valid {ext.upper()}",
                      detail={"bytes": size, "magic": head[:8].hex()})
    sshx.run_cmd(inst.key, inst.known_hosts, ip, ["rm", "-f", guest_path], timeout=10)
    return envelope(instance=inst.summary(meta), verified=True, effect="confirmed",
                    rung="guest-chromium",
                    data={"action": action, "url": url, "out_path": str(out),
                          "bytes": size, "sha256": sha256_file(out),
                          "magic_ok": True})
