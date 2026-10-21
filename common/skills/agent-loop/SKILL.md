---
name: agent-loop
description: Use when Cody asks about autonomous fleet status, admission, recovery, spending, or product/API decisions across agent-loop tickets.
---

# /agent-loop [status | start | resume | pause | unpause | stop | decide | spend | ledger | hopper]

Fleet operator surface for autonomous work. The canonical policy is
[Operations policy](references/operations-policy.md). Read that required reference before control
or maintenance actions.

## Role and capacity

- These limits govern autonomous fleet operation. A direct user request to fix the runner switches
  to authorized maintenance under the shared preflight. Act on that approval rather than stopping
  at diagnosis or asking again. Ticket application work uses foreground ownership.
- Production autonomous capacity is **2**. The `Config` fallback of 4 is an omitted-config
  default, not a production quota. The legacy drip count is also 2.
- Foreground work is outside the autonomous cohort. Do not stop, park, or resume unrelated work to
  make room for it. Only `/agent-loop-auto` in the owning foreground pane returns a ticket to
  normal autonomous admission.
- Higher-authority repository, security, communications, readiness, and deployment policy wins.
  This skill grants no permission to post, mark ready, assign reviewers, merge, or deploy.

## Bounded status

```bash
R="$HOME/workspace/agentfiles/common/skills/agent-loop/scripts"
cd "$R"
python3 runner.py overview
python3 runner.py supervise status
```

Report the autonomous slots, foreground ownership separately, real product/API decisions, active
recovery or repair waits, verified Human Gate 1 candidates, and next admission. Do not open panes,
tabs, agents, watches, or loops merely to get status. Do not model-poll.

## Explicit operations

### Admission

Preview before an explicitly authorized fleet start:

```bash
cd "$R"
python3 runner.py drip --dry-run
```

Run `python3 runner.py drip` only on an explicit user request or existing higher-authority fleet
authorization. It can create workspaces and surfaces. It must not admit a third autonomous ticket.

### Autonomous resume

```bash
cd "$R"
python3 runner.py resume --ticket "$TICKET"
```

Use only for an autonomous ticket after reading evidence. Do not override foreground ownership, a
legacy `HUMAN` takeover, a live writer lease, an active recovery contract, or a repair wait.
`resume --all` intentionally skips foreground and `HUMAN` ownership.

### Actual product/API guidance

After Cody supplies real guidance to a validated product/API decision:

```bash
cd "$R"
python3 runner.py decision-answer --ticket "$TICKET" --guidance "$GUIDANCE"
```

Do not invent a ruling to clear planning spend, a retry budget, a bad manifest, a runner defect, or
an ordinary recovery. Planning spend is advisory; routine recovery is bounded and evidence-based.

## Other operator commands

Only run control actions on explicit authorization; status and spend remain read-only.

| Request | Supported command from `$R` |
| --- | --- |
| Stop one autonomous supervisor, preserving ticket state | `python3 runner.py supervise stop --ticket "$TICKET"` |
| Deliberately park and release a slot | `python3 runner.py park --ticket "$TICKET"` |
| Inspect admission without starting work | `python3 runner.py drip --dry-run` |
| Cost reporting | `python3 runner.py spend` or `python3 runner.py ledger` |
| Inspect configured hopper | Read `../hopper.toml`; edits require explicit scope and verified commit. |

Global pause/unpause uses the existing operator kill switch: explicitly requested pause creates
`~/.local/state/agent-loop/PAUSE`; unpause removes only that file. It is not permission to alter
any ticket marker, force a handoff, or kill an active attempt. Supervisor stop is not foreground
handoff. Use `runner.py <verb> --help` for supported options.

## Hard boundaries

- Never hand-edit state, cohort, attempt, recovery, manifest, lease, `HUMAN`, or foreground
  ownership records. Do not delete planning or repair evidence.
- Do not unconditionally restart supervision or create a coordinator pane during status or resume.
- Routine defects belong to automatic repair. Explicit user-directed fixes use the shared
  maintenance preflight. Never dirty its checkout, advance HEAD, stop, or bypass another live
  repair. Never use `repair-approved` on an open defect.
- Never reclaim a lease from age alone. PID and boot identity evidence are required.
