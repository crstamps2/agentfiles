"""Run independent tasks of one ticket concurrently, each in its own git worktree off the ticket
branch, and land accepted diffs onto the ticket worktree in manifest order.

Why this is safe:
  * Worker stages are remote inference + edits (light); verification takes the machine-wide heavy
    lease inside the runner, so Rails/browser never run twice at once (runner.py).
  * Each task edits only its `allowed_files`; two tasks are *independent* iff those sets cannot
    match the same path AND neither names the other in `after`. Only independent tasks run at once.
  * Every task gets a disposable worktree (`<ticket-wt>/../.al-tasks/<key>/<task>/`) checked out at
    the ticket branch's current HEAD, so snapshots/restores/allowlist checks are per task.
  * Landing = `git apply --index` of the accepted attempt's diff (source paths only) onto the ticket
    worktree, then the normal publish (commit exact paths, push). Manifest order is preserved, so a
    later task's diff always lands on top of earlier ones exactly as it would have serially.

Bounded by `[arms] workers_parallel`. A task whose dependencies are not yet landed waits.
"""
from __future__ import annotations

import concurrent.futures as cf
import fnmatch
import json
import pathlib
import subprocess

import contracts
import state


def _sh(cmd, cwd, timeout=600):
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def _globs_overlap(a: str, b: str) -> bool:
    """Conservative: two allowlist entries may match the same path if either matches the other's
    literal prefix or they share a directory prefix up to the first wildcard."""
    if a == b:
        return True
    if fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a):
        return True
    pa, pb = a.split("*", 1)[0].rstrip("/"), b.split("*", 1)[0].rstrip("/")
    return bool(pa and pb and (pa.startswith(pb + "/") or pb.startswith(pa + "/") or pa == pb))


def independent(x: contracts.Task, y: contracts.Task) -> bool:
    if x.id in getattr(y, "after", []) or y.id in getattr(x, "after", []):
        return False
    return not any(_globs_overlap(a, b) for a in x.allowed_files for b in y.allowed_files)


def ready_batch(tasks: list, done_ids: set, in_flight: set, limit: int) -> list:
    """Next tasks that can run now: not done, not running, all `after` done, and pairwise independent
    with everything running and with each other. Manifest order; at most `limit - len(in_flight)`."""
    running = [t for t in tasks if t.id in in_flight]
    out = []
    for t in tasks:
        if t.id in done_ids or t.id in in_flight:
            continue
        if any(d not in done_ids for d in getattr(t, "after", [])):
            continue
        if all(independent(t, r) for r in running + out):
            out.append(t)
        if len(out) + len(in_flight) >= limit:
            break
    return out


# ----------------------------------------------------------------------------- worktrees

def task_worktree(ticket_wt: pathlib.Path, key: str, task_id: str) -> pathlib.Path:
    return ticket_wt.parent / ".al-tasks" / key.lower() / task_id


def ensure_task_worktree(ticket_wt: pathlib.Path, key: str, task_id: str) -> pathlib.Path:
    """A detached worktree at the ticket branch's current HEAD. Re-created fresh each time so it
    never carries a previous attempt's state. Shares the repo's object store (cheap)."""
    twt = task_worktree(ticket_wt, key, task_id)
    if twt.exists():
        _sh(["git", "worktree", "remove", "--force", str(twt)], ticket_wt)
    twt.parent.mkdir(parents=True, exist_ok=True)
    head = _sh(["git", "rev-parse", "HEAD"], ticket_wt).stdout.strip()
    r = _sh(["git", "worktree", "add", "--detach", str(twt), head], ticket_wt)
    if r.returncode:
        raise RuntimeError(f"git worktree add failed: {r.stderr.strip()[:300]}")
    # the task worktree needs the same untracked build/config state the tests expect
    for rel in (".wt.d", "config/master.key", "config/database.yml", ".env", ".env.local", "node_modules", "app/assets/builds"):
        src = ticket_wt / rel
        if src.exists() and not (twt / rel).exists():
            (twt / rel).parent.mkdir(parents=True, exist_ok=True)
            (twt / rel).symlink_to(src)
    return twt


def remove_task_worktree(ticket_wt: pathlib.Path, key: str, task_id: str) -> None:
    twt = task_worktree(ticket_wt, key, task_id)
    if twt.exists():
        _sh(["git", "worktree", "remove", "--force", str(twt)], ticket_wt)
    _sh(["git", "worktree", "prune"], ticket_wt)


