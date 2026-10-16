"""Firecracker API client over its Unix socket (stdlib only)."""

from __future__ import annotations

import http.client
import json
import socket
import time
from pathlib import Path

from .errors import MvError


class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, sock_path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.sock_path = sock_path

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.sock_path)
        self.sock = s


class FcApi:
    def __init__(self, sock: Path, timeout: float = 5.0):
        self.sock = str(sock)
        self.timeout = timeout

    def request(self, method: str, path: str, body: dict | None = None):
        conn = _UnixHTTP(self.sock, self.timeout)
        payload = None if body is None else json.dumps(body)
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        try:
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
        except OSError as exc:
            raise MvError(
                "api-unreachable",
                f"firecracker API not reachable at {self.sock}",
                hint="is the instance running? (`microvm-control status <id>`)",
                detail=str(exc),
            ) from exc
        except http.client.HTTPException as exc:
            raise MvError("api-error", f"{method} {path} protocol error", detail=str(exc)) from exc
        finally:
            conn.close()

        if resp.status >= 400:
            fault = ""
            try:
                fault = json.loads(raw.decode()).get("fault_message", "")
            except (ValueError, UnicodeDecodeError):
                fault = raw.decode(errors="replace").strip()
            raise MvError(
                "api-error",
                f"{method} {path} failed with HTTP {resp.status}",
                detail=fault or None,
            )
        if not raw:
            return None
        try:
            return json.loads(raw.decode())
        except (ValueError, UnicodeDecodeError):
            return {"raw": raw.decode(errors="replace")}

    # --- typed helpers -----------------------------------------------------
    def wait_socket(self, timeout: float) -> float:
        """Wait until the API socket accepts connections; return elapsed ms."""
        start = time.monotonic()
        deadline = start + timeout
        while time.monotonic() < deadline:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(0.25)
            try:
                s.connect(self.sock)
                return (time.monotonic() - start) * 1000.0
            except OSError:
                time.sleep(0.02)
            finally:
                s.close()
        raise MvError("api-timeout", f"firecracker API socket {self.sock} did not appear")

    def configure(self, *, kernel: Path, boot_args: str, vcpu: int, mem_mib: int,
                  track_dirty_pages: bool, drives: list[dict], iface: dict,
                  log_path: Path) -> None:
        self.request("PUT", "/boot-source", {
            "kernel_image_path": str(kernel),
            "boot_args": boot_args,
        })
        self.request("PUT", "/machine-config", {
            "vcpu_count": vcpu,
            "mem_size_mib": mem_mib,
            "smt": False,
            "track_dirty_pages": track_dirty_pages,
        })
        for d in drives:
            self.request("PUT", f"/drives/{d['drive_id']}", d)
        self.request("PUT", f"/network-interfaces/{iface['iface_id']}", iface)
        self.request("PUT", "/logger", {
            "log_path": str(log_path),
            "level": "Info",
            "show_level": True,
            "show_log_origin": True,
        })

    def start_instance(self) -> None:
        self.request("PUT", "/actions", {"action_type": "InstanceStart"})

    def ctrl_alt_del(self) -> None:
        self.request("PUT", "/actions", {"action_type": "SendCtrlAltDel"})

    def pause(self) -> None:
        self.request("PATCH", "/vm", {"state": "Paused"})

    def resume(self) -> None:
        self.request("PATCH", "/vm", {"state": "Resumed"})

    def snapshot_create(self, *, snapshot_path: Path, mem_path: Path,
                        snapshot_type: str = "Full") -> None:
        self.request("PUT", "/snapshot/create", {
            "snapshot_type": snapshot_type,
            "snapshot_path": str(snapshot_path),
            "mem_file_path": str(mem_path),
        })

    def snapshot_load(self, *, snapshot_path: Path, mem_path: Path,
                      resume: bool = True, track_dirty_pages: bool = True) -> None:
        self.request("PUT", "/snapshot/load", {
            "snapshot_path": str(snapshot_path),
            "mem_backend": {"backend_path": str(mem_path), "backend_type": "File"},
            "resume_vm": resume,
            "track_dirty_pages": track_dirty_pages,
        })

    def info(self) -> dict:
        return self.request("GET", "/") or {}
