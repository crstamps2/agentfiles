---
name: agent-loop
description: Use when the user asks about autonomous fleet status, admission, recovery, spending, or product/API decisions across configured agent-loop tickets.
---

# /agent-loop [status | start | resume | pause | unpause | stop | decide | spend | ledger | hopper]

Fleet operator surface for configured autonomous work. Read the required
[Operations policy](references/operations-policy.md) before a control or maintenance action. Read
[the operator guide](README.md) before creating or changing local configuration.

## Configuration gate

The runner requires the ignored local `hopper.toml` beside the tracked `hopper.example.toml`. If the
local file is missing, invalid, or incomplete, stop and report the configuration failure. Do not run
against the sample, synthesize defaults, or create a local configuration from service credentials.
The sample is never a runtime fallback. Credentials remain in environment variables or the approved
credential store.

## Role and capacity

- These limits govern autonomous fleet operation. A direct user request for one worktree is
  foreground work and follows the shared ownership policy.
- `cohort.capacity` is the configured admission limit. Worker slots, workspace limits, code
  fallbacks, and hard caps do not authorize a larger fleet.
- Foreground work is outside the autonomous cohort. Do not stop, park, or resume unrelated work to
  make room for it. Only `/agent-loop-auto` in the owning foreground pane returns a ticket to
  normal autonomous admission.
- Higher-authority repository, security, communications, readiness, review, and deployment policy
  wins. This skill grants no permission to post, mark ready, assign reviewers, merge, or deploy.

## Bounded status

Set `R` to the scripts directory in the local checkout:

```bash
R="<agentfiles checkout>/common/skills/agent-loop/scripts"
python3 "$R/runner.py" overview
python3 "$R/runner.py" supervise status
```

Report autonomous cohort slots, foreground ownership separately, real product or API decisions,
active recovery or repair waits, verified Human Gate 1 candidates, and the next eligible admission.
Do not open panes, tabs, agents, watches, or loops merely to obtain status. Do not model-poll.

## Explicit operations

### Admission

Preview before an explicitly authorized fleet start:

```bash
python3 "$R/runner.py" drip --dry-run
```

Run `python3 "$R/runner.py" drip` only on an explicit user request or existing higher-authority
fleet authorization. It can create workspaces and surfaces. It must not exceed the configured
cohort capacity.

### Autonomous resume

```bash
python3 "$R/runner.py" resume --ticket "$TICKET"
```

Use only for an autonomous ticket after reading evidence. Do not override foreground ownership, a
legacy `HUMAN` takeover, a live writer lease, an active recovery contract, or a repair wait.
`resume --all` intentionally skips foreground and `HUMAN` ownership.

### Actual product or API guidance

After the user supplies guidance to a validated product or API decision:

```bash
python3 "$R/runner.py" decision-answer --ticket "$TICKET" --guidance "$GUIDANCE"
```

Do not invent a ruling to clear planning spend, a retry budget, a bad manifest, a runner defect, or
ordinary recovery. Planning spend is advisory; routine recovery is bounded and evidence-based.

## Other operator commands

Only run control actions on explicit authorization. Status and spend remain read-only.

| Request | Supported command from `$R` |
| --- | --- |
| Stop one autonomous supervisor, preserving ticket state | `python3 "$R/runner.py" supervise stop --ticket "$TICKET"` |
| Deliberately park and release a slot | `python3 "$R/runner.py" park --ticket "$TICKET"` |
| Inspect admission without starting work | `python3 "$R/runner.py" drip --dry-run` |
| Cost reporting | `python3 "$R/runner.py" spend` or `python3 "$R/runner.py" ledger` |
| Inspect configuration | Read the ignored `hopper.toml`; edits require explicit scope and verification. |

Global pause and unpause use the configured state root's existing pause marker. They are not
permission to alter ticket markers, force a handoff, or kill an active attempt. Use
`python3 "$R/runner.py" <verb> --help` for supported options.

## Hard boundaries

- Never hand-edit state, cohort, attempt, recovery, manifest, lease, `HUMAN`, or foreground
  ownership records. Do not delete planning or repair evidence.
- Do not unconditionally restart supervision or create a coordinator pane during status or resume.
- Routine defects belong to automatic repair. Explicit user-directed fixes use the shared
  maintenance preflight. Never dirty its checkout, advance HEAD, stop, or bypass another live
  repair. Never use `repair-approved` on an open defect.
- Never reclaim a lease from age alone. PID and boot identity evidence are required.
