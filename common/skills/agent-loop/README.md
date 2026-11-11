# Agent loop — operator guide

Autonomous delivery of ZIP-6774 (ZUI containers) tickets to **Human Gate 1**: a ready-for-review PR
with CI green and the repo's Claude review bots already addressed. Cody looks at nothing before that.

Required operating authority, ownership, maintenance, and recovery-proof guidance lives in
[Operations policy](references/operations-policy.md), shared by both skill entrypoints. This guide
covers architecture; do not use historical descriptions to override that policy.

Adapted from the "strong reviewer, inexpensive workers" field guide. Everything lives on `agentfiles`
branch `agent-loop-1`, whose checkout is used by launchd. Spinup changes live on the dotfiles branch
`spinup-headless`. `~/.pi/agent/skills/{agent-loop,loop-watch}` are symlinks into this tree.

## Where things are

| what | where |
|---|---|
| runner + all modules + tests | `common/skills/agent-loop/scripts/` (`python3 runner.py …`, tests: `python3 -m unittest discover -p 'test_*.py'`) |
| drip rules (epic, max in flight), per-ticket overrides (deps/exclude/pin_arm), arms, budgets, admission | `common/skills/agent-loop/hopper.toml` |
| model tiers (author/critic/task-writer/workers) | `common/model-tiers.toml` → rendered agent defs in `~/.pi/agent/agents/*.md` (source: `common/agents/*.agent.md`) |
| operator skill (fleet) | `/agent-loop` → `common/skills/agent-loop/SKILL.md` |
| coordinator pane skill (one ticket) | `/loop-watch <TICKET>` → `common/skills/loop-watch/SKILL.md` |
| runtime state | `~/.local/state/agent-loop/`: `tickets/<K>/{state.json,workspace.json,ci.json,heal.jsonl,bad-guards.md,gates.out,screenshots/}`, `attempts/<K>/<task>/<n>/`, `plans/`, `botreview/`, `usage.jsonl`, `locks/`, `launchd/*.log` |
| per-ticket planning artifacts (never committed) | `<worktree>/planning/<key>/{plan.md,tasks.toml,plan-review.md,transcript.md}` (git-excluded via the repo's shared `info/exclude`) |
| supervisors | `~/Library/LaunchAgents/com.cody.agent-loop.<ticket>.plist`, run via the codesigned `~/.local/bin/agent-loop-python` |

## Intake (the drip)

`drip.py`: children of the epic, board Rank order, only the ready column (`Selected for Work`), any
assignee, minus `exclude`d tickets and tickets whose `deps` are not Done in Jira (no autonomous PR
stacks). Each pick uses the shared workspace lifecycle, then assigns to Cody, marks In Progress, and
starts its launchd supervisor. Runs every 15 min as `com.cody.agent-loop.drip` for board discovery.

## Workspace backends and bindings

### Selection

New unbound launches use the configured `[workspace].backend`. Its default is `orca`. The intended
production configuration supplies the local repository and status mapping:

```toml
[workspace]
backend = "orca"
repo = "~/workspace/example-app"

[workspace.statuses]
in_progress = "in-progress"
ready_for_review = "status-5"
```

To intentionally make **future unbound** launches use cmux, set `[workspace].backend` to `cmux`:

```toml
[workspace]
backend = "cmux"
```

A ticket with no binding but an existing worktree or workspace is legacy cmux, regardless of the
current default. A recorded binding is immutable: changing the default never relocates it, and an
explicit backend override that conflicts with it is refused. Do not delete or edit a binding to
force a backend change.

### Orca launch surface

A new Orca workspace requests Pi with `--agent pi --surface chat` and delivers the prompt
`/loop-watch KEY`, with `KEY` replaced by the ticket key. The binding records the actual native
outcome. A structured outcome is recorded as chat with its returned session identity; a host-provided
terminal fallback is retained and reported as that one agent. Do not start a second agent to obtain
chat. If chat is refused before launch, report the limitation rather than falling back to cmux or
another surface.

This request and its transport receipt do not prove that native chat can run `/loop-watch`, load the
Pi extension, or distinguish real-user input from automated or RPC input. Those compatibility checks,
and the first complete real Orca setup lifecycle, remain required rollout evidence.

### Recovery and status

Each ticket binding lives at `tickets/<KEY>/workspace.json`, separately from `state.json`, so a
legacy state writer cannot erase backend identity. It records the selected backend, local worktree,
branch, native workspace and repository identities, terminal and view handles, the actual agent
surface and session identity, phase, and bounded operation intents and receipts. It does not store
terminal histories or credentials.

An existing ticket with workspace/worktree metadata but no binding is legacy cmux. An unreadable, corrupt, or unsupported binding is
an error. For an uncertain phase or pending required operation, inspect the exact recorded resources
and receipts before any recovery. Do not issue a fresh create, agent launch, or prompt delivery; delete
the binding; or route the ticket through the other backend. A definite setup failure may use the
normal recovery path for its recorded workspace, but uncertainty permits observation only until the
identity and operation outcome are resolved.

Browser and reference steps are optional after confirmed agent submission. A failed view is reported
as a warning without blocking launch completion or supervisor startup. Its pending intent and any
receipt remain recorded, and it is not replayed on restart; this exception does not apply to workspace,
setup, dev, or agent operations. Cosmetic observation, notification, and optional view commands use
short timeouts independently of the longer setup and agent-launch budgets.

Shared policy computes the same workspace status for both backends. Verified Ready for Review is
mapped to Orca status `status-5` through `[workspace.statuses].ready_for_review`; it is not the
generic `in-review` status. If that mapping is absent, the board status remains unchanged. Status
presentation, notification, and review routing do not create agents, relaunch workspaces, or change
ownership.

Orca's installed CLI only replaces the whole shared comment, without a conditional update. When a
status change would require that write, presentation reports `orca-comment-update-unsupported`
instead of risking an operator's concurrent note. An already-current status requires no write;
unread notifications remain independent. Comment rendering needs a supported conditional update
or a separately owned status field before it can be enabled safely. Automatic board movement is
limited to In Progress → Ready for Review and separately requires safe native source-status enforcement.

### Metadata enrollment

```bash
runner.py workspace-adopt --ticket KEY --backend orca --workspace EXACT-ID --agent-terminal EXACT-HANDLE
```

This command enrolls metadata for one explicitly named, already-existing resource. It first verifies
the current ticket state, Git worktree, branch and repository, local native workspace, and exact live
agent terminal. It performs no launch, setup, prompt send, supervisor, cohort, lifecycle, or ownership
mutation.

Adoption is idempotent only for the same verified binding and refuses a conflicting one. It does not
auto-discover workspaces, bulk-adopt foreground work, trust stale handover evidence by itself, convert
a terminal to chat, or start a duplicate agent. After a reviewed rollout, enrollment is limited to the
six already-transferred canonical tickets, each freshly verified against current resources.

### Ownership, rollback, and rollout limits

Backend selection preserves the existing foreground and autonomous boundaries. It does not change
cohort capacity, reservation, recovery targets, supervisors, `HUMAN` markers, or the rule that only
`/agent-loop-auto` returns foreground work to autonomous admission. Existing jobs continue on their
recorded backend, and legacy jobs continue through cmux.

Rolling back the configured default affects only future unbound launches. It must not redirect an Orca
binding to cmux-only handling, delete a binding, or relocate a running workspace. During a mixed-version
refresh, older processes can still write `state.json`; the separate binding must remain intact. Do not
activate a new default while an older in-flight launcher could create the same ticket. If backend or
ownership continuity cannot be established, leave rollout pending and report the exact evidence.

This guide describes the candidate operator contract. It does not assert live activation, publication,
or completion of first-use verification.

## The cohort (two production background slots)

`cohort.json` is the only source of capacity truth once migration has created it; before that, the
drip uses the legacy `max_in_flight` count.

- **Two production slots, seven hard ceiling.** Omitted configuration defaults to four; that is
  not the deployed capacity. A ticket owns its slot from reservation until **verified Human
  Gate 1**: state `human-gate-1`, a recorded PR that GitHub reports open and ready, evidence SHA equal
  to the worktree HEAD, no active recovery, no open decision. A draft PR, a GitHub error, or a stale
  SHA releases nothing. Any override above seven is rejected before state changes.
- **Waiting keeps the slot.** `paused`, `blocked`, repairing, retrying, provider-waiting, rebase
  repair, `decision-required`, and `HUMAN` takeovers all retain their slot. Repair work runs outside
  the cohort and never admits work beyond configured capacity. Persistent foreground ownership is
  outside the cohort: its supported same-worktree handoff releases membership without displacing
  unrelated tickets. Preserve legacy `HUMAN` markers; return only through `/agent-loop-auto`.
- **Refill order.** A released slot goes to the oldest parked ticket (by original admission
  sequence), then a reserved spinup retry, then the next board candidate. A failed spinup keeps its
  reservation and retries the same ticket.
- **Reconciler.** `runner.py cohort-reconcile` (launchd, every 60 s via
  `supervise cohort-start`; `cohort-shadow-start` installs it with `--dry-run`) consumes repair
  events, resumes ready recoveries and provider waits, releases verified gates, ensures each member
  has a supervisor or a durable wait, and refills empty slots. No model is involved.
- **Recovery contracts.** Every failure boundary records its defect first, then activates a
  structured contract on the ticket: `retry-task` (exact id + slug), `resume-stage`, `republish`,
  `replan`, `retry-spinup`, or `wait-provider`. One repair landing wakes every ticket linked to that
  defect, exactly once per contract. A rejected repair becomes a diagnostic retry, never an
  abandoned ticket. Nothing parses `state.reason` to decide what to resume.
- **Product decisions.** Only a complete, validated ambiguity (sources, why unresolved, two or more
  different options with consequences, a reasoned recommendation, one question, a checkpoint) enters
  `decision-required`; everything else is repair work. Cody answers with
  `runner.py decision-answer --ticket X --guidance "..."`; the guidance is recorded, appended to
  `plan.md`/`transcript.md`, and the ticket resumes its saved stage (or re-plans) automatically.
- **Migration.** `runner.py cohort-migrate --dry-run` prints chronology (with its evidence source),
  the proposed members up to configured capacity, tickets to park, excluded review-ready tickets, and evidence-backed
  recovery contracts. `--apply` snapshots metadata to `cohort-migrations/`, writes `cohort.json`,
  then parks the rest and stops only their supervisors. `--rollback <snapshot>` restores metadata
  only, and refuses after later admissions or applied events unless `--force` (which snapshots
  first).
- **Park.** `runner.py park --ticket X` writes `PARKED` first, then releases the slot to the oldest
  waiting ticket; the parked ticket resumes first when a slot frees.
- **Operator overrides.** `resume` refuses a ticket with an active recovery contract; use
  `runner.py recovery-clear --ticket X [--resume-from STAGE]` to drop it deliberately. After applying a
  protected-path patch from `defects/needs-approval/` by hand, `runner.py repair-approved --defect ID
  [--sha SHA]` wakes every ticket waiting on it. A defect that keeps failing review is re-queued behind
  newer ones with backoff (one notice), and repair skips entirely while the reviewer's vendor is out of
  quota rather than reviewing on the implementer's vendor.

## Lifecycle (one ticket)

```
spinup → plan → plan-review → implement → gates → draft-pr → ready → bot-loop → human-gate-1
```

- **plan**: the Codex-first author (Sol) writes `plan.md` + `tasks.toml` from the Jira ticket and the
  repo's skills (`.agents/skills/zui-component-creation` is the definition of done). Schema check
  bounces violations to the author. The opposite-vendor critic (Claude Fable) reviews; a `revise` is
  applied by the **task-writer** (Terra), never a second author round; critic round 2 is
  approve-with-concerns unless it names a wrong-code blocker. Planning spend is advisory; ordinary
  failures retain bounded recovery rather than requiring a fabricated owner ruling.
- **implement**: runnable tasks in dependency order, with manifest-order tie-breaking; before each, rebase onto `origin/main` (generated-file
  conflicts regenerate themselves). Ladder per task: cheap ×2 (Terra) → premium Terra ×2 → blocked.
  `size = "small"` tasks start on Luna (Haiku 4.5 on a Codex outage). Worker stages are light (N slots); verification
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
  defer / question), replies posted without automatic AI-agent signatures, fix tasks `9nn` run,
  re-review, gate.

