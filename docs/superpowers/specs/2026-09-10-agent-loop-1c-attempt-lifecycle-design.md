# Agent Loop — Plan 1c Design: Attempt Lifecycle and Recovery

Status: Approved by Cody 2026-09-10; revised after a two-lane adversarial review
(OpenAI `cto` + `security-analyst`). Disposition of every finding is in
"Adversarial review disposition" at the end. Amends the *Per-ticket lifecycle*, *Escalation
ladder*, *Resources and admission control*, *Observability*, and *Error handling*
sections of `2026-09-10-zip-6774-agent-loop-design.md`. Motivated by three
consecutive "not mergeable" whole-branch reviews of `agent-loop-1` whose
surviving findings (R1–R6, R8–R10 in the Plan 1b ledger) all trace to one
cause: attempt recovery and finalization were patched per finding instead of
designed as one state machine.

## Goal

Make every attempt's lifecycle **crash-consistent by construction**: after a
runner death at any instruction boundary, the next runner start reaches a state
in which (a) no worker or verification process group from a prior run is alive
or unaccounted for, (b) the worktree is either the accepted result or the
recorded base, (c) ticket history, the ladder, metrics, and the lifecycle state
agree, and (d) nothing is dispatched until (a)–(c) hold — with **no behavior
that depends on the order of two writes to different files**.

## Trust boundary (stated plainly)

Workers run as Cody's UID with `bash`. They *can* rewrite `attempt.json`, the
fence, `state.json`, or `setsid` a process out of the recorded group. **This
design is therefore a crash-consistency design for cooperative workers, not a
security boundary against a hostile one.** The parent spec already defers OS
isolation; this section makes the consequence explicit so no one reads the
recovery guarantees as adversarial guarantees. The no-follow discipline below
prevents *accidental* and *model-sloppy* corruption (planted symlinks, stale
files), which is the realistic failure class for the pilot.

## Non-goals

- Defending recovery state against a deliberately hostile same-UID worker (see
  Trust boundary).
- fsync-backed power-loss durability. The design targets process death
  (crash, kill, timeout of the runner itself). Power loss is out of scope for
  the pilot and stated as a known limit.
- OS-level sandboxing of workers (spec: deferred).
- Any change to gates, PR, or review loops (Plans 2–3).

## The one rule

**`attempt.json` is the single source of truth for an attempt.** History in
`state.json`, rows in `metrics.jsonl`, the fence, and the lifecycle transition
are all *projections* of it, and every projection is recomputed idempotently
from `attempt.json` on runner start. No projection is ever written *before* the
`attempt.json` change that justifies it, and losing any projection is
recoverable by re-projecting.

## Attempt record

`attempts/<ticket>/<task>/<n>/attempt.json`, written only via the O_EXCL /
unique-tmp helpers, rewritten atomically. Fields:

```
attempt_id     "<ticket>/<task>@<lineage>/<n>"      stable identity for dedupe everywhere
lineage        "<ticket>/<task.id>"                   ladder + arm continuity key (survives re-slicing)
generation     fingerprint of (task manifest, worktree) informational; a change is LOGGED, not a reset
n              int
status         one of the states below
worktree       canonical absolute path of the worktree (validated: exists, is a git worktree)
repo_id        `git rev-parse --git-common-dir` resolved — recovery refuses to restore into a different repo
base_tree      git tree OID recorded before launch
observed_tree  git tree OID of the tree as found (set at CLASSIFYING / recovery, BEFORE any restore)
agent, model, tier, arm, rung, run_id, started_utc
proc           { boot_id, pgid, pid, start_time, cmd }  identity of the CURRENT stage's group, or null
stages         [ {kind: worker|verify, idx, proc, terminated: bool|null, timed_out: bool, rc: int|null, elapsed_s} ]
outcome, reason, changed_paths, violations     filled at CLASSIFIED
next_action    one of: none | block | pause-env | fence  — the DERIVED lifecycle decision, persisted at CLASSIFIED
verify_seconds, end_utc                         filled at CLASSIFIED
tree           one of: dirty | restored | kept        filled at FINALIZED
published      bool                                   metrics projection done (per stage, see Projections)
history        bool                                   state.json projection done
lifecycle      bool                                   ticket transition projection done
```

