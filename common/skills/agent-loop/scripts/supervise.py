"""launchd supervision: one LaunchAgent per active ticket keeps `runner.py run-once` alive until the
ticket reaches Human Gate 1 (or blocks / pauses for an owner decision).

Why launchd and not a monitor/child process: twice this week the loop died with the chat session
that had spawned it (2026-09-14 overnight, 2026-09-15 morning). launchd `KeepAlive` restarts the
job on exit; `reconcile()` on start recovers any in-flight attempt; run-once is idempotent against
the remote. The pane in cmux (later: the /loop-watch skill) only OBSERVES and triages; it never
owns the process.

Exit codes from `run-once --supervised`:
  0  ticket at human-gate-1 / done            -> SuccessfulExit=false keeps launchd from restarting; job unloads itself
  1  waiting (CI, bots, resources) or paused/blocked for an operator -> restart after ThrottleInterval
  3  fenced (another runner holds the ticket)   -> restart later
"""
from __future__ import annotations

import os
import pathlib
import plistlib
import subprocess
import sys

LABEL_PREFIX = "com.cody.agent-loop"
AGENTS_DIR = pathlib.Path("~/Library/LaunchAgents").expanduser()
SCRIPTS = pathlib.Path(__file__).resolve().parent
PYTHON = "/opt/homebrew/bin/python3"
LAUNCHER = pathlib.Path("~/.local/bin/agent-loop-python").expanduser()
LAUNCHER_SRC = SCRIPTS.parent / "launcher" / "agent-loop-python.c"


def ensure_launcher() -> str:
    """Build + ad-hoc codesign the stable-identity launcher if missing/stale. macOS TCC keys its
    'access data from other apps' grant to the launchd job's binary; Homebrew python3's identity
    changes on every rebuild, so without this Cody was prompted on every launch (2026-09-15)."""
    if LAUNCHER.exists() and LAUNCHER.stat().st_mtime >= LAUNCHER_SRC.stat().st_mtime:
        return str(LAUNCHER)
    LAUNCHER.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cc", "-O2", "-o", str(LAUNCHER), str(LAUNCHER_SRC)], check=True)
    subprocess.run(["codesign", "--force", "--sign", "-", "--identifier", "com.cody.agent-loop.python", "--options", "runtime", str(LAUNCHER)], check=True)
    return str(LAUNCHER)


def label(key: str) -> str:
    return f"{LABEL_PREFIX}.{key.lower()}"


def plist_path(key: str) -> pathlib.Path:
    return AGENTS_DIR / f"{label(key)}.plist"


def write_plist(key: str, worktree: str, state_root: pathlib.Path, throttle_s: int = 180) -> pathlib.Path:
    logs = state_root / "launchd"; logs.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": label(key),
        "ProgramArguments": [ensure_launcher(), str(SCRIPTS / "runner.py"), "run-once", "--ticket", key, "--worktree", worktree,
                             "--max-steps", "40", "--wait-on-resource", "120", "--supervised"],
        "WorkingDirectory": str(SCRIPTS),
        # mise shims FIRST: under launchd there is no login shell, so `bundle`/`ruby`/`node` must
        # resolve to the repo's pinned versions via the shims, not /usr/bin (system Ruby 2.6 broke
        # `bin/wt prepare` for a premium worker on ZIP-4294, 2026-09-15).
        "EnvironmentVariables": {"PATH": os.path.expanduser("~/.local/share/mise/shims") + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                                 "HOME": os.path.expanduser("~"), "LANG": "en_US.UTF-8", "MISE_YES": "1"},
        "RunAtLoad": True,
        # restart whenever the job exits non-zero (waiting / paused / fenced); a zero exit (gate reached) unloads
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": throttle_s,
        "StandardOutPath": str(logs / f"{key.lower()}.out.log"),
        "StandardErrorPath": str(logs / f"{key.lower()}.err.log"),
        "ProcessType": "Background",
        "Nice": 5,
    }
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    p = plist_path(key)
    with open(p, "wb") as f:
        plistlib.dump(plist, f)
    return p


def _launchctl(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def start(key: str, worktree: str, state_root: pathlib.Path) -> str:
    p = write_plist(key, worktree, state_root)
    uid = os.getuid()
    _launchctl("bootout", f"gui/{uid}/{label(key)}")            # idempotent: ignore "not found"
    # launchd unloads asynchronously; a bootstrap that races the bootout fails with EIO (5). Retry briefly.
    import time
    last = None
    for _ in range(10):
        r = _launchctl("bootstrap", f"gui/{uid}", str(p))
        if r.returncode == 0:
            return label(key)
        last = r.stderr.strip(); time.sleep(0.5)
    raise RuntimeError(f"launchctl bootstrap failed: {last}")


def stop(key: str) -> None:
    _launchctl("bootout", f"gui/{os.getuid()}/{label(key)}")
    p = plist_path(key)
    if p.exists():
        p.unlink()


def status(key: str) -> str:
    r = _launchctl("print", f"gui/{os.getuid()}/{label(key)}")
    if r.returncode:
        return "not loaded"
    st = {}
    for line in r.stdout.splitlines():
        line = line.strip()
        for k in ("state", "pid", "last exit code", "runs"):
            if line.startswith(k + " ="):
                st[k] = line.split("=", 1)[1].strip()
    return ", ".join(f"{k}={v}" for k, v in st.items()) or "loaded"


def active() -> list[str]:
    return sorted(p.stem[len(LABEL_PREFIX) + 1:].upper() for p in AGENTS_DIR.glob(f"{LABEL_PREFIX}.*.plist"))


if __name__ == "__main__":
    for k in active():
        print(k, status(k))
