# Agent loop — operator guide

Autonomous delivery of ZIP-6774 (ZUI containers) tickets to **Human Gate 1**: a ready-for-review PR
with CI green and the repo's Claude review bots already addressed. Cody looks at nothing before that.

Adapted from the "strong reviewer, inexpensive workers" field guide. Everything lives on `agentfiles`
branch `agent-loop-1` (unmerged, never pushed). Spinup changes live on the dotfiles branch
`spinup-headless`. `~/.pi/agent/skills/{agent-loop,loop-watch}` are symlinks into this tree.

## Where things are

| what | where |
|---|---|
| runner + all modules + tests | `common/skills/agent-loop/scripts/` (`python3 runner.py …`, tests: `python3 -m unittest discover -p 'test_*.py'`) |
| ticket order, arms, budgets, admission thresholds | `common/skills/agent-loop/hopper.toml` |
| model tiers (author/critic/task-writer/workers) | `common/model-tiers.toml` → rendered agent defs in `~/.pi/agent/agents/*.md` (source: `common/agents/*.agent.md`) |
| operator skill (fleet) | `/agent-loop` → `common/skills/agent-loop/SKILL.md` |
| coordinator pane skill (one ticket) | `/loop-watch <TICKET>` → `common/skills/loop-watch/SKILL.md` |
| runtime state | `~/.local/state/agent-loop/` — `tickets/<K>/{state.json,ci.json,heal.jsonl,bad-guards.md,gates.out,screenshots/}`, `attempts/<K>/<task>/<n>/`, `plans/`, `botreview/`, `usage.jsonl`, `locks/`, `launchd/*.log` |
| per-ticket planning artifacts (never committed) | `<worktree>/planning/<key>/{plan.md,tasks.toml,plan-review.md,transcript.md}` (git-excluded via the repo's shared `info/exclude`) |
| supervisors | `~/Library/LaunchAgents/com.cody.agent-loop.<ticket>.plist`, run via the codesigned `~/.local/bin/agent-loop-python` |

## Lifecycle (one ticket)

```
spinup → plan → plan-review → implement → gates → draft-pr → ready → bot-loop → human-gate-1
```

- **plan**: author (Opus 5 / Sol, alternating per ticket) writes `plan.md` + `tasks.toml` from the Jira
  ticket and the repo's skills (`.agents/skills/zui-component-creation` is the definition of done).
  Schema check bounces violations to the author. Critic (Fable / Astra — always the *other* vendor)
  reviews; a `revise` is applied by the **task-writer** (Sonnet), never a second author round; critic
  round 2 is approve-with-concerns unless it names a wrong-code blocker. $25 cap per planning run.
- **implement**: tasks in manifest order; before each, rebase onto `origin/main` (generated-file
  conflicts regenerate themselves). Ladder per task: cheap ×2 (Sonnet) → Terra ×2 → blocked. `size =
  "small"` tasks try the free local Ollama model first. Worker stages are light (N slots); verification
  (Rails/browser) is one-at-a-time on the heavy lane. Every acceptance also runs the repo's rubocop +
  reek (+ i18n normalization when `en.yml` changed), then commits exactly the task's paths and pushes.
- **guard soundness**: a verification command that rejects an attempt is re-run on the pre-work base
  tree; if it fails there too the *guard* is wrong → attempt is `environment` (rung kept), guard goes to
  `bad-guards.md`, task-writer repairs the manifest, ticket resumes. A premium worker's honest
  `blocked/<reason>` is a *contract complaint* and goes the same way.
- **gates**: `bin/wt prepare`, `yarn build`, full `test/views/components/zui/`, rubocop
  `--force-exclusion`, Lookbook screenshots (headless Chrome for Testing) for visual tasks.
- **draft-pr**: PR body in house style written by the critic model from diff + plan against a real team
  PR, linted for workflow vocabulary; title from the ticket summary. Screenshots via `gh image`.
- **ready / bot-loop**: CI classified (wait / rerun / rebase / fix task / escalate), mark ready
  (triggers the Claude review workflows), comments adjudicated by `review-adjudicator` (fix / decline /
  defer / question), replies posted with `— posted by Cody's AI agent (<model>) on his behalf`, fix
  tasks `9nn` run, re-review, gate.

## Self-healing (heal.py)

Every `paused` reason has a policy and a per-reason budget; a human is consulted only when the policy
says so (hand-written merge conflict, design question) or the budget is spent. Recoveries are logged
to `tickets/<K>/heal.jsonl`. Notifications (cmux + macOS) fire once per ticket at Human Gate 1, on an
operator-needed pause, or on block.

## Daily operation

```
/agent-loop                       # fleet: state, PR, spend, supervisor per ticket
/agent-loop resume all            # after a reboot or a stop: everything back in motion + panes
/agent-loop start [N]             # next eligible hopper ticket(s): spinup + supervisor + pane
/agent-loop pause | unpause       # touch/rm ~/.local/state/agent-loop/PAUSE
/agent-loop stop ZIP-X            # unload one supervisor (state kept)
/agent-loop ledger [since DATE]   # billed $ by model / role / ticket / day
```
Per-ticket, from `scripts/`: `runner.py watch --ticket X` (live pane view), `resume --ticket X
[--from stage]`, `supervise status|start|stop`, `overview`.

Reboot: supervisors auto-start (`RunAtLoad`); run `/agent-loop resume all` to reopen the panes.
Kill switch: `touch ~/.local/state/agent-loop/PAUSE`. Take a ticket over by hand: `touch
~/.local/state/agent-loop/tickets/ZIP-X/HUMAN`.

## Hard rules the loop enforces

Draft PRs only until CI is green and bots have run; never assign reviewers; never merge; never a bare
force-push (lease only); GitHub writes only through `publish.github_write()`'s allowlist; workers have
no `fallbackModels` and a fixed tool allowlist; `planning/` and `.pi/` never committed; verification
commands use repo tools only (schema rejects `planning/` probes and >3 bespoke commands per task).

## Costs seen (2026-09-12 → 16)

Well (first PR): $69. Planning is the dominant cost when it loops (one bad night: $154 for zero PRs);
under the current shape planning is ~$6–20/ticket. Terra ~$15–25/ticket; Sonnet cheap arm ~$3–8.
`runner.py ledger` is the source of truth.

## Known gaps

- Intra-ticket task parallelism is built (`parallel.py`) but gated off (`tasks_parallel`) until
  `implement_task` is thread-safe per ticket.
- `spinup --loop` (agent tab launches `/loop-watch`, supervisor starts after setup) not yet in the
  spinup skill; `/agent-loop start` does it in three steps.
- Figma Code Connect: workers produce a static mapping that parses; the handoff/publish step is an
  owner action and is listed among the tier-3 items in each Gate 1 report.
