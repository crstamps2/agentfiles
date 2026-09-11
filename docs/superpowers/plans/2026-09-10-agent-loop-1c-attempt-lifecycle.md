# Agent Loop — Plan 1c: Attempt Lifecycle and Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the runner's patched recovery with the designed attempt state machine: one durable `attempt.json` per attempt as the source of truth, three idempotent projections, global pre-dispatch reconciliation under a held lease, process identity that fences instead of killing on any doubt, and a 40-row crash-window test table as the acceptance contract.

**Architecture:** Three new modules carry the design — `procid.py` (process identity classification), `attempt.py` (the record, its states, and atomic transitions), `reconcile.py` (global recovery + projections) — and `runner.py` is rewritten to drive `_attempt` through `attempt.py` transitions and to require a `RunContext` from `reconcile.py`. `worktree.py`, `metrics.py`, `locks.py`, and `ladder.py` get the contract changes the design names. No new external dependencies.

**Tech Stack:** Python 3.14 stdlib (`/opt/homebrew/bin/python3`), `unittest`, git, macOS `ps`/`sysctl`. Tests run from `common/skills/agent-loop/scripts/` with `python3 -m unittest discover -p 'test_*.py'` (currently 239 OK).

**Spec:** `docs/superpowers/specs/2026-09-10-agent-loop-1c-attempt-lifecycle-design.md` (binding for this plan; it amends `2026-09-10-zip-6774-agent-loop-design.md`). The design's **Acceptance criteria** table is the definition of done; Task 9 implements it row by row.

## Global Constraints

- Branch `agent-loop-1` in `~/workspace/agentfiles` (head `a50e159`). Commit per task; never push; never merge. Plan/spec changes are `git add -f`.
- **The one rule:** `attempt.json` is the single source of truth. History, metrics rows, the fence, and lifecycle transitions are projections recomputed idempotently from it. No projection is written before the `attempt.json` change that justifies it.
- Every `attempt.json` transition is a single atomic rewrite via the no-follow/unique-temp helper. The only admitted cross-file ordering is fence creation, guarded by the `FENCING` state.
- Process groups are signaled **only** when classified `ours-alive` (boot_id + leader pid + start_time + cmd all match). `dead` = `killpg` → `ESRCH`. Everything else is `unknown` → fence, never signal.
- Reconciliation is global (all tickets, all tasks, under the held runner lease) and runs before any dispatch. `implement_task` requires a `RunContext`; there is no un-reconciled entry point.
- An unreadable, malformed, symlinked, or legacy (missing `worktree`/`repo_id`) `attempt.json` makes the runner exit 3. Recovery never skips a record it cannot understand.
- Every runner read of a worker-writable path and every runner write into `attempts/` uses the no-follow helpers; a symlink found is a `protocol` outcome recorded before any bytes move.
- Snapshot includes ignored files present in the tree; restore removes ignored files not in `base_tree`; `tree=restored` is set only after `snapshot(worktree) == base_tree` is verified.
- Metrics: one row per stage keyed `(attempt_id, stage_kind, idx)`; torn-tail repair truncates from the first invalid trailing record; interior corruption fails closed.
- Stdlib only. Existing tests may be **refactored** to go through `RunContext` (the design mandates it) but may not be weakened; every removed assertion is named in the task report.

---

## File Structure

Create (all under `common/skills/agent-loop/scripts/`):

- `procid.py` — `ProcId` dataclass; `capture(pid) -> ProcId`; `classify(recorded: ProcId | None) -> "ours-alive" | "dead" | "unknown"`.
- `attempt.py` — `STATES`, `Record` dataclass, `load/create/transition`, path helpers, `safe_open_read/safe_write/safe_rewrite` (moves the no-follow helpers out of `runner.py`), `attempt_id/lineage/generation`.
- `reconcile.py` — `reconcile(cfg, run_id) -> RunContext`; the fence check, the sweep, INTERRUPTED/CLASSIFIED/FENCING handling; `project_history/project_metrics/project_lifecycle`; `finalize(record)`.
- `test_procid.py`, `test_attempt.py`, `test_reconcile.py`, `test_crash_windows.py` (the 40-row table).

