# Agent-loop operations policy and recovery evidence

This is the canonical policy shared by `/agent-loop` and `/loop-watch`. It governs authority,
ownership, recovery, maintenance, and evidence. It does not override higher-authority repository,
security, communications, readiness, or deployment instructions.

## 1. Configuration and authority boundary

The loop is local, project-specific automation. Its public template is not an operational
configuration.

- `hopper.example.toml` is tracked and sanitized. It documents the required shape.
- `hopper.toml` is ignored local configuration. The runner must load this file before it can act.
- A missing, unreadable, invalid, or incomplete local file is a fail-closed condition. Do not point
  the runner at the sample, synthesize defaults, or create state from the sample.
- Credentials belong in environment variables or the approved credential store. They do not belong
  in either hopper file, a runtime record, a prompt, or an external artifact.

| Actor or surface | May do | Must not do |
| --- | --- | --- |
| Direct user steering | Own one worktree as foreground work and direct a bounded change. | Return that worktree to autonomy automatically or displace unrelated autonomous work. |
| `/agent-loop` | Observe and explicitly operate the autonomous fleet. | Coordinate one application issue or claim foreground ownership. |
| `/loop-watch` | Observe one issue from bounded runner evidence and escalate facts. | Admit issues, create observation surfaces, restart supervision, or run implementation work. |
| Runner, reconciler, and repair service | Apply durable contracts and allowlisted automation. | Expand communication, readiness, review, merge, or deployment permission. |

A direct user request takes precedence over autonomous startup for its worktree. Delegated work uses
the configured agent definition and approved integration path; a helper or coordinator must not
silently replace its model, tool restrictions, or reviewer authority.

## 2. Capacity and ownership

### Autonomous capacity

Treat the configured cohort capacity as the operating limit. Worker concurrency, workspace limits,
implementation fallbacks, and hard caps are separate safeguards, not authorization to increase that
limit.

- A ticket retains its cohort slot from reservation through waits, retries, structured recovery,
  provider delays, rebase repair, valid decision waits, and legacy `HUMAN` takeover.
- Human Gate 1 is the normal release path. It requires an open, ready pull request, evidence SHA
  equal to the worktree HEAD, and no active recovery or open decision.
- A draft pull request, remote error, stale SHA, or dashboard label does not release a slot.
- Repair work is outside the cohort. It cannot create capacity for additional application work.

### Foreground ownership

Persistent foreground ownership is outside the autonomous cohort. It does not consume a cohort slot
and must not stop unrelated autonomous tickets.

The interactive lane may claim foreground control only after real interactive user input. Coordinator
startup, extension wakes, RPC, headless processes, and automated input do not claim it. If the same
worktree has a live writer, foreground tools wait for that writer to yield.

Autonomy resumes only when the foreground owner explicitly invokes `/agent-loop-auto`. Returning an
issue queues it for normal admission; it does not create an extra slot. Idleness, a reload, an
automated wake, or a coordinator action never returns a ticket to autonomy.

### Legacy and lease preservation

- Preserve a legacy `HUMAN` takeover. Do not delete it, restore over manual edits, or run
  background recovery against it.
- Use the supported interactive handoff. Do not manually acquire or rewrite writer leases.
- A timestamp alone is not proof that a lease is stale. Reclaim only through the supported lease
  path after the owner is provably gone.
- Never hand-edit cohort records, parked markers, attempt records, state, recovery records, locks,
  or foreground ownership records to force an outcome.

## 3. Visibility and opening tools

Visibility is runner-driven, event-driven, and bounded. No model polls status on a timer.

A coordinator may take one `runner.py watch --once` snapshot in response to an explicit user
request, an actual state-change event, or an authorized bounded check. The fleet may take an
explicit status snapshot. Then the observer goes idle.

Opening a terminal, workspace surface, browser tab, issue page, live watch, agent, or loop is an
action, not harmless visibility. Create one only on an explicit user request or when higher-authority
policy already authorizes it. In particular:

- `/loop-watch` does not create a pane or live view merely because it started.
- `/loop-watch` does not start or restart a supervisor merely because status says it is absent.
- `/agent-loop` status and resume do not open coordinator panes or replace foreground supervision.
- A runner-owned surface that already exists may be read without creating another one.

## 4. Decisions and recovery

### Valid decision waits

A user decision is required only for a genuine product or API ambiguity. A valid decision request
contains:

- sources checked and why they do not resolve the ambiguity;
- at least two materially different options and their consequences;
- a recommendation with reasoning;
- one concrete question; and
- a durable checkpoint and valid resume target.

