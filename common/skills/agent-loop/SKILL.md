---
name: agent-loop
description: Operate the autonomous ZIP-6774 agent loop -- status, pause/resume, human takeover of a ticket, dry runs. Use when Cody asks about the loop, the hopper, worker attempts, or wants to stop/take over autonomous work.
---

# /agent-loop

Operator surface for the runner in `scripts/runner.py`. Design:
`docs/superpowers/specs/2026-09-10-zip-6774-agent-loop-design.md`.

## Commands

All run from anywhere; `--config` defaults to this skill's `hopper.toml`.

- **Status** -- `python3 ~/.pi/agent/skills/agent-loop/scripts/runner.py status`
  Prints pause state, heavy-lane holder, and each hopper ticket's state and reason.
- **Run-once** (Plan 1 stub) -- `python3 ~/.pi/agent/skills/agent-loop/scripts/runner.py run-once`
  performs no work; ticket selection is not wired until Plan 3.
- **Dry run** (no models, no Jira, fake worker):
  `python3 .../runner.py dry-run --worktree <path> --tasks <tasks.toml> [--scenario <name>] [--ticket <KEY>]`
  Scenarios: `pass fail malformed escape env_escape tests timeout owner env pass_on_feedback`.
  The CLI always replaces `pi` with the bundled fake worker; it never launches a real model.
  Manifest verification commands still run in the scratch worktree.

## State on disk

`~/.local/state/agent-loop/` (mode 0700):
`tickets/<KEY>/state.json`, `attempts/<KEY>/<task>/<n>/{task.md,task.toml,prompt.md,body.md,result.md,diff.patch,base_tree,stdout.log,stderr.log,session/}`,
`attempts/<KEY>/<task>/feedback.md`, `locks/heavy`, `locks/heavy.fence`, `locks/runner`, `metrics.jsonl`, `PAUSE`.

- `locks/runner` -- global runner lease; a second instance exits with code 3.
- `locks/heavy.fence` -- an unverified worker process group. The lane remains fenced until its PGID is dead.
  If the group's pgid is unreachable (`PermissionError`, e.g. a foreign/unreapable zombie), recovery marks the
  attempt `orphaned` and leaves the fence in place for an operator: run
  `python3 .../runner.py clear-fence` to print the fence and its current process-group state, and it only
  removes the fence when that state is `dead` (pass `--force` to clear it anyway once you've verified by hand).

## Pause, resume, and takeover

- Pause new atomic steps: `touch ~/.local/state/agent-loop/PAUSE`; resume: `rm ~/.local/state/agent-loop/PAUSE`.
- Take over one ticket: `touch ~/.local/state/agent-loop/tickets/<KEY>/HUMAN`; resume it by removing that file.
  The runner completes its current atomic step, releases the heavy lane, and never touches that worktree while `HUMAN` exists.
- `~/.pi/agent/skills/agent-loop` exists only after `bootstrap.sh --tool pi`.

## What is NOT wired yet (Plans 2-3)

Ledger token/cost enrichment and dashboard; gates (lint, tests, Playwright, Figma, lens-review);
draft PR, ready, bot/colleague loops; real ticket selection (`run-once`); launchd tick going live.
