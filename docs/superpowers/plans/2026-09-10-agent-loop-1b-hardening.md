# Agent Loop — Plan 1b: Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every finding the Plan 1 final whole-branch review and its scoped re-review left open — two P0 safety gaps, four P1 crash/persistence gaps, the fail-open thermal probe, and the missing regression tests — so `agent-loop-1` is merge-safe and Plan 2 can build on its persistence and outcome contracts.

**Architecture:** No new modules. Surgical changes to `runner.py`, `procs.py`, `worktree.py`, `metrics.py`, `admission.py`, and their tests under `common/skills/agent-loop/scripts/`, plus a one-paragraph spec correction. Every task is "write the test that reproduces the finding, watch it fail, fix, watch it pass."

**Tech Stack:** Python 3.14 stdlib (`/opt/homebrew/bin/python3`), `unittest`, git. Tests run from `common/skills/agent-loop/scripts/` with `python3 -m unittest discover -p 'test_*.py'` (currently 169 OK).

**Spec:** `docs/superpowers/specs/2026-09-10-zip-6774-agent-loop-design.md`. **Findings source:** the "Final whole-branch review" and "Final fix wave" entries in `.superpowers/sdd/2026-09-10-agent-loop-1-runner-core/progress.md`, and `.superpowers/sdd/2026-09-10-agent-loop-1-runner-core/final-fix-wave-brief.md` (C1–C9, I1–I7). Finding IDs below refer to those.

## Global Constraints

- Branch `agent-loop-1` in `~/workspace/agentfiles` (head `a37be29`). Commit per task; never push; never merge. `docs/superpowers/` is gitignored — plan/spec changes are `git add -f`.
- Stdlib only. No signature changes to any public function except where a task says so explicitly.
- The runner, not any model or worker-authored text, decides pass/fail. Violations are authoritative regardless of worker `STATUS`/`REASON`.
- Heavy lane capacity 1. A stage whose termination is not verified fences the lane; nothing dispatches past a fence while its process group is alive.
- Every attempt's diff is preserved; a rejected attempt restores the snapshot; a clean `environment` or clean `timeout` keeps the tree.
- Locks reclaimable only on dead PID / different boot-id. Unknown machine readings fail closed.
- Test double `fake_worker.py` may only gain scenarios; existing scenario behavior is fixed.
- Local worker model must be a ctx-pinned Ollama model (`*-ctx32k:*`); stock 4K-context models are rejected by config validation (Task 9).

---

## File Structure

Modify only (all under `common/skills/agent-loop/scripts/` unless noted):

- `runner.py` — `_attempt` verification path (T1), `_recover` (T2), `main` multi-task resume (T4), `_publish_row`/`_rewrite_artifact` (T5, T6), C8 verify-log writes (T7), C4 verification `on_start` (T2)
- `procs.py` — no change expected; T7 may add an `open_flags` hook
- `worktree.py` — unborn HEAD (T3)
- `metrics.py` — torn-tail repair under lock (T5)
- `admission.py` — thermal/memory unknowns (T8), `parse_therm` fail-closed
- `config.py` — local model ctx-pin validation (T9)
- `fake_worker.py` — new scenarios `verify_escape`, `env_escape_verify` (T1), `stale_result` (T2)
- `test_runner.py`, `test_worktree.py`, `test_metrics.py` (new), `test_admission.py`, `test_config.py` — regression tests per task
- `docs/superpowers/specs/2026-09-10-zip-6774-agent-loop-design.md` — residency budget line, ctx-pin requirement, thermal signal (T10)

---

### Task 1: Verification timeout cannot bypass recheck/restore (re-review P0 #2, C5)

**Files:** Modify `runner.py:311-331`, `fake_worker.py`; Test `test_runner.py`

**Defect:** In `_attempt`, when a verification command times out, `outcome` becomes `environment` and the code skips both the post-verification allowlist recheck (only run `if outcome == "accepted"`) and restore (environment is excluded). A verification command `mkdir -p bin; touch bin/oops; sleep 60` leaves `bin/oops` in the tree.

**Interfaces:** unchanged.

- [ ] **Step 1: Write the failing tests** (append to `RunnerFinalFixTests` or a new class in `test_runner.py`; reuse the existing harness helpers `self.run_task`, `self.tasks_with(...)` — if no such helper exists, add `def tasks_with(self, **overrides)` that writes a TASKS variant and returns `contracts.load_tasks(...)`)

