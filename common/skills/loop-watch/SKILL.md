---
name: loop-watch
description: Use when a workspace was launched for one autonomous agent-loop ticket, a runner state-change event arrives, or Cody asks what a specific loop ticket is doing.
---

# /loop-watch <TICKET>

Per-ticket coordinator surface. It is not fleet control or a background implementation agent. The
required canonical policy is [Operations policy](../agent-loop/references/operations-policy.md).
Read it before triage or a handoff.

## Start with one snapshot

These commands are copyable after setting `TICKET` to the requested key:

```bash
TICKET="${1:?ticket key required}"
R="$HOME/workspace/agentfiles/common/skills/agent-loop/scripts"
cd "$R"
python3 runner.py watch --ticket "$TICKET" --once
python3 runner.py supervise status --ticket "$TICKET"
```

Report the observed line and stop. Do not start or restart a missing supervisor, open a cmux tab,
browser tab, terminal, agent, loop, or live view, or edit runtime records, manifests, attempts,
leases, plan rulings, or application files. Append timestamped observations, evidence paths, and
actual actions to `planning/<ticket-lower>/transcript.md`; do not rewrite prior evidence.

## Event-driven visibility

Run one further snapshot only for an explicit user status request, an actual runner/platform
state-change event, or an authorized bounded check:

```bash
cd "$R"
python3 runner.py watch --ticket "$TICKET" --once
```

Then go idle. Do not create a model timer, repeatedly read state files, or create a watcher. An
existing runner-owned view may be read. Opening any new surface needs an explicit user request or
higher-authority authorization.

## Evidence-based recovery

When the ticket is progressing, waiting on bounded recovery, foreground-owned, or held by a live
writer, report that fact and do nothing. Do not compete with the runner or edit its manifest.

For `paused` or `blocked`, preserve the exact observed reason. Let routine recovery proceed through
the runner and reconciler. If observed evidence identifies a runner defect or traceback and
reporting it is authorized, use the supported path:

```bash
REASON='paste the exact observed runner failure summary here'
cd "$R"
python3 runner.py report-defect --ticket "$TICKET" --reason "$REASON"
```

Do not run a repair, clear a recovery, mark a defect approved, fabricate a Cody ruling, or resume a
ticket merely because it is paused. Planning spend is advisory. Only a complete, validated
product/API ambiguity waits for Cody; report that evidence to Cody rather than answering for Cody. A mechanical recovery that exhausts
ordinary retries needs a runner defect, not a coordinator ruling. If no active recovery already
owns it, report the observed defect through `report-defect`; otherwise leave the existing repair
in control.

## Direct user steering and gates

A real interactive user message in this worktree is foreground work. The interactive-lane extension
claims it, waits for any same-worktree writer to yield, and leaves other autonomous tickets running.
Do not run worktree tools during that handoff. Once established, follow the direct user request
under normal repository and higher-authority gates. It returns to autonomy only through:

```text
/agent-loop-auto
```

Automated wakes, RPC, headless input, and `/loop-watch` startup do not claim foreground ownership.
Preserve legacy `HUMAN` manual work. If a snapshot reports `READY FOR CODY`, report the observed PR
and remaining gates once. Do not mark ready, post, merge, deploy, or open new review tools without
explicit higher-authority authorization.

## Never cross the role boundary

Do not admit or park tickets, alter capacity, restart foreground supervision, reclaim a lease by
age, erase `HUMAN`, or claim repair, publication, or Human Gate 1 completion without the receipts
in the shared reference.