Modify:

- `locks.py` — `Lease.acquire(hold=True)` keeps the `flock` fd open for the lease's life.
- `worktree.py` — `snapshot` uses `add -A --force`; `restore` cleans ignored extras; `verify_restored`.
- `metrics.py` — key on `(attempt_id, stage_kind, idx)`; repair scans to the last parseable record; interior corruption raises `MetricsCorrupt`.
- `ladder.py` — `append_feedback` via `attempt.safe_write`; `next_action(attempts, arm, outcome) -> str`.
- `runner.py` — `_attempt` rewritten as state transitions; `_recover`, `_publish_row`, `_fence`, `_clear_or_honor_fence`, `_write_artifact`, `_rewrite_artifact` deleted (moved/replaced); `implement_task(ctx, ...)`; `main` calls `reconcile` first; `clear-fence` rewritten.
- `config.py` — `[local].model` regex `-ctx32k:`; empty model with table present is an error; `Config.local_model` consumed by the launcher check.
- `test_runner.py`, `test_final_fix_wave.py`, `test_worktree.py`, `test_metrics.py`, `test_locks.py`, `test_config.py` — refactor to `RunContext`, extend for new contracts.
- `SKILL.md` — states, `reconcile` on start, `clear-fence` semantics + break-glass, trust boundary paragraph.

---

### Task 1: Process identity (`procid.py`)

**Files:** Create `procid.py`, `test_procid.py`

**Interfaces:**
- `ProcId(boot_id: str, pgid: int, pid: int, start_time: str, cmd: str)` frozen dataclass; `to_dict()/from_dict()`.
- `procid.capture(pid: int) -> ProcId` — `boot_id` from `locks.boot_id()`; `pgid` from `os.getpgid`; `start_time` and `cmd` from `ps -o lstart=,comm= -p <pid>` (one call; parse the fixed-width `lstart` as the leading 24 chars, `comm` as the remainder stripped).
- `procid.classify(rec: ProcId | None) -> str` — `None` → `"dead"`. Rules from the spec's *Attempt record* section: boot_id ≠ ours → `"unknown"`; `killpg(pgid, 0)` → `ESRCH` → `"dead"`; `EPERM` → `"unknown"`; group exists: `ps -p rec.pid` succeeds AND `lstart`/`comm` equal the recorded → `"ours-alive"`; leader pid gone or mismatch while the group exists → `"unknown"`.

- [ ] **Step 1: Write the failing tests**

```python
# test_procid.py
import os, subprocess, sys, time, unittest
from unittest.mock import patch
import locks, procid

class CaptureTests(unittest.TestCase):
    def test_capture_self_has_all_fields(self):
        p = procid.capture(os.getpid())
        self.assertEqual(p.boot_id, locks.boot_id()); self.assertEqual(p.pid, os.getpid())
        self.assertEqual(p.pgid, os.getpgid(os.getpid())); self.assertTrue(p.start_time); self.assertTrue(p.cmd)
        self.assertEqual(procid.ProcId.from_dict(p.to_dict()), p)

class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.p = subprocess.Popen(["sleep", "60"], start_new_session=True); time.sleep(0.1)
        self.rec = procid.capture(self.p.pid)
    def tearDown(self):
        try: os.killpg(self.rec.pgid, 9); self.p.wait(timeout=2)
        except Exception: pass

    def test_live_matching_leader_is_ours_alive(self):
        self.assertEqual(procid.classify(self.rec), "ours-alive")
    def test_none_is_dead(self):
        self.assertEqual(procid.classify(None), "dead")
    def test_dead_group_is_dead(self):
        os.killpg(self.rec.pgid, 9); self.p.wait(timeout=2); time.sleep(0.1)
        self.assertEqual(procid.classify(self.rec), "dead")
    def test_other_boot_id_is_unknown(self):
        rec = procid.ProcId(boot_id="other-boot", pgid=self.rec.pgid, pid=self.rec.pid, start_time=self.rec.start_time, cmd=self.rec.cmd)
        self.assertEqual(procid.classify(rec), "unknown")
    def test_start_time_mismatch_is_unknown(self):
        rec = procid.ProcId(**{**self.rec.to_dict(), "start_time": "Thu Jan  1 00:00:00 1970"})
        self.assertEqual(procid.classify(rec), "unknown")
    def test_cmd_mismatch_is_unknown(self):
        rec = procid.ProcId(**{**self.rec.to_dict(), "cmd": "definitely-not-sleep"})
        self.assertEqual(procid.classify(rec), "unknown")
    def test_leader_gone_group_populated_is_unknown(self):
        # spawn a leader that forks a child then exits: group stays populated without its leader
        code = "import os,time,subprocess; subprocess.Popen(['sleep','60']); time.sleep(0.2)"
        p = subprocess.Popen([sys.executable, "-c", code], start_new_session=True); time.sleep(0.1)
        rec = procid.capture(p.pid); p.wait(timeout=5); time.sleep(0.2)
        try:
            self.assertEqual(procid.classify(rec), "unknown")
        finally:
            try: os.killpg(rec.pgid, 9)
            except ProcessLookupError: pass
    def test_eperm_is_unknown(self):
        with patch("procid.os.killpg", side_effect=PermissionError):
            self.assertEqual(procid.classify(self.rec), "unknown")

if __name__ == "__main__": unittest.main()
```