```python
    def test_verification_timeout_with_forbidden_change_is_rejected_and_restored(self):
        tasks = self.tasks_with(verification_commands=["mkdir -p bin && touch bin/oops && sleep 60"])
        outcome, rows = self.run_task("pass", tasks=tasks)
        self.assertEqual(rows[0]["outcome"], "rejected")
        self.assertIn("forbidden change", rows[0]["reason"])
        self.assertFalse((self.wt / "bin" / "oops").exists())

    def test_verification_timeout_with_clean_tree_is_environment_and_keeps_edit(self):
        tasks = self.tasks_with(verification_commands=["sleep 60"])
        outcome, rows = self.run_task("pass", "pass", tasks=tasks)   # env does not consume a rung; 2nd env pauses
        self.assertEqual(rows[0]["outcome"], "environment")
        self.assertIn("verification timeout", rows[0]["reason"])
        self.assertTrue((self.wt / "app" / "components" / "worker_touch.rb").exists())
```

(The harness sets `heavy_stage_s = 3`, so `sleep 60` times out in 3 s.)

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m unittest test_runner -k verification_timeout -v`
Expected: first test FAILS (`environment` != `rejected`, `bin/oops` exists).

- [ ] **Step 3: Restructure the verification block in `_attempt`**

Replace the `if outcome == "accepted":` block (runner.py ~lines 311–331) with:

```python
            if outcome == "accepted":
                verify_outcome, verify_reason = "accepted", "none"
                for i, cmd in enumerate(task.verification_commands):
                    vs = procs.run_stage(["/bin/sh", "-c", cmd], wt, timeout, env,
                                         adir / f"verify-{i}.out", adir / f"verify-{i}.err")
                    verification_seconds += vs.elapsed_s
                    if not vs.terminated:
                        self._fence(vs, t, task, n)
                        verify_outcome, verify_reason = "environment", f"termination unverified pgid {vs.pgid}"
                        break
                    if vs.timed_out:
                        verify_outcome, verify_reason = "environment", f"verification timeout: {cmd}"; break
                    if vs.returncode != 0:
                        verify_outcome, verify_reason = "rejected", f"verification failed: {cmd}"; break
                # ALWAYS recheck the tree after verification ran, whatever its exit — a test that
                # times out may still have executed worker code that wrote a forbidden path.
                changed = worktree.changed_paths(wt, base)
                violations = worktree.check_allowlist(changed, task, self.cfg.protected_paths, self.cfg.test_path_globs)
                if violations:
                    outcome, reason = "rejected", "verification introduced forbidden change: " + "; ".join(violations)
                else:
                    outcome, reason = verify_outcome, verify_reason
```

And make the restore decision independent of a possibly-undefined local: just above the `diff = subprocess.run(...)` line define `violations = violations if 'violations' in locals() else []` **before** the `if stage.terminated:` branch (initialize `violations = []` next to `verification_seconds = 0.0`), then replace the two restore conditions with a single:

```python
        restore = outcome in ("rejected", "protocol", "blocked") or (outcome == "timeout" and violations)
        diff = subprocess.run(["git", "-C", str(wt), "diff", base, worktree.snapshot(wt)], capture_output=True, text=True).stdout
        self._write_artifact(adir / "diff.patch", diff)
        if restore:
            worktree.restore(wt, base)
```

Delete the dead `if outcome in (...): pass` block.

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest test_runner -v`
Expected: all PASS including the two new tests.

- [ ] **Step 5: Commit**

```bash
git add common/skills/agent-loop/scripts/runner.py common/skills/agent-loop/scripts/test_runner.py
git commit -m "agent-loop: recheck allowlist after verification regardless of exit; violations always restore"
```

---

### Task 2: Recovery never dispatches while an old group survives; verification is journaled (re-review P0 #6, C4)

**Files:** Modify `runner.py:172-194` (`_recover`), `runner.py:311-320` (verification `on_start`), `fake_worker.py`; Test `test_runner.py`

**Defects:** (a) `_recover` calls `kill_group` and ignores its return; if the group survives it still marks the attempt `interrupted` and the runner proceeds to acquire the lane and launch. (b) Verification stages run with no `on_start`, so a crash during verification leaves no PGID to reconcile. (c) A stale `result.md` in an interrupted dir is never read only because dirs are never reused — assert that explicitly.

- [ ] **Step 1: Write the failing tests**

