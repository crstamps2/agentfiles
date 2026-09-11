---
name: agent-loop
description: Operate the autonomous ZIP-6774 agent loop -- status, pause/resume, human takeover of a ticket, dry runs. Use when Cody asks about the loop, the hopper, worker attempts, or wants to stop/take over autonomous work.
---

# /agent-loop

Operator surface for the runner in `scripts/runner.py`. Design:
`docs/superpowers/specs/2026-09-10-zip-6774-agent-loop-design.md`, amended by
`docs/superpowers/specs/2026-09-10-agent-loop-1c-attempt-lifecycle-design.md`
(attempt lifecycle, crash recovery, and `clear-fence`).

## Trust boundary (read this before touching state by hand)

Workers run as Cody's UID with `bash`. They *can* rewrite `attempt.json`, the fence,
`state.json`, or escape their recorded process group. The recovery machinery below is a
**crash-consistency design for cooperative workers, not a security boundary against a
hostile one** -- it guarantees the loop recovers cleanly from a runner crash or kill at any
instruction boundary, not that a worker cannot corrupt its own recovery state on purpose.
OS-level sandboxing of workers remains out of scope for the pilot.

## Every start reconciles

Every subcommand except `status` and `clear-fence` runs the global recovery pass
(`reconcile.reconcile`) **before** touching a ticket, admission, or the heavy lane: it
checks `locks/heavy.fence`, then sweeps every attempt directory under `attempts/` (all
tickets, all tasks) and drives each non-terminal record forward -- killing a live group
left over from a prior crash, fencing anything it cannot prove is dead,
restoring/classifying/projecting anything it can. Nothing is dispatched until that pass
completes. If it cannot proceed safely it exits 3 and prints `fenced: <reason>` to stderr;
an operator must inspect (and, if a fence is involved, run `clear-fence`).

`clear-fence` is the deliberate exception, not an oversight: it is precisely the operator
path for when `reconcile()` has already refused (exited 3) because of `locks/heavy.fence`.
Running the full sweep first would just re-hit the same fence and exit 3 again before
`clear-fence` ever got to do its job. Instead, under the same global runner lease
`reconcile()` would take, it performs only the record-integrity check for the one attempt
the fence references (load it, validating its worktree/repo_id) -- not the full multi-ticket
sweep -- before classifying the fenced process and deciding whether to clear the fence.

An attempt moves through these states (`attempt.json` is the single source of truth; every
other record -- ticket history, metrics, the ladder -- is a *projection* of it):

```
CREATED -> LAUNCHING -> RUNNING -> STAGE_DONE -> CLASSIFYING -> CLASSIFIED -> FINALIZED -> PROJECTED
                                        |                            \
                                        v                             (crash anywhere above)
                                    FENCING -> ORPHANED -> INTERRUPTED
```

`PROJECTED` and `INTERRUPTED` are terminal. `ORPHANED` is terminal until `clear-fence` moves
it to `INTERRUPTED` (or re-fences it, if the group is still alive).

## Commands

All run from anywhere; `--config` defaults to this skill's `hopper.toml`.

- **Status** (skips reconcile; see "Every start reconciles" above for the other exception,
  `clear-fence`) --
  `python3 ~/.pi/agent/skills/agent-loop/scripts/runner.py status`
  Prints pause state, heavy-lane holder, and each hopper ticket's state and reason.
- **Run-once** (Plan 1c stub) -- `python3 ~/.pi/agent/skills/agent-loop/scripts/runner.py run-once`
  reconciles, then performs no ticket work; real ticket selection is not wired until Plan 3.
- **Dry run** (no models, no Jira, fake worker):
  `python3 .../runner.py dry-run --worktree <path> --tasks <tasks.toml> [--scenario <name>] [--ticket <KEY>] [--skip-admission]`
  Scenarios: `pass fail malformed escape env_escape tests timeout owner env pass_on_feedback`.
  The CLI always replaces `pi` with the bundled fake worker; it never launches a real model.
  Manifest verification commands still run in the scratch worktree.
  - `--skip-admission`: For tests and demos on a loaded machine; never for real runs. Bypasses the admission controller's resource checks (compressor, load, memory pressure, thermal state, disk) and allows the task to proceed unconditionally. Defaults to off (admission is enforced).