- [ ] **Step 2: Run** `python3 -m unittest test_procid -v` → FAIL `ModuleNotFoundError`.
- [ ] **Step 3: Write `procid.py`**

```python
"""Process-group identity that fences on doubt. Signal only what is provably ours and alive."""
from __future__ import annotations
import dataclasses
import os
import subprocess
import locks


@dataclasses.dataclass(frozen=True)
class ProcId:
    boot_id: str
    pgid: int
    pid: int
    start_time: str
    cmd: str
    def to_dict(self) -> dict: return dataclasses.asdict(self)
    @staticmethod
    def from_dict(d: dict) -> "ProcId": return ProcId(**{k: d[k] for k in ("boot_id", "pgid", "pid", "start_time", "cmd")})


def _ps(pid: int) -> tuple[str, str] | None:
    r = subprocess.run(["ps", "-o", "lstart=,comm=", "-p", str(pid)], capture_output=True, text=True)
    line = r.stdout.strip("\n")
    if r.returncode != 0 or not line.strip():
        return None
    return line[:24].strip(), line[24:].strip()


def capture(pid: int) -> ProcId:
    ps = _ps(pid)
    if ps is None:
        raise ProcessLookupError(pid)
    return ProcId(boot_id=locks.boot_id(), pgid=os.getpgid(pid), pid=pid, start_time=ps[0], cmd=ps[1])


def classify(rec: ProcId | None) -> str:
    if rec is None:
        return "dead"
    if rec.boot_id != locks.boot_id():
        return "unknown"
    try:
        os.killpg(rec.pgid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "unknown"
    ps = _ps(rec.pid)
    if ps is None:
        return "unknown"            # group populated, leader gone: descendants may be writing
    if ps != (rec.start_time, rec.cmd):
        return "unknown"            # recycled pid
    return "ours-alive"
```

- [ ] **Step 4: Run** → 9 PASS. **Step 5: Commit** `git add …/procid.py …/test_procid.py && git commit -m "agent-loop: process identity classification (ours-alive/dead/unknown)"`

---

### Task 2: Attempt record and transitions (`attempt.py`)

**Files:** Create `attempt.py`, `test_attempt.py`; Modify `runner.py` (remove `_write_artifact`/`_rewrite_artifact` only after Task 6 switches callers — in THIS task just add the new module).