```python
    def test_recovery_with_unkillable_group_fences_and_pauses(self):
        # Simulate: attempt.json says running with a live pgid; kill_group reports failure.
        root = self.cfg.state_root / "attempts" / "ZIP-7873" / "001" / "1"; root.mkdir(parents=True)
        sleeper = subprocess.Popen(["sleep", "60"], start_new_session=True); pgid = os.getpgid(sleeper.pid)
        (root / "attempt.json").write_text(json.dumps({"status": "running", "pgid": pgid}))
        with patch("runner.procs.kill_group", return_value=False):
            outcome, rows = self.run_task("pass")
        self.assertEqual(outcome, "paused"); self.assertEqual(self.launches, 0)
        fence = json.loads((self.cfg.state_root / "locks" / "heavy.fence").read_text())
        self.assertEqual(fence["pgid"], pgid)
        t = state.load(self.cfg.ticket_dir("ZIP-7873")); self.assertIn("fenced", t.reason)
        procs.kill_group(pgid)

    def test_recovery_kills_live_group_then_proceeds(self):
        root = self.cfg.state_root / "attempts" / "ZIP-7873" / "001" / "1"; root.mkdir(parents=True)
        sleeper = subprocess.Popen(["sleep", "60"], start_new_session=True); pgid = os.getpgid(sleeper.pid)
        (root / "attempt.json").write_text(json.dumps({"status": "running", "pgid": pgid}))
        (root / "result.md").write_text("STATUS: pass\nREASON: none\n")      # stale claim must never be read
        outcome, rows = self.run_task("fail", "pass")
        self.assertFalse(procs.group_alive(pgid))
        self.assertEqual(json.loads((root / "attempt.json").read_text())["status"], "interrupted")
        self.assertEqual([r["outcome"] for r in rows if r["stage"] == "implement" and r.get("attempt") != 1],
                         ["rejected", "accepted"])            # stale pass was NOT consumed; real attempts ran
        self.assertEqual(rows[0]["outcome"], "interrupted")

    def test_verification_stage_writes_pgid_to_attempt_json(self):
        seen = {}
        real = procs.run_stage
        def spy(argv, cwd, timeout_s, env, out, err, on_start=None):
            if argv[:2] == ["/bin/sh", "-c"]:
                seen["on_start"] = on_start
            return real(argv, cwd, timeout_s, env, out, err, on_start=on_start)
        with patch("runner.procs.run_stage", side_effect=spy):
            self.run_task("pass")
        self.assertIsNotNone(seen.get("on_start"))
        adir = self.cfg.state_root / "attempts" / "ZIP-7873" / "001" / "1"
        aj = json.loads((adir / "attempt.json").read_text())
        self.assertEqual(aj["status"], "completed"); self.assertIn("verify_pgids", aj)
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m unittest test_runner -k recovery -k verification_stage -v`
Expected: FAIL (first: `launches == 1`, no fence; third: `on_start is None`).

- [ ] **Step 3: Implement**

In `_recover`, replace the kill block:

```python
            if obj and obj.get("status") in {"launching", "running"}:
                pgids = [obj.get("pgid")] + list(obj.get("verify_pgids", []))
                survivor = None
                for pgid in [g for g in pgids if g]:
                    if procs.group_alive(int(pgid)) and not procs.kill_group(int(pgid)):
                        survivor = int(pgid); break
                if survivor is not None:
                    self._fence_path().parent.mkdir(parents=True, exist_ok=True)
                    self._rewrite_artifact(self._fence_path(), json.dumps({"pgid": survivor, "ticket": t.key,
                        "task": task.id, "attempt": adir.name, "reason": "recovery: group survived kill"}))
                    return "fenced"
                obj["status"] = "interrupted"
                ... (existing interrupted-row logic unchanged)
        return None
```

In `implement_task`, change `self._recover(t, task)` to:

```python
        if self._recover(t, task) == "fenced":
            self._save(tdir, state.transition(t, "paused", reason="fenced: recovery found a live group")); return "paused"
```

In `_attempt`, keep a mutable `verify_pgids = []` and pass an `on_start` to each verification `run_stage` that appends the pgid and rewrites `attempt.json` with `"status": "running", "verify_pgids": verify_pgids`; after the loop, the final `"completed"` rewrite includes `"verify_pgids": verify_pgids`. Move the `"completed"` rewrite to AFTER verification (it currently happens before).

- [ ] **Step 4: Run tests** → `python3 -m unittest test_runner -v` all PASS.

- [ ] **Step 5: Commit**

```bash
git add common/skills/agent-loop/scripts/runner.py common/skills/agent-loop/scripts/test_runner.py
git commit -m "agent-loop: recovery fences on unkillable group; verification stages journal their pgid"
```

---

### Task 3: Snapshot works on an unborn HEAD (re-review P1 #1)

**Files:** Modify `worktree.py:23-40`; Test `test_worktree.py`

- [ ] **Step 1: Failing test**

