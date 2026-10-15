---
name: agent-loop
description: Operate the autonomous ZIP-6774 agent loop -- status, pause/resume, human takeover of a ticket, dry runs. Use when Cody asks about the loop, the hopper, worker attempts, or wants to stop/take over autonomous work.
---

# /agent-loop [status | resume | start | pause | unpause | stop | spend | ledger | hopper]

Cody's operator surface for the autonomous loop. Every command below is `python3 runner.py <verb>`
run from `~/workspace/agentfiles/common/skills/agent-loop/scripts`; never hand-edit state files.
The loop itself runs under launchd (one LaunchAgent per ticket); the cmux agent pane of each
workspace runs `/loop-watch <TICKET>` as the coordinator. This skill is the *fleet* view.

## `/agent-loop` or `/agent-loop status`  (default)

1. `python3 runner.py overview` -- with a cohort: `COHORT n/4` slot rows (admission #, state,
   recovery or decision wait, supervisor, progress), `WAITING ADMISSION` (parked order, next
   candidate) and `REVIEW READY` (PRs). Without one: the legacy per-ticket lines. Print it verbatim.
2. `python3 runner.py supervise status` -- which ticket LaunchAgents are loaded and alive.
3. Summarise in ≤5 lines: tickets at Human Gate 1 (PR links), cohort members and what each waits on,
   open product decisions (question + `decision-answer` command), next admission. If
   `~/.local/state/agent-loop/PAUSE` exists say so first.

## `/agent-loop decide TICKET "guidance"`  -- answer a product decision
`python3 runner.py decision-answer --ticket X --guidance "..."`. Only for tickets in
`decision-required`; it records the answer, appends `## Owner rulings` to plan.md, and resumes the
saved stage (or re-plans). Never edit state files to answer.

## `/agent-loop resume [TICKET | all]`  -- "pick up where they left off"

Normally unnecessary: supervisors are `RunAtLoad` and pauses self-heal by policy (`heal.py`). Use this
after `supervise stop`, after removing a `HUMAN` file, or when `overview` shows `needs operator`.

`all`: start with `python3 runner.py resume --all`. It restarts the supervisor of every in-flight ticket
that has a worktree without changing its state (heal.py then handles `paused` by policy), leaves running
supervisors alone, and lists `HUMAN` takeovers and `blocked` tickets under `needs_operator`. Work through
only those listed tickets with the rules below, then reopen panes.

With a cohort, a ticket outside it cannot be resumed while all four slots are owned (`resume` refuses);
members waiting on a recovery contract are resumed by the reconciler when their repair lands, so do
not resume them by hand (`resume` refuses; `runner.py recovery-clear --ticket X [--resume-from STAGE]` is the
deliberate override, and `runner.py repair-approved --defect ID` wakes tickets after Cody applies a
`needs-approval` patch). A `needs Cody` notification is a genuine owner question: answer it, then `resume`.
For one ticket, or each ticket `--all` listed under `needs_operator`:
- state `paused` or `blocked`: read the reason.
  - `resource:` / `heavy lane` → `python3 runner.py resume --ticket X --from implement` (transient).
  - `plan failed` / `budget exceeded` → `python3 runner.py resume --ticket X` (back to `plan`, which
    REPAIRS from the existing revise review before it ever re-plans). Planner `blocked` (tier-4 owner
    question) → read plan-review.md / plan.md and report the question; do not resume blindly.
  - `gates failed` → `--from gates`; `publish failed`/`reply failed`/`mark ready failed` → `--from ready`
    (or `--from bot-loop` if a PR is already ready).
  - a traceback / "Runner defect" → do NOT resume; the repair agent fixes it (`runner.py report-defect` if not queued).
- any other state: `python3 runner.py resume --ticket X` just (re)starts the supervisor.
Then make sure each workspace has its coordinator pane: `cmux tree --workspace <ws>`; if there is no
terminal titled `pi '/loop-watch X'`, open one (new terminal surface → `cd <worktree>` →
`NODE_OPTIONS= pi '/loop-watch X'`) and a Jira tab in the browser pane if missing.
Finish with `/agent-loop status`.

