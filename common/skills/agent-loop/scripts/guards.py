"""Guard soundness: prove that a task's verification commands can tell right work from wrong
BEFORE they are allowed to reject a worker, and re-check them AFTER a rejection so a wrong guard
is charged to the plan, not the worker.

The defect class this closes (top cause of lost time 2026-09-14/15):
  * ZIP-7875: `! grep -E 'class_name\\b'` -- rejected 4 premium attempts; matched `class_names(`,
    the helper the skill REQUIRES. The guard could never pass on correct code.
  * ZIP-7872: "regenerated .rubocop/zui_todo.yml changes only Nav-Link lines" -- true when the plan
    was written, false once origin/main moved. The guard failed for reasons unrelated to the task.
  * ZIP-7872 (night before): 38 probe commands under planning/ with bugs of their own.

Two checks, both deterministic, both cheap relative to a premium attempt:

1. `precheck(task, wt)` at plan approval, on the BASE tree (task not yet done):
   - a NEGATIVE guard (starts with `!`, or is a `test !`/`grep -L`/`diff`-style absence check) is
     expected to FAIL on the base tree when it asserts "the task's output has property P"; if it
     already passes, it tests nothing. It is expected to PASS when it asserts "the base's property
     Q still holds" (e.g. "no content_tag anywhere") -- so we do not require a direction; we only
     record the base outcome and require that the guard be RUNNABLE (rc in {0,1}, not 127/2).
   - any guard that cannot run (command not found, syntax error, missing tool) is a violation.
2. `attribute_rejection(task, cmd, wt)` when a verification command rejects an attempt: run the
   same command on a pristine checkout of the attempt's BASE tree. If it ALSO fails there, the
   failure is independent of the worker's work -> the guard is wrong ("environmental" for the
   worker; "revise" for the plan). If it passes on base and fails on the work, the worker is
   charged as before.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import tempfile

RUN_TIMEOUT_S = 600
UNRUNNABLE_RC = {2, 126, 127}   # sh syntax / not executable / command not found


def _sh(cmd: str, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-lc", cmd], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=RUN_TIMEOUT_S)


def is_negative(cmd: str) -> bool:
    c = cmd.strip()
    return c.startswith("!") or bool(re.search(r"\btest\s+!|\bgrep\s+-[a-zA-Z]*L\b|\b\[\s+!\s", c))


def is_side_effecting(cmd: str) -> bool:
    """Commands that mutate the tree or need the whole environment are not pre-checked on base."""
    return bool(re.search(r"\b(rails test|rails runner|rspec|yarn build|yarn install|bundle exec ruby linters/|generate_zui_todo|wt prepare|figma connect|git (commit|push|add))\b", cmd))


def precheck(task, wt) -> list[str]:
    """Run each cheap, side-effect-free guard on the current (base) tree. Returns violations."""
    problems = []
    for cmd in task.verification_commands:
        if is_side_effecting(cmd):
            continue
        r = _sh(cmd, wt)
        if r.returncode in UNRUNNABLE_RC:
            problems.append(f"task {task.id}: guard is not runnable on the base tree (rc={r.returncode}): {cmd[:110]!r} :: {(r.stderr or r.stdout).strip()[:160]}")
    return problems


def base_checkout(wt, base_tree: str) -> tempfile.TemporaryDirectory:
    """A disposable directory containing exactly `base_tree` (the attempt's pre-work snapshot)."""
    tmp = tempfile.TemporaryDirectory(prefix="al-base-")
    r = subprocess.run(["git", "-C", str(wt), "archive", "--format=tar", base_tree], capture_output=True)
    if r.returncode:
        tmp.cleanup(); raise RuntimeError(f"git archive {base_tree[:10]} failed: {r.stderr.decode(errors='replace')[:200]}")
    subprocess.run(["tar", "-x", "-C", tmp.name], input=r.stdout, check=True)
    return tmp


def _referenced_paths(cmd: str) -> list[str]:
    return re.findall(r"\b((?:app|test|config|lib|linters|spec|db)/[\w./-]+)", cmd)


def attribute_rejection(task, cmd: str, wt, base_tree: str) -> str:
    """Who is wrong when `cmd` rejected an attempt?
      'worker'  -- cmd passes on the base tree (the change broke it), OR cmd fails on base only
                   because it inspects files the task CREATES (absent on base -> expected).
      'guard'   -- cmd is unrunnable, or fails on base while every path it inspects already exists
                   there (the failure is independent of the worker's work).
      'unknown' -- side-effecting / environment-bound command; not attributable this way."""
    if is_side_effecting(cmd):
        return "unknown"
    try:
        tmp = base_checkout(wt, base_tree)
    except Exception:  # noqa: BLE001
        return "unknown"
    try:
        d = pathlib.Path(tmp.name)
        for rel in ("node_modules", ".wt.d", "config/master.key", "app/assets/builds"):
            src = pathlib.Path(wt) / rel
            if src.exists() and not (d / rel).exists():
                (d / rel).parent.mkdir(parents=True, exist_ok=True); (d / rel).symlink_to(src)
        r = _sh(cmd, d)
        if r.returncode == 0:
            return "worker"
        paths = _referenced_paths(cmd)
        if paths and not all((d / p).exists() for p in paths):
            return "worker"                      # inspects the task's own (not-yet-existing) output; base failure expected
        if r.returncode in UNRUNNABLE_RC:
            return "guard"                       # missing tool / syntax error, with every referenced path present
        return "guard"                           # fails on base with everything present: independent of the work
    finally:
        tmp.cleanup()
