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


def _has_head(wt) -> bool:
    return subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "--verify", "-q", "HEAD"], capture_output=True
    ).returncode == 0


def snapshot(wt) -> str:
    """Tree OID of the current working state using a throwaway index (real index untouched)."""
    with tempfile.NamedTemporaryFile(prefix="al-index-", delete=False) as tf:
        idx = tf.name
    try:
        os.unlink(idx)
        env = {**os.environ, "GIT_INDEX_FILE": idx}
        # A new index otherwise has no tracked ignored entries, causing an ignored
        # file that is nevertheless tracked to disappear from a snapshot. On an unborn
        # HEAD (no commits yet) there is nothing to seed.
        if _has_head(wt):
            _git(wt, "read-tree", "HEAD", env=env)
        # --force: capture untracked-but-ignored paths a worker may have written (e.g. tmp/x),
        # not just tracked-ignored content already seeded from HEAD above.
        _git(wt, "add", "-A", "--force", env=env)
        return _git(wt, "write-tree", env=env).strip()
    finally:
        try:
            os.unlink(idx)
        except FileNotFoundError:
            pass


def changed_paths(wt, base_tree: str) -> list:
    now = snapshot(wt)
    # -z is required: quoted line output loses the exact repository pathname.
    r = subprocess.run(["git", "-C", str(wt), "diff", "--no-renames", "--name-only", "-z", base_tree, now],
                       capture_output=True)
    if r.returncode:
        raise RuntimeError(f"git diff failed: {r.stderr.decode(errors='replace').strip()}")
    return [p.decode("utf-8", "surrogateescape") for p in r.stdout.split(b"\0") if p]


def restore(wt, tree: str) -> None:
    """Make the working tree match `tree` exactly for tracked-or-added content; ignored files
    present in `tree` survive, but ignored files a worker wrote that are NOT in `tree` are
    removed too (snapshot() now captures ignored writes via --force, so "restored to base"
    must actually mean it for ignored content as well)."""
    before = snapshot(wt)
    _git(wt, "read-tree", "--reset", "-u", tree)
    _git(wt, "clean", "-fd")            # remove untracked (non-ignored) leftovers
    # Paths present before restore but absent from the target tree: read-tree/clean -fd never
    # touch ignored paths (clean -fd skips them by design), so remove any such leftovers
    # directly. -z is required for exact repository pathnames.
    r = subprocess.run(["git", "-C", str(wt), "diff", "--no-renames", "--diff-filter=A", "--name-only", "-z", tree, before],
                       capture_output=True)
    if r.returncode:
        raise RuntimeError(f"git diff failed: {r.stderr.decode(errors='replace').strip()}")
    extras = [p.decode("utf-8", "surrogateescape") for p in r.stdout.split(b"\0") if p]
    for extra in extras:
        fp = pathlib.Path(wt) / extra
        try:
            fp.unlink()
        except (FileNotFoundError, IsADirectoryError):
            pass
    # read-tree left the real index pointing at `tree`; put it back to HEAD so status is sane.
    # On an unborn HEAD, `git reset` fails (there is no HEAD to reset to) and is unneeded.
    if _has_head(wt):
        _git(wt, "reset", "-q")


def verify_restored(wt, tree: str) -> bool:
    """True iff the working tree's current snapshot matches `tree` exactly."""
    return snapshot(wt) == tree


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
