"""SSH/scp helpers with pinned host keys and deterministic options."""

from __future__ import annotations

import contextlib
import os
import shlex
import socket
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


def free_port() -> int:
    """A loopback port the kernel just handed us; bound then released."""
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def forward_argv(key: Path, known_hosts: Path, ip: str, local_port: int,
                 guest_port: int) -> list[str]:
    """`ssh -N -L 127.0.0.1:LOCAL:127.0.0.1:GUEST root@ip` with the pinned options."""
    argv = ssh_argv(key, known_hosts, ip)
    destination = argv[-1]
    return [*argv[:-1], "-N", "-L",
            f"127.0.0.1:{int(local_port)}:127.0.0.1:{int(guest_port)}",
            destination]


@contextlib.contextmanager
def forward(key: Path, known_hosts: Path, ip: str, guest_port: int,
            timeout: float = 10.0):
    """An ephemeral local forward to the guest's loopback port.

    One `ssh -N -L` per browser invocation: it is opened here, used for the
    call, and reaped on the way out. Nothing survives the process, so there
    is no tunnel pid to reconcile and no local port to leak.
    """
    local_port = free_port()
    argv = forward_argv(key, known_hosts, ip, local_port, guest_port)
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + max(1.0, timeout)
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                stderr = (proc.stderr.read() if proc.stderr else b"")
                raise MvError("ssh-failed",
                              f"the forward to {ip}:{guest_port} exited",
                              detail=_decode(stderr)[-300:] or None)
            try:
                with socket.create_connection(("127.0.0.1", local_port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise MvError("ssh-failed",
                          f"the forward to {ip}:{guest_port} did not open")
        yield local_port
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        if proc.stderr is not None:
            with contextlib.suppress(OSError):
                proc.stderr.close()
