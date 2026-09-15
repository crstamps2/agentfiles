---
name: agent-loop
description: Operate the autonomous ZIP-6774 agent loop -- status, pause/resume, human takeover of a ticket, dry runs. Use when Cody asks about the loop, the hopper, worker attempts, or wants to stop/take over autonomous work.
---

# /agent-loop [status | resume | start | pause | unpause | stop | ledger | hopper]

Cody's operator surface for the autonomous loop. Every command below is `python3 runner.py <verb>`
run from `~/workspace/agentfiles/common/skills/agent-loop/scripts`; never hand-edit state files.
The loop itself runs under launchd (one LaunchAgent per ticket); the cmux agent pane of each
workspace runs `/loop-watch <TICKET>` as the coordinator. This skill is the *fleet* view.

## `/agent-loop` or `/agent-loop status`  (default)

1. `python3 runner.py overview` -- one line per hopper ticket: state, PR, worktree, spend, deps,
   supervisor. Print it verbatim.
2. `python3 runner.py supervise status` -- which LaunchAgents are loaded and alive.
3. Summarise in ≤5 lines: tickets at Human Gate 1 (PR links), in flight (stage), paused/blocked
   (reason, one line each), queued-but-eligible. If `~/.local/state/agent-loop/PAUSE` exists say so first.

## `/agent-loop resume [TICKET | all]`  -- "pick up where they left off"

For each in-flight ticket (has a worktree; state not human-gate-1/done):
- state `paused` or `blocked`: read the reason.
  - `resource:` / `heavy lane` → `python3 runner.py resume --ticket X --from implement` (transient).
  - `plan failed` / `budget exceeded` / planner `blocked` → `python3 runner.py resume --ticket X`
    (re-plans under the current rules).
  - `gates failed` → `--from gates`; `publish failed`/`reply failed`/`mark ready failed` → `--from ready`
    (or `--from bot-loop` if a PR is already ready).
  - a traceback / "Runner defect" → do NOT resume; report it (the operator session fixes the runner).
- any other state: `python3 runner.py resume --ticket X` just (re)starts the supervisor.
Then make sure each workspace has its coordinator pane: `cmux tree --workspace <ws>`; if there is no
terminal titled `pi '/loop-watch X'`, open one (new terminal surface → `cd <worktree>` →
`NODE_OPTIONS= pi '/loop-watch X'`) and a Jira tab in the browser pane if missing.
Finish with `/agent-loop status`.

## `/agent-loop start [N]`  -- "start new work from the hopper"

Eligible = in `hopper.toml` order, no worktree yet, every `deps` ticket is `done`. For the first N
(default 1; never more than 3 in flight at once):
1. `spinup <TICKET>` (the normal cmux spinup; until `spinup --loop` lands, start the agent tab idle).
2. When setup finishes: `python3 runner.py supervise start --ticket <TICKET>`.
3. In the workspace's agent tab: `/loop-watch <TICKET>`.
Report each ticket's workspace and that its supervisor is loaded.

## `/agent-loop pause` / `unpause`
`touch ~/.local/state/agent-loop/PAUSE` -- every runner and coordinator halts at its next boundary
(nothing is killed mid-attempt). `rm` it to continue. Say which tickets were in flight.

## `/agent-loop stop TICKET`
`python3 runner.py supervise stop --ticket X`. The ticket keeps its state; `resume` brings it back.
Use `touch ~/.local/state/agent-loop/tickets/X/HUMAN` when Cody takes a ticket over by hand
(runner refuses to touch it until the file is removed).

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
- If a runner shows a traceback, that is code to fix in the operator session with a test -- not a
  ticket to resume.
