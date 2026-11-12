# Agent loop operator guide

The agent loop is an optional local delivery runner. It advances a configured issue through a
bounded workflow to Human Gate 1: an open, ready-for-review pull request with matching evidence
and no unresolved recovery or decision. It does not assign reviewers, merge, deploy, or expand
communication permissions.

[Operations policy](references/operations-policy.md) is the authoritative policy for ownership,
recovery, maintenance, and evidence. This guide explains the local configuration and normal
operator workflow.

## Configure the loop locally

The public repository contains a tracked, sanitized template:

- `hopper.example.toml` is documentation and a starting point. It contains placeholders only.
- `hopper.toml` is the real local configuration. It is ignored by Git and must never be committed,
  copied into an issue, or included in a support artifact.

Bootstrap the local file from the agentfiles checkout:

```bash
LOOP_DIR="<agentfiles checkout>/common/skills/agent-loop"
cd "$LOOP_DIR"
cp -n hopper.example.toml hopper.toml
```

Complete the local file before using the loop, then set `project.configured = true`. The template
ships with this flag disabled; neither a fresh copy nor passing the template directly can start
the runtime. `cp -n` preserves an existing local file. The runner loads `hopper.toml` only. If that file is
missing, unreadable, invalid, or incomplete, commands fail closed before admission, workspace
creation, supervisor changes, or external writes. The sample is never an automatic fallback and
must not be passed to the runner as a substitute for local configuration.

Keep credentials, API tokens, and private host authentication in environment variables or the
approved credential store. Do not put them in `hopper.example.toml`, `hopper.toml`, prompts, logs,
or planning artifacts.

### Required project configuration

The template preserves the existing operational tables for hopper selection, paths, timeouts,
admission, budgets, protection, worker arms, and outcomes. Fill every required placeholder in
those tables for the local environment.

The required `[project]` values identify the remote project separately from the local workspace:

| Setting | Purpose |
| --- | --- |
| `project.configured` | Set to `true` only after checking the local project settings. |
| `project.github_repo` | Repository slug, for example `example-org/example-app`. |
| `project.jira_base_url` | Full issue browse-link base, ending in `/browse`, for example `https://example.atlassian.net/browse`. |
| `project.operator_email` | Account identity used by configured project services. |
| `project.worktrees_root` | Local parent directory for managed worktrees. |
| `workspace.repo` | Existing local checkout of the application repository. |

`project.github_repo` is an external repository identity. `workspace.repo` is a local filesystem
path. Set both; do not infer one from the other. The optional `project.circleci_project` overrides
the CI project when the default derived from `github_repo` is not correct. `project.pr_footer` and
`project.house_style_example_pr` are optional local project conventions.

The template also documents optional drip, cohort, workspace backend, and workspace status
settings. Keep organization-specific issue keys, board statuses, local paths, and service settings
only in `hopper.toml`.

## Where things are

| What | Location or source of truth |
| --- | --- |
| Runner modules and tests | `scripts/` (`python3 runner.py ...`; tests: `python3 -m unittest discover -p 'test_*.py'`) |
| Public configuration template | `hopper.example.toml` |
| Private local configuration | ignored `hopper.toml` |
| Model tiers and agent definitions | `common/model-tiers.toml` and `common/agents/` |
| Fleet operator skill | `/agent-loop` and `SKILL.md` |
| Per-ticket coordinator skill | `/loop-watch <TICKET>` in `common/skills/loop-watch/` |
| Runtime state and recovery evidence | configured `[paths].state_root` |
| Per-ticket planning artifacts | local worktree `planning/<ticket>/`; never committed |
| Supervisors | platform-local service definitions configured by the operator |

Runtime state is private operational evidence. It can include ticket snapshots, workspace bindings,
attempt records, logs, locks, recovery contracts, repair records, usage data, and screenshots.
Preserve it when investigating a failure. Do not commit, delete, hand-edit, or use it to recreate a
configuration file.

## Intake, workspaces, and capacity

### Intake and workspace bindings

The drip selects eligible issues from the configured tracker scope and order. It applies local
exclusions and dependency checks before admission. An admitted issue receives a workspace binding
that records the chosen backend and resource identities.

- `[workspace].backend` applies only to new, unbound issues.
- A recorded binding is immutable for that issue. Changing the default does not relocate it.
- An existing workspace without a binding is treated as legacy state. Inspect evidence before any
  recovery; do not delete the binding or create a replacement workspace to force a backend change.
- `[workspace].repo` must name the configured local application checkout. Workspace status mappings
  are project-specific local configuration.
- Optional views and notifications are observational. Their failure must not create a second agent,
  replay a completed launch, or switch backends.

### Capacity distinctions

The autonomous cohort, worker concurrency, workspace availability, and foreground work are
separate limits:

- `cohort.capacity` is the configured number of autonomously admitted issues.
- `arms.workers_parallel` limits worker-stage concurrency. It is not permission to admit more
  issues than the cohort capacity.
- `workspace.max_open` limits local workspace count. It is not an admission limit.
- A direct interactive request owns its worktree as foreground work outside the autonomous cohort.
  It must not evict, park, or displace unrelated autonomous work.

A cohort member keeps its slot from reservation through normal waits, recovery, provider delays,
rebase repair, valid product decisions, and legacy `HUMAN` takeovers. A slot releases only after a
verified Human Gate 1 receipt: the pull request is open and ready, its evidence SHA matches the
worktree HEAD, and no recovery or decision remains open. Draft pull requests, stale evidence, and
remote errors do not release capacity.