**Interfaces:**
- `STATES = ("CREATED","LAUNCHING","RUNNING","STAGE_DONE","CLASSIFYING","CLASSIFIED","FINALIZED","PROJECTED","FENCING","ORPHANED","INTERRUPTED")`; `TERMINAL = {"PROJECTED","INTERRUPTED"}`; `NEXT` forward map exactly as the spec's *States* section, plus `LAUNCHING|RUNNING|STAGE_DONE|CREATED|CLASSIFYING → FENCING|INTERRUPTED`, `FENCING → ORPHANED`, `ORPHANED → INTERRUPTED`, `CLASSIFIED → FINALIZED` (recovery completes it).
- `Record` dataclass with every field from the spec's *Attempt record* (defaults `None`/`[]`/`False`); `to_json/from_json` with unknown-key tolerance; `attempt_id`, `lineage`, `generation` computed by `attempt.identity(ticket, task, worktree, n) -> (attempt_id, lineage, generation)`.
- `attempt.create(cfg, ticket_key, task, worktree, n, agent, arm, rung, run_id) -> Record` — computes `worktree` canonical, `repo_id` (`git rev-parse --git-common-dir` resolved), `base_tree` (`worktree.snapshot`), writes `task.toml/task.md/body.md/prompt.md/base_tree` then `attempt.json` (state CREATED) — CREATED is only valid once the record exists.
- `attempt.transition(rec, to, **fields) -> Record` — validates `NEXT`, sets fields, atomic rewrite, returns the new record; raises `IllegalAttemptTransition`.
- `attempt.load(adir) -> Record` — raises `UnreadableRecord` on missing/malformed/symlinked/legacy (no `worktree` or `repo_id`).
- Helpers moved here from runner: `safe_write(path, text)` (O_EXCL|O_NOFOLLOW), `safe_rewrite(path, text)` (unique temp + replace + stale sweep), `safe_read(path) -> str` (O_NOFOLLOW|O_RDONLY after `lstat` regular; raises `UnsafePath` for symlink/other), `safe_mkdir(path)` (parents must be non-symlink dirs).
- `attempt.attempt_dir(cfg, ticket, task_id, n)`, `attempt.next_n(root) -> int` (1 + max existing dir number).

- [ ] **Step 1: Write the failing tests** (`test_attempt.py`): a temp git worktree with one commit; `create` writes all artifacts and a CREATED record with `worktree`, `repo_id`, `base_tree`, identity fields; `transition` forward path CREATED→…→PROJECTED and the recovery edges; illegal skip raises; `load` raises `UnreadableRecord` for missing file, corrupt JSON, a symlink at `attempt.json`, and a legacy record lacking `worktree`; `safe_read` refuses a symlink; `safe_write` refuses an existing symlink and an existing file; `safe_rewrite` tolerates a stale `*.tmp`; `safe_mkdir` refuses a symlinked parent; `identity`: same task+worktree → same lineage and generation; changed summary → same lineage, different generation; `next_n` skips gaps (dirs 1,3 → 4).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** per the interfaces (copy the two helpers from `runner.py` verbatim as the base for `safe_write/safe_rewrite`; add `safe_read`/`safe_mkdir`).
- [ ] **Step 4: Run** `test_attempt` → PASS; full suite → 239 + new. **Step 5: Commit** `agent-loop: attempt record, states, atomic transitions, no-follow I/O helpers`

---

### Task 3: Contract changes in `locks.py`, `worktree.py`, `metrics.py`, `ladder.py`, `config.py`

**Files:** Modify those five + their tests. One commit per module.

