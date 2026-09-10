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
- **Run-once** (stub) -- `python3 ~/.pi/agent/skills/agent-loop/scripts/runner.py run-once`
  Selects and attempts one ticket from the hopper (not yet wired to ticket selection logic).
- **Dry run** (no models, no Jira, fake worker):
  `AL_SCENARIO=pass python3 .../runner.py dry-run --worktree <path> --tasks <tasks.toml> [--scenario <name>] [--ticket <KEY>]`
  Scenarios: `pass fail malformed escape tests timeout owner env pass_on_feedback`.
  Requires the fake worker to be substituted for `pi`; see `test_runner.py` for the launcher hook.
  In this plan the dry-run CLI uses the real `pi` argv unless patched -- it exists to exercise
  state, locks, and metrics plumbing on a scratch worktree, not to call models.

## State on disk

`~/.local/state/agent-loop/` (mode 0700):
`tickets/<KEY>/state.json`, `attempts/<KEY>/<task>/<n>/{task.md,task.toml,prompt.md,body.md,result.md,diff.patch,base_tree,stdout.log,stderr.log,session/}`,
`attempts/<KEY>/<task>/feedback.md`, `locks/heavy`, `locks/runner`, `metrics.jsonl`, `PAUSE`.

- `locks/runner` -- global runner lease; a second instance exits with code 3.

## What is NOT wired yet (Plans 2-3)

Ledger token/cost enrichment and dashboard; gates (lint, tests, Playwright, Figma, lens-review);
draft PR, ready, bot/colleague loops; real ticket selection (`run-once`); launchd tick going live.