- **`clear-fence [--force]`** -- the operator's break-glass tool for `locks/heavy.fence`. See
  below.

## `clear-fence`

Runs **under the global runner lease** -- it can never run concurrently with a live runner.

- **A runner is live** (the lease is held): refuses immediately, printing the holder's pid
  and heartbeat to stderr, exit 3. The fence is untouched. Break-glass sequence:
  1. Verify the runner is actually wedged (check `status`, check the pid's process table
     entry, check the last heartbeat) -- do not skip this step.
  2. Kill that pid.
  3. Re-run `clear-fence`. The lease is now reclaimable (a dead PID never blocks it).
- **No runner holds the lease:** reads the fence and classifies its process group.
  - **Dead** -- drives the referenced attempt `ORPHANED -> INTERRUPTED` (records the
    observed tree, restores to `base_tree`, verifies convergence, projects) and removes the
    fence. The next runner start proceeds normally.
  - **Alive (ours or unknown) without `--force`** -- refuses, printing the process
    description, exit 3. Nothing changes.
  - **Alive (ours or unknown) with `--force`** -- removes the fence and marks the attempt
    `operator_forced: true`, but does **not** restore or project -- a live/unknown group may
    still be writing to the worktree. The next runner start sees an `ORPHANED` record with no
    fence and **re-fences it** (fail closed) unless the group is dead by then, in which case
    it completes the `INTERRUPTED` path itself. `--force` unblocks an operator who has
    independently confirmed and killed the process; it never authorizes a restore under a
    live writer.
  - **Corrupt fence** (unreadable, wrong shape, a non-string/empty `attempt_dir`, an
    `attempt_dir` outside `attempts/`, or a `proc` that doesn't parse as a well-typed
    `ProcId` -- e.g. a non-dict `proc`, a non-integer `pgid`, or a non-positive `pgid`) --
    clearable only with `--force` (nothing else can be done for it; there is no attempt
    record to safely act on).
  - **`--force` on a fence whose referenced attempt is still `FENCING`** (a crash landed
    between `fence()` writing the fence file and its own `ORPHANED` transition) --
    `--force` promotes the record to `ORPHANED` first, then applies the same
    `operator_forced: true` marking as the live/unknown case above. A referenced attempt in
    any other status has no recovery story here; `--force` clears the fence file only and
    leaves the record untouched.

## State on disk

`~/.local/state/agent-loop/` (mode 0700):
`tickets/<KEY>/state.json`,
`attempts/<KEY>/<task>/<n>/{attempt.json,task.md,task.toml,prompt.md,body.md,result.md,diff.patch,base_tree,stdout.log,stderr.log,verify-*.out/err,session/}`,
`attempts/<KEY>/<task>/feedback.md`, `locks/heavy`, `locks/heavy.fence`, `locks/runner`, `metrics.jsonl`, `PAUSE`.

- `locks/runner` -- global runner lease, held (exclusive `flock`) for the life of the
  process by both the runner and `clear-fence`; a second instance exits with code 3.
- `locks/heavy.fence` -- an attempt whose process group's termination could not be verified.
  The lane stays fenced until an operator runs `clear-fence` (see above); a fenced runner
  start also exits 3 without dispatching anything.

## Pause, resume, and takeover

- Pause new atomic steps: `touch ~/.local/state/agent-loop/PAUSE`; resume: `rm ~/.local/state/agent-loop/PAUSE`.
- Take over one ticket: `touch ~/.local/state/agent-loop/tickets/<KEY>/HUMAN`; resume it by removing that file.
  The runner completes its current atomic step, releases the heavy lane, and never touches that worktree while `HUMAN` exists.
- `~/.pi/agent/skills/agent-loop` exists only after `bootstrap.sh --tool pi`.

## What is NOT wired yet (Plans 2-3)

Ledger token/cost enrichment and dashboard; gates (lint, tests, Playwright, Figma, lens-review);
draft PR, ready, bot/colleague loops; real ticket selection (`run-once`); launchd tick going live.
