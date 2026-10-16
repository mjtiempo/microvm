"""Backup and restore: tar.zst / raw-dir / qcow2 archives with sha256 manifests.

A backup is a staging directory containing the disks, `meta.json` and a
`manifest.json` with size + sha256 for every member, archived with zstd.
A running instance is paused for the disk copy (reflink on btrfs) and resumed
immediately; a best-effort `sync` in the guest runs first. Restore verifies
every manifest hash before materialising a new instance with a fresh identity.
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import shutil
import tempfile
from pathlib import Path

from microvm_control.errors import DryRun, MvError
from microvm_control.lifecycle import (  # shared internals
    _materialize_instance,
    _source_paused,
    _validate_sizes,
)
from microvm_control.output import envelope
from microvm_control.store import (
    Instance,
    enforce_instance_quota,
    ensure_home,
    validate_id,
)
from microvm_control.util import (
    atomic_write_json,
    copy_file,
    du_bytes,
    ensure_space,
    now_epoch,
    now_iso,
    run,
    sha256_file,
    which,
)

MANIFEST = "manifest.json"
BACKUP_FORMATS = ("tar.zst", "raw-dir", "qcow2")
FREESPACE_HEADROOM = 256 * 1024**2


# --------------------------------------------------------------------------- #
# guarded helpers
# --------------------------------------------------------------------------- #

def _mkdir(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MvError("io-failed", f"cannot create {path}", detail=str(exc)) from exc


def _rmtree(path: Path) -> None:
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError as exc:
        raise MvError("io-failed", f"cannot stat {path}", detail=str(exc)) from exc


def _write_json(path: Path, obj: dict) -> None:
    try:
        atomic_write_json(path, obj)
    except OSError as exc:
        raise MvError("io-failed", f"cannot write {path}", detail=str(exc)) from exc


def _staging_dir(home: Path) -> Path:
    base = home / "backups"
    _mkdir(base)
    try:
        return Path(tempfile.mkdtemp(prefix=".staging-", dir=str(base)))
    except OSError as exc:
        raise MvError("io-failed", f"cannot create a staging dir under {base}",
                      detail=str(exc)) from exc


def _patch_meta(meta: dict) -> dict:
    out = dict(meta)
    out.pop("pid", None)
    out["state"] = "stopped"
    return out


# --------------------------------------------------------------------------- #
# archive plumbing
# --------------------------------------------------------------------------- #

def _tar_zstd(staging: Path, out: Path) -> None:
    if which("tar") is None or which("zstd") is None:
        raise MvError("missing-tool", "tar and zstd are required for tar.zst backups")
    cp = run(["tar", "--zstd", "-cf", str(out), "-C", str(staging), "."])
    if cp.returncode != 0:
        raise MvError("backup-failed", "tar --zstd failed",
                      detail=(cp.stderr or cp.stdout).strip()[-500:])


def _untar_zstd(archive: Path, staging: Path) -> None:
    if which("tar") is None or which("zstd") is None:
        raise MvError("missing-tool", "tar and zstd are required to read this backup")
    cp = run(["tar", "--zstd", "-xf", str(archive), "-C", str(staging)])
    if cp.returncode != 0:
        raise MvError("backup-corrupt", "the archive could not be read",
                      detail=(cp.stderr or cp.stdout).strip()[-500:])


def _assert_safe_members(archive: Path) -> None:
    """Refuse archives with absolute or parent-escaping member paths."""
    cp = run(["tar", "--zstd", "-tf", str(archive)])
    if cp.returncode != 0:
        raise MvError("backup-corrupt", "the archive could not be listed",
                      detail=(cp.stderr or cp.stdout).strip()[-300:])
    bad = []
    for line in (cp.stdout or "").splitlines():
        name = line.strip()
        if not name:
            continue
        normalized = posixpath.normpath(name)
        if normalized.startswith("/") or normalized == ".." or normalized.startswith("../"):
            bad.append(name)
    if bad:
        raise MvError("backup-corrupt", "archive contains unsafe member paths",
                      detail={"members": bad[:10]})


def _tar_read_member(archive: Path, name: str) -> bytes:
    cp = run(["tar", "--zstd", "-xOf", str(archive), "./" + name])
    if cp.returncode != 0:
        raise MvError("backup-corrupt", f"cannot read {name} from the archive",
                      detail=(cp.stderr or cp.stdout).strip()[-300:])
    return cp.stdout.encode()


def _convert_to_qcow(staging: Path, names: list[str]) -> list[str]:
    if which("qemu-img") is None:
        raise MvError("missing-tool", "qemu-img is required for --format qcow2")
    out_names = []
    for name in names:
        if not name.endswith(".ext4"):
            out_names.append(name)
            continue
        enc = name.replace(".ext4", ".qcow2")
        cp = run(["qemu-img", "convert", "-f", "raw", "-O", "qcow2", "-c",
                  str(staging / name), str(staging / enc)])
        if cp.returncode != 0:
            raise MvError("backup-failed", f"qemu-img failed for {name}",
                          detail=(cp.stderr or cp.stdout).strip()[-300:])
        try:
            (staging / name).unlink()
        except OSError as exc:
            raise MvError("io-failed", f"cannot remove {name} after conversion",
                          detail=str(exc)) from exc
        out_names.append(enc)
    return out_names


def _convert_from_qcow(staging: Path) -> None:
    if which("qemu-img") is None:
        raise MvError("missing-tool", "qemu-img is required to restore a qcow2 backup")
    for raw in ("rootfs.ext4", "data.ext4"):
        enc = raw.replace(".ext4", ".qcow2")
        if not (staging / enc).is_file():
            continue
        cp = run(["qemu-img", "convert", "-f", "qcow2", "-O", "raw",
                  str(staging / enc), str(staging / raw)])
        if cp.returncode != 0:
            raise MvError("restore-failed", f"qemu-img failed to decode {enc}",
                          detail=(cp.stderr or cp.stdout).strip()[-300:])


# --------------------------------------------------------------------------- #
# verbs
# --------------------------------------------------------------------------- #

def _copy_disks(inst: Instance, staging: Path, no_data: bool) -> list[str]:
    names = ["rootfs.ext4"]
    copy_file(inst.dir / "disks" / "rootfs.ext4", staging / "rootfs.ext4")
    if not no_data:
        copy_file(inst.dir / "disks" / "data.ext4", staging / "data.ext4")
        names.append("data.ext4")
    return names


def backup(ns, home: Path):
    inst = Instance(home, ns.id)
    meta = inst.reconcile()
    ensure_home(home)
    fmt = ns.format or "tar.zst"
    if fmt not in BACKUP_FORMATS:
        raise MvError("bad-args", f"unknown format {fmt!r}",
                      hint=f"one of: {', '.join(BACKUP_FORMATS)}")
    if ns.name:
        validate_id(ns.name)
    running = inst.pid_alive(meta)
    pause = running and not ns.no_pause
    out_dir = Path(ns.out).expanduser() if ns.out else home / "backups"
    label = ns.name or str(int(now_epoch()))
    target = out_dir / f"{inst.id}-{label}"
    plan = [
        f"pause {inst.id} (sync first)" if pause else
        (f"copy {inst.id} while running (no pause)" if running else f"{inst.id} is stopped"),
        f"copy disks to a staging dir (reflink when possible){' without /persist' if ns.no_data else ''}",
        "sha256 + manifest.json",
        f"write {'/'.join([fmt, str(target)])}",
    ]
    if ns.dry_run:
        raise DryRun(plan)
    needed = du_bytes(inst.dir) + FREESPACE_HEADROOM
    ensure_space(home, needed, label="the backup staging copy")
    ensure_space(out_dir, needed if fmt == "raw-dir" else needed // 2,
                 label=f"the {fmt} archive")

    staging = _staging_dir(home)
    synced = None
    disk_names = ["rootfs.ext4"]
    try:
        if pause:
            with _source_paused(inst, meta, sync=True) as state:
                synced = state["synced"]
                disk_names = _copy_disks(inst, staging, ns.no_data)
        else:
            disk_names = _copy_disks(inst, staging, ns.no_data)
        _write_json(staging / "meta.json", _patch_meta(meta))
    except BaseException:
        _rmtree(staging)
        raise

    disk_format = "raw"
    if fmt == "qcow2":
        try:
            disk_names = _convert_to_qcow(staging, disk_names)
            disk_format = "qcow2"
        except BaseException:
            _rmtree(staging)
            raise
    manifest = {
        "schema": 1,
        "instance": inst.id,
        "created_at": now_iso(),
        "firecracker": (meta.get("config") or {}).get("firecracker"),
        "kernel": (meta.get("config") or {}).get("kernel"),
        "config": meta.get("config"),
        "net": meta.get("net"),
        "disk_format": disk_format,
        "format": fmt,
        "files": [{"name": name, "size": _size(staging / name),
                   "sha256": sha256_file(staging / name)}
                  for name in [*disk_names, "meta.json"]],
    }
    _write_json(staging / MANIFEST, manifest)

    try:
        if fmt == "raw-dir":
            archive_path = out_dir / target.name
            _mkdir(out_dir)
            _rmtree(archive_path)
            try:
                os.rename(staging, archive_path)
            except OSError:
                try:
                    shutil.copytree(staging, archive_path)
                except OSError as exc:
                    raise MvError("backup-failed", f"cannot write {archive_path}",
                                  detail=str(exc)) from exc
                _rmtree(staging)
        else:
            _mkdir(out_dir)
            archive_path = target.with_suffix(".tar.zst")
            _tar_zstd(staging, archive_path)
    finally:
        _rmtree(staging)

    if not _exists(archive_path):
        raise MvError("backup-failed", f"{archive_path} was not created")
    if archive_path.is_dir():
        try:
            read_back = json.loads((archive_path / MANIFEST).read_text())
        except (OSError, ValueError) as exc:
            raise MvError("backup-failed", "the manifest could not be read back",
                          detail=str(exc)) from exc
    else:
        try:
            read_back = json.loads(_tar_read_member(archive_path, MANIFEST).decode())
        except (ValueError, UnicodeDecodeError) as exc:
            raise MvError("backup-failed", "the manifest could not be read back from the archive",
                          detail=str(exc)) from exc
    if read_back.get("files") != manifest["files"] or read_back.get("disk_format") != disk_format:
        raise MvError("backup-failed", "manifest read-back did not match")
    return envelope(instance=inst.summary(inst.reconcile()), verified=True, effect="confirmed",
                    rung="fs (pause/reflink/tar)" if pause else "fs (reflink/tar)",
                    data={"archive": str(archive_path), "format": fmt,
                          "disk_format": disk_format,
                          "bytes": du_bytes(archive_path),
                          "sha256": sha256_file(archive_path) if archive_path.is_file() else None,
                          "files": manifest["files"], "paused_source": pause,
                          "guest_synced": synced})


def _exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def restore(ns, home: Path):
    archive = Path(ns.archive).expanduser()
    if not _exists(archive):
        raise MvError("not-found", f"archive {archive} does not exist")
    ensure_home(home)
    enforce_instance_quota(home)
    staging = _staging_dir(home)
    try:
        source_bytes = du_bytes(archive)
        ensure_space(staging, source_bytes * 4 + FREESPACE_HEADROOM,
                     label="the restore workspace")
        if archive.is_dir():
            for child in archive.iterdir():
                dest = staging / child.name
                if child.is_dir():
                    shutil.copytree(child, dest)
                else:
                    copy_file(child, dest)
        else:
            _assert_safe_members(archive)
            _untar_zstd(archive, staging)
        manifest_path = staging / MANIFEST
        if not manifest_path.is_file():
            raise MvError("backup-corrupt", "the archive has no manifest.json")
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, ValueError) as exc:
            raise MvError("backup-corrupt", "manifest.json is unreadable", detail=str(exc)) from exc

        bad = []
        for entry in manifest.get("files") or []:
            path = staging / entry["name"]
            if not path.is_file():
                bad.append({"name": entry["name"], "reason": "missing"})
            elif path.stat().st_size != entry["size"]:
                bad.append({"name": entry["name"], "reason": "size"})
            elif sha256_file(path) != entry["sha256"]:
                bad.append({"name": entry["name"], "reason": "sha256"})
        if bad:
            raise MvError("backup-corrupt",
                          f"{len(bad)} file(s) failed verification",
                          detail={"files": bad})
        if manifest.get("disk_format") == "qcow2":
            _convert_from_qcow(staging)

        original = manifest.get("instance") or "restored"
        new_id = ns.id or f"{original}-r{int(now_epoch())}"
        validate_id(new_id)
        if Instance(home, new_id).exists():
            raise MvError("already-exists", f"instance '{new_id}' already exists",
                          hint="pass --id with a different name")

        explicit = any(value is not None for value in (ns.tap, ns.ip, ns.gw))
        if explicit and not (ns.tap and ns.ip):
            raise MvError("bad-args", "--tap and --ip must be given together")
        saved_net = manifest.get("net") or {}
        inherit = {"tap": saved_net.get("tap"), "ip": saved_net.get("ip"),
                   "gw": saved_net.get("gw"), "prefix": saved_net.get("prefix"), "index": None}
        target = None
        if explicit:
            target = {"tap": ns.tap, "ip": ns.ip, "gw": ns.gw or inherit["gw"],
                      "prefix": ns.prefix or inherit["prefix"], "index": None}
        plan = [
            f"extract {archive}",
            f"verify {len(manifest.get('files') or [])} file hashes",
            f"materialise instance '{new_id}' with a fresh identity",
            "boot + ssh probe" if ns.verify_boot else "leave stopped",
        ]
        if ns.dry_run:
            raise DryRun(plan)

        from microvm_control import lifecycle, netpool
        if target is None:
            target = netpool.allocate(home, new_id) if netpool.configured(home) else dict(inherit)
        if not target.get("tap") or not target.get("ip"):
            raise MvError("bad-args", "no usable network config (archive has none; pass --tap/--ip)")
        has_data = (staging / "data.ext4").is_file()
        config = manifest.get("config") or {}
        _validate_sizes(config.get("vcpu"), config.get("mem_mib"))
        allocated = target.get("index") is not None and not explicit
        try:
            inst, meta, ident, mode = _materialize_instance(
                home, new_id,
                rootfs_src=staging / "rootfs.ext4",
                data_src=staging / "data.ext4",
                fresh_data=ns.fresh_data or not has_data,
                target=target, config=config, ttl=ns.ttl,
                source={"restored_from": str(archive), "original": original,
                        "at": now_iso()})
        except BaseException:
            if allocated:
                netpool.release(home, new_id)
            raise

        boot_verified = None
        if ns.verify_boot:
            start_ns = argparse.Namespace(id=new_id, no_wait_ssh=False, dry_run=False,
                                          timeout=ns.timeout_verb or ns.timeout,
                                          timeout_verb=None)
            doc = lifecycle.start(start_ns, home)
            boot_verified = bool(doc.get("verified"))
            if not boot_verified:
                raise MvError("restore-verify-failed",
                              f"'{new_id}' was restored but did not become reachable",
                              hint=f"microvm-control logs {new_id}",
                              detail={"instance": new_id})
        return envelope(instance=inst.summary(inst.reconcile()), verified=True,
                        effect="confirmed", rung="tar+sha256+debugfs",
                        data={"restored_from": str(archive), "original": original,
                              "files_verified": len(manifest.get("files") or []),
                              "disk_format": manifest.get("disk_format", "raw"),
                              "boot_verified": boot_verified})
    finally:
        _rmtree(staging)