Do not invent an owner ruling, user answer, or decision request to clear a queue. Direct user
steering is foreground work, not a fabricated decision wait.

### Bounded self-healing

Planning spend is advisory. A warning or planning ceiling does not by itself create a user hold.
Ordinary recovery remains bounded and evidence-based, including retries, environment preparation,
manifest repair, scoped fix tasks, replanning, republishing, provider waits, and runner repair.

For every recovery, preserve the original failure, create the structured contract, and let the
reconciler consume its event. Do not infer a resume target from a free-form reason or issue `resume`
to bypass an active contract.

A runner defect is repair work, not permission to fake approval or alter runtime state. The repair
path must use isolated change preparation, a regression test that fails on the base and passes with
the fix, the relevant runner suite, and the required independent review. A required reviewer or
provider outage is a bounded wait, not permission to silently substitute authority or bypass review.

`repair-approved` is valid only after the authorized process has applied and independently verified
the exact protected-path patch represented by an approval-needed defect. It must not be used on an
open defect, a failed repair, or to make a ticket appear repaired.

## 5. Runtime and manifest safety

- Never edit a live task manifest while its runner writer lease exists.
- Never fabricate an attempt result, accepted state, repair landing, commit, push, review, or Human
  Gate 1 receipt.
- Preserve attempts, planning artifacts, recovery events, repair records, and audit logs. They are
  local evidence, not cleanup targets.
- Do not use reset, forced checkout, deletion of untracked evidence, or an ad hoc runtime edit to
  resolve a coordination conflict.
- Use local evidence paths and bounded, redacted excerpts in recovery reports. Do not copy whole
  state records into prompts or external artifacts. Never expose credentials or tokens.

## 6. Maintenance coordination

Before changing runner or supervisor source, establish the candidate and inspect the fleet from the
local scripts directory:

```bash
git status --short
git rev-parse --show-toplevel
git branch --show-current
git rev-parse HEAD

R="<agentfiles checkout>/common/skills/agent-loop/scripts"
python3 "$R/runner.py" supervise status
```

If repair owns its lease or ownership is uncertain, wait. Prepare proposals outside the live
checkout. Do not edit it, advance its HEAD, stop the service, kill a process, or bypass the lease.
After release, recheck the candidate, branch, cleanliness, and ownership immediately before an
authorized change.

Preserve unrelated changes and concurrent writers. Before comparing history, fetch the current
remote base and compare against that remote base rather than a stale local branch. For runner
changes, run the full suite from `$R`:

```bash
cd "$R"
python3 -m unittest discover -p 'test_*.py'
```

Commit or publish only verified, scoped work owned by the current operator. Before public
publication, run the repository security scan on staged material and obtain any required security
review. A clean scan does not replace review or the communications gate.

## 7. Recovery-proof checklist

Use precise verbs. These states are different:

| Claim | Minimum receipt | Does not prove |
| --- | --- | --- |
| Resumed | Recovery event or supervisor action and resulting lifecycle snapshot. | Acceptance, commit, publication, or review. |
| Accepted | Exact worker attempt accepted by runner guards for its task identity and slug. | Commit or publication. |
| Committed | Exact local commit SHA and scoped diff. | The remote branch contains it. |
| Pushed | Remote branch evidence matching the claimed commit SHA. | CI, review, readiness, or Human Gate 1. |
| Verified Human Gate 1 | Open ready pull request, matching worktree SHA, no active recovery or open decision, and recorded gate evidence. | Human review, merge, or deployment. |

Every recovery report includes:

- original failure and exact failing gate or observed reason;
- recovery or repair identity, including the repair commit when one exists;
- regenerated task identity and verification output when the manifest or guard changed;
- local commit and publication receipts, if claimed;
- gates that remain unverified;
- unavailable or rejected reviews; and
- a clear statement of what is not proved.

If a receipt is unavailable, say so. Do not substitute a stale log, status label, or assumed remote
state.

## 8. Regression standard

Runner changes need behavioral coverage for the affected boundary. A test that passes on the base
tree does not prove a regression was fixed. Preserve or add evidence for relevant cases, including:

- dependency ordering and no dispatch for missing or cyclic prerequisites;
- accepted task identity after replanning;
- base-tree attribution for rejecting guards;
- complete preservation of failed gates across generated fix tasks;
- lease and foreground handoff safety; and
- repair rejection when the proposed regression test already passes on the base.

A green unit test does not authorize communication, readiness, review, merge, or deployment.