```python
class UnbornHeadTests(unittest.TestCase):
    def test_snapshot_and_restore_in_repo_with_no_commits(self):
        with tempfile.TemporaryDirectory() as d:
            wt = pathlib.Path(d); git(wt, "init", "-q", "-b", "main")
            (wt / "a.txt").write_text("a\n")
            base = worktree.snapshot(wt)
            (wt / "a.txt").write_text("b\n"); (wt / "new.txt").write_text("n\n")
            self.assertEqual(sorted(worktree.changed_paths(wt, base)), ["a.txt", "new.txt"])
            worktree.restore(wt, base)
            self.assertEqual((wt / "a.txt").read_text(), "a\n"); self.assertFalse((wt / "new.txt").exists())
```

- [ ] **Step 2: Run** → FAIL with `git read-tree HEAD failed`.

- [ ] **Step 3: Implement** — in `snapshot`, replace the unconditional `read-tree HEAD` with:

```python
        has_head = subprocess.run(["git", "-C", str(wt), "rev-parse", "--verify", "-q", "HEAD"],
                                  capture_output=True).returncode == 0
        if has_head:
            _git(wt, "read-tree", "HEAD", env=env)      # seed tracked-but-ignored entries
        _git(wt, "add", "-A", env=env)
```

`restore` needs the same guard on its final `git reset -q` (which fails on unborn HEAD): wrap it in `if has_head(wt)` — extract a module-level `def _has_head(wt) -> bool`.

- [ ] **Step 4: Run** `python3 -m unittest test_worktree -v` → all PASS.
- [ ] **Step 5: Commit** `git commit -am "agent-loop: snapshot/restore tolerate unborn HEAD"`

---

### Task 4: Multi-task CLI resume does not clobber history (re-review P1 #3)

**Files:** Modify `runner.py:205-215, 373-376`; Test `test_runner.py`

**Defect:** `implement_task` replaces its local `t` on resume via `state.transition`, but `main` keeps passing the original object; `transition` copies `attempts`, so task 2 saves stale history over task 1's accepted record.

- [ ] **Step 1: Failing test**

```python
    def test_paused_two_task_cli_resume_preserves_first_task_history(self):
        tasks_toml = self.cfg.state_root.parent / "tasks2.toml"
        tasks_toml.write_text(TASKS + TASKS.replace('id = "001"', 'id = "002"').replace("touch", "touch2"))
        t = state.load(self.cfg.ticket_dir("DRY-1")); t.worktree = str(self.wt)
        for s in ("spinup", "plan", "plan-review", "implement", "paused"): t = state.transition(t, s)
        state.save(self.cfg.ticket_dir("DRY-1"), t)
        with patch("runner.procs.run_stage", side_effect=self.launcher):
            self.scenarios = ["pass", "pass"]
            rc = runner.main(["--config", str(self.cfg.state_root.parent / "hopper.toml"), "dry-run",
                              "--worktree", str(self.wt), "--tasks", str(tasks_toml)])
        self.assertEqual(rc, 0)
        saved = state.load(self.cfg.ticket_dir("DRY-1"))
        self.assertEqual(len([k for k in saved.attempts if k.startswith("001@")]), 1)
        self.assertEqual(len([k for k in saved.attempts if k.startswith("002@")]), 1)
```

- [ ] **Step 2: Run** → FAIL (task 001 history missing after resume).

- [ ] **Step 3: Implement** — make `implement_task` return `(outcome, ticket)`? No — that is a signature change used by many tests. Instead, at the top of the `for task in tasks:` loop in `main`, reload: `t = state.load(cfg.ticket_dir(a.ticket)); t.worktree = a.worktree`. And in `implement_task`, after every `self._save(tdir, t)` that follows a `state.transition`, the local `t` is already the saved object — no change needed there.

- [ ] **Step 4: Run** → PASS. **Step 5: Commit** `git commit -am "agent-loop: CLI reloads ticket state between tasks"`

---

### Task 5: Torn metrics tail is repaired under the append lock (re-review P1 #4, I1)

**Files:** Modify `metrics.py`; Create `test_metrics.py`

- [ ] **Step 1: Failing tests**

```python
# test_metrics.py
import json, pathlib, tempfile, unittest
import metrics

class TornTailTests(unittest.TestCase):
    def setUp(self): self.tmp = tempfile.TemporaryDirectory(); self.root = pathlib.Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()
    def test_append_after_torn_tail_repairs_and_stays_readable(self):
        p = self.root / "metrics.jsonl"
        p.write_text(json.dumps({"a": 1}) + "\n" + '{"torn": tr')
        metrics.append(self.root, {"b": 2})
        rows = metrics.read_all(self.root)
        self.assertEqual([r.get("a", r.get("b")) for r in rows], [1, 2])
        metrics.append(self.root, {"c": 3})
        self.assertEqual(len(metrics.read_all(self.root)), 3)
    def test_read_all_skips_only_final_torn_line(self):
        p = self.root / "metrics.jsonl"; p.write_text('{"a":1}\n{"bad\n{"c":3}\n')
        with self.assertRaises(json.JSONDecodeError): metrics.read_all(self.root)
```