# ----------------------------------------------------------------------------- landing

def land(cfg, key: str, ticket_wt: pathlib.Path, task: contracts.Task, attempt_dir: pathlib.Path) -> list[str]:
    """Apply the accepted attempt's diff (source paths only) to the ticket worktree. Returns the
    paths applied. Raises on conflict -- which cannot happen for independent tasks but is checked."""
    import worktree
    patch = attempt_dir / "diff.patch"
    if not patch.exists():
        raise RuntimeError(f"{attempt_dir}: no diff.patch to land")
    rec = json.loads((attempt_dir / "attempt.json").read_text())
    changed = rec.get("changed_paths") or []
    twt = pathlib.Path(rec["worktree"])
    ignored = worktree.ignored_paths(twt, changed) if twt.exists() else set()
    globs = getattr(cfg, "harness_artifact_globs", ())
    paths = [p for p in changed if p not in ignored and not any(worktree._match(p, g) for g in globs)]
    if not paths:
        return []
    args = ["git", "apply", "--index", "--3way", *[f"--include={p}" for p in paths], str(patch)]
    r = _sh(args, ticket_wt)
    if r.returncode:
        raise RuntimeError(f"landing task {task.id} conflicted: {r.stderr.strip()[:400]}")
    return paths


# ----------------------------------------------------------------------------- orchestration

def implement_parallel(cfg, runner, ctx, t, tasks: list, ticket_wt: pathlib.Path, is_done, publish_one, log=print) -> str:
    """Drive all remaining tasks. Returns "accepted" when every task is landed, else the first
    non-accepted outcome ("paused"/"blocked"). `is_done(task)` and `publish_one(task, paths)` are
    injected so this module stays free of lifecycle/publish imports."""
    limit = max(1, int(getattr(cfg, "workers_parallel", 1)))
    done = {x.id for x in tasks if is_done(x)}
    in_flight: dict[str, cf.Future] = {}
    pending_land: dict[str, tuple] = {}
    with cf.ThreadPoolExecutor(max_workers=limit) as pool:
        while True:
            busy = set(in_flight) | set(pending_land)          # finished-but-unlanded tasks must not be re-dispatched
            for task in ready_batch(tasks, done, busy, limit):
                twt = ensure_task_worktree(ticket_wt, t.key, task.id)
                idx = sum(1 for x in tasks if x.visual == task.visual and tasks.index(x) < tasks.index(task))
                log(f"{t.key} task {task.id}: dispatch (parallel, worktree {twt.name})")
                in_flight[task.id] = pool.submit(runner.implement_task, ctx, state.load(cfg.ticket_dir(t.key)), task, idx, str(twt))
            if not in_flight:
                break
            finished, _ = cf.wait(list(in_flight.values()), return_when=cf.FIRST_COMPLETED)
            for tid, fut in list(in_flight.items()):
                if fut not in finished:
                    continue
                del in_flight[tid]
                task = next(x for x in tasks if x.id == tid)
                try:
                    out = fut.result()
                except Exception as e:  # noqa: BLE001
                    out = f"error: {type(e).__name__}: {e}"
                if out != "accepted":
                    log(f"{t.key} task {tid}: {out}")
                    for f in in_flight.values(): f.cancel()
                    return out if out in ("paused", "blocked") else "paused"
                pending_land[tid] = task
            # land in MANIFEST order: only tasks whose predecessors (by order) are landed
            for task in tasks:
                earlier_landed = all(x.id in done for x in tasks[:tasks.index(task)])
                if task.id in pending_land and earlier_landed:
                    adir = _latest_accepted_attempt(cfg, t.key, task.id)
                    paths = land(cfg, t.key, ticket_wt, task, adir)
                    publish_one(task, paths)
                    remove_task_worktree(ticket_wt, t.key, task.id)
                    done.add(task.id); del pending_land[task.id]
                    log(f"{t.key} task {task.id}: landed {len(paths)} path(s)")
    return "accepted" if len(done) == len(tasks) else "paused"


def _latest_accepted_attempt(cfg, key: str, task_id: str) -> pathlib.Path:
    root = cfg.state_root / "attempts" / key / task_id
    for d in sorted((p for p in root.iterdir() if p.name.isdigit()), key=lambda p: -int(p.name)):
        rec = json.loads((d / "attempt.json").read_text())
        if rec.get("outcome") == "accepted":
            return d
    raise RuntimeError(f"no accepted attempt for {key}/{task_id}")
