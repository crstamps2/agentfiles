"""Run a stage in its own process group; on timeout kill the whole group and verify."""
from __future__ import annotations
import dataclasses
import os
import signal
import subprocess
import time


class LogPathExists(RuntimeError):
    """Raised when a stage log path already exists (e.g. a worker-planted symlink); the log
    is opened O_EXCL|O_NOFOLLOW so we never write through or follow a pre-existing path."""


def _open_log(path):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise LogPathExists(f"log path exists: {path}")
    return os.fdopen(fd, "wb")


@dataclasses.dataclass(frozen=True)
class StageResult:
    returncode: int | None
    timed_out: bool
    elapsed_s: float
    pgid: int
    terminated: bool = True


def group_alive(pgid: int) -> bool:
    """Probe a process group without altering it: return True iff the group is not provably dead.
    
    Returns False (group is dead) only on ProcessLookupError. PermissionError (group exists but
    inaccessible) returns True (fail-safe alive assumption).
    """
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Assume alive; can't signal but group exists


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
    """Run and always fence off the process group, including on interrupts.

    The child is held at a launch gate (blocked reading a pipe) immediately after fork+exec
    of a wrapper shell, so it cannot begin executing the real command until `on_start` has
    durably journaled the group's pgid. This closes the window where a crash between
    process creation and journaling would leave a live, un-journaled worker that `_recover`
    cannot find. If `on_start` raises, the gate is never released (the child's `read` fails
    and it exits 97 without ever exec'ing the real command), the group is killed, and the
    exception is re-raised to the caller.
    """
    t0 = time.monotonic()
    p = None
    pgid = 0
    rc = None
    timed_out = False
    r, w = None, None
    try:
        r, w = os.pipe()
        gated_argv = ["/bin/sh", "-c", f'read _ <&{r} || exit 97; exec "$@"', "gate", *argv]
        so = _open_log(stdout_path)
        try:
            se = _open_log(stderr_path)
        except LogPathExists:
            try:
                so.close()
            except OSError:
                pass
            try:
                os.unlink(stdout_path)
            except FileNotFoundError:
                pass
            raise
        with so, se:
            p = subprocess.Popen(gated_argv, cwd=str(cwd), env=env, stdout=so, stderr=se,
                                 start_new_session=True, pass_fds=(r,))
            os.close(r); r = None  # child holds its own inherited copy; parent doesn't need it
            pgid = os.getpgid(p.pid)
            try:
                if on_start is not None:
                    on_start(pgid, p.pid)
            except BaseException:
                os.close(w); w = None  # child's `read` fails closed -> exit 97, never execs
                kill_group(pgid, grace_s=0.5, reap=p)
                raise
            os.write(w, b"go\n")
            os.close(w); w = None
            try:
                rc = p.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
    finally:
        if r is not None:
            try:
                os.close(r)
            except OSError:
                pass
        if w is not None:
            try:
                os.close(w)
            except OSError:
                pass
        # This is deliberately in finally: Ctrl-C must not leave a worker group behind.
        terminated = kill_group(pgid, grace_s=0.5, reap=p) if pgid else True
        if p is not None:
            try:
                p.wait(timeout=0)
            except subprocess.TimeoutExpired:
                pass
    return StageResult(returncode=rc, timed_out=timed_out, elapsed_s=time.monotonic() - t0, pgid=pgid,
                       terminated=terminated)
