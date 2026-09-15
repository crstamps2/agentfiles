"""The attempt record: `attempts/<ticket>/<task>/<n>/attempt.json` is the single source of
truth for an attempt's lifecycle. Every projection (history, metrics, lifecycle) is
recomputed idempotently from it. This module owns the record shape, the forward-only
state machine, and the no-follow I/O discipline used for every write/read under
`attempts/`.
"""
from __future__ import annotations
import contextlib
import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import re
import os
import pathlib
import shutil
import stat
import subprocess
import uuid

import contracts
import locks
import worktree as worktree_mod


class UnsafePath(RuntimeError):
    pass


class UnreadableRecord(ValueError):
    pass


class IllegalAttemptTransition(ValueError):
    pass


class AttemptDirExists(RuntimeError):
    """Raised when create() is asked to create an attempt dir that already exists on disk.
    create() only ever cleans up (rmtree) a leaf directory it itself made; a pre-existing
    dir -- ours or a duplicate caller's -- is left untouched."""
    pass


class StaleRecord(IllegalAttemptTransition):
    """Raised when the in-memory record is stale relative to the on-disk record (a
    concurrent writer has already advanced status/n). Never overwrite a newer record."""
    pass


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# No-follow I/O helpers. Every write into attempts/ and every read of a
# worker-writable path goes through these. See the design's "Runner reads and
# writes into attempts/" section.
# ---------------------------------------------------------------------------