- [ ] **Step 1 (locks):** `Lease.acquire(heartbeat_s=60, hold=False)`; with `hold=True` the exclusive `flock` fd is kept open on `self._held_fd` until `release()`; a second `acquire(hold=True)` in another process blocks → make it non-blocking (`LOCK_EX|LOCK_NB`) and return `False`. Test: two `Popen` python processes; the second fails to acquire while the first holds; after the first exits without `release()` (simulated crash) the second acquires (fd closed by the kernel). Commit `agent-loop: held flock lease`.
- [ ] **Step 2 (worktree):** `snapshot`: `git add -A --force` into the temp index (after the HEAD seed) so ignored files present are captured. `restore(wt, tree)`: after `read-tree --reset -u` + `clean -fd`, compute paths in `snapshot(wt)`-before-restore that are not in `tree` and are ignored, and remove them (`git clean -fdX -- <paths>` or direct unlink of the listed paths); then `verify_restored(wt, tree) -> bool` = `snapshot(wt) == tree`. Tests: ignored `tmp/x` written after base is in `changed_paths`; restore removes it; a tracked-ignored file survives; `verify_restored` True after restore, False before. Commit `agent-loop: snapshots include ignored writes; restore verified`.
- [ ] **Step 3 (metrics):** rows carry `attempt_id`, `stage_kind`, `idx`; `has(state_root, attempt_id, stage_kind, idx) -> bool`; `append` repair: read the file, find the last offset at which a complete valid JSON line ends; if any line BEFORE that is invalid → raise `MetricsCorrupt` (no write); else truncate to that offset and append. `read_all` treats a trailing unterminated object as not committed. Tests: torn tail repaired and prior bytes identical; unterminated-but-valid tail treated as uncommitted by both `read_all` and dedupe; interior corruption raises and leaves the file untouched. Commit `agent-loop: per-stage metrics keys; repair only the trailing invalid record`.
- [ ] **Step 4 (ladder):** `append_feedback` uses `attempt.safe_write`/`safe_rewrite` (append = read-safe + rewrite); `next_action(history: list[Attempt], arm, outcome, env_failures) -> str` returns `"block"` when `outcome == "blocked"` or `next_rung(history + [this], arm) is None and outcome != "accepted"`, `"pause-env"` when `outcome == "environment" and env_failures >= 2`, else `"none"`. Tests for each branch; feedback symlink → `UnsafePath`. Commit `agent-loop: ladder next_action; feedback writes are no-follow`.
- [ ] **Step 5 (config):** regex `-ctx32k:`; `[local]` present with empty/missing `model` → `ConfigError`. Update `test_local_model_must_be_ctx_pinned` to reject `-ctx4k:`; add empty-model case. Commit `agent-loop: [local].model must be -ctx32k:`.

---

### Task 4: Reconcile — projections and finalization (`reconcile.py` part 1)

**Files:** Create `reconcile.py`, `test_reconcile.py`

**Interfaces:**
- `finalize(cfg, rec: Record) -> Record` — from `CLASSIFIED`: if `outcome in {rejected, protocol, blocked}` → `worktree.restore(rec.worktree, rec.base_tree)`; assert `verify_restored`; `tree="restored"`; else `tree="kept"`. Regenerate `diff.patch` as `git diff base_tree observed_tree` via `safe_rewrite`. → `FINALIZED`.
- `project_history(cfg, rec)`, `project_metrics(cfg, rec)`, `project_lifecycle(cfg, rec)` — each idempotent by `attempt_id` / `(attempt_id, kind, idx)` / current ticket state; each sets its flag via `transition` in place (state unchanged) — implement as `attempt.set_flags(rec, **flags)`; when all three true and state is `FINALIZED` → `PROJECTED`.
- `project_all(cfg, rec) -> Record`.

- [ ] **Step 1: Failing tests** — build a `CLASSIFIED` record by hand in a temp worktree for each outcome: `rejected` restores (worker file gone, `tree=restored`, `diff.patch` non-empty and equals `git diff base observed`); `accepted` keeps; `project_history` twice → one record; `project_metrics` for a record with `stages=[worker, verify-0]` → two rows, run twice → still two; `project_lifecycle` with `next_action="block"` → ticket `blocked`, run twice → no IllegalTransition; a record with `history=True` already → `project_history` no-op.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS. **Step 5: Commit** `agent-loop: reconcile finalization and idempotent projections`

---

### Task 5: Reconcile — the sweep and `RunContext` (`reconcile.py` part 2)

**Files:** Modify `reconcile.py`, `test_reconcile.py`