- [ ] **Step 2: Run** → first FAILS (`read_all` raises or returns 1 row).

- [ ] **Step 3: Implement** in `metrics.append`, under the lock, before writing:

```python
    with open(p, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0); data = f.read()
            if data and not data.endswith("\n"):
                # torn final append: drop it (it is an unpublished row.json's job to republish)
                keep = data[: data.rfind("\n") + 1]
                f.seek(0); f.truncate(); f.write(keep); f.flush()
            f.write(json.dumps(row, sort_keys=True) + "\n")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
```

(`a+` on macOS positions writes at end regardless of seek — the truncate+write sequence above is correct for that mode; verify with the test.)

- [ ] **Step 4: Run** → PASS. **Step 5: Commit** `git add …/metrics.py …/test_metrics.py && git commit -m "agent-loop: repair torn metrics tail under the append lock"`

---

### Task 6: Leftover `.tmp` cannot block recovery (re-review P1 #5)

**Files:** Modify `runner.py:115-124` (`_rewrite_artifact`); Test `test_runner.py`

- [ ] **Step 1: Failing test**

```python
    def test_rewrite_artifact_tolerates_leftover_tmp(self):
        p = self.cfg.state_root / "x.json"; runner.Runner._write_artifact(p, "{}")
        (self.cfg.state_root / "x.json.tmp").write_text("stale")
        runner.Runner._rewrite_artifact(p, '{"ok":1}')
        self.assertEqual(json.loads(p.read_text()), {"ok": 1})
        self.assertFalse(list(self.cfg.state_root.glob("x.json.*tmp*")))
```

- [ ] **Step 2: Run** → FAIL with `FileExistsError`.
- [ ] **Step 3: Implement** — use a unique temp name and clean up on failure:

```python
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            Runner._write_artifact(tmp, text); os.replace(tmp, path)
        finally:
            if tmp.exists(): tmp.unlink()
        for stale in path.parent.glob(f"{path.name}.*tmp"):      # sweep predecessors' leftovers
            try: stale.unlink()
            except OSError: pass
```

- [ ] **Step 4: Run** → PASS. **Step 5: Commit** `git commit -am "agent-loop: unique temp names for atomic rewrites; sweep stale tmp"`

---

### Task 7: Verification logs cannot follow worker-planted symlinks (C8 residual)

**Files:** Modify `procs.py:57-58`, `runner.py` verify calls; Test `test_procs.py`, `test_runner.py`

- [ ] **Step 1: Failing test** (`test_runner.py`)

```python
    def test_worker_planted_verify_log_symlink_does_not_clobber_target(self):
        outside = self.cfg.state_root.parent / "outside.txt"; outside.write_text("keep")
        adir = self.cfg.state_root / "attempts" / "ZIP-7873" / "001" / "1"   # will be attempt 1
        # pre-plant via a scenario: fake_worker `plant_verify_symlink` creates adir/verify-0.out -> outside
        outcome, rows = self.run_task("plant_verify_symlink")
        self.assertEqual(outside.read_text(), "keep")
        self.assertIn(rows[0]["outcome"], ("rejected", "protocol"))
```

Add fake scenario `plant_verify_symlink`: does a normal `pass` edit and result, plus `os.symlink(str(pathlib.Path(os.environ["AL_OUTSIDE"])), task_dir / "verify-0.out")`; the test passes `AL_OUTSIDE` via env in `launcher`.

- [ ] **Step 2: Run** → FAIL (`outside.txt` truncated/overwritten).
- [ ] **Step 3: Implement** — in `procs.run_stage`, open logs with `os.open(path, O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW, 0o600)` wrapped in `os.fdopen(fd, "wb")`; on `FileExistsError` raise `RuntimeError(f"log path exists: {path}")`. In the runner, catch that around verification and classify as `protocol` ("worker pre-created verification artifact"). Existing tests that pass a fresh temp path are unaffected.
- [ ] **Step 4: Run** full suite → PASS. **Step 5: Commit** `git commit -am "agent-loop: stage logs are created O_EXCL|O_NOFOLLOW; planted artifacts are protocol failures"`

