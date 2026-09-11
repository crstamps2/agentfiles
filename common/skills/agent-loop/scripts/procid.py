"""Process-group identity that fences on doubt. Signal only what is provably ours and alive."""
from __future__ import annotations
import dataclasses
import os
import subprocess
import locks


@dataclasses.dataclass(frozen=True)
class ProcId:
    boot_id: str
    pgid: int
    pid: int
    start_time: str
    cmd: str
    def to_dict(self) -> dict: return dataclasses.asdict(self)
    @staticmethod
    def from_dict(d: dict) -> "ProcId": return ProcId(**{k: d[k] for k in ("boot_id", "pgid", "pid", "start_time", "cmd")})


def _ps(pid: int) -> tuple[str, str] | None:
    r = subprocess.run(["ps", "-o", "lstart=,comm=", "-p", str(pid)], capture_output=True, text=True)
    line = r.stdout.strip("\n")
    if r.returncode != 0 or not line.strip():
        return None
    return line[:24].strip(), line[24:].strip()


def capture(pid: int) -> ProcId:
    ps = _ps(pid)
    if ps is None:
        raise ProcessLookupError(pid)
    return ProcId(boot_id=locks.boot_id(), pgid=os.getpgid(pid), pid=pid, start_time=ps[0], cmd=ps[1])


def classify(rec: ProcId | None) -> str:
    if rec is None:
        return "dead"
    if rec.boot_id != locks.boot_id():
        return "unknown"
    try:
        os.killpg(rec.pgid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "unknown"
    ps = _ps(rec.pid)
    if ps is None:
        return "unknown"            # group populated, leader gone: descendants may be writing
    if ps[0] != rec.start_time:
        return "unknown"            # recycled pid (start time is the identity; cmd is informational --
                                    # the gate shell execs into the real command, so cmd legitimately changes)
    return "ours-alive"
