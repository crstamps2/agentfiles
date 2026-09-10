"""Run a stage in its own process group; on timeout kill the whole group and verify."""
from __future__ import annotations
import dataclasses
import os
import signal
import subprocess
import time


@dataclasses.dataclass(frozen=True)
class StageResult:
    returncode: int | None
    timed_out: bool
    elapsed_s: float
    pgid: int
    terminated: bool = True


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def kill_group(pgid: int, grace_s: float = 5.0, reap: subprocess.Popen | None = None) -> bool:
    for sig, wait in ((signal.SIGTERM, grace_s), (signal.SIGKILL, 2.0)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass  # Process exists but can't kill; try next signal
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if reap is not None:
                reap.poll()  # non-blocking waitpid; reaps the leader so group_alive can observe death
            if not group_alive(pgid):
                return True
            time.sleep(0.05)
    if reap is not None:
        reap.poll()
    return not group_alive(pgid)


def run_stage(argv: list, cwd, timeout_s: float, env: dict | None,
              stdout_path, stderr_path) -> StageResult:
    t0 = time.monotonic()
    with open(stdout_path, "wb") as so, open(stderr_path, "wb") as se:
        p = subprocess.Popen(argv, cwd=str(cwd), env=env, stdout=so, stderr=se,
                             start_new_session=True)   # new session ⇒ new process group, pgid == pid
        pgid = os.getpgid(p.pid)
        try:
            rc = p.wait(timeout=timeout_s)
            timed_out = False
        except subprocess.TimeoutExpired:
            kill_group(pgid, reap=p)
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            rc, timed_out = None, True
    # Reap any stragglers that re-parented; verified by group_alive below.
    terminated = kill_group(pgid, grace_s=0.5, reap=p)
    return StageResult(returncode=rc, timed_out=timed_out, elapsed_s=time.monotonic() - t0, pgid=pgid,
                        terminated=terminated)