---

### Task 8: Thermal and memory unknowns fail closed; real thermal signal (I5, slice-1 finding)

**Files:** Modify `admission.py`; Test `test_admission.py`

**Slice-1 fact:** `pmset -g therm` on this Mac prints no `CPU_Speed_Limit` line → `parse_therm("")` returns False → not limited → fails OPEN.

- [ ] **Step 1: Failing tests**

```python
    def test_therm_missing_line_is_unknown_not_ok(self):
        self.assertIsNone(admission.parse_therm("Note: No thermal warning level has been recorded\n"))
        self.assertIsNone(admission.parse_therm(""))
        self.assertFalse(admission.parse_therm("CPU_Speed_Limit = 100"))
        self.assertTrue(admission.parse_therm("CPU_Speed_Limit = 60"))
    def test_xcpm_thermal_level_used_when_pmset_silent(self):
        r = self.r(therm_limited=None, thermal_level=0); self.assertTrue(admission.decide(r, TH).ok)
        r = self.r(therm_limited=None, thermal_level=3); self.assertFalse(admission.decide(r, TH).ok)
        r = self.r(therm_limited=None, thermal_level=None)
        self.assertIn("thermal: unknown", admission.decide(r, TH).reasons[0])
```

(`Reading` gains `thermal_level: int | None` from `sysctl -n machdep.xcpm.cpu_thermal_level`; update `self.r(...)` helper defaults with `thermal_level=0`.)

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** — `parse_therm` returns `None` when no `CPU_Speed_Limit` line; `probe` adds `thermal_level` via `sysctl -n machdep.xcpm.cpu_thermal_level` (int or None); `decide`: `therm_limited is True` → red; `therm_limited is None and thermal_level is None` → red "thermal: unknown"; `thermal_level is not None and thermal_level > 0` → red `f"thermal: xcpm level {n}"`. Keep the compressor/load/disk unknown handling from the fix wave; additionally, `parse_vm_stat`/`parse_uptime` return `None` (not 0.0) when their regex does not match.
- [ ] **Step 4: Run** `python3 -m unittest test_admission -v` → PASS; run `python3 -c "import admission; print(admission.probe('/tmp'))"` on this machine and confirm `thermal_level` is an int.
- [ ] **Step 5: Commit** `git commit -am "agent-loop: thermal unknown fails closed; xcpm thermal level as the signal"`

---

### Task 9: Config rejects non-ctx-pinned local models (slice-1 finding)

**Files:** Modify `config.py`, `hopper.toml`; Test `test_config.py`

- [ ] **Step 1: Failing test**

```python
    def test_local_model_must_be_ctx_pinned(self):
        self.path.write_text(MINI.format(root=self.tmp.name + "/state") + '\n[local]\nmodel = "ollama-local/gpt-oss:20b"\n')
        with self.assertRaisesRegex(config.ConfigError, "ctx"):
            config.load(self.path)
        self.path.write_text(MINI.format(root=self.tmp.name + "/state") + '\n[local]\nmodel = "ollama-local/gpt-oss-ctx32k:20b"\nunload_after_attempt = true\n')
        self.assertEqual(config.load(self.path).local_model, "ollama-local/gpt-oss-ctx32k:20b")
```

- [ ] **Step 2: Run** → FAIL (`ConfigError` not raised / no `local_model`).
- [ ] **Step 3: Implement** — `Config` gains `local_model: str | None` and `local_unload_after_attempt: bool`; `load` reads optional `[local]`; if present and `model` does not match `r"-ctx\d+k:"`, raise `ConfigError("local model must be a ctx-pinned derived model (…-ctx32k:…); Ollama's default 4K context truncates pi's system prompt")`. Add to `hopper.toml`:

```toml
[local]
model = "ollama-local/gpt-oss-ctx32k:20b"   # MUST be ctx-pinned: stock num_ctx=4096 silently truncates pi's ~11K system prompt
unload_after_attempt = true                # runner calls `ollama stop <model>` after each local attempt (Plan 3 wires it)
```

- [ ] **Step 4: Run** → PASS. **Step 5: Commit** `git commit -am "agent-loop: [local] config with ctx-pin validation"`

---

### Task 10: Spec corrections from slice 1

**Files:** Modify `docs/superpowers/specs/2026-09-10-zip-6774-agent-loop-design.md`

