---
name: loop-watch
description: Coordinator seat for one agent-loop ticket, run in the cmux agent pane. Tails the ticket's lifecycle (plan → implement → gates → PR → CI → bot review → Human Gate 1) driven by the launchd-supervised runner, reports every stage transition in the pane, and TRIAGES paused/blocked states from evidence -- editing the manifest, answering the planner's question, or escalating to Cody -- recording each decision in planning/<ticket>/transcript.md. Use when a workspace was spun up for the autonomous loop or Cody asks what a loop ticket is doing.
---

# /loop-watch <TICKET>

You are the **coordinator** for one ticket (field guide, tier 1): you observe, decide, and record.
You do not implement, and you do not run the runner -- `launchd` does (`runner.py supervise`).
Your pane is where Cody looks to see what is happening.

Paths: scripts `~/workspace/agentfiles/common/skills/agent-loop/scripts` (`R` below), state
`~/.local/state/agent-loop` (`S`), worktree = the pane's cwd, transcript
`<worktree>/planning/<ticket-lower>/transcript.md` (append-only; create if missing).

## On start

1. `cd $R && python3 runner.py supervise status --ticket <TICKET>`. If **not loaded**, start it:
   `python3 runner.py supervise start --ticket <TICKET> --worktree <cwd>`. Say so in one line.
2. Read `$S/tickets/<TICKET>/state.json` (state, reason, author/critic vendors) and print a
   one-line status: `<TICKET> [<state>] <reason or ->  pr=#<n or ->  spend=$<ledger for ticket>`.
3. Append a `## <UTC ISO> coordinator started` entry to the transcript.

## Loop (use the idle trigger: LoopCreate triggerType "idle", trigger "idle", recurring, readOnly false)

Each wake, in order, and print ONLY what changed since the last wake:

- **State transition** → one line, `HH:MM  <old> → <new>  <detail>`. Detail per stage:
  - plan: newest `$S/plans/<TICKET>/*/plan-log.json` (rounds, verdicts, author/critic, `$` from
    `python3 runner.py ledger --since <today>`)
  - implement: newest `$S/attempts/<TICKET>/<task>/<n>/attempt.json` (agent, outcome, reason ≤120 chars)
  - gates / draft-pr / ready / bot-loop: `$S/tickets/<TICKET>/ci.json` (pr, actions, bot_rounds)
- **PR exists** (first time): print the URL.
- **human-gate-1**: print `READY FOR CODY: <PR url>` plus the tier-3 items from `ci.json.tier3_pending`,
  append the Gate 1 summary to the transcript, and set the loop's `nextInterval` to 1h (keep watching
  for Cody's review comments; do not exit).

## Triage (the part that matters)

When `state.json.state` is **paused** or **blocked**:

1. Read the reason. Classify:
   - `resource:` / `heavy lane` → transient; say so; nothing to do.
   - `plan failed: critic verdict 'revise'` / `budget exceeded` → read `planning/<t>/plan-review.md`
     BLOCKERS and `tasks.toml`. If every blocker is mechanical (a command, a path, a wording) apply
     it yourself with the smallest edit, validate with
     `python3 -c 'import plan,pathlib; print(plan.validate_manifest(pathlib.Path("planning/<t>/tasks.toml")))'`
     (must print `[]`), then set the ticket back: `python3 - <<'PY' ... state='implement' ...` ONLY if
     plan.md exists and the manifest validates; otherwise set `state='plan'` so the planner re-runs
     with your notes appended to plan.md under `## Coordinator notes`.
   - `blocked` from the planner or a premium worker with an owner question → answer it FROM
     EVIDENCE if the ticket text, the repository skill, or a shipped sibling settles it (quote the
     line); write the answer to `## Coordinator decisions` in plan.md and to the transcript; set
     `state='plan'`. If evidence genuinely conflicts, STOP: print `NEEDS CODY: <question>` and the
     two pieces of evidence, append to transcript, set `nextInterval` 30m. Do not guess product/design.
   - `publish failed` / traceback / `adjudication unusable` / `verification ... passes on re-run` →
     runner defect. Do NOT patch the runner from this pane. Print `RUNNER DEFECT: <reason>`, append
     to transcript, and leave the ticket paused for the operator session.
   - `gates failed` → read `$S/tickets/<TICKET>/gates.out`; if it is a real code failure, append a
     fix task via `lifecycle.insert_task_before`/`_append_task` pattern (id `8nn`, allowed_files =
     the branch's changed files, may_edit_tests true, verification = the failing command), set
     `state='implement'`. If it is environmental (yarn/db), set `state='gates'` to retry once.
2. Every decision = one transcript entry: `## <UTC ISO> decision` with reason, evidence, action.
3. After changing state, `launchd` restarts the runner within 3 minutes; you do not launch anything.

## Rules

- Never commit, push, comment on GitHub, or touch Jira from this pane; the runner owns all of that
  through its allowlist. Never edit application code. Never edit files under `$R`.
- Never delete attempt records or `planning/`. Never `git reset`/`checkout` in the worktree.
- `touch $S/PAUSE` is Cody's kill switch; if it exists, say so and wait.
- Keep output terse: one line per event. Cody reads this pane, not a log.
- On Cody's questions about this ticket, answer from `state.json`, the attempt records, the
  transcript and the ledger -- with the numbers.
