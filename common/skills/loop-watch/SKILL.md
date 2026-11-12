---
name: loop-watch
description: Use when a workspace was launched for one configured autonomous agent-loop ticket, a runner state-change event arrives, or the user asks what that ticket is doing.
---

# /loop-watch <TICKET>

Per-ticket coordinator surface. It is not fleet control or a background implementation agent. Read
the required [Operations policy](../agent-loop/references/operations-policy.md) before triage or a
handoff.

## Configuration gate

The loop requires a valid ignored `hopper.toml` in the agent-loop skill directory. If configuration
cannot load, report that failure and take no action. Never use `hopper.example.toml` as a runtime
fallback or put credentials in either hopper file.

## Start with one snapshot

Set `TICKET` and `R` to the requested ticket and local scripts directory:

```bash
TICKET="${1:?ticket key required}"
R="<agentfiles checkout>/common/skills/agent-loop/scripts"
python3 "$R/runner.py" watch --ticket "$TICKET" --once
python3 "$R/runner.py" supervise status --ticket "$TICKET"
```

Report the observed line and stop. Do not start or restart a missing supervisor; open a workspace,
browser tab, terminal, agent, loop, or live view; or edit runtime records, manifests, attempts,
leases, planning rulings, or application files. Preserve timestamped observations and evidence in
the approved local record without rewriting prior evidence.

## Event-driven visibility

Run one further snapshot only for an explicit user status request, an actual runner or platform
state-change event, or an authorized bounded check:

```bash
python3 "$R/runner.py" watch --ticket "$TICKET" --once
```

Then go idle. Do not create a model timer, repeatedly read state files, or create a watcher. An
existing runner-owned view may be read. Opening a new surface needs explicit user request or
higher-authority authorization.

## Evidence-based recovery

When the ticket is progressing, waiting on bounded recovery, foreground-owned, or held by a live
writer, report that fact and do nothing. Do not compete with the runner or edit its manifest.

For `paused` or `blocked`, preserve the exact observed reason. Let routine recovery proceed through
the runner and reconciler. If observed evidence identifies a runner defect or traceback and
reporting it is authorized, use the supported path:

```bash
REASON='paste the exact observed runner failure summary here'
python3 "$R/runner.py" report-defect --ticket "$TICKET" --reason "$REASON"
```

Do not run a repair, clear a recovery, mark a defect approved, fabricate a user ruling, or resume a
ticket merely because it is paused. Planning spend is advisory. Only a complete, validated product
or API ambiguity waits for a user decision. A mechanical recovery that exhausts ordinary retries
needs a runner defect, not a coordinator ruling. If an active recovery already owns the problem,
leave that repair in control.

## Direct user steering and gates

A real interactive user message in this worktree is foreground work. The interactive lane waits for
any same-worktree writer to yield and leaves other autonomous tickets running. Do not run worktree
tools during that handoff. Once established, follow the direct user request under normal repository
and higher-authority gates. It returns to autonomy only through:

```text
/agent-loop-auto
```

Automated wakes, RPC, headless input, and `/loop-watch` startup do not claim foreground ownership.
Preserve legacy `HUMAN` manual work. If a snapshot reports a ready pull request, report the observed
pull request and remaining gates once. Do not mark ready, post, merge, deploy, or open new review
tools without explicit higher-authority authorization.

## Never cross the role boundary

Do not admit or park tickets, alter capacity, restart foreground supervision, reclaim a lease by
age, erase `HUMAN`, or claim repair, publication, or Human Gate 1 completion without the receipts
in the shared reference.
