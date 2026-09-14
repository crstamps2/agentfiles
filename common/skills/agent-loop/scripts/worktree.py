"""Git snapshots of the whole working state (tracked + untracked, not ignored), restore,
changed-path listing, and the runner-enforced diff allowlist."""
from __future__ import annotations
import fnmatch
import os
import pathlib
import re
import stat
import subprocess
import tempfile


def _git(wt, *args, env=None) -> str:
    r = subprocess.run(["git", "-C", str(wt), *args], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
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
    # C4: remove worker-written extras (incl. ignored paths) BEFORE read-tree reinstalls the base
    # topology. Doing it after would let the unlink traverse a *restored* base symlink and delete
    # outside the worktree. Compute extras against the observed tree, delete them while the tree
    # is still the observed one, and refuse to follow any symlink component on the way.
    r = subprocess.run(["git", "-C", str(wt), "diff", "--no-renames", "--diff-filter=A", "--name-only", "-z", tree, before],
                       capture_output=True)
    if r.returncode:
        raise RuntimeError(f"git diff failed: {r.stderr.decode(errors='replace').strip()}")
    extras = [p.decode("utf-8", "surrogateescape") for p in r.stdout.split(b"\0") if p]
    root = pathlib.Path(wt).resolve()
    for extra in extras:
        _unlink_nofollow(root, extra)
    _git(wt, "read-tree", "--reset", "-u", tree)
    _git(wt, "clean", "-fd")            # remove untracked (non-ignored) leftovers
    # read-tree left the real index pointing at `tree`; put it back to HEAD so status is sane.
    # On an unborn HEAD, `git reset` fails (there is no HEAD to reset to) and is unneeded.
    if _has_head(wt):
        _git(wt, "reset", "-q")


def _unlink_nofollow(root: pathlib.Path, rel: str) -> None:
    """Unlink `root/rel` without following any symlink component. Walks the path one component at
    a time with lstat; if any intermediate component is a symlink (or not a directory) the unlink
    is skipped -- the subsequent read-tree/clean handle tracked content, and refusing is the
    fail-safe choice for anything that would resolve outside the worktree."""
    cur = root
    parts = pathlib.PurePosixPath(rel).parts
    for comp in parts[:-1]:
        cur = cur / comp
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            return
    target = cur / parts[-1]
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        return                          # directories are handled by git clean -fd
    os.unlink(target)                   # unlink() never follows the final component


def verify_restored(wt, tree: str) -> bool:
    """True iff the working tree matches `tree` for every path that is NOT gitignored.

    Ignored paths are excluded from the verdict on purpose: build caches such as
    tmp/cache/bootsnap are rewritten by merely running the repo's test suite (the runner's own
    verification gate does it), so a byte-exact tree comparison can never converge and would
    wedge every attempt at finalize (first overnight run, 2026-09-12). Worker-WRITTEN ignored
    extras are still removed by restore(); a worker edit to a protected-but-ignored path is
    caught earlier by check_allowlist (protected wins over ignored)."""
    now = snapshot(wt)
    if now == tree:
        return True
    r = subprocess.run(["git", "-C", str(wt), "diff", "--no-renames", "--name-only", "-z", tree, now], capture_output=True)
    if r.returncode:
        raise RuntimeError(f"git diff failed: {r.stderr.decode(errors='replace').strip()}")
    differing = [p.decode("utf-8", "surrogateescape") for p in r.stdout.split(b"\0") if p]
    return not (set(differing) - ignored_paths(wt, differing))


def _match(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):
        return path.startswith(pattern) or path == pattern.rstrip("/")
    if "**" in pattern:
        rx = re.escape(pattern).replace(r"\*\*/", "(?:.*/)?").replace(r"/\*\*", "(?:/.*)?").replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
        return re.fullmatch(rx, path) is not None
    return fnmatch.fnmatchcase(path, pattern)


def ignored_paths(wt, paths: list) -> set:
    """Subset of `paths` that git would ignore (gitignore/exclude rules). Batch call via
    `check-ignore --stdin -z`; a nonexistent path is still classified by the rules."""
    if not paths:
        return set()
    r = subprocess.run(["git", "-C", str(wt), "check-ignore", "--stdin", "-z", "--no-index"],
                       input=b"\0".join(p.encode("utf-8", "surrogateescape") for p in paths) + b"\0",
                       capture_output=True)
    # rc 0 = some ignored, 1 = none ignored, 128 = error
    if r.returncode not in (0, 1):
        raise RuntimeError(f"git check-ignore failed: {r.stderr.decode(errors='replace').strip()}")
    return {p.decode("utf-8", "surrogateescape") for p in r.stdout.split(b"\0") if p}


def check_allowlist(paths: list, task, protected_paths: list, test_globs: list, wt=None, harness_globs: list = ()) -> list:
    """Runner-enforced diff allowlist. Precedence per path: protected (always a violation) ->
    gitignored (exempt: build artifacts such as tmp/cache, .ruby-lsp, node_modules/.cache are
    side effects of running the repo's own verification commands, not worker edits; restore()
    still removes them so 'restored to base' stays true) -> allowed_files -> test/fixture rule.
    `wt` enables the gitignore exemption; without it every path is judged (legacy callers)."""
    violations = []
    ignored = ignored_paths(wt, paths) if wt is not None else set()
    for p in paths:
        if any(_match(p, pp) for pp in protected_paths):
            violations.append(f"{p}: protected path (never editable by workers)")
            continue
        if p in ignored or any(_match(p, g) for g in harness_globs):
            continue                      # build/harness artifact, not a worker edit
        if not any(_match(p, a) for a in task.allowed_files):
            violations.append(f"{p}: not in allowed_files for task {task.id}")
            continue
        if not task.may_edit_tests and any(_match(p, g) for g in test_globs):
            violations.append(f"{p}: test/fixture path but task {task.id} has may_edit_tests=false")
    return violations
