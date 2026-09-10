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
              stdout_path, stderr_path, on_start=None) -> StageResult:
    """Run and always fence off the process group, including on interrupts."""
    t0 = time.monotonic()
    p = None
    pgid = 0
    rc = None
    timed_out = False
    try:
        with open(stdout_path, "wb") as so, open(stderr_path, "wb") as se:
            p = subprocess.Popen(argv, cwd=str(cwd), env=env, stdout=so, stderr=se,
                                 start_new_session=True)
            pgid = os.getpgid(p.pid)
            if on_start is not None:
                on_start(pgid)
            try:
                rc = p.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
    finally:
        # This is deliberately in finally: Ctrl-C must not leave a worker group behind.
        terminated = kill_group(pgid, grace_s=0.5, reap=p) if pgid else True
        if p is not None:
            try:
                p.wait(timeout=0)
            except subprocess.TimeoutExpired:
                pass
    return StageResult(returncode=rc, timed_out=timed_out, elapsed_s=time.monotonic() - t0, pgid=pgid,
                       terminated=terminated)
