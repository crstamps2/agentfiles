# Agent Loop — Plan 1c Design: Attempt Lifecycle and Recovery

Status: Draft for Cody's approval. Amends the *Per-ticket lifecycle*, *Escalation
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

## Non-goals

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
attempt_id     "<ticket>/<task>@<fingerprint>/<n>"   stable identity for dedupe everywhere
generation     fingerprint of (task manifest, worktree)  — history key
n              int
status         one of the states below
base_tree      git tree OID recorded before launch
agent, model, tier, arm, rung, run_id, started_utc
proc           { boot_id, pgid, pid, start_time }      identity of the CURRENT stage's group, or null
stages         [ {kind: worker|verify, idx, proc, terminated: bool|null, elapsed_s} ]
outcome, reason, changed_paths, violations     filled at CLASSIFIED
verify_seconds, end_utc                         filled at CLASSIFIED
tree           one of: dirty | restored | kept        filled at FINALIZED
published      bool                                   metrics projection done
history        bool                                   state.json projection done
lifecycle      bool                                   ticket transition projection done (blocked/paused only)
```

`proc.boot_id` is `locks.boot_id()`; `proc.start_time` is the group leader's
process start time (`ps -o lstart= -p <pid>`, parsed). A recorded group is
**ours** only if boot_id matches and the live leader's start time matches.
Otherwise the pgid is recycled and is never signaled.

## States

```
CREATED      dir + task artifacts + base_tree written; proc = null
LAUNCHING    stage record appended with proc; child is gated (cannot exec yet)
RUNNING      gate released; child executing
STAGE_DONE   stage terminated (verified) → next stage or CLASSIFYING
CLASSIFYING  result parsed, changed_paths/violations computed (pure reads)
CLASSIFIED   outcome/reason persisted
FINALIZED    tree action done (restored | kept), diff.patch written
PROJECTED    history=true, published=true, lifecycle=true (each idempotent)
---
INTERRUPTED  recovery found a CREATED..CLASSIFYING attempt with no live group
ORPHANED     recovery found a live/unknown group it could not verifiably kill
```

Terminal states: `PROJECTED`, `INTERRUPTED` (after its own projection),
`ORPHANED` (until an operator acknowledges).

Transitions are strictly forward; every transition is a single atomic rewrite
of `attempt.json`. The runner never holds two "pending" facts in two files.

## Recovery: global, before any dispatch

On every runner start (any subcommand except `status`), under the **global
runner lease**, before selecting a ticket or touching the heavy lane:

1. **Fence check.** If `locks/heavy.fence` exists → read it → if the recorded
   proc is *ours* and alive → runner exits 3 ("fenced"). If it is dead or not
   ours (recycled) → delete the fence. Corrupt fence → exit 3 (fail closed;
   `clear-fence` handles it).
2. **Sweep every attempt dir under `attempts/`** (all tickets, all tasks), not
   just the one about to run. For each non-terminal `attempt.json`:
   - `RUNNING` / `LAUNCHING` with a proc: if proc is ours and alive →
     `kill_group`; if it dies → continue as INTERRUPTED. If it survives, or
     `group_state` is zombie-or-foreign → **write the fence first**, then set
     `ORPHANED`, then exit 3. (Fence-before-orphan: a crash between the two
     leaves a fence, which is the safe side.)
   - Then, for the interrupted attempt: compute `changed_paths` against
     `base_tree`; **always restore to `base_tree`** (an interrupted attempt has
     no accepted result); write `diff.patch` of what was there; set
     `INTERRUPTED` with `outcome="interrupted"`, `tree="restored"`.
   - `CLASSIFIED` (crash before finalize): finish finalization from the
     persisted outcome — `rejected|protocol|blocked → restore`; else keep.
   - `FINALIZED` or `INTERRUPTED` with any projection flag false → project.
3. Only then: ticket selection, `_stop` checks, admission, heavy lease, dispatch.

`_recover(t, task)` is deleted; there is one `reconcile(state_root)`.

## Projections (idempotent, each keyed by `attempt_id`)

- **history**: `state.json.attempts[generation]` gets the record `{n, rung,
  outcome, reason, arm, attempt_id}` if no record with that `attempt_id`
  exists. Then `history=true`.
- **metrics**: append the row if no row with that `attempt_id` exists (replaces
  the `evidence_path` heuristic). `metrics.append` repairs a torn tail by
  truncating **only the trailing bytes after the last `\n`** — never rewriting
  the valid prefix — and `read_all` treats an unterminated final object as
  *not committed*, matching `append`. Then `published=true`.
- **lifecycle**: if outcome is `blocked` (owner or ladder exhausted) or the
  attempt completes an environment pair, apply the ticket transition if the
  ticket is not already in that state. Then `lifecycle=true`.

The in-run path calls the same three functions immediately after `FINALIZED`;
recovery calls them for any record where a flag is false. Order among the three
no longer matters.

## Verification stages

Each verification command is a stage with its own `proc` recorded **before
release** (the launch gate already guarantees exec-after-journal). `stages[].terminated`
is set per stage. If any stage's termination is unverified:
`attempt.json` → fence written (proc of that stage) → `ORPHANED` → exit 3.
**No `changed_paths`, no recheck, no restore while an unverified group may be
writing.** Aggregate `terminated` in the metrics row is the AND over stages.

## Feedback and other runner writes

`ladder.append_feedback` and every other runner write into the attempts tree
use a no-follow, regular-file-checked writer (`O_NOFOLLOW`, `lstat` regular or
absent). A planted symlink at any runner-written path is a `protocol` outcome.
Fence fields used to build paths (`ticket`, `task`, `n`) are validated as single
path components before joining.

## `clear-fence`

Runs **under the global runner lease** (exits 3 if a runner is live). Reads the
fence, re-probes with the ours-and-alive test, and clears only if dead/not-ours
or `--force`. On clear it also sets the referenced attempt's status from
`ORPHANED` to `INTERRUPTED` and runs its finalization + projections, so the
next runner start does not re-fence. A corrupt fence is clearable only with
`--force`. The fence's `attempt` field is an int in exactly one schema; the CLI
test uses a fence produced by `_fence()`, never a hand-written one.

## Config contract

`[local].model` must match `-ctx32k:` (the spec's value), not `-ctx\d+k:`; an
empty model is an error when the table is present. The runner asserts at
launch that the rendered `local-worker` agent's `model` equals `[local].model`,
so there is one validated source for what actually runs.

## Acceptance criteria (the crash-window table)

Each row below is a test that kills the runner (or simulates the kill by
returning early) at the named boundary, restarts it, and asserts the listed
post-condition. The plan is complete when every row is green.

| Kill after… | Post-condition on restart |
| --- | --- |
| dir created, before LAUNCHING | attempt → INTERRUPTED, no process, row published once |
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
| `clear-fence` while a runner is live | exits 3 without touching the fence |
| `clear-fence` on a fence produced by `_fence()` | clears; attempt → INTERRUPTED; next run proceeds, no re-fence |
| metrics.jsonl has a torn trailing line | next append repairs only the tail; earlier rows intact byte-for-byte |
| worker plants `../feedback.md` symlink | outside target unchanged; outcome protocol |
| `[local].model = "…-ctx4k:…"` | ConfigError |
| rendered local-worker model ≠ `[local].model` | launch refused with a clear error |

Plus: `runner.py` recovery code path count drops (one `reconcile`, no
`_recover`); the Plan 1b ledger's R1–R11 each map to at least one row above.

## Risks

- Process identity via `ps -o lstart` is macOS-specific and has 1 s
  resolution; a recycled pgid whose leader started in the same second as the
  recorded one would be misidentified as ours. Mitigation: also compare
  command name. Accepted for the pilot.
- Restoring an interrupted attempt discards a worker's partial edit that a
  timeout would have kept. Deliberate: an interrupted attempt was never
  classified, so its tree is unknown; the diff is preserved for humans.