def safe_write(path, text: str) -> None:
    """Create a file without ever traversing a supplied symlink. Fails if the path exists."""
    path = pathlib.Path(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def safe_rewrite(path, text: str) -> None:
    """Atomic rewrite: unique temp file + os.replace. Refuses to rewrite over a non-regular
    path (symlink, device, etc). Sweeps stale temp siblings left by a prior crashed writer."""
    path = pathlib.Path(path)
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise UnsafePath(f"unsafe artifact path: {path}")
    except FileNotFoundError:
        return safe_write(path, text)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        safe_write(tmp, text)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    prefix = f"{path.name}.tmp-"
    for stale in path.parent.glob(f"{prefix}*"):      # sweep predecessors' leftovers, scoped
        if stale == tmp or not stale.name.startswith(prefix):
            continue
        pid = _temp_owner_pid(stale.name, prefix)
        if pid is not None and locks.pid_alive(pid):
            continue    # a live writer's in-flight temp; leave it alone
        try:
            stale.unlink()
        except OSError:
            pass


def _temp_owner_pid(tmp_name: str, prefix: str) -> int | None:
    """Extract the pid embedded in a `<name>.tmp-<pid>-<hex8>` temp filename. Returns None
    if the suffix doesn't parse (so it's treated as unowned and swept)."""
    suffix = tmp_name[len(prefix):]
    pid_str, _, rest = suffix.partition("-")
    if not pid_str.isdigit() or not rest:
        return None
    return int(pid_str)


def safe_read(path) -> str:
    """Read a worker-writable path without ever traversing a symlink. Raises UnsafePath for
    a symlink or anything that is not a regular file. A missing file raises the ordinary
    FileNotFoundError (uncaught) so callers can distinguish "absent" from "unsafe"."""
    path = pathlib.Path(path)
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise UnsafePath(f"not a regular file: {path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    with os.fdopen(fd, "r", encoding="utf-8") as f:
        return f.read()


def safe_mkdir(path) -> None:
    """Create `path` (and any missing parents) after validating the deepest already-existing
    ancestor is not a symlink; every component created here is one we made ourselves (and is
    therefore trusted). Pre-existing system ancestors above that (e.g. macOS's /var -> /private/var)
    are out of scope -- the attempts root itself is trusted (see Trust boundary)."""
    path = pathlib.Path(path)
    missing = []
    existing = path
    while not existing.exists():
        missing.append(existing.name)
        existing = existing.parent
    if existing.is_symlink():
        raise UnsafePath(f"symlinked parent: {existing}")
    if not existing.is_dir():
        raise UnsafePath(f"parent is not a directory: {existing}")
    cur = existing
    for name in reversed(missing):
        cur = cur / name
        os.mkdir(cur, 0o700)


# ---------------------------------------------------------------------------
# task.md / task.toml (moved here from runner.py; runner will import these in
# a later task once it switches its callers over).
# ---------------------------------------------------------------------------

READ_FIRST_RE = re.compile(r"Read first:\s*([^\s#]+)(?:#([A-Za-z0-9._-]+))?")
EXCERPT_MAX_CHARS = 7000


def _slug(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", h.lower()).strip("-")


def required_reading(task: contracts.Task, wt: pathlib.Path) -> str:
    """Render the `Read first: <path>#<section>` reference from the task summary as a bounded
    excerpt (the section's heading through the next heading of the same or higher level).

    Why: the cheap local model has a 32K window; reading a 600-line skill whole plus the sibling
    components exhausted it and the attempt died silently (ZIP-7873/001 attempt 1, 2026-09-14).
    The excerpt delivers the governing rule to every arm at a fixed cost. The path is resolved
    inside the worktree only; anything else is ignored (remote text is data, not instructions)."""
    m = READ_FIRST_RE.search(task.summary or "")
    if not m:
        return ""
    rel, anchor = m.group(1), m.group(2)
    try:
        target = (pathlib.Path(wt) / rel).resolve(); target.relative_to(pathlib.Path(wt).resolve())
        text = target.read_text(errors="replace")
    except (OSError, ValueError):
        return f"\n## Required reading\n`{rel}` could not be read from the worktree; read it yourself before editing.\n"
    body = text
    if anchor:
        lines = text.splitlines(); start = None; level = 0
        for i, ln in enumerate(lines):
            hm = re.match(r"^(#{1,6})\s+(.*)$", ln)
            if hm and _slug(hm.group(2)) == anchor:
                start, level = i, len(hm.group(1)); break
        if start is not None:
            end = len(lines)
            for j in range(start + 1, len(lines)):
                hm = re.match(r"^(#{1,6})\s+", lines[j])
                if hm and len(hm.group(1)) <= level:
                    end = j; break
            body = "\n".join(lines[start:end])
    truncated = len(body) > EXCERPT_MAX_CHARS
    body = body[:EXCERPT_MAX_CHARS]
    return (f"\n## Required reading (excerpt of `{rel}`" + (f" §{anchor}" if anchor else "") + ")\n"
            "The rules below are part of the acceptance criteria. Do NOT re-read the whole file; open other sections only if an acceptance line cites them.\n\n"
            + body + ("\n\n[... excerpt truncated; the cited section continues in the file]" if truncated else "") + "\n")


def task_md(task: contracts.Task, task_dir: pathlib.Path, wt: pathlib.Path) -> str:
    def bl(items): return "\n".join(f"- {i}" for i in items) or "- (none)"
    return f"""# Task {task.id}: {task.summary}
Task directory: {task_dir}
Worktree: {wt}
Visual evidence required: {"yes" if task.visual else "no"}
Stage timeout: {task.timeout_s // 60} minutes
May edit tests/fixtures: {"yes" if task.may_edit_tests else "no"}

## Allowed files
{bl(task.allowed_files)}
Files or globs listed here that do not exist yet are files you are expected to CREATE. Their absence is the starting point of the task, not a blocker.

## Invariants
{bl(task.invariants)}

## Out of scope
{bl(task.out_of_scope)}

## Acceptance
{bl(task.acceptance)}

## Verification commands
{bl(task.verification_commands)}

## Stop / escalate when
{bl(task.stop_when)}
{required_reading(task, wt)}
If `{task_dir}/feedback.md` exists, read it first. Write `{task_dir}/result.md` when done, even on failure -- that is an ABSOLUTE path; do not prefix it with `./`.
Do not commit or push. Never weaken an acceptance check.
"""


def task_toml(task: contracts.Task) -> str:
    def arr(xs): return "[" + ", ".join(json.dumps(x) for x in xs) + "]"
    return (f'id = {json.dumps(task.id)}\nslug = {json.dumps(task.slug)}\nsummary = {json.dumps(task.summary)}\n'
            f'allowed_files = {arr(task.allowed_files)}\nverification_commands = {arr(task.verification_commands)}\n'
            f'acceptance = {arr(task.acceptance)}\nmay_edit_tests = {str(task.may_edit_tests).lower()}\n'
            f'visual = {str(task.visual).lower()}\ntimeout_s = {task.timeout_s}\n')


# ---------------------------------------------------------------------------
# States and the forward-only transition table.
# ---------------------------------------------------------------------------

STATES = ("CREATED", "LAUNCHING", "RUNNING", "STAGE_DONE", "CLASSIFYING", "CLASSIFIED",
          "FINALIZED", "PROJECTED", "FENCING", "ORPHANED", "INTERRUPTED")

TERMINAL = {"PROJECTED", "INTERRUPTED"}

_FENCE_OR_INTERRUPT = {"FENCING", "INTERRUPTED"}

NEXT = {
    "CREATED": {"LAUNCHING"} | _FENCE_OR_INTERRUPT,
    "LAUNCHING": {"RUNNING"} | _FENCE_OR_INTERRUPT,
    "RUNNING": {"STAGE_DONE"} | _FENCE_OR_INTERRUPT,
    "STAGE_DONE": {"LAUNCHING", "CLASSIFYING"} | _FENCE_OR_INTERRUPT,
    "CLASSIFYING": {"CLASSIFIED"} | _FENCE_OR_INTERRUPT,
    "CLASSIFIED": {"FINALIZED"},
    "FINALIZED": {"PROJECTED"},
    "FENCING": {"ORPHANED"},
    "ORPHANED": {"INTERRUPTED", "FENCING"},
    "PROJECTED": set(),
    "INTERRUPTED": set(),
}


# ---------------------------------------------------------------------------
# The attempt record.
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Record:
    attempt_id: str
    lineage: str
    generation: str
    n: int
    status: str
    worktree: str
    repo_id: str
    base_tree: str
    observed_tree: str | None = None
    agent: str | None = None
    model: str | None = None
    tier: str | None = None
    arm: str | None = None
    rung: dict | None = None
    run_id: str | None = None
    started_utc: str | None = None
    proc: dict | None = None
    stages: list = dataclasses.field(default_factory=list)
    outcome: str | None = None
    reason: str | None = None
    changed_paths: list = dataclasses.field(default_factory=list)
    violations: list = dataclasses.field(default_factory=list)
    next_action: str | None = None
    verify_seconds: float | None = None
    end_utc: str | None = None
    tree: str | None = None
    published: bool = False
    history: bool = False
    lifecycle: bool = False
    operator_forced: bool = False
    rev: int = 0

    # Not a dataclass field: dataclasses.fields()/asdict() never see it, so it never
    # leaks into to_json(); it is the on-disk location this record was loaded from /
    # will be rewritten to.
    def __post_init__(self):
        if not hasattr(self, "path"):
            self.path = None


def to_json(rec: Record) -> str:
    return json.dumps(dataclasses.asdict(rec), sort_keys=True)


def from_json(text: str, path=None) -> Record:
    try:
        d = json.loads(text)
    except json.JSONDecodeError as e:
        raise UnreadableRecord(f"corrupt attempt.json: {e}")
    if not isinstance(d, dict) or "worktree" not in d or "repo_id" not in d:
        raise UnreadableRecord("legacy or malformed record: missing worktree/repo_id")
    field_names = {f.name for f in dataclasses.fields(Record)}
    kwargs = {k: v for k, v in d.items() if k in field_names}
    try:
        rec = Record(**kwargs)
    except TypeError as e:
        raise UnreadableRecord(f"malformed record: {e}")
    if rec.status not in STATES:
        raise UnreadableRecord(f"unknown status: {rec.status!r}")
    rec.path = pathlib.Path(path) if path is not None else None
    return rec


def _validate_worktree(rec: Record) -> None:
    """Guard against a record whose `worktree` no longer exists, isn't absolute, or whose
    `repo_id` no longer matches that worktree's git-common-dir (e.g. the worktree was
    removed and the path recycled for an unrelated repo)."""
    wt = pathlib.Path(rec.worktree) if rec.worktree else None
    if wt is None or not wt.is_absolute() or not wt.is_dir():
        raise UnreadableRecord(f"{rec.attempt_id}: worktree/repo mismatch: worktree {rec.worktree!r} "
                                f"does not exist or is not absolute")
    try:
        actual_repo_id = _repo_id(wt)
    except ValueError as e:
        raise UnreadableRecord(f"{rec.attempt_id}: worktree/repo mismatch: {e}")
    if actual_repo_id != rec.repo_id:
        raise UnreadableRecord(f"{rec.attempt_id}: worktree/repo mismatch: recorded repo_id "
                                f"{rec.repo_id!r} != actual {actual_repo_id!r} for {wt}")


def load(adir, validate_worktree: bool = True) -> Record:
    """Load and validate the attempt record at `adir`. Always raises UnreadableRecord (never
    lets a decode/IO error escape as-is) for a corrupt/unsafe/missing file, and -- unless the
    caller opts out with `validate_worktree=False` (used by the internal CAS re-load, which
    must not require git/filesystem access just to detect staleness) -- for a record whose
    worktree/repo_id no longer resolve consistently."""
    adir = pathlib.Path(adir)
    try:
        text = safe_read(adir / "attempt.json")
        rec = from_json(text, path=adir)
        # A TERMINAL record has nothing left to do in its worktree; the worktree may legitimately be
        # gone (a disposable per-task worktree removed after landing/rollback). Validate only live ones.
        if validate_worktree and rec.status not in TERMINAL:
            _validate_worktree(rec)
        return rec
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError,
            ValueError, UnsafePath) as e:
        raise UnreadableRecord(str(e)) from e


def _check_not_stale(rec: Record) -> None:
    """Compare-and-swap guard: re-read the on-disk record (skipping worktree validation --
    this is a same-process freshness check, not a trust check) and refuse to overwrite it if
    it has moved on since `rec` was loaded. `rev` is bumped on every rewrite (transition or
    set_flags), so comparing it alone subsumes any status/n check -- two same-status
    set_flags calls that raced would otherwise pass a status/n comparison and clobber each
    other's flags. Must be called while holding `_attempt_lock(rec.path)` so the
    reload-compute-rewrite sequence is atomic across processes."""
    on_disk = load(rec.path, validate_worktree=False)
    if on_disk.rev != rec.rev:
        raise StaleRecord(
            f"{rec.attempt_id}: stale record -- on-disk is rev={on_disk.rev!r} (status={on_disk.status!r}), "
            f"in-memory is rev={rec.rev!r} (status={rec.status!r})")


@contextlib.contextmanager
def _attempt_lock(adir):
    """Exclusive fcntl lock spanning an attempt record's reload -> compute -> rewrite, so two
    processes racing on the same attempt dir are serialized rather than relying on the CAS
    check alone to detect (not prevent) the race. The lock file is created (safe_write-style,
    no-follow + O_EXCL) the first time; a pre-existing one is opened normally."""
    lock_path = pathlib.Path(adir) / ".attempt.lock"
    try:
        safe_write(lock_path, "")
    except FileExistsError:
        pass
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_path, flags)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def transition(rec: Record, to: str, **fields) -> Record:
    allowed = NEXT.get(rec.status, set())
    if to not in allowed:
        raise IllegalAttemptTransition(f"{rec.attempt_id}: {rec.status} -> {to} is not a legal transition")
    with _attempt_lock(rec.path):
        _check_not_stale(rec)
        new_rec = dataclasses.replace(rec, status=to, rev=rec.rev + 1, **fields)
        new_rec.path = rec.path
        safe_rewrite(pathlib.Path(rec.path) / "attempt.json", to_json(new_rec))
    return new_rec


def set_flags(rec: Record, **flags) -> Record:
    """Rewrite the record with updated projection flags (history/published/lifecycle) or
    other non-state fields, without a state transition."""
    with _attempt_lock(rec.path):
        _check_not_stale(rec)
        new_rec = dataclasses.replace(rec, rev=rec.rev + 1, **flags)
        new_rec.path = rec.path
        safe_rewrite(pathlib.Path(rec.path) / "attempt.json", to_json(new_rec))
    return new_rec


def maybe_project(rec: Record) -> Record:
    """FINALIZED + all three projection flags true -> PROJECTED. No-op otherwise."""
    if rec.status == "FINALIZED" and rec.published and rec.history and rec.lifecycle:
        return transition(rec, "PROJECTED")
    return rec


# ---------------------------------------------------------------------------
# Identity: attempt_id / lineage / generation.
# ---------------------------------------------------------------------------

def identity(ticket: str, task: contracts.Task, worktree, n: int) -> tuple[str, str, str]:
    lineage = f"{ticket}/{task.id}"
    canonical = str(pathlib.Path(worktree).resolve())
    raw = json.dumps(dataclasses.asdict(task), sort_keys=True) + "|" + canonical
    generation = hashlib.sha256(raw.encode()).hexdigest()[:12]
    attempt_id = f"{ticket}/{task.id}@{lineage}/{n}"
    return attempt_id, lineage, generation


def _repo_id(wt) -> str:
    r = subprocess.run(["git", "-C", str(wt), "rev-parse", "--git-common-dir"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise ValueError(f"not a git worktree: {wt}: {r.stderr.strip()}")
    gd = pathlib.Path(r.stdout.strip())
    if not gd.is_absolute():
        gd = pathlib.Path(wt) / gd
    return str(gd.resolve())


# ---------------------------------------------------------------------------
# Directory layout.
# ---------------------------------------------------------------------------

def attempt_dir(cfg, ticket: str, task_id: str, n: int) -> pathlib.Path:
    return cfg.state_root / "attempts" / ticket / task_id / str(n)


def next_n(root) -> int:
    root = pathlib.Path(root)
    if not root.exists():
        return 1
    nums = [int(p.name) for p in root.iterdir() if p.is_dir() and p.name.isdigit()]
    return 1 + max(nums, default=0)


# ---------------------------------------------------------------------------
# create(): writes task artifacts, then the CREATED attempt.json (the record's
# existence is what makes the attempt dir valid).
# ---------------------------------------------------------------------------

def create(cfg, ticket_key: str, task: contracts.Task, worktree, n: int, agent, arm, rung, run_id) -> Record:
    wt = pathlib.Path(worktree).resolve()
    if not wt.is_dir():
        raise ValueError(f"worktree does not exist: {wt}")
    repo_id = _repo_id(wt)
    attempt_id, lineage, generation = identity(ticket_key, task, wt, n)

    adir = attempt_dir(cfg, ticket_key, task.id, n)
    safe_mkdir(adir.parent)
    try:
        os.mkdir(adir, 0o700)
    except FileExistsError:
        raise AttemptDirExists(f"attempt dir already exists: {adir}")
    created_leaf = True

    try:
        safe_write(adir / "task.toml", task_toml(task))
        safe_write(adir / "task.md", task_md(task, adir, wt))

        task_level = adir.parent
        feedback = task_level / "feedback.md"
        # No exists() pre-check (that's a TOCTOU race against a symlink swap); safe_read
        # itself does the no-follow check and raises UnsafePath for a symlink/non-regular
        # file, which we deliberately let propagate -- a booby-trapped feedback.md must
        # fail attempt creation, not be silently skipped. A genuinely absent file raises
        # the ordinary FileNotFoundError, which is the only thing "no feedback" means.
        try:
            text = safe_read(feedback)
        except FileNotFoundError:
            text = None
        if text is not None:
            safe_write(adir / "feedback.md", text)

        body = getattr(agent, "body", "") if not isinstance(agent, str) else ""
        safe_write(adir / "body.md", body)
        safe_write(adir / "prompt.md",
                   f"Your task file is {adir / 'task.md'}. Read it, then begin.\n"
                   f"When done, write your result to this ABSOLUTE path (starts with '/', no './' prefix):\n"
                   f"{adir / 'result.md'}\n")

        base_tree = worktree_mod.snapshot(wt)
        safe_write(adir / "base_tree", base_tree)

        agent_name = agent if isinstance(agent, str) else getattr(agent, "name", None)
        rung_dict = dataclasses.asdict(rung) if dataclasses.is_dataclass(rung) else rung

        rec = Record(
            attempt_id=attempt_id, lineage=lineage, generation=generation, n=n, status="CREATED",
            worktree=str(wt), repo_id=repo_id, base_tree=base_tree,
            agent=agent_name, arm=arm, rung=rung_dict, run_id=run_id, started_utc=_now(),
            proc=None, stages=[],
        )
        rec.path = adir
        safe_write(adir / "attempt.json", to_json(rec))
        return rec
    except BaseException:
        if created_leaf:
            shutil.rmtree(adir, ignore_errors=True)
        raise