**Interfaces:**
- `class FenceExit(SystemExit)` with code 3 and a `reason`.
- `RunContext(cfg, run_id, lease: locks.Lease)` — the only way to obtain one is `reconcile(cfg, run_id)`.
- `reconcile(cfg, run_id) -> RunContext`: acquire runner lease `hold=True` (else `FenceExit("runner live")`); **fence check** per spec (`fence_path = locks/heavy.fence`, JSON `{attempt_dir, proc}`): classify proc → `ours-alive|unknown` → `FenceExit("fenced")`; `dead` → load the attempt (must be `ORPHANED`), run `interrupt(cfg, rec)` then `project_all`, then unlink fence; corrupt → `FenceExit`. **Sweep** `attempts/*/*/*/attempt.json` sorted by mtime: `load` (UnreadableRecord → `FenceExit("unreadable record …")`); per state: `FENCING` → `FenceExit`; `LAUNCHING|RUNNING|STAGE_DONE` with `proc` → classify → `ours-alive` → `procs.kill_group`; dead after → fall through; else → `fence(cfg, rec)` (transition `FENCING` → write fence via `safe_write` → transition `ORPHANED`) → `FenceExit`; `STAGE_DONE` whose last stage `timed_out` and whose current `changed_paths`/`check_allowlist` are clean → treat as classifiable (leave for the runner: transition to `CLASSIFYING` is the runner's job — reconcile marks nothing, but records `observed_tree` and sets `outcome="timeout"` path? **Ruling:** reconcile classifies it itself: `observed_tree`, `outcome="timeout"`, `reason="stage timeout (recovered)"`, `next_action` via `ladder.next_action`, → `CLASSIFIED` → `finalize` (keeps) → `project_all`); any other `CREATED..CLASSIFYING` → `interrupt`; `CLASSIFIED` → `finalize` → `project_all`; `FINALIZED` → `project_all`; `ORPHANED` with no fence file → re-fence (unless `classify` now says `dead` → `interrupt`); `INTERRUPTED|PROJECTED` with a false flag → `project_all`.
- `interrupt(cfg, rec) -> Record`: verify `repo_id` (mismatch → `FenceExit`); `observed_tree = snapshot`; restore; `verify_restored` (fail → `FenceExit`); `outcome="interrupted"`, `next_action="none"`, `tree="restored"` → `INTERRUPTED` → `project_all`.

- [ ] **Step 1: Failing tests** — each reconcile branch with hand-built records in temp worktrees; `FenceExit` code 3 for: live runner lease, FENCING record, unreadable record, repo_id mismatch, ours-alive survivor (patch `procs.kill_group` → False), unknown proc; a RUNNING record with a real `sleep` group → killed, INTERRUPTED, restored, projected; a CLASSIFIED rejected → finalized; ORPHANED with fence deleted by hand → re-fenced; ORPHANED + dead → INTERRUPTED and fence removed; two INTERRUPTED records in different tickets both projected in one call (global sweep).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS. **Step 5: Commit** `agent-loop: global reconcile with fence check, sweep, and RunContext`

---

### Task 6: Runner rewrite — `_attempt` as transitions; `implement_task(ctx, …)`

**Files:** Modify `runner.py`, `fake_worker.py` (no scenario changes expected), `test_runner.py`, `test_final_fix_wave.py`

- [ ] **Step 1: Failing tests** — refactor `RunnerHarness.run_task` to obtain `ctx = reconcile.reconcile(self.cfg, "test")` and call `r.implement_task(ctx, t, task, i, wt)`; keep every existing assertion; add: after a `pass` run the record is `PROJECTED` with `history/published/lifecycle` all true; `attempt.json` `stages` has `[worker, verify-0]` with `terminated=True`; metrics has two rows for the attempt; after `fail` the record shows `tree="restored"` and `observed_tree != base_tree`; a `blocked/owner` run has `next_action="block"` and the ticket is blocked; `implement_task` without a `RunContext` raises `TypeError`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Rewrite `_attempt`:**

```
rec = attempt.create(...)                                  # CREATED
worker stage:
  rec = transition(rec, "LAUNCHING", proc=None, stages=[{kind:"worker", idx:0, proc:None}])
  on_start(pgid): rec = transition(rec, "RUNNING", proc=procid.capture(pid).to_dict()) — pass pid via on_start(pgid, pid) (extend procs.on_start signature to (pgid, pid); update fake launcher)
  stage = self.launch(...)
  rec = transition(rec, "STAGE_DONE", stages[-1] |= {terminated, timed_out, rc, elapsed_s}, proc=None)
  if not terminated → fence(cfg, rec); raise FenceExit
classifying:
  rec = transition(rec, "CLASSIFYING", observed_tree=snapshot(wt))
  res via attempt.safe_read (UnsafePath → protocol); changed/violations; _classify
  if accepted: for each verify cmd: LAUNCHING/RUNNING/STAGE_DONE cycle as above (kind "verify", idx i); unverified → fence+exit; then recheck (Task 1b-T1 logic)
  next_action = ladder.next_action(...)
  rec = transition(rec, "CLASSIFIED", outcome, reason, changed_paths, violations, next_action, verify_seconds, end_utc)
rec = reconcile.finalize(cfg, rec)                         # FINALIZED (restore/keep + diff.patch)
rec = reconcile.project_all(cfg, rec)                      # PROJECTED
return rec.outcome, rec.reason
```

`implement_task(self, ctx, t, task, stratum_index, wt)` asserts `isinstance(ctx, reconcile.RunContext)`; history/ladder rebuilt from `t.attempts[lineage]`; the loop body no longer saves state or appends rows itself — projections did. Delete `_recover`, `_publish_row`, `_fence`, `_clear_or_honor_fence`, `_write_artifact`, `_rewrite_artifact`, `_attempt_root` (use `attempt.attempt_dir`).

- [ ] **Step 4: Run** full suite → PASS (report every test whose assertions changed and why). **Step 5: Commit** `agent-loop: runner drives attempts through the state machine; implement_task requires RunContext`

---

### Task 7: `main` — reconcile first; `clear-fence` rewrite; launch-model check

**Files:** Modify `runner.py`, `test_runner.py`, `SKILL.md`

- [ ] **Step 1: Failing tests** — `dry-run` on a state root with a RUNNING record and a live `sleep` group → the group is dead before the fake worker launches (assert via a spy on `procs.kill_group` order vs launcher); `dry-run` with an `ORPHANED` record + live fence → rc 3, nothing launched; `clear-fence` while another process holds the runner lease → rc 3, stderr names the pid; `clear-fence` on a `_fence()`-produced dead fence → attempt INTERRUPTED + projected, fence gone; `clear-fence --force` on a live group → fence gone, attempt `operator_forced=True`, tree NOT restored; next `dry-run` → rc 3 re-fenced; rendered `local-worker` model ≠ `cfg.local_model` → `dry-run` fails before launch with a message naming both.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement:** `main`: for every subcommand except `status`, `ctx = reconcile.reconcile(cfg, run_id)` (catch `FenceExit` → print reason, return 3); `dry-run`/`run-once` use `ctx`; `clear-fence` implemented in `reconcile.clear_fence(cfg, force) -> int`; before the first launch of a `local-worker` rung, assert `agentdef.load(...).model == cfg.local_model` (only when `cfg.local_model` is set). SKILL.md: states list, "every start reconciles", `clear-fence` semantics and the break-glass sequence, trust-boundary paragraph, `dry-run --skip-admission` note retained.
- [ ] **Step 4: Run** → PASS. **Step 5: Commit** `agent-loop: reconcile before dispatch; clear-fence under the lease with forced semantics; launch-model check`

---

### Task 8: Fence schema, procs `on_start(pgid, pid)`, and cleanup

**Files:** Modify `procs.py`, `test_procs.py`, `reconcile.py`

- [ ] **Step 1:** `procs.run_stage(..., on_start=None)` calls `on_start(pgid, p.pid)`; update all callers/tests. Fence file schema: `{"attempt_dir": str, "proc": ProcId.to_dict(), "reason": str}` written only by `reconcile.fence`; on read, `attempt_dir` must `resolve()` to a path under `cfg.state_root / "attempts"` else the fence is treated as corrupt (`FenceExit`); `test_reconcile` uses a fence produced by `reconcile.fence`, never hand-built, plus one test with a fence whose `attempt_dir` escapes the attempts root → exit 3. Remove `procs.group_state`/`group_alive` callers in favor of `procid.classify` where identity matters (keep `group_alive` for `kill_group`'s poll loop).
- [ ] **Step 2:** Run full suite → PASS. **Step 3: Commit** `agent-loop: on_start carries pid; single fence schema`

---

### Task 9: The crash-window table (`test_crash_windows.py`)

**Files:** Create `test_crash_windows.py`; may add tiny test seams (e.g. a `reconcile._KILL_AFTER` hook read from env in tests only) — name every seam in the report.

Implement **every row** of the spec's Acceptance table as one test, in table order, named `test_cw_NN_<slug>`. Technique per row: drive the runner through `RunnerHarness` (or a subprocess for the CLI rows) to the named boundary using a kill hook (`raise reconcile.SimulatedCrash` from a patched function at the boundary), then call `reconcile.reconcile` and assert the post-condition. Rows requiring a live group use a real `sleep` under `start_new_session=True` and kill it in `tearDown`. For "recorded pgid now belongs to an unrelated process" spawn a new session, record its `ProcId`, kill it, spawn another session and patch `procid._ps` to return the new pid's `lstart` for the old pid — assert classify → `unknown` and nothing signaled (spy `os.killpg`).

- [ ] **Step 1:** Write all 40 tests (they fail or error until the earlier tasks are complete — this task runs LAST; write the file, run it, fix any production defect the table exposes minimally and name it).
- [ ] **Step 2:** `python3 -m unittest test_crash_windows -v` → 40 PASS; full suite → PASS twice consecutively.
- [ ] **Step 3: Commit** `agent-loop: crash-window acceptance table (40 rows)`

---

### Task 10: Ledger mapping and spec status

**Files:** Modify the 1c spec (status line), `SKILL.md`

- [ ] **Step 1:** In the spec, set Status to "Implemented on `agent-loop-1` at <sha>; acceptance table green" and add a table mapping Plan 1b findings R1–R11 → the `test_cw_NN` rows that cover each.
- [ ] **Step 2:** `git add -f` the spec; commit `spec: 1c implemented; R1–R11 → crash-window rows`.

---

## Self-Review

**Spec coverage:** *The one rule* → T2/T4/T6; *Attempt record* (all fields incl. `observed_tree`, `next_action`, `lineage`, `proc`, `stages[].timed_out/rc`) → T2; *Process identity* → T1; *States* incl. `FENCING` → T2/T5; *Recovery* steps 1–3, global sweep, unreadable → exit 3, timeout-kept rule, repo_id check, verified restore → T5; `_recover` deleted, `RunContext` mandatory → T6; *Snapshot completeness* → T3; *Projections* (per-stage metrics, lineage history, `next_action` lifecycle, repair semantics) → T3/T4; *Verification stages* → T6; *No-follow reads and writes* → T2/T3 (feedback)/T6 (result.md via `safe_read`); *clear-fence* (lease, dead/alive/force/corrupt, break-glass) → T7; *Config contract* → T3/T7; *Lease semantics* → T3; *Acceptance criteria* → T9; *Trust boundary* → T7 (SKILL.md). Gap check: the spec's "Fence fields validated as single path components" — fence now stores `attempt_dir` (T8); add to T8: validate it resolves under `state_root/attempts` before use. **Added.**

**Placeholder scan:** T5 Step 1 contains an inline "Ruling:" resolving the timed-out-STAGE_DONE handling — intentional, concrete. No TBDs.

**Type consistency:** `procid.ProcId.to_dict()` is what `Record.proc` and `stages[].proc` store (T1↔T2↔T6); `procs.on_start(pgid, pid)` (T8) is what T6's `_attempt` passes to `procid.capture(pid)`; `ladder.next_action` returns the spec's four strings consumed by `project_lifecycle` (T3↔T4); `metrics.has(state_root, attempt_id, stage_kind, idx)` (T3) is what `project_metrics` (T4) calls; `reconcile.FenceExit` is caught only in `main` (T7); `attempt.safe_*` are the only I/O helpers after T6 deletes the runner's copies.
