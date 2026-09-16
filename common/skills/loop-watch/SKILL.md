---
name: loop-watch
description: Coordinator seat for one agent-loop ticket, run in the cmux agent pane. Shows the ticket's live progress (deterministic, free) and wakes the model ONLY when the ticket is paused/blocked to triage from evidence, record the decision in planning/<ticket>/transcript.md, and resume. Use when a workspace was spun up for the autonomous loop or Cody asks what a loop ticket is doing.
---

# /loop-watch <TICKET>

You are the **coordinator** for one ticket (field guide, tier 1). Two rules govern this pane:

1. **Visibility is free.** The live view is `runner.py watch`, a zero-token loop that prints a line
   on every change (stage, task/attempt, worker, elapsed, PR, CI, supervisor, spend) and a heartbeat
   every 5 minutes. You do not poll state files yourself and you do not summarise the same state
   twice. (A watcher that re-read state every few minutes on a flagship model cost $151 in one day.)
2. **You act only on `paused` / `blocked`.** Everything else is the runner's business.

Paths: scripts `~/workspace/agentfiles/common/skills/agent-loop/scripts` (`R`), state
`~/.local/state/agent-loop` (`S`), worktree = this pane's cwd, transcript
`<worktree>/planning/<ticket-lower>/transcript.md` (append-only; create if missing).

## On start (once)

1. `cd $R && python3 runner.py supervise status --ticket <TICKET>`; if **not loaded**:
   `python3 runner.py supervise start --ticket <TICKET> --worktree <cwd>`.
2. `python3 runner.py watch --ticket <TICKET> --once` and print its line verbatim.
3. Append `## <UTC ISO> coordinator started` to the transcript.
4. **Hand the pane to the live view**: run `python3 runner.py watch --ticket <TICKET>` in the
   FOREGROUND with a long timeout (it exits by itself at Human Gate 1). Cody reads its output.
   Wrap it: `python3 runner.py watch --ticket <TICKET> 2>&1 | tee -a <worktree>/planning/<t>/watch.log`
   is fine. When the command returns, go to **Triage** (if paused/blocked) or **Gate** (if READY).

Because `watch` blocks, use the idle loop (LoopCreate triggerType "idle", trigger "idle", recurring)
ONLY as a fallback wake: on each idle wake run `watch --once`; if the state is paused/blocked run
Triage, otherwise re-enter the foreground `watch` and say nothing else. **Never pause or complete
the loop yourself** except at `READY FOR CODY`; there is no state in which this pane should be
silent while the ticket is not at the gate.

## Triage (the only time you spend tokens)

When `watch` shows **paused** or **blocked**, read the reason line and classify:

- `resource:` / `heavy lane` / `slot` → transient. `python3 runner.py resume --ticket X --from implement`.
- `plan failed` / `budget exceeded` → read `planning/<t>/plan-review.md` BLOCKERS + `tasks.toml`.
  Mechanical blockers → fix `tasks.toml` yourself (smallest edit), validate:
  `python3 -c 'import plan,pathlib; print(plan.validate_manifest(pathlib.Path("planning/<t>/tasks.toml")))'`
  must print `[]`, then `resume --from implement`. Otherwise `resume` (re-plan) with your notes
  appended to plan.md under `## Coordinator notes`.
- planner/premium `blocked` with an owner question → answer FROM EVIDENCE (ticket text, the repo
  skill, a shipped sibling; quote the line), write it under `## Coordinator decisions` in plan.md,
  `resume`. If evidence genuinely conflicts: print `NEEDS CODY: <question>` + both pieces of
  evidence, append to transcript, and re-enter `watch` (do not resume).
- worker asked for a **browser / tool it does not have** (e.g. "grant this session a browser tool")
  → the task is mis-scoped as a worker task. Add `visual = true` if it is a screenshot task, or move
  the browser check into a system test the task owns; `resume --from implement`.
- `gates failed` → read `$S/tickets/<T>/gates.out`; real failure → append a fix task (id `8nn`,
  allowed_files = branch's changed files, may_edit_tests true, verification = the failing command),
  `resume --from implement`; environmental → `resume --from gates`.
- `publish failed` / `reply failed` / traceback / `adjudication unusable` / `verification ... passes
  on re-run` → **runner defect**. Print `RUNNER DEFECT: <reason>`, append to transcript, do NOT patch
  `$R`, and **re-enter the foreground `watch`** -- never stop the loop. The operator fixes the runner
  and runs `runner.py resume`; `watch` will print the state change and you continue from there.
  A pane that stops "pending operator repair" goes stale the moment the operator resumes the ticket
  from elsewhere (ZIP-7872 showed a 7-hour-old pause while the runner shipped three tasks).

Every decision = one transcript entry `## <UTC ISO> decision — <one line>` with reason/evidence/action.
Then re-enter the foreground `watch`.

## Gate

When `watch` prints `READY FOR CODY`, append the PR URL and `ci.json.tier3_pending` (the design
decisions Cody must confirm) to the transcript and print them once. Then stop the loop; the pane
is done until Cody reviews.

## Rules

- Never commit, push, comment on GitHub, or touch Jira from this pane. Never edit application code
  or anything under `$R`. Never delete attempt records or `planning/`. Never `git reset`/`checkout`.
- `touch $S/PAUSE` is Cody's kill switch; if present, say so and re-enter `watch`.
- Keep model output to the decision lines above. The watch loop is the pane's voice.
- Your own pi session writes `.pi/` state into the worktree; it is git-excluded. Never format,
  lint, or otherwise touch `.pi/**` or `planning/**` files -- they are not source.
