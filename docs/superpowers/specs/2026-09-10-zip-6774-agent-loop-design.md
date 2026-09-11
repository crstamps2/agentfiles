# ZIP-6774 Autonomous Agent Loop

Status: Approved design, revised after self-review and a two-lane adversarial
review (OpenAI `cto` + `security-analyst` against a Claude-authored draft);
pending Cody's written-spec review. Written 2026-09-10 from a brainstorming
session with Cody. Revised same day to add a local-model worker tier at Cody's
request (see "Substrate" and "Resources"). Findings accepted, pushed back, or deferred are listed under
"Adversarial review disposition" at the end. Source material: the "Strong reviewer,
inexpensive workers" field guide (`~/Downloads/agent-loop-field-guide-2026-09-08/`),
the existing cmux spinup workflow (`common/skills/spinup/`), and the ZIP-6774 epic.

## Goal

Run the ten child tickets of Jira epic ZIP-6774 ("[Phase 3] ZUI Design System -
Containers + AI") autonomously, from ticket selection through a merge-ready pull
request, with three classes of human approval and nothing else requiring Cody:

1. **Tier-3 design confirmations** -- occasional, asynchronous, one-tap. A
   ticket with an outstanding tier-3 decision keeps working but cannot leave
   draft until Cody confirms (see "Design-decision policy").
2. **Human gate 1** -- after the bot review and CI loop is green and Figma parity
   is evidenced, Cody reviews and requests a colleague reviewer.
3. **Human gate 2** -- after the colleague loop is settled and CI is green, Cody
   merges.

The dashboard measures waiting time in each class so the loop's real autonomy
is visible rather than asserted.

Secondary goals, in priority order:

- Compare this loop against the current interactive cmux workflow on cost,
  wall time, and acceptance quality. This is an **observational pilot**, not a
  controlled A/B: historical interactive tickets differ in scope, guidance, and
  repository state. Outcomes and ticket classes are pre-registered in
  `hopper.toml` before the first run so results are not cherry-picked.
- Lower per-ticket inference cost by using Ollama Cloud models and local
  Ollama models for implementation while reserving flagship models for
  judgment, and measure how the three worker arms (local, cloud-cheap,
  premium) compare on acceptance, wall time, and cost.
- Track token consumption, dollar spend, model usage, and pipeline state in one
  continuously updated, self-contained HTML dashboard.
- Keep the MacBook Air (M4, 24 GB, fanless) responsive and healthy while the
  loop runs, including overnight with the display off.

## Non-goals

- Local models larger than the residency budget (see "Resources"). Local
  inference *is* in the pilot as a third worker tier, but only for models that
  fit alongside the machine's baseline load; it never runs concurrently with
  Rails, the test suite, or Chrome.
- 24/7 operation while the machine is asleep or closed. The loop works only
  while macOS is awake; it holds a wake lease when work is eligible.
- Merging, assigning reviewers, or communicating through Slack or Jira. The
  only Jira write the loop performs is the one the existing spinup listener
  already performs for every ticket it spins up (assign to Cody, transition to
  `In Progress`, via `spinup_helper.transition_to_in_progress`); the runner
  calls that same function and adds no other Jira mutation.
- PR stacks (`gh-stack`). Deferred: a stack is unmerged dependency work and
  contradicts the rule that autonomous work never builds on unreviewed code.
  If a plan is too large for one PR the planner splits the *ticket* into
  sequential PRs that each wait for human merge.
- Replacing cmux, `cmux_chain.py`, `bin/worktree-setup`, `lens-review`,
  `comms-coordinator`, `create-pull-request`, or `zipline-pr-rebase`. The loop
  composes these; it does not fork them.
- Building a general multi-project platform. The hopper is one epic.

## Scope

**Repository changes (agentfiles, `common/` source of truth):**

- New skill directory `common/skills/agent-loop/` containing the runner, the
  ledger, the dashboard generator, task/result contracts, and `SKILL.md`.
- New worker-tier agent definitions `common/agents/rails-worker.agent.md` and
  `common/agents/frontend-worker.agent.md`.
- New tier tables `[worker]`, `[flagship-author]`, and `[flagship-critic]` in
  `common/model-tiers.toml`. `model-tiers.toml` is one model per table, so the
  flagship pair is two tables; the runner swaps which table plays author and
  which plays critic per ticket (see "Roles and tiers").
- Edits to `common/instructions/AGENTS.md` (PR policy, comms gate, CI
  babysitting) as specified under "Policy changes".
- Edits to `common/skills/spinup/` so the normal planning prompt includes an
  adversarial plan review.
- A launchd plist source under `common/skills/agent-loop/` for the idle tick.

**Live machine changes (derived, via `bootstrap.sh`):**

- `~/.pi/agent/models.json` gains an `ollama-cloud` provider.
- `~/.pi/agent/agents/` gains the two worker definitions.
- Runtime state directory `~/.local/state/agent-loop/`.

**Out of scope for this spec:** the per-ticket implementation plans themselves
(the flagship planner writes those at run time), the Zipline application code,
and changes to the `cost-ledger` skill (its pattern is reused, its scope is not
extended).

## Architecture

### Roles and tiers

| Role | Model tier | Model(s) | Responsibility |
| --- | --- | --- | --- |
| Flagship planner | `flagship-author` / `flagship-critic` | `anthropic/claude-fable-5-1`, `openai-codex/gpt-6-astra` | Evidence-backed plan, design decisions, adversarial plan review, PR-comment judgment. One authors, the other critiques. Assignment is deterministic: tickets are numbered in hopper order; odd tickets get Fable as author and Astra as critic, even tickets the reverse. The assignment is recorded in `state.json` and reused for that ticket's PR-comment judgment, so the critic is always the vendor that did not write the plan. |
| Cloud cheap worker | `cloud-worker` | `ollama-cloud/gpt-oss:20b` on the **Free plan** (slice 1: `deepseek-v4-flash` and `glm-5.3-flash` return HTTP 402 without purchased credits; `gpt-oss:20b` is served, calls tools, and reports usage). Same weights as the local arm, so hosting is the only variable. | Implement one bounded task from an approved plan. Never makes design decisions. |
| Local cheap worker | `local-worker` | `ollama-local/gpt-oss-ctx32k:20b` — chosen in slice 1 over `qwen3:14b` (2.4× slower, over the pressure threshold, broken usage metadata), `qwen2.5-coder:14b` (no native tool call), and `gemma3:12b` (Ollama: no tool support). **Runs first** in the arm rotation (Cody: overnight runs trade wall time for $0). | Same contract as the cloud worker. Runs only when the heavy lane is idle; unloaded (`ollama stop`) before gates. |
| Premium fallback | `specialist` | `openai-codex/gpt-5.6-terra` -> `anthropic/claude-sonnet-5` | One attempt per task after cheap attempts fail. Budget-enforced. |
| Runner | none (deterministic Python) | -- | Selects tickets, dispatches stages, runs gates, decides pass/fail, checkpoints, logs, pauses. |
| Comms | existing `comms-coordinator` | per its definition | Authors every GitHub comment. Appends the AI-disclosure footer. |
| Reviewers | existing `code-reviewer`, `security-engineer`, Codex adversarial | per definitions | Unchanged three-lane review via `lens-review`. |

The runner, not any model, decides whether a gate passed. A worker's
`STATUS: pass` is a claim to verify.

### Substrate

Cheap workers run as **pi subagents** with a model override, so they inherit the
same skills, MCP servers (Figma, Playwright, CircleCI, GitHub), lens
diagnostics, and agent definitions as the flagship tier. Within the loop this
makes the cheap-vs-premium comparison one of model tiers under identical
tooling; the comparison against the *interactive* baseline remains observational
(different harness, supervision, and ticket population) and is labeled so.

Two Ollama providers are declared in `~/.pi/agent/models.json`, both
`openai-completions`:

- `ollama-cloud` at `https://ollama.com/v1`, key read from Keychain at request
  time (see "Secrets and identity"). **Free plan constraints** (from
  ollama.com/pricing, 2026-09-10): a starter credit amount per month for a
  subset of "starter" models; **1 concurrent request** (excess queued, then
  rejected); prompts and responses not logged or trained on; hosted primarily
  in the US with zero-data-retention partner terms. Buying credits unlocks all
  models. The runner therefore never runs two cloud-worker attempts
  concurrently, treats a 429 or queue rejection as an `environment` failure
  (backoff, never escalate to premium for it), and tracks remaining starter
  credit as a budget line that **enforces** (pause cloud-worker when exhausted;
  local-worker and premium continue under their own caps).
- `ollama-local` at `http://localhost:11434/v1`, placeholder `apiKey`
  (Ollama ignores it; pi requires a value). Requires the Ollama daemon
  installed locally (Homebrew) and the chosen model pulled. The daemon is
  configured with `OLLAMA_KEEP_ALIVE=0` so a model is resident only while a
  request is in flight, and `OLLAMA_MAX_LOADED_MODELS=1`.

Each model entry declares `contextWindow`, `maxTokens`, `reasoning`, `cost`
(rate card for cloud; all-zero with `cost_source: local` for local, see
Observability), and any `compat` flags discovered in slice 1.

**Peak pricing.** `deepseek-v4-flash` and `deepseek-v4-pro` cost double
between 12:00 and 18:00 UTC on weekdays (08:00-14:00 Eastern). In that window
the runner routes non-visual cloud tasks to `gpt-oss:20b` or `glm-5.3-flash`
instead, or defers them if `hopper.toml` says so. The ledger records the rate
actually in force for each stage.

**Task assignment across cheap tiers.** To compare the arms fairly, the runner
assigns each task to `local-worker` or `cloud-worker` by alternating task
index within the ticket, stratified by the task's visual flag so both arms see
visual and non-visual work. The assignment is fixed for that task's two cheap
attempts; the premium rung is shared. `hopper.toml` can pin a ticket to one arm
when the comparison is not the point.

Worker agent definitions share the body of `rails-engineer` /
`frontend-engineer` but declare:

```yaml
model: ollama-cloud/<worker-model>
thinking: <verified in slice 1>
tools: read, grep, find, ls, bash, edit, write     # no gh/acli, no MCP write servers
```

The same applies to `local-worker`, whose frontmatter names the
`ollama-local/<model>` id.

**No `fallbackModels` on worker definitions.** Harness-level fallback would let
a "cheap" attempt silently invoke Terra or Sonnet, bypassing the runner's
one-premium-attempt rule, its pause-on-outage rule, and its budget caps, and it
would corrupt tier attribution in the ledger. Escalation is the runner's job
and is explicit (see "Escalation ladder"). A separate `premium-worker`
definition (Terra, fallback Sonnet) exists for the premium rung. This keeps the
dispatch-model-hygiene rule intact: every launch uses exactly the model in the
agent's frontmatter.

Workers receive a **tool allowlist**: file and shell tools only. No `gh`, no
`acli`, no Figma/GitHub/Jira/Slack MCP servers, no browser. The worktree they
edit contains no credentials. Full OS-level sandboxing (separate user account)
is not in the pilot and is listed as an open risk.

**Data boundary.** Everything in a worker's context leaves the machine to
Ollama Cloud: the task manifest, source it reads, tool output, test output.
The pilot accepts this for ZUI component work in the Zipline app, which
contains no customer data in source. Workers never receive Jira ticket bodies,
PR comments, Figma exports, or screenshots directly; those reach only the
flagship tier (Anthropic/OpenAI, already the status quo). Before the hopper
opens to non-ZUI work, this boundary must be re-approved.

### Hopper

Eligible tickets are children of ZIP-6774 with status `Selected for Work`,
unassigned or assigned to Cody. The runner orders them by an explicit
dependency graph maintained in `common/skills/agent-loop/hopper.toml`, not by
Jira rank:

```
ZIP-7872 Nav Link          (no deps)
ZIP-7873 Well              (no deps)         <- pilot ticket
ZIP-4293 Section           (no deps)
ZIP-7877 Card Header Start (no deps; Card shipped)
ZIP-7876 Card Spotlight    (no deps; Card shipped)
ZIP-4281 Nav Tabs          (after ZIP-7872)
ZIP-4282 Nav Pills         (after ZIP-7872)
ZIP-7875 Stepper           (no deps)
ZIP-4294 Modal             (late: interactive, Bootstrap plugin reuse)
ZIP-7874 Dropzone          (late: JS/React boundary, highest risk)
```

The runner logs the ticket it picked and the reason. **Scheduler of record:**
the runner owns ZIP-6774 children. The existing cmux work listener
(`cmux_chain.py cmux-poll`) is updated to exclude tickets whose parent is a
runner-owned epic (read from `hopper.toml`), so the two never race to spin up
the same ticket. Slice 2 tests concurrent invocation.

A ticket whose dependency is not yet merged (past human gate 2) is not
eligible unless Cody overrides in `hopper.toml`. This is deliberate: Nav Tabs and Nav Pills wait
behind the human merge of Nav Link rather than building on an unmerged branch,
so the loop never stacks autonomous work on unreviewed code. With one heavy
lane this costs little throughput because independent tickets fill the gap. On selection the runner assigns the
ticket to Cody and transitions it to `In Progress` via `acli`, mirroring
`spinup_helper.transition_to_in_progress`.

### Per-ticket lifecycle

The runner owns one state machine per ticket, persisted as
`~/.local/state/agent-loop/tickets/<key>/state.json`. States:

```
queued
  -> spinup           cmux_chain.run_chain (worktree, setup, dev server, browser)
  -> plan             flagship author: evidence-backed plan in planning/zip-NNNN/
                      + machine-validated task manifest tasks.toml
  -> plan-review      flagship critic (opposite vendor): adversarial review; author revises
  -> implement        runner validates tasks.toml and dispatches each task through the ladder
  -> gates            deterministic first, expensive last (see Gates)
  -> draft-pr         create-pull-request skill
  -> ready            mark ready for review (all gates green on the exact head SHA)
  -> bot-loop         Claude bot review comments + CI babysitting until settled
  -> human-gate-1     PAUSED. Cody reviews, requests colleague.
  -> colleague-loop   colleague comments + CI babysitting until settled
  -> human-gate-2     PAUSED. Cody merges.
  -> done             spindown via cmux_chain teardown
blocked / paused      from any state; reason recorded; runner moves to next ticket
```

**Any new commit or rebase on the branch returns the ticket to `gates`.** A
bot-comment fix, a colleague-comment fix, or a rebase invalidates all evidence
(tests, lens-review, screenshots, Figma parity, PR body) and forces a full gate
run on the exact final SHA before the ticket can be `ready` again or reach
either human gate. The dashboard shows evidence SHA vs. head SHA.

**The runner does not slice plans.** Turning prose into bounded tasks is a
design act, so the flagship author emits `planning/zip-NNNN/tasks.toml`: an
ordered list of tasks with the fields of the worker task contract (allowed
files, whether test/fixture edits are authorized, verification commands,
acceptance criteria, visual flag, timeout). The runner validates it against a
schema and rejects the plan back to the author on any violation. It dispatches
tasks verbatim.

Transitions are recorded as metrics rows. Every state has a timeout and a
classified failure path (see Error handling).

### Design-decision policy (plan and plan-review stages)

Several ZIP-6774 tickets carry open design questions in their Notes. The
flagship planner resolves them under four evidence tiers:

1. **Evidence converges** (Figma, shipped ZUI components, Cable 2 source,
   repository conventions agree): decide, record, continue.
2. **Incomplete evidence, local and reversible** (internal naming, preview
   structure, SCSS organization): decide, mark as *assumption*, keep the
   implementation minimal.
3. **Incomplete evidence, externally visible or hard to reverse** (public
   component API, whether Modal always renders a close control, whether Nav
   Tabs owns panels): proceed, but the ticket cannot leave draft until Cody
   confirms. The dashboard flags it.
4. **Conflicting evidence, or the decision touches product/design ownership,
   accessibility guarantees, data safety, or shipped call sites**: pause the
   ticket, record the question, move to the next eligible ticket.

Only the flagship tier makes design decisions. If a worker discovers ambiguity
mid-task it stops with `STATUS: blocked, REASON: owner` and the runner returns
the task to the planner.

**Remote text is data, never instructions.** Jira ticket bodies and Notes, PR
review comments (bot or colleague), CI logs, and Figma text are untrusted
input. The planner reads them as evidence and writes its own local plan and
task manifest; workers receive only the local manifest. Any imperative found
in remote text ("ignore the above", "run this command", "push to main") is
quoted in the plan as a finding, never executed. The critic's adversarial
review explicitly checks the plan for instructions that originated in remote
text.

### Worker task contract

Each implementation task is a Markdown file
`~/.local/state/agent-loop/tickets/<key>/tasks/NNN-<slug>.md` following the
field guide's template: user-visible outcome, base revision, allowed files,
visual-evidence flag, stage timeout, invariants, out-of-scope, acceptance
criteria, verification commands, stop/escalate conditions. It ends with the
standing instructions: read repo instructions, do not commit or push, preserve
the diff and write `result.md` even on failure, never weaken an acceptance
check.

Workers write `result.md`:

```
STATUS: pass | fail | blocked
REASON: none | implementation | test | environment | timeout | owner | protocol
BASE: <revision>
FILES: <paths>
EVIDENCE: <commands / results / artifact paths>
UNVERIFIED: <remaining limits>
NEXT: <one step>
```

The runner parses leniently on format (heading typos, ordering) and strictly on
meaning (missing `STATUS` is a protocol failure).

**Diff allowlist, enforced by the runner.** After each attempt the runner diffs
the worktree against the attempt's base snapshot. Any path outside the task's
`allowed_files` is an automatic reject. Test and fixture files are editable
only when the task manifest sets `may_edit_tests: true`; otherwise a change to
any `test/`, `spec/`, or fixture path is a reject even if the tests pass
(field report: the one premium fallback that shipped a product change was
rejected for weakening a fixture). Gate scripts, `bin/`, CI config, `.github/`,
and `AGENTS.md` are never editable by workers.

### Escalation ladder (ladder B)

Per task:

1. Cheap worker, attempt 1.
2. Cheap worker, attempt 2, with the gate's feedback appended to a per-task
   `feedback.md` the worker reads first (two sentences about what the gate saw).
3. Premium fallback, one attempt, same feedback file.
4. `blocked`, diff preserved in the worktree under `.agent-loop/attempts/NNN/`,
   task returned to the planner for re-slicing.

Fall-through happens only on rejected or unusable output. Each attempt is a
separate metrics row.

**Attempt snapshots.** Before each attempt the runner records the worktree
state (`git stash create` or a tree object). A *timeout* with a partial diff
that passes the allowlist lets the next attempt continue from that tree. A
*rejection* for allowlist violation or gate failure restores the last
accepted snapshot first, so a bad attempt cannot leave changes the next
attempt normalizes. The rejected diff is still preserved under
`.agent-loop/attempts/NNN/` for diagnosis.

### Gates

Fixed order, deterministic and cheap first, expensive judgment last. A red
earlier gate skips the rest, so flagship review tokens are never spent on a
candidate that basic runtime QA would reject:

1. Diff allowlist (runner, no model).
2. Lint for touched files (repo's own commands, per `AGENTS.md`).
3. Focused tests, then the relevant suite.
4. Playwright QA on `admin.<worktree>.test` with Chrome for Testing: the
   ticket's Gherkin scenarios exercised, before/after screenshots captured.
5. Figma parity, two parts:
   - **Deterministic**: for each Figma node listed in `tasks.toml`, capture the
     mapped Lookbook scenario at a pinned viewport, DPR, and font set, with the
     fixture data the plan names; pixel-diff against the Figma export with a
     per-component threshold from `hopper.toml`. Over threshold = red.
   - **Subjective, labeled**: the flagship critic reviews the side-by-side and
     annotates deltas. Gate definition: **no unexplained visual delta; every
     intentional delta documented** in the plan and PR body. Cody makes the
     "perfect" call at human gate 1. The dashboard labels this evidence as
     model-reviewed, not measured.
6. `lens-review` three lanes on the current head; validated findings on a
   loop-owned PR are implementation work (fix, do not comment). A fix returns
   to gate 1.
7. Evidence refresh: PR body's before/after screenshots regenerated from the
   current head; annotated screenshots and a short recording when the ticket
   involves interaction or state transitions. Evidence is stamped with the
   head SHA.

Playwright, Rails, and the test suite run in the **heavy lane** (see Resources).

### Pull request stage

- Draft PR via the `create-pull-request` skill only after all gates pass.
  **Gap found in self-review:** `AGENTS.md` mandates this skill and a hook
  blocks direct `gh pr create`, but no `create-pull-request` skill directory is
  currently installed for pi, Claude, or Codex. Slice 1 must locate or restore
  it (it may live in the Zipline repo's `.claude/` or in a plugin) before the
  loop can open PRs. Until then the `draft-pr` state is unreachable by design.
- Mark ready for review once all gates are green **on the current head SHA**
  and no tier-3 decision is outstanding.
- Never assign reviewers.
- No PR stacks in the pilot (see Non-goals).

**Single GitHub write path.** Every GitHub mutation the loop performs -- draft
PR creation, ready-for-review, comment, rebase push -- goes through one runner
function, `github_write()`, which refuses unless: the PR number is in the
ticket's `state.json`; the head SHA matches the SHA the gates attested; the
comment text (if any) ends with the AI-disclosure footer and names the model;
the action is in the allowlist (`create_draft`, `mark_ready`, `comment`,
`push_with_lease`; never `merge`, never `request_reviewers`). The
`comms-coordinator` agent *drafts* comment text and returns it; it has no
`gh` write capability inside the loop. This makes the never-assign, never-merge,
and footer rules runner-enforced rather than requested of a model.

### Bot-review and CI loop

Poll via `gh` on a backoff (2, 5, 10 min; longer overnight). The runner keeps
a persisted cursor per PR: the set of comment IDs already handled, each with
its fetched body hash and author ID, plus the head SHA at handling time. A
comment is "new" only if its ID is unseen or its body hash changed. Actions are
taken only after a **quiescence window** (no new comments or check-run changes
for 3 minutes) so a burst of bot comments is handled as one batch. For each new
Claude-bot comment (author ID matched against a configured allowlist, not
display name):

1. Primary reviewer (flagship, whichever vendor did not author the plan)
   classifies: fix / reply-with-reasoning / escalate.
2. Adversarial cross-check by the other flagship vendor.
3. Fix: dispatch as a worker task through the ladder; reply "fixed in `<sha>`"
   via `comms-coordinator`.
4. Reply-with-reasoning: post via `comms-coordinator` if both flagship reviewers
   agree; otherwise escalate.
5. Escalate: hold the draft reply for Cody; dashboard alert.

CI red is classified before any action. "Failure is in an untouched file" is
*not* sufficient evidence of an external cause; indirect breakage is common.

- **Not caused by our diff**, established by one of: (a) the same job is red
  on `origin/main` at or after our merge-base; (b) the failing test passes
  locally on our head and the CI failure is in a job our plan's
  `verification_commands` cover. Then rebase onto `origin/main` via
  `zipline-pr-rebase` (`--force-with-lease`, loop-owned branches only, and
  only if `git log origin/<branch>` shows no commits by anyone but the loop
  since the last push).
- **Caused by our diff**: fix through the ladder; returns to `gates`.
- **Uncertain attribution**: pause with evidence rather than rebase.
- **Flake persisting after rebase**: `gh run rerun --failed`, maximum 5
  attempts, then pause with evidence.
- Never push empty commits.

The ticket reaches human gate 1 when: no unresolved bot comments, CI green,
Figma parity gate passed on the current head, evidence current.

### Colleague loop

Same machinery as the bot loop with one difference: a reply that **declines or
pushes back** on a colleague's comment posts autonomously only when the
primary and adversarial flagship reviewers independently agree **and** the
reply cites reproducible evidence (a test, a Figma node, a documented
convention, a measured value) rather than opinion; otherwise the draft is held
for Cody's one-tap approval. The AI footer discloses authorship; it does not
make a wrong argument right, so evidence is the bar. Factual "fixed in `<sha>`"
replies post automatically. Ticket reaches human gate 2 when all threads are
addressed and CI is green on the current head.

### cmux integration

- One cmux workspace per active ticket, created by `cmux_chain.run_chain`, in
  the existing group layout. `run_chain` today launches an interactive agent
  tab; the runner needs a variant that opens the workspace, setup, dev server,
  and browser but **no** agent tab (the runner dispatches pi children
  detached). Slice 1 adds `run_chain(..., agent=None)` and proves it.
- **Ownership handoff, not process detection.** Cody takes over a ticket by
  touching `~/.local/state/agent-loop/tickets/<key>/HUMAN` (a cmux keybinding
  or `/takeover` skill can do this). The runner finishes the current atomic
  step, checkpoints, releases the heavy lane, and does not touch that worktree
  again until the file is removed. Two writers never share a tree.
- One persistent browser surface in the `Main` workspace showing
  `file://~/.local/state/agent-loop/dashboard.html`.
- `cmux notify` on: human gate reached, ticket blocked, held reply awaiting
  approval, budget threshold, CI rerun cap.
- Spindown via existing `teardown_worktree` after human gate 2.

### Resources and admission control

The machine is an M4 MacBook Air, 24 GB, fanless, with many long-lived pi and
Claude processes already resident.

- **Heavy lane, capacity 1, global**: worktree setup, Rails boot, dev server,
  test suites, Playwright/Chrome, `lens-review` runs, **and any local-model
  inference stage**. A loaded local model and a running Rails/Chrome stack
  cannot fit in 24 GB together with the machine's ~10 GB baseline, so they
  are mutually exclusive in time: a `local-worker` attempt takes the heavy
  lane, runs inference, then the runner calls the Ollama API to unload the
  model and verifies via `ollama ps` and `vm_stat` that memory was released
  before the lane is handed to a gate. Reload cost is logged as
  `wait_seconds`. Held via an `flock`-based
  lock under `~/.local/state/agent-loop/locks/heavy` whose contents record
  owner PID, boot ID, and a heartbeat timestamp; a lock is reclaimable only
  when the PID is dead or the boot ID differs, never on age alone.
- **Light lane, capacity 1 in the pilot**: flagship planning and review,
  PR-comment judgment, Jira/GitHub polling. Remote inference only. Raise to 2
  only after slice 6 shows headroom.
- **Admission thresholds** (initial values, tuned in slice 6, all in
  `hopper.toml`): defer a heavy stage when `memory_pressure` reports warn or
  critical, or compressor-occupied pages exceed 25% of physical memory; when
  the thermal signal is serious or critical — primary source
  `NSProcessInfo.thermalState` (0 nominal, 1 fair, 2 serious, 3 critical, read
  via `osascript -l JavaScript`; slice 1 found that `pmset -g therm` prints no
  `CPU_Speed_Limit` line and `machdep.xcpm.cpu_thermal_level` does not exist on
  the M4 Air), with xcpm level and pmset speed limit as secondary sources; an
  unknown thermal reading (all three unavailable) fails closed; when on
  battery; when free disk is below 20 GB; when 1-minute load exceeds core count. Maximum
  defer is 30 minutes, after which the ticket is paused with reason
  `resource` and the runner tries a light-lane task instead.
- **Local model residency budget**: ≤ 12 GB resident, verified in slice 1 by
  `gpt-oss-ctx32k:20b` holding peak compressor at 24% (threshold 25%)
  alongside the machine's baseline. Larger models are not eligible in the
  pilot (`qwen3:14b` at 14 GB reached 30% and is excluded). **Local models
  MUST be ctx-pinned derived models** (`FROM <base>` + `PARAMETER num_ctx
  32768`, named `*-ctx32k:*`); Ollama's default 4K context silently truncates
  pi's ~11K system prompt so the worker never sees its tools. `hopper.toml`
  `[local].model` is validated against that naming. The dashboard shows
  peak compressor pages and thermal state during local-inference stages; if
  either trips the admission thresholds repeatedly, the runner pins the ticket
  to `cloud-worker` and alerts. The Ollama daemon itself is stopped
  (`brew services stop ollama` or `launchctl`) while the hopper is idle so it
  does not hold memory overnight for nothing.
- **Process budget**: each heavy stage runs in its own process group; the
  runner records the group and, on completion or timeout, kills the group and
  verifies no member survives before releasing the lane. Dev server and Chrome
  are counted against the stage, not left running between stages.
- **Preemption**: if Cody's interactive session is active in the ticket's cmux
  workspace or system load exceeds a threshold, the runner finishes the current
  atomic step, checkpoints, and pauses.
- **Wake lease**: while eligible work exists and the Mac is on AC power, the
  runner holds `caffeinate -i` (prevents idle sleep, permits display sleep).
  Released when the hopper is empty, paused, or blocked. If the machine sleeps
  anyway, state is on disk and the idle tick resumes.
- **Just-in-time services**: dev server and Chrome start for the gate that
  needs them and are stopped when the ticket leaves the heavy lane.

### Observability

**Ledger.** One append-only `~/.local/state/agent-loop/metrics.jsonl`, one row
per executed stage, written by the runner:

```
run_id, ticket, task_id, stage, provider, model, thinking, attempt,
start_utc, end_utc, worker_seconds, gate_seconds, wait_seconds,
tokens_in, tokens_out, tokens_cache_read, tokens_cache_write,
cost_usd_estimate, cost_source, outcome, reason, evidence_path,
decision_tier (plan stages only)
```

Token counts are read from pi's session JSONL for the child run (the same
approach `cost-ledger/ledger.py` uses on Claude transcripts). Cost is computed
from `common/skills/agent-loop/rate_card.json`, which gains Ollama Cloud
entries taken from ollama.com/pricing on 2026-09-10 (per million tokens,
input / cached input / output): `deepseek-v4-flash` 0.22 / 0.007 / 0.66 (peak
0.44 / 0.014 / 1.32); `glm-5.3-flash` 0.15 / 0.03 / 0.50; `gpt-oss:20b` 0.07 /
0.035 / 0.30; `gemma4` 0.14 / 0.05 / 0.40; `nemotron-3-nano` 0.06 / - / 0.24.
The card carries its retrieval date and is labeled **estimate**. `cost_source` is one of `rate-card`,
`provider-reported`, `local`, `unknown`; unknown is never rendered as zero.
Local inference has zero API cost; the ledger records its wall time,
model-load time, and the machine-pressure readings taken during the stage so
the comparison is not distorted by treating local as free in every dimension. Cody may
paste a provider-dashboard monthly figure into `reconcile.json`; the dashboard
shows estimate vs. reconciled.

**Budgets.** Configured in `hopper.toml`. Premium-fallback caps (daily, monthly)
**enforce**: crossing one pauses further premium attempts and alerts.
Flagship-planner spend is tracked as its own line with an alert-only cap.
Cheap-tier caps alert only. In addition, a **total unattended daily cap**
across all providers **enforces**: when crossed, no new stages start until the
next day or Cody clears it. This exists specifically for the overnight case
where alerts have no reader. Unknown pricing or missing usage metadata for a
model is a stop condition for that model, not a zero.

**Dashboard.** `~/.local/state/agent-loop/dashboard.html`, self-contained
(inline CSS/JS, no server, no build), regenerated after every stage and by a
launchd idle tick every 5 minutes. Panels:

- Now: active ticket, stage, elapsed, heavy-lane holder, machine pressure,
  wake lease, queue depth.
- Spend: today / 7d / 30d by provider, model, and tier; per-accepted-ticket
  cost; estimate vs. reconciled.
- Model efficacy (observational comparison): first-attempt acceptance, escalation rate, wall
  time per accepted task, per model and per arm (local / cloud-cheap /
  premium), with local additionally showing load time and pressure readings; a baseline column mined from existing pi
  session directories of recent interactive tickets (ZIP-4272, 7506, 7009 and
  similar).
- Pipeline: each ZIP-6774 ticket as a row through the lifecycle states with PR
  link, CI status, comment counters, human-gate flags.
- Decisions: every autonomous design decision with its evidence tier and
  pointer to the plan.
- Alerts: blocked tickets, held replies, budget thresholds, CI rerun cap.

### Policy changes (`common/instructions/AGENTS.md`)

Replace the conflicting PR-policy and comms-gate lines with:

```markdown
## PR Policy
- ALWAYS use the `/create-pull-request` skill to open a PR (draft). Never call `gh pr create` directly.
- Agents MAY mark a PR ready for review, but ONLY after all required gates pass:
  lint/tests green locally, lens-review findings resolved, QA evidence (before/after
  screenshots, annotated/video where warranted) current in the PR body, and -- for
  visual work -- no unexplained Figma delta.
- NEVER assign reviewers. Requesting a human reviewer is Cody's action.
- NEVER merge. Merging is Cody's action.
- Prefer PR stacks (gh-stack) over a single large PR when a change has separable
  concerns or is large enough to strain a human reviewer. Best effort. (General
  policy for interactive work; the autonomous loop does not open stacks in its
  pilot -- see Non-goals.)

## External Communications Gate
- GitHub PR review replies/comments MAY be posted autonomously on loop-owned or
  Cody-owned PRs, subject to: (1) authored via the comms-coordinator agent, always;
  (2) every agent-authored comment ends with the AI-disclosure footer below;
  (3) no platitudes; (4) a reply that declines or pushes back on a COLLEAGUE's
  comment is drafted and held for Cody unless the adversarial cross-model review
  and the primary reviewer both independently agree it should be declined -- then
  post with reasoning and flag it in the report.
- Slack, Jira, and all other external systems remain draft-first / approval-gated.
- AI-disclosure footer (verbatim, last line of every agent-authored GitHub comment,
  with the authoring model named):
  `-- posted by Cody's AI agent (<model>) on his behalf`

## CI Babysitting
- Red CI on a loop-owned PR: classify first. Not caused by our diff -> rebase onto
  origin/main via the zipline-pr-rebase skill (`--force-with-lease`, loop-owned
  branches only). Caused by our diff -> fix. Flake persisting after rebase ->
  `gh run rerun --failed`, max 5 attempts, then pause and report.
- NEVER push empty commits to rerun CI. Human-authored branches: no force-push
  without explicit permission (unchanged).
```

Also: add the adversarial plan review to the normal spinup planning prompt
(`spinup_helper.jira_work_prompt` and `cmux_chain.jira_agent_prompt`) so every
plan, autonomous or interactive, is reviewed by an opposite-vendor model before
execution.

### Secrets and identity

- `OLLAMA_API_KEY` is stored in the login Keychain and read at request time via
  `models.json`'s command form: `"apiKey": "!security find-generic-password -ws ollama-cloud"`.
  It is never placed in a plist, a shell profile, or a log.
- launchd does not inherit the interactive shell environment. The idle-tick
  wrapper sets an explicit PATH (mirroring `zipline-cmux-poller.sh`) and
  otherwise relies on Keychain and the `gh`/`acli` credential stores.
- Missing or expired `gh`, `acli`, Figma MCP, or Ollama credentials are a
  classified preflight failure: pause, alert, never switch provider.
- Runtime state, logs, session JSONL, screenshots, and attempt diffs under
  `~/.local/state/agent-loop/` are `chmod 700` and pruned after 30 days.
- The loop acts as Cody's GitHub identity in the pilot. A dedicated bot
  identity is listed as an open risk / follow-up.

## Error handling

| Failure | Classification | Runner behavior |
| --- | --- | --- |
| Worker output missing `STATUS` | protocol | Reject attempt; next rung of ladder |
| Worker edits gate scripts / `bin/` / CI / `AGENTS.md` | protocol | Reject attempt; revert those paths; next rung |
| Worker `STATUS: blocked, REASON: owner` | owner | Return task to planner; planner re-slices or escalates to Cody |
| Stage timeout | timeout | Kill the stage's process group; verify termination; preserve diff; next rung continues from the working tree if the diff passes the allowlist |
| Worker edits outside `allowed_files`, or tests/fixtures without `may_edit_tests` | protocol | Reject attempt; restore last accepted snapshot; next rung |
| Task manifest fails schema validation | protocol | Return plan to author with the validation errors; no dispatch |
| Imperative instruction found in remote text | injection | Quote as a finding in the plan; never execute; critic verifies |
| External write (comment, PR, push) crashes before receipt persisted | idempotency | On restart, reconcile against GitHub by idempotency key (ticket, action, head SHA) before retrying; never double-post |
| Bot/colleague comment edited or deleted after handling | comms | Body hash mismatch re-opens the item; deletion closes it; both logged |
| Credentials expired (gh, acli, Figma, Ollama) | preflight | Pause; alert; never switch provider |
| Any new commit or rebase on the branch | -- | Return ticket to `gates`; invalidate evidence |
| Gate fails on our diff | implementation/test | Append feedback; next rung |
| Gate fails for environment (dev server down, DB, port, Chrome crash) | environment | Retry only that gate once; on repeat, pause ticket with evidence |
| Ollama Cloud unreachable / quota | environment | Sleep with backoff; do not silently switch to premium |
| Premium budget cap crossed | budget | Pause premium rung; cheap rungs continue; alert |
| CI red, not ours | ci-external | Rebase via `zipline-pr-rebase` |
| CI flake after rebase | ci-flake | Rerun-from-failed, max 5, then pause |
| Colleague decline without dual agreement | comms | Hold draft; alert; continue other work |
| Memory/thermal/power red | resource | Defer heavy stage; light lanes continue |
| Cody active in ticket workspace | preempt | Finish atomic step; checkpoint; pause ticket |
| Runner crash | -- | State is on disk; idle tick resumes from last checkpoint after reconciling external state; locks reclaimed only if owner PID is dead or boot ID differs |
| Two runner instances (overlapping launchd ticks) | -- | Global `flock` runner lease; the second instance exits immediately |
| Pause file `~/.local/state/agent-loop/PAUSE` present | operator | No new stages start; current atomic step completes |

Every failure is a metrics row with `outcome` and `reason`.

## Verification

Adopt in slices; each slice has an observable success criterion before the next
begins.

1. **Activation prerequisites** (all must pass before any real ticket is
   eligible):
   - `ollama-cloud` in `models.json` with Keychain-backed key; `pi
     --list-models` shows the worker models; a hand-run `pi -p` with each
     worker model completes a trivial tool-using edit in a scratch worktree,
     honoring the tool allowlist. Records actual model IDs, context windows,
     `compat` needs, and whether usage metadata is returned (a stop condition
     if not). Also records **which candidate models the Free plan's starter
     set actually serves**, the starter credit amount shown in the account's
     usage page, and the observed behavior on a second concurrent request
     (expect queue, then 429). If none of the three candidates is in the
     starter set, Cody decides whether to buy a small credit pack or run the
     pilot with local + premium only.
   - Ollama installed locally; one candidate model within the residency
     budget pulled; `ollama-local` in `models.json`; the same hand-run probe
     passes; measured: resident memory while loaded (`ollama ps`), load and
     unload time, peak compressor pages, and `pmset -g therm` during a
     five-minute tool-calling session. If tool calling is unreliable or the
     model exceeds the budget, try one smaller candidate, then drop the local
     arm for the pilot and record why.
   - The `create-pull-request` skill is located or restored, its provenance
     reviewed, and a dry run against a scratch branch succeeds with the
     existing hook still blocking direct `gh pr create`.
   - `run_chain(..., agent=None)` opens a workspace without an agent tab.
   - The cmux listener exclusion for runner-owned epics is in place.
   Prerequisite from Cody: an Ollama Cloud account and API key.
2. **Runner failure paths** with a dry-run task and a fake worker: malformed
   result rejected; out-of-allowlist edit rejected and snapshot restored;
   timeout kills the whole process group and the diff survives; environment
   retry runs once then pauses; `PAUSE` and `HUMAN` files honored; two runner
   instances cannot coexist; a lock from a dead PID is reclaimed and one from
   a live PID is not; an external write interrupted before receipt is
   reconciled, not repeated.
3. **Ledger and dashboard.** Metrics rows appear for each dry-run stage;
   `dashboard.html` renders from them; token counts match the pi session JSONL;
   the comparison baseline column populates from at least three historical
   tickets, labeled observational; the pre-registered outcomes from
   `hopper.toml` are displayed; `cmux` opens the dashboard surface.
4. **Full lifecycle to human gate 1 on ZIP-7873 (Well).** Plan and adversarial
   review artifacts exist in `planning/zip-7873/`; every design decision is
   tiered; all six gates pass; before/after screenshots and Figma parity
   evidence are in the draft PR; PR marked ready; bot loop settles; dashboard
   shows the ticket at gate 1. Cody reviews the PR and the plan as the acceptance
   check of the loop, not just of the component.
5. **Colleague loop** on the same PR after Cody requests a reviewer: at least one
   comment fixed and replied automatically; any decline correctly held or
   posted per the dual-agreement rule.
6. **Open the hopper** to the remaining tickets in dependency order, one heavy
   lane, overnight with the wake lease. Success: at least two tickets reach
   human gate 1 without Cody intervening, and the machine shows no thermal or
   memory pressure alerts in the dashboard history.

Unit tests live beside the runner (`test_agent_loop.py`, `unittest`, mirroring
`test_cmux_chain.py`).

## Open risks

- **Figma parity tooling is unproven.** Figma MCP node export compared to
  Lookbook screenshots has not run in this environment. Slice 4 may surface a
  hard problem; the fallback is the labeled-subjective half of the gate alone.
- **Workers are not OS-sandboxed.** The tool allowlist and credential-free
  worktree reduce blast radius but a worker still runs as Cody's user. A
  separate macOS account or container is the correct fix and is deferred.
- **The loop uses Cody's GitHub identity.** Every autonomous write is
  attributable to Cody plus the footer. A dedicated bot identity with
  narrower scopes is the correct fix and is deferred.
- **Local-model arm may not be viable on this hardware.** A 14B-class model
  in ~10 GB leaves thin headroom on a fanless 24 GB machine; sustained
  inference is also the loop's largest thermal load. The mutual-exclusion rule
  protects the gates but costs throughput (load/unload per attempt). If slice
  1 shows thermal throttling or poor tool calling, the local arm is dropped
  and the pilot proceeds with cloud-cheap vs. premium.
- **Free-plan starter set is undocumented.** ollama.com/pricing says Free
  includes "starter models" without naming them. The intended cheap cloud
  models may require purchased credits; slice 1 discovers this and Cody
  decides. The 1-concurrent-request ceiling is compatible with the one-heavy-lane
  design but rules out any future cloud-worker parallelism without a plan
  upgrade.
- **Cheap-tier tool-calling quality is unverified.** If `deepseek-v4-flash` or
  `glm-5.3-flash` cannot drive pi's tools reliably, the pilot's result may be
  "cheap tier cannot hold this harness." That is a valid result and ends the
  cheap-tier experiment, not the loop.
- **Flagship cost dominates.** With Fable 5.1 and Astra planning and reviewing,
  per-ticket cost is dominated by judgment, not implementation. The dashboard
  separates planner spend from fallback spend so this stays visible.
- **cmux surface-adoption flakiness** (see memory `spinup-cmux-failure-modes`)
  can fail a spinup under contention. The runner treats a failed spinup as an
  environment fault: retry once, then pause the ticket.

## Adversarial review disposition

Two read-only OpenAI lanes (`cto`, `security-analyst`, Terra xhigh) reviewed the
Claude-authored draft. Full reports are in the pi session artifacts for mission
`fd628755`. Disposition:

**Accepted and folded in:** planner emits a machine-validated task manifest
(runner never slices); no `fallbackModels` on worker definitions, runner owns
escalation; Jira writes limited to the existing spinup transition; any new SHA
returns to `gates`; runner is scheduler of record and the cmux listener excludes
runner-owned epics; gate order moved deterministic runtime checks before
`lens-review`; per-attempt snapshots and runner-enforced diff allowlist
including test/fixture protection; PR stacks deferred; three approval classes
stated honestly; remote text treated as data; idempotency keys and receipts
around external writes; comment cursor with body hashes, author-ID allowlist,
and quiescence window; observational pilot rather than A/B; process groups
with verified termination; `flock` leases with PID/boot-ID, never age-only
reclaim; Keychain-backed secrets and launchd PATH; credential-expiry preflight;
total unattended daily cap that enforces; CI attribution requires a positive
test, not "untouched file"; single `github_write()` path enforcing PR
allowlist, attested SHA, footer, and action allowlist.

**Pushed back, middle path taken:**

- *Remove all write credentials from agents / build an action broker.* Correct
  architecture, disproportionate for the pilot. Middle path: `github_write()`
  is the single enforcement point inside the runner; `comms-coordinator`
  drafts but does not post.
- *Hold every reasoning or disagreement reply for Cody.* Cody chose the
  dual-agreement rule for colleague declines. Kept, with the added bar that an
  autonomous decline must cite reproducible evidence.
- *Enforce caps on every tier.* Cody chose alert-only on the cheap tier. Kept,
  plus a total unattended daily cap that enforces.
- *OS-sandbox workers in a separate account.* Correct; deferred. Tool allowlist
  and credential-free worktree in the pilot; listed as an open risk.
- *Dedicated GitHub bot identity.* Correct; deferred; listed as an open risk.

**Rejected:**

- *"Start with one active ticket, not two light lanes."* Light lane reduced to
  capacity 1; a second light lane is gated on slice 6 evidence. Reducing to one
  ticket total would remove the overlap (plan next while implementing current)
  that makes serial throughput viable on one heavy lane.