- [ ] **Step 1:** In *Resources and admission control*, replace the "Local model residency budget: ~10 GB resident (14B-class at Q4 or smaller)" sentence with: "**Local model residency budget:** ≤ 12 GB resident, verified in slice 1 by `gpt-oss-ctx32k:20b` holding peak compressor at 24% (threshold 25%) alongside the machine's baseline. Local models MUST be ctx-pinned derived models (`PARAMETER num_ctx 32768`); Ollama's default 4K context silently truncates pi's system prompt and the worker never sees its tools."
- [ ] **Step 2:** In the same section's admission bullet, replace "when `pmset -g therm` reports CPU speed limit below 100" with "when `pmset -g therm` reports CPU speed limit below 100, or — because that line is absent on some Macs — when `sysctl machdep.xcpm.cpu_thermal_level` is above 0; an unknown thermal reading fails closed."
- [ ] **Step 3:** In *Roles and tiers*, update the two worker rows' model columns to `ollama-local/gpt-oss-ctx32k:20b` and `ollama-cloud/gpt-oss:20b (Free plan)`, noting deepseek/glm require purchased credits (402 on Free).
- [ ] **Step 4:** Commit: `git add -f docs/superpowers/specs/2026-09-10-zip-6774-agent-loop-design.md && git commit -m "spec: slice-1 corrections — residency budget, ctx-pinned local models, thermal signal, arm models"`

---

### Task 11: Missing regression tests from the re-review (C1, C3, I3, I4, I6, I7)

**Files:** Modify `test_runner.py`, `test_contracts.py`, `fake_worker.py` (only if a scenario is missing)

No production code changes expected; if a test exposes a real defect, fix it minimally in the same commit and say so in the report.

- [ ] **Step 1: C1 — dry-run never invokes real `pi`, proven from a subprocess**

```python
    def test_dry_run_cli_subprocess_never_calls_pi(self):
        shim = self.cfg.state_root.parent / "shim"; shim.mkdir()
        sentinel = shim / "PI_WAS_CALLED"
        (shim / "pi").write_text(f"#!/bin/sh\ntouch {sentinel}\nexit 1\n"); (shim / "pi").chmod(0o755)
        env = {**os.environ, "PATH": f"{shim}:{os.environ['PATH']}", "AL_SCENARIO": "pass"}
        r = subprocess.run([sys.executable, str(HERE / "runner.py"), "--config", str(self.cfg.state_root.parent / "hopper.toml"),
                            "dry-run", "--worktree", str(self.wt), "--tasks", str(self.cfg.state_root.parent / "tasks.toml")],
                           capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(sentinel.exists())
        self.assertEqual(metrics.read_all(self.cfg.state_root)[-1]["outcome"], "accepted")
```

- [ ] **Step 2: C3 — fence re-entry**

```python
    def test_fence_with_dead_pgid_is_cleared_and_run_proceeds(self):
        child = subprocess.run([sys.executable, "-c", "import os;print(os.getpgid(0))"], capture_output=True, text=True)
        (self.cfg.state_root / "locks").mkdir(exist_ok=True)
        (self.cfg.state_root / "locks" / "heavy.fence").write_text(json.dumps({"pgid": int(child.stdout)}))
        outcome, rows = self.run_task("pass")
        self.assertEqual(outcome, "accepted"); self.assertFalse((self.cfg.state_root / "locks" / "heavy.fence").exists())

    def test_fence_with_live_pgid_pauses(self):
        sleeper = subprocess.Popen(["sleep", "60"], start_new_session=True); pgid = os.getpgid(sleeper.pid)
        (self.cfg.state_root / "locks").mkdir(exist_ok=True)
        (self.cfg.state_root / "locks" / "heavy.fence").write_text(json.dumps({"pgid": pgid}))
        outcome, rows = self.run_task("pass")
        self.assertEqual(outcome, "paused"); self.assertEqual(self.launches, 0)
        procs.kill_group(pgid)
```

- [ ] **Step 3: I3 — per-field manifest validation** (`test_contracts.py`)

```python
    def test_manifest_rejects_each_bad_field(self):
        good = {"id": "001", "slug": "s", "summary": "s", "allowed_files": ["a/**"], "verification_commands": ["true"], "acceptance": ["a"]}
        bad = {"allowed_files": [7], "verification_commands": [""], "acceptance": [None], "timeout_s": True,
               "figma_nodes": [1], "allowed_files": ["/abs"], "allowed_files": ["../x"], "invariants": "notalist"}
        for k, v in [("allowed_files", [7]), ("verification_commands", [""]), ("acceptance", [None]), ("timeout_s", True),
                     ("figma_nodes", [1]), ("allowed_files", ["/abs"]), ("allowed_files", ["../x"]), ("invariants", "notalist")]:
            with self.subTest(field=k, value=v):
                errs = contracts.validate_tasks({"tasks": [{**good, k: v}]})
                self.assertTrue(any(k in e for e in errs), errs)
        self.assertEqual(contracts.validate_tasks({"tasks": "notalist"}), ["tasks: must be a list of tables"][:1] or contracts.validate_tasks({"tasks": "notalist"}))
```