## Self-healing (heal.py)

Every `paused` reason has a policy and a per-reason budget. When the policy says `human` or a budget
is spent, the ticket gets a structured recovery contract and a queued runner defect (it keeps its
cohort slot); only a validated product/API decision request waits on Cody. Recoveries are logged to
`tickets/<K>/heal.jsonl`. Notifications (the bound workspace + macOS) fire at Human Gate 1 and for open product
decisions, re-raised every six hours while a decision stands.

| pause reason | action | budget |
|---|---|---|
| `resource:` / heavy lane / slot | retry | 200 |
| verification timeout / heavy lane busy | retry | 6 |
| `gates failed` / `visual gate failed` | prepare + retry | 2–3 |
| `guard failed on the BASE tree` / contract complaint / guard disagreement | task-writer repairs the manifest (bad-guards.md) | 4 |
| `plan failed` / budget exceeded / writer left manifest invalid | back to `plan`: repair from the revise review (≤2), then full re-plan | 3 |
| `publish failed` / `finalize failed` / mark-ready / reply / adjudication | republish (after the runner fix that caused it) | 3 |
| rebase conflict in hand-written files / spent budget / bare `blocked: owner` | structured recovery + repair (slot kept) | — |
| validated product/API ambiguity | **decision-required** (Cody answers via `decision-answer`) | — |
| Unvalidated `need(s) Cody` / owner or tier label | Classify from evidence; only a validated product/API decision waits on Cody | — |
| `HUMAN` file | **human** takeover (slot kept) | — |

