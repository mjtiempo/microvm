"""Guest identity injection into ext4 images without mounting (debugfs)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .errors import MvError
from .util import run, sha256_file

# sshd_config in the base image lists ed25519 and rsa host keys.
HOST_KEY_TYPES = ("ed25519", "rsa")
STALE_HOST_KEY_TYPES = ("ecdsa", "mldsa44-ed25519")


def _debugfs(image: Path, commands: list[str], *, write: bool = False, check: bool = True):
    argv = ["debugfs"] + (["-w"] if write else []) + ["-f", "-", str(image)]
    cp = run(argv, input="\n".join(commands) + "\n")
    if check and cp.returncode != 0:
        raise MvError(
            "image-error",
            f"debugfs failed on {image.name}",
            detail=(cp.stderr or cp.stdout).strip()[-500:],
        )
    return cp


def _stat(image: Path, path: str) -> str:
    cp = run(["debugfs", "-R", f"stat {path}", str(image)])
    if "Inode:" not in cp.stdout:
        return ""
    return cp.stdout


def file_exists(image: Path, path: str) -> bool:
    return bool(_stat(image, path))


def read_file(image: Path, path: str) -> bytes:
    cp = run(["debugfs", "-R", f"cat {path}", str(image)])
    if cp.returncode != 0 and not _stat(image, path):
        raise MvError("image-read-failed", f"cannot read {path} from {image.name}")
    lines = [ln for ln in cp.stdout.splitlines(keepends=True) if not ln.startswith("debugfs ")]
    return "".join(lines).encode()


def ensure_dir(image: Path, path: str) -> None:
    st = _stat(image, path)
    if st:
        if "Type: directory" not in st:
            raise MvError("image-error", f"{path} exists and is not a directory")
        return
    _debugfs(image, [f"mkdir {path}"], write=True)


def write_file(image: Path, path: str, data: bytes, mode: str = "0100644") -> None:
    fd, tmp_path = tempfile.mkstemp(prefix="mvi-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        # rm may fail when the file is new; that is fine (check=False)
        _debugfs(image, [f"rm {path}"], write=True, check=False)
        _debugfs(image, [f"write {tmp_path} {path}"], write=True)
        for field, value in (("mode", mode), ("uid", "0"), ("gid", "0")):
            _debugfs(image, [f"sif {path} {field} {value}"], write=True, check=False)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    if read_file(image, path) != data:
        raise MvError("image-write-failed", f"read-back of {path} does not match what was written")


def _generate_host_keys(workdir: Path, instance: str) -> Path:
    sshdir = workdir / "etc" / "ssh"
    sshdir.mkdir(parents=True, exist_ok=True)
    for kind in HOST_KEY_TYPES:
        key = sshdir / f"ssh_host_{kind}_key"
        argv = ["ssh-keygen", "-q", "-N", "", "-C", f"microvm-{instance}", "-t", kind]
        if kind == "rsa":
            argv += ["-b", "3072"]
        argv += ["-f", str(key)]
        cp = run(argv)
        if cp.returncode != 0:
            raise MvError("identity-failed", f"ssh-keygen {kind} failed",
                          detail=(cp.stderr or "").strip()[-400:])
    return sshdir


def inject_identity(rootfs: Path, *, instance: str, hostname: str, ip: str,
                    gw: str, prefix: int, workdir: Path) -> dict:
    """Rewrite identity files and host keys inside the copied rootfs image.

    Returns the injected values plus the known_hosts lines for pinning.
    """
    sshdir = _generate_host_keys(workdir, instance)

    interfaces = (
        "auto lo\n"
        "iface lo inet loopback\n"
        "\n"
        "auto eth0\n"
        "iface eth0 inet static\n"
        f"\taddress {ip}/{prefix}\n"
        f"\tgateway {gw}\n"
    )
    identity_env = (
        f"MICROVM_INSTANCE={instance}\n"
        f"MICROVM_HOSTNAME={hostname}\n"
        f"MICROVM_IP={ip}\n"
        f"MICROVM_GW={gw}\n"
        f"MICROVM_PREFIX={prefix}\n"
    )

    ensure_dir(rootfs, "/etc")
    ensure_dir(rootfs, "/etc/microvm")
    write_file(rootfs, "/etc/hostname", (hostname + "\n").encode())
    write_file(rootfs, "/etc/network/interfaces", interfaces.encode())
    write_file(rootfs, "/etc/microvm/identity.env", identity_env.encode())
    ensure_dir(rootfs, "/etc/ssh")

    for stale in STALE_HOST_KEY_TYPES:
        for suffix in ("", ".pub"):
            _debugfs(rootfs, [f"rm /etc/ssh/ssh_host_{stale}_key{suffix}"],
                     write=True, check=False)

    for kind in HOST_KEY_TYPES:
        for suffix, mode in (("", "0100600"), (".pub", "0100644")):
            src = sshdir / f"ssh_host_{kind}_key{suffix}"
            write_file(rootfs, f"/etc/ssh/{src.name}", src.read_bytes(), mode)

    known_lines = []
    for kind in HOST_KEY_TYPES:
        pub = sshdir / f"ssh_host_{kind}_key.pub"
        parts = pub.read_text().split()
        known_lines.append(f"{ip} {parts[0]} {parts[1]}")

    return {
        "hostname": hostname,
        "ip": ip,
        "gw": gw,
        "prefix": prefix,
        "known_hosts_lines": known_lines,
        "host_key_sha256": {
            kind: sha256_file(sshdir / f"ssh_host_{kind}_key") for kind in HOST_KEY_TYPES
        },
    }