## `/agent-loop start` / `drip`  -- "start new work from the board"

The hopper is a **drip fed from Jira**: children of the epic in board Rank order (top first), any
assignee, only tickets in the ready column (`Selected for Work`), skipping tickets excluded in
`hopper.toml` (Martin's audit) and tickets whose `deps` are not Done in Jira (no autonomous PR
stacks). With `cohort.json` present it admits only into empty cohort slots (four; parked tickets
first, by original admission order); without it, the legacy `[drip].max_in_flight` count applies.

- `python3 runner.py drip --dry-run` -- show what it would start and why others were skipped.
- `python3 runner.py drip` -- start now (spinup with all panes incl. `/loop-watch`, assign to Cody,
  In Progress, supervisor).
- `python3 runner.py supervise drip-start` -- install the 15-minute LaunchAgent so the drip runs
  itself forever; `drip-stop` removes it.
- `python3 runner.py supervise cohort-start` -- install the 60-second deterministic reconciler
  (`cohort-shadow-start` runs it with `--dry-run`; `cohort-stop` removes it).
- `python3 runner.py cohort-migrate --dry-run | --apply | --rollback <snapshot> [--force]` -- move
  legacy in-flight tickets into the cohort (snapshot first; metadata-only rollback).
Only verified Human Gate 1 releases a slot. Paused, blocked, repairing, provider-waiting, and
`decision-required` tickets keep theirs; `park --ticket X` releases one deliberately.

## `/agent-loop pause` / `unpause`
`touch ~/.local/state/agent-loop/PAUSE` -- every runner and coordinator halts at its next boundary
(nothing is killed mid-attempt). `rm` it to continue. Say which tickets were in flight.

## `/agent-loop stop TICKET`
`python3 runner.py supervise stop --ticket X`. The ticket keeps its state; `resume` brings it back.
Use `touch ~/.local/state/agent-loop/tickets/X/HUMAN` when Cody takes a ticket over by hand
(runner refuses to touch it until the file is removed).

## `/agent-loop spend [since YYYY-MM-DD] [ticket ZIP-1234]`
`python3 runner.py spend [--since ...] [--ticket ZIP-1234] [--by ticket,model,stage,day,role]` --
what the loop cost, as a bar chart: totals, share and call counts by ticket, model and stage. With
`--ticket`, where that one ticket's money went. This is reporting only; the loop is never stopped by
a dollar figure (Cody, 2026-09-23). Read it in the morning: a night where planning dominates, or one
model dwarfs the rest, is visible at a glance.

## `/agent-loop ledger [since YYYY-MM-DD]`
`python3 runner.py ledger [--since ...]` -- billed $ by model / role / ticket / day. Quote the first
line (total) and the by-ticket table.

## `/agent-loop hopper`
Show `hopper.toml`'s `[[hopper.tickets]]` order with each ticket's state. To add a ticket, append a
table (`key`, optional `deps`, `pin_arm = "cloud"`) and commit on `agentfiles` branch `agent-loop-1`.

## Rules
- Draft-PR creation, pushes to the loop's own branch, marking ready, and bot-comment replies are
  the runner's (allowlisted). Never do them from here. Never merge, never assign reviewers.
- Never delete `~/.local/state/agent-loop/attempts/*` or `planning/`; they are the audit trail.
- A runner traceback is code to fix, not a ticket to resume. The repair agent (`com.cody.agent-loop.repair`,
  every 5 min) fixes it test-first, gets an opposite-vendor review and fast-forwards the live branch;
  queue: `~/.local/state/agent-loop/defects/{open,landed,failed,needs-approval}/`. Protected safety files
  (contracts, publish, agentdef, worktree, repair) are never changed automatically: those land as a
  patch in `needs-approval/` for Cody.