Other invariants the runner enforces so these do not recur: clean base before each attempt (stray
tracked edits reverted + logged), pre-task rebase (generated-file conflicts regenerate), lint guards
honour repo exclusions, rejecting guards are re-run on the base tree and charged to the plan when
they fail there too, accepted work is committed completely (deletions included) or publish refuses.

## Daily operation

```
/agent-loop                       # fleet: COHORT slots, WAITING ADMISSION, REVIEW READY
/agent-loop resume all            # explicit autonomous resume; preserve foreground/HUMAN, do not open panes
/agent-loop start                 # drip: next ready ticket(s) from the top of the epic board (Jira Rank)
#   runner.py supervise drip-start   # install the 15-min LaunchAgent so the drip runs itself
/agent-loop pause | unpause       # touch/rm ~/.local/state/agent-loop/PAUSE
/agent-loop stop ZIP-X            # unload one supervisor (state kept)
/agent-loop ledger [since DATE]   # billed $ by model / role / ticket / day
```
Per-ticket, from `scripts/`: `runner.py watch --ticket X` (live pane view), `resume --ticket X
[--from stage]`, `supervise status|start|stop`, `overview`.

Reboot: supervisors and the drip auto-start (`RunAtLoad`); the drip skips ticks until `bin/dev-services status`
is green. Inspect status first; use `/agent-loop resume all` (`runner.py resume --all`) only for an
explicitly authorized autonomous resume. Never restart foreground supervision or reopen panes merely
for status.
Kill switch: `touch ~/.local/state/agent-loop/PAUSE`. Direct interactive requests use the persistent
foreground handoff described in the shared policy. Never create or remove `HUMAN` to simulate it.

