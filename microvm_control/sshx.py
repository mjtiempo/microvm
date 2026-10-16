"""SSH/scp helpers with pinned host keys and deterministic options."""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path

from .errors import MvError

# -F /dev/null keeps this deterministic (also inside user namespaces, where the
# host's /etc/ssh/ssh_config.d may be owned by an unmapped uid).
_SSH_BASE = [
    "ssh", "-F", "/dev/null",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=3",
    "-o", "StrictHostKeyChecking=yes",
    "-o", "IdentitiesOnly=yes",
    "-o", "PreferredAuthentications=publickey",
    "-o", "LogLevel=ERROR",
]


def ssh_argv(key: Path, known_hosts: Path, ip: str) -> list[str]:
    return [
        *_SSH_BASE,
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-i", str(key),
        f"root@{ip}",
    ]


def probe(key: Path, known_hosts: Path, ip: str, timeout: float = 5.0) -> bool:
    try:
        cp = subprocess.run(
            ssh_argv(key, known_hosts, ip) + ["true"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return cp.returncode == 0


def wait_ready(key: Path, known_hosts: Path, ip: str, timeout: float,
               cancel=None) -> float:
    """Wait for SSH; returns ready time in ms. cancel() -> bool aborts."""
    start = time.monotonic()
    deadline = start + timeout
    last_error = "timed out"
    while time.monotonic() < deadline:
        if cancel is not None and cancel():
            raise MvError("boot-failed", "firecracker exited before SSH became ready")
        try:
            cp = subprocess.run(
                ssh_argv(key, known_hosts, ip) + ["true"],
                capture_output=True, text=True, timeout=max(1.0, min(3.0, deadline - time.monotonic())),
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            last_error = str(exc)
        else:
            if cp.returncode == 0:
                return (time.monotonic() - start) * 1000.0
            last_error = cp.stderr.strip()[-200:]
        time.sleep(0.25)
    raise MvError(
        "ssh-timeout",
        f"guest at {ip} did not accept SSH within {timeout:.0f}s",
        detail=last_error or None,
    )


def run_cmd(key: Path, known_hosts: Path, ip: str, cmd: list[str],
            timeout: float) -> dict:
    argv = ssh_argv(key, known_hosts, ip)
    if cmd:
        argv = argv + [shlex.join(cmd)]
    start = time.monotonic()
    try:
        cp = subprocess.run(argv, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise MvError(
            "exec-timeout",
            f"command did not finish within {timeout:.0f}s",
            hint="raise --timeout; the remote command may still be running in the guest",
            detail=_decode(exc.stdout)[-400:] or None,
        ) from exc
    except OSError as exc:
        raise MvError("ssh-failed", "could not run ssh", detail=str(exc)) from exc
    return {
        "exit_code": cp.returncode,
        "stdout": _decode(cp.stdout),
        "stderr": _decode(cp.stderr),
        "duration_ms": (time.monotonic() - start) * 1000.0,
    }


def _decode(data) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode(errors="replace")
    return str(data)


def interactive(key: Path, known_hosts: Path, ip: str):
    """Replace this process with an interactive ssh session."""
    argv = ssh_argv(key, known_hosts, ip) + ["-tt"]
    try:
        os.execvp(argv[0], argv)
    except OSError as exc:
        raise MvError("ssh-failed", "could not exec ssh", detail=str(exc)) from exc


def scp(key: Path, known_hosts: Path, src: str, dst: str) -> None:
    argv = [
        "scp", "-F", "/dev/null", "-q",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "LogLevel=ERROR",
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-i", str(key),
        "-r",
        src, dst,
    ]
    cp = subprocess.run(argv, capture_output=True, text=True)
    if cp.returncode != 0:
        raise MvError("scp-failed", f"scp {src} -> {dst} failed",
                      detail=(cp.stderr or cp.stdout).strip()[-400:])