Implementation fallbacks and hard caps are safety rails, not operational authorization. The local
configuration is the only source of the intended operating limit. Repair work runs outside the
cohort and cannot use a repair to admit extra application work.

## Lifecycle and safeguards

```text
spinup -> plan -> plan-review -> implement -> gates -> draft-pr -> ready -> bot-loop -> human-gate-1
```

- Planning produces an evidence-backed plan and bounded task manifest. A review revision changes
  the manifest through the designated planning path, not by bypassing review.
- Implementation follows explicit dependencies and deterministic task order. Each accepted task
  is guarded, committed with its scoped changes, and verified before the next stage advances.
- A rejecting verification guard is rerun on the base tree. If it also fails there, treat the guard
  or manifest as the problem rather than charging the implementation attempt.
- Gates use the target repository's documented checks. A green formatter, stale log, or status
  label does not prove a failed gate is fixed.
- Draft pull requests remain drafts until the configured checks and review automation have run.
  The loop never assigns reviewers, merges, or deploys.
- `planning/` and harness artifacts are local evidence. They must not enter a commit or pull request.

## Recovery, decisions, and proof

Every failure records its evidence before it enters a structured recovery contract, such as a task
retry, stage resume, republish, replan, spinup retry, or provider wait. The reconciler consumes that
contract. Do not infer a resume target from free-form state text or use `resume` to bypass an active
contract.

A decision wait is reserved for a real product or API ambiguity with sources checked, materially
different options, consequences, a recommendation, one question, and a durable resume point.
Ordinary failures, spent advisory budgets, malformed manifests, provider waits, and runner defects
are recovery work, not reasons to manufacture a decision.

Use precise evidence claims:

| Claim | Minimum evidence |
| --- | --- |
| Resumed | Recovery or supervisor receipt and a resulting lifecycle snapshot. |
| Accepted | Exact worker attempt accepted for its task identity and slug. |
| Committed | Local commit SHA and scoped diff. |
| Pushed | Remote branch evidence matching the claimed SHA. |
| Verified Human Gate 1 | Open ready pull request, matching worktree SHA, no active recovery or decision, and recorded gate evidence. |

Do not collapse these states into "fixed." State what remains unverified.

## Daily operation

Set `R` to the scripts directory in the local agentfiles checkout before using runner commands:

```bash
R="<agentfiles checkout>/common/skills/agent-loop/scripts"
```

Read-only inspection:

```bash
python3 "$R/runner.py" overview
python3 "$R/runner.py" supervise status
python3 "$R/runner.py" drip --dry-run
python3 "$R/runner.py" watch --ticket "$TICKET" --once
```

Explicitly authorized control operations:

```bash
python3 "$R/runner.py" drip
python3 "$R/runner.py" resume --ticket "$TICKET"
python3 "$R/runner.py" decision-answer --ticket "$TICKET" --guidance "$GUIDANCE"
python3 "$R/runner.py" supervise stop --ticket "$TICKET"
python3 "$R/runner.py" park --ticket "$TICKET"
python3 "$R/runner.py" spend
python3 "$R/runner.py" ledger
```

### Controlled recovery and migration

These commands change durable fleet state. Use them only through the authorized recovery or
maintenance process and only after reading the current evidence:

| Purpose | Command |
| --- | --- |
| Preview a cohort migration | `python3 "$R/runner.py" cohort-migrate --dry-run` |
| Apply or roll back a reviewed migration | `python3 "$R/runner.py" cohort-migrate --apply` or `python3 "$R/runner.py" cohort-migrate --rollback "$SNAPSHOT"` |
| Deliberately clear an active recovery contract | `python3 "$R/runner.py" recovery-clear --ticket "$TICKET" --resume-from "$STAGE"` |
| Wake tickets after an approved protected repair | `python3 "$R/runner.py" repair-approved --defect "$DEFECT" --sha "$SHA"` |
| Record one already-existing verified workspace binding | `python3 "$R/runner.py" workspace-adopt --ticket "$TICKET" --backend orca --workspace "$WORKSPACE_ID" --agent-terminal "$TERMINAL_HANDLE"` |

`workspace-adopt` records one explicitly identified existing resource. It does not discover,
launch, prompt, or relocate a workspace. `repair-approved` does not approve a repair: it records
the result of a separately authorized and independently verified protected-path change. A migration
must preserve active bindings, ownership, admission order, and recovery evidence.

Use `/agent-loop` for fleet status and explicitly requested operations. Use `/loop-watch <TICKET>`
for one bounded, event-driven ticket snapshot. Pause and unpause are explicit operator actions that
use the configured state root; they do not authorize editing ticket markers, killing attempts, or
forcing a foreground handoff.

Before runner maintenance, inspect the candidate branch, runtime status, and active repair lease.
Do not alter a live checkout while repair owns its lease. For runner changes, run the full suite from
`$R`:

```bash
cd "$R"
python3 -m unittest discover -p 'test_*.py'
```

## Non-negotiable boundaries

- Never hand-edit configuration-derived runtime records, cohort membership, bindings, attempts,
  manifests, locks, recovery records, `HUMAN`, or foreground ownership files.
- Never restart a supervisor, reopen a workspace, create an agent, or send a prompt merely to
  observe status.
- Never use a local sample, default, stale dashboard, worker slot count, or workspace limit to
  override the configured cohort limit.
- Never fabricate a task result, repair landing, commit, publication, review, or Human Gate 1
  receipt.
- Keep local configuration, runtime evidence, credentials, and project-specific artifacts out of
  version control and external communication.