## Automatic runner repair (repair.py)

A supervised crash, a heal give-up on a recurring failure, or a pane's `runner.py report-defect` queues a
defect under `~/.local/state/agent-loop/defects/open/`. Every 5 minutes `com.cody.agent-loop.repair`
(`runner.py repair`) takes one: `repair-agent` (Terra, max) fixes it test-first in a throwaway worktree;
gates require a new test that fails on the base, no removed test or assertion, only runner code
changed, and the full suite passing; the opposite-vendor `repair-reviewer` (Opus 5.5) approves; the live branch
fast-forwards and the ticket's heal budgets reset. Rejected repairs retain diagnostic evidence and
bounded retry/backoff; routine failures are not product decisions. Protected safety
files (contracts, publish, agentdef, worktree, repair) never land automatically; their patches wait in
`defects/needs-approval/`.

## Hard rules the loop enforces

Draft PRs only until CI is green and bots have run; never assign reviewers; never merge; never a bare
force-push (lease only); GitHub writes only through `publish.github_write()`'s allowlist; workers have
no `fallbackModels` and a fixed tool allowlist; `planning/` and `.pi/` never committed; verification
commands use repo tools only (schema rejects `planning/` probes and >3 bespoke commands per task).

## Costs seen (2026-09-12 → 16)

Well (first PR): $69. Planning is the dominant cost when it loops (one bad night: $154 for zero PRs);
under the current shape planning is ~$6–20/ticket. Current model pricing may change these historical figures.
`runner.py ledger` is the source of truth.

## Known gaps

- Intra-ticket task parallelism is built (`parallel.py`) but gated off (`tasks_parallel`) until
  `implement_task` is thread-safe per ticket.
- Drip spinups launch `/loop-watch` directly on the selected backend's one agent surface; the manual
  `spinup` skill does not yet have a `--loop` flag (`/agent-loop start` does it in three steps).
- Figma Code Connect: workers produce a static mapping that parses; the handoff/publish step is an
  owner action and is listed among the tier-3 items in each Gate 1 report.
