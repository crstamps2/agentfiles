"""Leases keyed by PID + boot ID. Reclaim only when the owner is provably gone; never by age."""
from __future__ import annotations
import datetime as dt
import fcntl
import json
import os
import pathlib
import subprocess

_BOOT_ID = None


def boot_id() -> str:
    global _BOOT_ID
    if _BOOT_ID is None:
        for key in ("kern.bootsessionuuid", "kern.boottime"):
            r = subprocess.run(["sysctl", "-n", key], capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                _BOOT_ID = r.stdout.strip()
                break
        else:
            _BOOT_ID = "unknown-boot"
    return _BOOT_ID


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def owner(path: pathlib.Path) -> dict | None:
    try:
        return json.loads(pathlib.Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Lease:
    def __init__(self, path: pathlib.Path, name: str):
        self.path = pathlib.Path(path)
        self.name = name
        self.held = False

    def _flock_path(self) -> pathlib.Path:
        return self.path.with_suffix(self.path.suffix + ".flock")

    def acquire(self, heartbeat_s: int = 60) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._flock_path(), "w") as fl:
            fcntl.flock(fl, fcntl.LOCK_EX)
            try:
                rec = owner(self.path)
                if rec and rec.get("boot_id") == boot_id() and pid_alive(int(rec.get("pid", -1))):
                    return False
                self._write()
                self.held = True
                return True
            finally:
                fcntl.flock(fl, fcntl.LOCK_UN)

    def _write(self) -> None:
        self.path.write_text(json.dumps({"pid": os.getpid(), "boot_id": boot_id(), "heartbeat_utc": _now(), "name": self.name}))

    def heartbeat(self) -> None:
        if self.held:
            self._write()

    def release(self) -> None:
        if self.held:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            self.held = False

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