(Keep only the `for` loop and a separate `assertTrue(contracts.validate_tasks({"tasks": "notalist"}))`; drop the `bad` dict — it was scaffolding.)

- [ ] **Step 4: I4 — transport failure classification**

```python
    def test_no_result_with_429_stderr_is_environment(self):
        # scenario `transport_429`: fake writes "HTTP 429 Too Many Requests" to stderr, no result.md, exits 1
        outcome, rows = self.run_task("transport_429", "transport_429")
        self.assertEqual([r["outcome"] for r in rows], ["environment", "environment"]); self.assertEqual(outcome, "paused")
```

Add fake scenario `transport_429`: `print("HTTP 429 Too Many Requests", file=sys.stderr); sys.exit(1)` before any edit.

- [ ] **Step 5: I6 — stratified arms and persistence**

```python
    def test_stratified_arms_for_mixed_visual_flags(self):
        toml = TASKS
        for i, vis in (("002", "true"), ("003", "false"), ("004", "true")):
            toml += TASKS.replace('id = "001"', f'id = "{i}"').replace("touch", f"touch{i}").replace('acceptance = ["AC-1"]', f'acceptance = ["AC-1"]\nvisual = {vis}')
        tasks_toml = self.cfg.state_root.parent / "tasks4.toml"; tasks_toml.write_text(toml)
        with patch("runner.procs.run_stage", side_effect=self.launcher):
            self.scenarios = ["pass"] * 4
            runner.main(["--config", str(self.cfg.state_root.parent / "hopper.toml"), "dry-run", "--worktree", str(self.wt), "--tasks", str(tasks_toml)])
        arms = [r["arm"] for r in metrics.read_all(self.cfg.state_root) if r["stage"] == "implement"]
        self.assertEqual(arms, ["local", "local", "cloud", "cloud"])   # hopper alternate is ["local","cloud"]; strata F,T,F,T

    def test_arm_persists_across_alternate_change(self):
        self.run_task("fail")                                       # attempt 1 on the stratum-0 arm (local)
        object.__setattr__(self.cfg, "arms_alternate", ["cloud", "local"])
        outcome, rows = self.run_task("pass")
        self.assertEqual({r["arm"] for r in rows}, {"local"})
```

- [ ] **Step 6: I7 — timeout keeps the partial edit**: in `test_timeout_kills_and_advances_keeping_clean_tree`, after the timeout row assert add `self.assertTrue((self.wt / "app" / "components" / "worker_touch.rb").exists())` **before** the second scenario runs — restructure to `run_task("timeout")` alone (expect outcome `paused`? no: timeout advances the ladder, so run `("timeout", "fail", "fail")` and assert the file's content is the timeout scenario's text after row 0).

- [ ] **Step 7: Run** `python3 -m unittest discover -p 'test_*.py'` → all PASS. **Step 8: Commit** `git commit -am "agent-loop: regression tests for C1, C3, I3, I4, I6, I7"`

---

## Self-Review

**Spec coverage vs. the open findings:**

| Finding | Task |
|---|---|
| re-review P0 #2 verification timeout bypass / C5 | 1 |
| re-review P0 #6 recovery dispatch with live group / C4 verify pgid / C4 stale result | 2 |
| re-review P1 #1 unborn HEAD | 3 |
| re-review P1 #3 multi-task resume clobber / I2 | 4 |
| re-review P1 #4 torn metrics tail / I1 | 5 |
| re-review P1 #5 leftover .tmp | 6 |
| C8 verify-log symlink | 7 |
| I5 thermal/memory unknown fail-open + slice-1 `pmset` finding | 8 |
| slice-1 ctx-pin requirement | 9, 10 |
| C1 subprocess CLI test with `pi` shim | 11 |
| C3 fence re-entry tests | 11 |
| I3 / I4 / I6 / I7 regression tests | 11 |

**Placeholder scan:** Task 4 Step 3 contains a rejected alternative ("make `implement_task` return a tuple? No") — acceptable as rationale, the chosen implementation is concrete. No TBDs.

**Type consistency:** `Reading.thermal_level` (T8) must be added to every `Reading(...)` construction in `test_admission.py` and `admission.probe`; `Config.local_model` (T9) is optional so existing `MINI` fixtures still load. `_recover` return value `"fenced" | None` (T2) is consumed in `implement_task` only.
