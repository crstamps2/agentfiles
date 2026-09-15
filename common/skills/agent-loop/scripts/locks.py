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


def is_held(path: pathlib.Path) -> bool:
    """True iff some live process holds the flock for this lease (non-blocking probe)."""
    fl = pathlib.Path(path).with_suffix(pathlib.Path(path).suffix + ".flock")
    if not fl.exists():
        return False
    with open(fl, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
        return False


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Lease:
    def __init__(self, path: pathlib.Path, name: str):
        self.path = pathlib.Path(path)
        self.name = name
        self.held = False
        self._held_fd = None

    def _flock_path(self) -> pathlib.Path:
        return self.path.with_suffix(self.path.suffix + ".flock")

    def acquire(self, heartbeat_s: int = 60, hold: bool = False) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if hold:
            fl = open(self._flock_path(), "w")
            try:
                fcntl.flock(fl, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                fl.close()
                return False
            try:
                self._write()
            except BaseException:
                fcntl.flock(fl, fcntl.LOCK_UN)
                fl.close()
                raise
            self.held = True
            self._held_fd = fl
            return True
        with open(self._flock_path(), "w") as fl:
            fcntl.flock(fl, fcntl.LOCK_EX)
            try:
                rec = owner(self.path)
                if rec and rec.get("boot_id") == boot_id() and self._is_owner_alive(rec):
                    return False
                self._write()
                self.held = True
                return True
            finally:
                fcntl.flock(fl, fcntl.LOCK_UN)

    def _is_owner_alive(self, rec: dict) -> bool:
        """Check if record's PID is alive. Missing or invalid pid means no owner."""
        pid_val = rec.get("pid")
        if pid_val is None:
            return False
        try:
            return pid_alive(int(pid_val))
        except (ValueError, TypeError):
            return False

    def _write(self) -> None:
        """Atomically write owner record using temp file + os.replace()."""
        data = json.dumps({"pid": os.getpid(), "boot_id": boot_id(), "heartbeat_utc": _now(), "name": self.name})
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(data)
        os.replace(tmp_path, self.path)

    def heartbeat(self) -> None:
        """Update heartbeat timestamp if held. Write is atomic."""
        if self.held:
            self._write()

    def release(self) -> None:
        if self.held:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            self.held = False
        if self._held_fd is not None:
            try:
                fcntl.flock(self._held_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            self._held_fd.close()
            self._held_fd = None

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


def slot(base: pathlib.Path, n: int, name: str) -> "Lease | None":
    """Acquire one of `n` numbered leases `<base>-0..n-1` (a counting semaphore made of flocks).
    Returns the held Lease or None if all slots are busy. `n <= 1` degrades to a single lease."""
    for i in range(max(1, int(n))):
        lease = Lease(pathlib.Path(f"{base}-{i}"), f"{name} [slot {i}]")
        # hold=True: the slot is owned by an OPEN FILE DESCRIPTOR, not by a pid record. Several
        # threads of one runner process each hold their own slot, and a slot dies with its holder.
        if lease.acquire(hold=True):
            return lease
    return None