`observed_tree` is what makes the pre-restore diff recoverable: `diff.patch` is
always regenerable as `git diff base_tree observed_tree`, so a crash between
restore and patch-write loses nothing.

`next_action` is persisted because the lifecycle decision is **not** derivable
from one attempt in isolation ("ladder exhausted" and "environment pair" depend
on prior attempts). The runner computes it once, at CLASSIFIED, from the sorted
lineage history plus this attempt, and the projection replays only that stored
decision.

`proc.boot_id` is `locks.boot_id()`; `proc.start_time` is the group leader's
process start time (`ps -o lstart= -p <pid>`, parsed); `proc.cmd` is the
leader's command. A recorded group is classified:

- **ours-alive**: boot_id matches, leader pid exists, start_time and cmd match
  → may be signaled.
- **dead**: boot_id matches and no process in the pgid exists
  (`killpg(pgid, 0)` → `ESRCH`) → nothing to do.
- **unknown**: anything else — boot_id differs, leader gone but the pgid is
  still populated (descendants outlived the leader), start_time or cmd
  mismatch, `EPERM` → **never signaled; always fenced**.

The 1-second `lstart` resolution therefore degrades to *fencing*, never to
killing a foreign process. The cost is an occasional false fence an operator
clears.

## States

```
CREATED      dir + task artifacts + worktree/repo_id/base_tree written; proc = null
LAUNCHING    stage record appended with proc; child is gated (cannot exec yet)
RUNNING      gate released; child executing
STAGE_DONE   stage reaped: terminated/timed_out/rc persisted → next stage or CLASSIFYING
CLASSIFYING  observed_tree recorded; result parsed; changed_paths/violations computed
CLASSIFIED   outcome/reason/next_action persisted
FINALIZED    tree action done (restored | kept), diff.patch written (regenerable)
PROJECTED    history, published, lifecycle all true (each idempotent)
---
FENCING      recovery decided to fence; written BEFORE the fence file exists
ORPHANED     fence file exists for this attempt; awaiting operator
INTERRUPTED  recovery found no live group; observed_tree recorded; restored; projected
```

Terminal: `PROJECTED`, `INTERRUPTED` (after its own projection). `ORPHANED` is
terminal until `clear-fence` moves it to `INTERRUPTED`.

Transitions are strictly forward; every transition is a single atomic rewrite
of `attempt.json`. **The one admitted cross-file ordering is fence creation,
and `FENCING` exists precisely to make it recoverable:** a runner that finds
`FENCING` with no fence file must exit 3 (fail closed) — it does not know
whether the fence write failed before or after the group died.

## Recovery: global, before any dispatch

On every runner start (any subcommand except `status`), under the **global
runner lease**, before selecting a ticket or touching the heavy lane:

1. **Fence check.** If `locks/heavy.fence` exists: classify its proc.
   *ours-alive* or *unknown* → exit 3. *dead* → the fence is **not** simply
   deleted: the referenced attempt (still `ORPHANED`) is driven through
   INTERRUPTED (observed_tree → restore → projections) and only then is the
   fence removed. A corrupt fence → exit 3.
2. **Sweep every attempt dir under `attempts/`** (all tickets, all tasks), not
   just the one about to run, sorted by mtime. For each non-terminal record:
   - Malformed / unreadable / symlinked `attempt.json`, or a legacy record
     without `worktree`/`repo_id` → **exit 3** (fail closed; the operator
     inspects). Recovery never skips a record it cannot understand.
   - `LAUNCHING` / `RUNNING` / `STAGE_DONE` with a proc: classify. *ours-alive*
     → `kill_group`; if it dies → fall through. *unknown*, or *ours-alive* that
     survives the kill → set `FENCING` → write the fence → set `ORPHANED` →
     exit 3. `FENCING` found with no fence → exit 3.
   - A `STAGE_DONE` record whose last stage `timed_out` with a **clean**
     allowlist keeps the parent spec's timeout-keeps-tree rule: it proceeds to
     CLASSIFYING as a normal `timeout` outcome, not INTERRUPTED. Everything
     else in `CREATED..CLASSIFYING` becomes INTERRUPTED.
   - INTERRUPTED path: verify `repo_id` matches the worktree (else exit 3);
     record `observed_tree = snapshot(worktree)`; restore to `base_tree`;
     verify `snapshot(worktree) == base_tree` before setting `tree="restored"`;
     set `INTERRUPTED` with `outcome="interrupted"`, `next_action="none"`.
   - `CLASSIFIED` (crash before finalize): finish from the persisted outcome —
     `rejected|protocol|blocked → restore` (with the same post-restore
     verification); else keep.
   - Any record with a projection flag false → project.
