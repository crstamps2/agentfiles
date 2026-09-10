"""Git snapshots of the whole working state (tracked + untracked, not ignored), restore,
changed-path listing, and the runner-enforced diff allowlist."""
from __future__ import annotations
import fnmatch
import os
import pathlib
import re
import subprocess
import tempfile


def _git(wt, *args, env=None) -> str:
    r = subprocess.run(["git", "-C", str(wt), *args], capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def head(wt) -> str:
    return _git(wt, "rev-parse", "HEAD").strip()


def snapshot(wt) -> str:
    """Tree OID of the current working state using a throwaway index (real index untouched)."""
    with tempfile.NamedTemporaryFile(prefix="al-index-", delete=False) as tf:
        idx = tf.name
    try:
        os.unlink(idx)
        env = {**os.environ, "GIT_INDEX_FILE": idx}
        _git(wt, "add", "-A", env=env)
        return _git(wt, "write-tree", env=env).strip()
    finally:
        try:
            os.unlink(idx)
        except FileNotFoundError:
            pass


def changed_paths(wt, base_tree: str) -> list:
    now = snapshot(wt)
    out = _git(wt, "diff", "--no-renames", "--name-only", base_tree, now)
    return [l.strip() for l in out.splitlines() if l.strip()]


def restore(wt, tree: str) -> None:
    """Make the working tree match `tree` exactly for tracked-or-added content; ignored files survive."""
    _git(wt, "read-tree", "--reset", "-u", tree)
    _git(wt, "clean", "-fd")            # remove untracked (non-ignored) leftovers
    # read-tree left the real index pointing at `tree`; put it back to HEAD so status is sane.
    _git(wt, "reset", "-q")


def _match(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):
        return path.startswith(pattern) or path == pattern.rstrip("/")
    if "**" in pattern:
        rx = re.escape(pattern).replace(r"\*\*/", "(?:.*/)?").replace(r"/\*\*", "(?:/.*)?").replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
        return re.fullmatch(rx, path) is not None
    return fnmatch.fnmatchcase(path, pattern)


def check_allowlist(paths: list, task, protected_paths: list, test_globs: list) -> list:
    violations = []
    for p in paths:
        if any(_match(p, pp) for pp in protected_paths):
            violations.append(f"{p}: protected path (never editable by workers)")
            continue
        if not any(_match(p, a) for a in task.allowed_files):
            violations.append(f"{p}: not in allowed_files for task {task.id}")
            continue
        if not task.may_edit_tests and any(_match(p, g) for g in test_globs):
            violations.append(f"{p}: test/fixture path but task {task.id} has may_edit_tests=false")
    return violations