3. Only then: ticket selection, `_stop` checks, admission, heavy lease, dispatch.

`_recover(t, task)` is deleted; there is one `reconcile(state_root)`, and
`implement_task` **requires** a `RunContext` produced by it (the tests that
currently call `implement_task` directly are refactored to go through the
context; there is no un-reconciled entry point).

**Snapshot completeness.** `worktree.snapshot()` must include ignored files
that exist in the tree (a worker can write an ignored path), and `restore()`
must remove ignored files that are not in `base_tree` while preserving
ignored files that are. Otherwise "restored to base" is false for ignored
content. (Implementation: `git add -A --force` into the temp index for the
snapshot; `git clean -fdx` scoped to paths present in `observed_tree` but not
`base_tree` for the restore.)

## Projections (idempotent, each keyed by `attempt_id`)

- **history**: `state.json.attempts[lineage]` gets the record `{n, rung,
  outcome, reason, arm, attempt_id, generation}` if no record with that
  `attempt_id` exists. A generation change is recorded on the record and
  logged; it does **not** reset the ladder or the arm (re-slicing a task keeps
  its cheap/premium consumption — the planner must issue a new task id to
  start over). Then `history=true`.
- **metrics**: one row **per stage** (worker, verify-0, verify-1, …) keyed by
  `(attempt_id, stage_kind, idx)`, honoring the parent spec's "one row per
  executed stage"; append each if absent. Torn-tail repair: scan back to the
  last **parseable complete JSONL record** and truncate from the first invalid
  record onward (rows are canonical JSON with escaped newlines, so a raw
  newline inside a string cannot occur in a well-formed row; if corruption is
  found *before* the tail, fail closed and alert rather than repair).
  `read_all` treats an unterminated final object as not committed, matching
  `append`. Then `published=true`.
- **lifecycle**: replay the persisted `next_action` — `block` → ticket
  blocked; `pause-env` → paused; `fence` → paused(fenced); `none` → nothing —
  if the ticket is not already there. Then `lifecycle=true`.

The in-run path calls the same three functions immediately after `FINALIZED`;
recovery calls them for any record where a flag is false. Because `next_action`
is stored, order among the three does not matter.

## Verification stages

Each verification command is a stage with its own `proc` recorded **before
release** (the launch gate already guarantees exec-after-journal). `stages[].terminated`
is set per stage. If any stage's termination is unverified:
`attempt.json` → fence written (proc of that stage) → `ORPHANED` → exit 3.
**No `changed_paths`, no recheck, no restore while an unverified group may be
writing.** Aggregate `terminated` in the metrics row is the AND over stages.

## Runner reads and writes into `attempts/` (the no-follow discipline)

Every runner **write** into the attempts tree — `attempt.json`, `task.toml`,
`task.md`, `body.md`, `prompt.md`, `base_tree`, `diff.patch`, `row.json`,
`stdout.log`, `stderr.log`, `verify-*.out/err`, the per-attempt `feedback.md`
copy, the task-level `feedback.md`, and all rewrite temp siblings — goes
through one helper that opens with `O_NOFOLLOW` (create: `O_EXCL`; rewrite:
unique temp + `os.replace`) after `lstat` shows the path is a regular file or
absent. Every runner **read** of a worker-writable path (`result.md`, the
task-level `feedback.md`) opens with `O_NOFOLLOW` and rejects non-regular
files — never `exists()` / `read_text()`. A symlink found by either is a
`protocol` outcome, recorded **before** any bytes are read or written, so the
detection precedes the disclosure. Directories are created with `mkdir` on a
path whose parents are validated non-symlink (the attempts root is trusted;
see Trust boundary for what this does and does not defend). Fence fields used
to build paths (`ticket`, `task`, `n`) are validated as single path components
before joining.

## `clear-fence`

Runs **under the global runner lease**. If a runner holds the lease, it prints
the lease owner (pid, started) and exits 3 — the break-glass sequence is
documented in SKILL.md: verify that runner is wedged, kill it, re-run
`clear-fence` (the dead-PID lease is then reclaimable). Reads the fence,
classifies its proc, and:

- *dead* → drives the referenced attempt `ORPHANED → INTERRUPTED` (observed
  tree, restore, verify, project) and then removes the fence.
- *ours-alive* or *unknown* without `--force` → refuses, prints the proc.
- `--force` → removes the fence and marks the attempt `ORPHANED` with
  `operator_forced=true` but **does not restore or project** — a live/unknown
  group may still be writing. The next runner start sees an `ORPHANED` record
  with no fence and **re-fences it** (fail closed) unless the group is by then
  *dead*, in which case it completes the INTERRUPTED path. `--force` therefore
  unblocks an operator who has independently killed the process; it never
  authorizes a restore under a live writer.
- corrupt fence → clearable only with `--force`, same semantics.

The fence's `attempt` field is an int in exactly one schema; the CLI test uses
a fence produced by `_fence()`, never a hand-written one.

## Config contract

`[local].model` must match `-ctx32k:` (the spec's value), not `-ctx\d+k:`; an
empty model is an error when the table is present. The runner asserts at
launch that the rendered `local-worker` agent's `model` equals `[local].model`,
so there is one validated source for what actually runs.

## Lease semantics

`locks.Lease` today takes `flock` only during acquire and relies on the PID
record afterward. For the runner and `clear-fence` this is upgraded to hold
the exclusive `flock` **for the life of the process** (the fd stays open), so
mutual exclusion does not depend on PID-reuse reasoning. The PID/boot-id record
remains for `status` and break-glass diagnostics.

## Acceptance criteria (the crash-window table)

Each row below is a test that kills the runner (or simulates the kill by
returning early) at the named boundary, restarts it, and asserts the listed
post-condition. The plan is complete when every row is green.

| Kill after… | Post-condition on restart |
| --- | --- |
| dir + artifacts exist, before a valid CREATED record | exit 3 (unreadable record is never skipped) |
| dir created, before LAUNCHING | attempt → INTERRUPTED, no process, row published once |
| gated child forked, before proc committed | child never execs (gate EOF); INTERRUPTED |
| gate released, before RUNNING persisted | worker killed or fenced; INTERRUPTED |
| leader exited with a live descendant in the pgid | classified unknown → fenced, exit 3; never restored under it |
| stage reaped, before STAGE_DONE receipt | treated as INTERRUPTED (unknown outcome) |
| STAGE_DONE timed_out, clean allowlist, before CLASSIFYING | proceeds as `timeout`, tree KEPT |
| observed_tree recorded, after restore, before FINALIZED | diff.patch regenerated from observed_tree; restored tree verified |
| mid-restore (partial) | restore re-run; verified == base_tree before `tree=restored` |
| history projected, before flag | second projection is a no-op (attempt_id dedupe) |
| metrics row appended, before flag | no duplicate (stage-key dedupe) |
| lifecycle transition applied, before flag | idempotent (already in state) |
| FENCING written, fence write fails | exit 3 |
| fence write torn (temp exists, no fence) | FENCING found → exit 3 |
| LAUNCHING written, child gated | child exits 97 (never exec'd); INTERRUPTED |
| RUNNING, worker alive, **different task selected next** | worker killed or fenced before any dispatch |
| RUNNING, worker created `bin/oops`, killed before classify | `bin/oops` absent; diff.patch shows it; next attempt's base_tree == original |
| RUNNING, recorded pgid now belongs to an unrelated process (start_time mismatch) | not signaled; treated as dead |
| CLASSIFIED `rejected`, before restore | restored on restart; row published once |
| CLASSIFIED `accepted`, before projections | history has the record; metrics has one row; no re-run |
| FINALIZED, history=true, before metrics | one row appears; no duplicate |
| FINALIZED `blocked`, before lifecycle | ticket becomes blocked; not dispatched |
| verify stage `terminated=False` | fence written; ORPHANED; no restore; runner exits 3 |
| fence written, before ORPHANED | fence present → runner exits 3 (fail closed) |
| `clear-fence` while a runner is live | exits 3, prints lease owner, fence untouched |
| `clear-fence` on a dead fence produced by `_fence()` | attempt → INTERRUPTED (restored, projected); fence removed; next run proceeds |
| `clear-fence --force` on a live group | fence removed; attempt ORPHANED+forced; NOT restored; next runner start re-fences |
| `clear-fence` killed after attempt transition, before fence unlink | next start: fence present, attempt INTERRUPTED → fence removed, no re-fence |
| ORPHANED attempt whose fence file was deleted by hand | re-fenced on start (unless group now dead → INTERRUPTED) |
| metrics.jsonl has a torn trailing line | next append repairs only the tail; earlier rows intact byte-for-byte |
| metrics.jsonl has an invalid row BEFORE the tail | append fails closed with an alert; no truncation |
| worker plants `../feedback.md` symlink | detected before write; outside target unchanged; outcome protocol |
| task-level `feedback.md` is a symlink at next attempt's copy/read | not read; outcome protocol; no external bytes in the prompt |
| worker writes an ignored path (`tmp/x`) then is rejected | restore removes it; snapshot(worktree) == base_tree |
| attempt record points at a different repo_id than the worktree | exit 3, nothing restored |
| manifest re-sliced (same task.id, new fingerprint) after one cheap failure | ladder continues at cheap attempt 2; generation logged |
| `[local].model = "…-ctx4k:…"` or empty with `[local]` present | ConfigError |
| rendered local-worker model ≠ `[local].model` | launch refused with a clear error |

Plus: `runner.py` recovery code path count drops (one `reconcile`, no
`_recover`); the Plan 1b ledger's R1–R11 each map to at least one row above.

## Risks

- Process identity via `ps -o lstart` is macOS-specific with 1 s resolution.
  Under this design a same-second collision degrades to a **false fence**
  (operator clears it), never to signaling a foreign process. Accepted.
- Restoring an interrupted attempt discards a worker's partial edit that a
  timeout would have kept — except the specific case where the timeout was
  already durably recorded at STAGE_DONE, which keeps the tree. Deliberate.
- A hostile same-UID worker can corrupt recovery state (see Trust boundary).
  Accepted for the pilot; OS isolation remains the parent spec's deferred fix.

## Adversarial review disposition

Two OpenAI lanes (`cto`, `security-analyst`) reviewed the Claude-authored
draft. Accepted and folded in: canonical `worktree` + `repo_id` in the record;
`observed_tree` so the pre-restore diff is regenerable; dead-fence handling
drives the orphan through INTERRUPTED instead of deleting the fence;
`--force` never restores under a live group; leader-gone-but-pgid-populated is
*unknown* → fence; `STAGE_DONE` persists `timed_out`/`rc` so a durable timeout
keeps the tree; `next_action` persisted so lifecycle projection is order-free;
snapshot/restore cover ignored files; `lineage` key so re-slicing does not
reset the ladder; one metrics row per stage keyed `(attempt_id, kind, idx)`;
`implement_task` requires a reconciled `RunContext`; `FENCING` state makes the
single admitted cross-file ordering recoverable; no-follow discipline extended
to reads and to every enumerated write; metrics repair scans to the last
parseable record and fails closed on interior corruption; lease upgraded to a
held `flock`; break-glass sequence for `clear-fence`; Trust boundary section
states plainly that this is not a defense against a hostile same-UID worker;
1-second identity collision reclassified from "accepted risk" to "degrades to
fence"; nine additional crash-window rows.

Pushed back: *"use OS-enforced containment / a durable supervisor."* Correct
architecture; explicitly deferred by the parent spec; the Trust boundary
section now makes the consequence honest rather than pretending the rest of
the design closes it.
