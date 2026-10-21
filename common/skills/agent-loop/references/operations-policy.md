# Agent-loop operations policy and recovery evidence

This is the canonical policy shared by `/agent-loop` and `/loop-watch`. It describes authority,
ownership, recovery, maintenance, and proof. It does not replace higher-authority repository,
security, communications, readiness, or deployment instructions.

## 1. Authority and role boundary

| Actor or surface | May do | Must not do |
| --- | --- | --- |
| Direct user steering | Own one worktree as foreground work and direct a bounded change. | Automatically return the worktree to autonomy or displace unrelated autonomous work. |
| `/agent-loop` | Observe and explicitly operate the autonomous fleet. | Coordinate application work in one ticket or claim foreground ownership. |
| `/loop-watch` | Observe one ticket from bounded runner evidence and escalate from facts. | Admit tickets, create observation surfaces, restart supervision, or run an autonomous implementation loop. |
| Runner, reconciler, and repair service | Apply their existing durable contracts and allowlisted automation. | Expand higher-authority comms/readiness permission. |

A direct user request takes precedence over autonomous startup for its worktree. The fleet and
coordinator must preserve that boundary even when the user request arrives while a ticket has a
live runner, recovery, or cohort reservation.

A direct request to fix the runner authorizes maintenance under section 6. Do not stop at diagnosis
or ask for the same approval again. Routine coordinator recovery still belongs to automatic repair.

For Pi delegation, use the native `subagent` tool when available. The supported integration seams
are RPC v1 and owned-leaf structured delegation, not the old Orchestration v2 protocol. The local
headless helper probes v1, then delegates one leaf through the package-owned executor:

```bash
D="$HOME/workspace/agentfiles/tools/pi/scripts/subagent-dispatch.mjs"
node "$D" --probe
node "$D" --agent code-reviewer --task-file /tmp/review-task.txt \
  --cwd "$HOME/workspace/agentfiles" --report /tmp/review-result.json
```

Run long checks through a background monitor, not a new interactive Pi pane. Agent definitions own
model, thinking, and tools; the helper does not override them. The helper requires the operator's
managed Pi install, validates owner/path permissions, ignores ambient executable-root overrides,
and disables project trust and automatic context loading. Reviewed repositories cannot shadow the
user's reviewer definitions. It is a trusted local operator tool, not a sandbox or privileged CI
entrypoint. Filesystem checks cover POSIX owners/modes, not ACL attestation; the operator must
trust every principal able to modify the managed install or its ancestors. The helper does not
make an adversarial or shared-write installation safe. A successful
probe proves availability only. Require an actual `completed` response, effective model/thinking,
and the review text; completion does not mean approval. A cancellation request is not terminal
proof. Retain the exact attempt identity after an uncertain result and never blindly redispatch.
Do not fake a v2 handshake, silently substitute a CLI reviewer, or inject reload into a busy pane.

## 2. Capacity and ownership

### Autonomous capacity

- Production autonomous capacity is **2** tickets, set in the deployed hopper configuration.
- `Config.cohort_capacity` has a fallback default of **4** when configuration omits a cohort
  capacity. That fallback is not authorization to run four production tickets.
- The legacy drip count is also 2. The hard ceiling of 7 is a guardrail, not a target.
- An autonomous ticket retains its cohort slot through normal waits and structured recovery. A
  verified Human Gate 1 release is the normal release path.

Do not use an implementation default, worker-slot count, stale dashboard text, or a paused ticket
to justify more than two autonomous tickets.

### Foreground ownership

Persistent foreground ownership is outside the autonomous cohort. It does not consume one of the
two autonomous slots and it must not cause the fleet to stop unrelated tickets.

The interactive-lane extension claims foreground control only after a real interactive user input.
Coordinator startup, extension wakes, RPC, headless runs, and automated input do not claim it.
When the same worktree has a live writer, foreground tools remain blocked until that writer yields.
Only then may the existing supervisor stop and any autonomous slot release.

Autonomy resumes only when the foreground owner explicitly runs `/agent-loop-auto`. Returning
queues the ticket for normal admission; it does not create a third autonomous slot. Idleness,
settling a response, a reload, an automated wake, or a coordinator action never returns a ticket.

### Legacy and lease preservation

- Preserve a legacy `HUMAN` takeover. Never delete it, restore over its manual edits, or treat it
  as a reason to run background recovery.
- Use the supported interactive handoff, which waits for the same ticket writer lease. Do not
  acquire or rewrite it manually, or run worktree tools while the handoff is still yielding.
- Leases are keyed by PID and boot identity. A timestamp is not proof that a holder is stale.
  Reclaim only through the existing lease path after the owner is provably gone.
- Never hand-edit `INTERACTIVE.json`, `HUMAN`, cohort records, parked markers, attempt records,
  state, recovery records, or lock files to force an outcome.

## 3. Visibility and opening tools

Visibility must be runner-driven, event-driven, and bounded. No model polls status on a timer.

A coordinator may take one `runner.py watch --once` snapshot only in response to an explicit user
request, an actual state-change event, or an authorized bounded check. It then goes idle. The fleet
may take an explicit status snapshot. Neither role repeatedly re-reads state files to simulate a
watcher.

Opening a terminal, cmux surface, browser tab, Jira page, live watch, agent, or loop is an action,
not harmless visibility. Open one only when the user explicitly asks or a higher-authority policy
already authorizes it. In particular:

- `/loop-watch` never creates a pane or a live view merely because it started.
- `/loop-watch` never starts or restarts a supervisor merely because status says it is absent.
- `/agent-loop` status and resume do not open coordinator panes or replace foreground supervision.
- A runner-owned surface that already exists can be read without creating another one.

## 4. Decision and recovery policy

### What waits for Cody

Cody waits for a genuine product/API decision only. A valid decision request contains:

- sources checked and why they do not resolve the ambiguity;
- at least two materially different options and their consequences;
- a recommended option with reasoning;
- one concrete question; and
- a durable checkpoint and a valid resume target.

Do not invent a decision request, an owner ruling, or a user answer to clear a queue. Direct user
steering is foreground work, not a fabricated decision wait.

### What self-heals

Planning spend is advisory. It must not turn into a Cody hold merely because a configured warning
or planning ceiling was reached. Ordinary recovery remains bounded and evidence-based, including
retries, environment preparation, manifest repair, scoped fix tasks, re-planning, republishing,
provider waits, and runner repair.

For every recovery, preserve the original failure, use the structured recovery contract, and let
the reconciler consume the resulting event. Do not infer a resume target from a free-form reason,
and do not issue `resume` to bypass an active contract.

A runner defect is code to repair, not a reason to fake approval or manually alter runtime state.
The repair path works in a throwaway worktree, requires a new test that fails on the base and passes
with the fix, runs the full runner suite, requires opposite-vendor review, and only then can
fast-forward the live branch. A reviewer outage is a bounded wait, not permission to swap away the
required reviewer or bypass review.

`repair-approved` is valid only after the authorized process has applied and independently verified
the exact protected-path patch represented by a `needs-approval` defect. It must never be used on
an open defect, a failed repair, or as a way to make a ticket look repaired.

## 5. Runtime and manifest safety

- Never edit a live task manifest while its runner writer lease exists. Do not edit it from a
  coordinator to repair a task, gate, or dependency problem.
- Never fabricate an attempt result, accepted state, repair landing, commit, push, review, or
  Human Gate 1 receipt.
- Preserve attempts, planning artifacts, recovery events, repair records, and audit logs. They are
  evidence, not cleanup targets.
- Do not use `git reset`, forced checkout, deletion of untracked evidence, or an ad hoc runtime
  file edit to resolve a coordination conflict.
- Use local evidence paths and bounded, redacted excerpts in recovery reports. Do not copy whole
  state records into prompts or external artifacts. Never expose credentials or tokens.

## 6. Maintenance coordination checklist

Use this checklist before editing launchd-related source, changing the runner, or advancing the
live branch. These checks are observational unless a higher-authority process authorizes a change.

1. Establish the candidate and cleanliness:

   ```bash
   git status --short
   git rev-parse --show-toplevel
   git branch --show-current
   git rev-parse HEAD
   ```

2. Inspect fleet and repair service status from the runner directory:

   ```bash
   R="$HOME/workspace/agentfiles/common/skills/agent-loop/scripts"
   cd "$R"
   python3 runner.py supervise status
   ```

3. Inspect the repair lease owner, held flock, process liveness, and current boot identity.
   `repair.py` acquires this lease with `hold=True`; its open file descriptor owns the flock.
   A stale timestamp is never proof that a lease is free.

   ```bash
   cd "$R"
   python3 - <<'PY'
   import config
   import locks

   cfg = config.load()
   lease = cfg.state_root / "locks" / "repair"
   owner = locks.owner(lease)
   held = locks.is_held(lease)
   print({"lease": str(lease), "owner": owner, "flock_held": held,
          "current_boot_id": locks.boot_id()})
   if held:
       raise SystemExit("Repair flock held: prepare outside this checkout and wait")
   if owner:
       try:
           pid = int(owner["pid"])
           if pid <= 0:
               raise ValueError("invalid PID")
           live = locks.pid_alive(pid)
       except (KeyError, TypeError, ValueError):
           raise SystemExit("Uncertain repair ownership: stop maintenance and investigate")
       print({"pid_alive": live, "same_boot": owner.get("boot_id") == locks.boot_id()})
       if live and owner.get("boot_id") == locks.boot_id():
           raise SystemExit("Live repair owns maintenance: prepare outside this checkout and wait")
   PY
   ```

4. If a repair is live or ownership is uncertain, wait. Inspect its candidate worktree and record
   its base SHA. Prepare proposals outside the live checkout; do not edit it or advance HEAD during
   that repair's suite or review. Never stop, kill, or bypass the service. After release, recheck
   branch, HEAD, cleanliness, and ownership immediately before applying the candidate. This
   observational preflight is not a reservation. For an authorized maintenance window, acquire
   the same existing `locks.Lease(.../locks/repair, "operator maintenance")` with `hold=True`,
   hold its file descriptor throughout source/HEAD changes, and release in `finally`. A failed
   acquisition means wait for the real owner, not stop the service. Do not use the default
   PID-only acquisition mode: automatic repair also checks the held flock.
5. Preserve unrelated and concurrent changes. Do not reset, clean, force-push, or alter another
   writer's checkout to make a candidate clean.
6. Freeze the candidate for a full suite and independent review through authorized coordination,
   not by stopping repair. Record the exact candidate SHA and diff under review. If HEAD moves,
   reread the new source and rerun affected validation and review. Before any history comparison,
   fetch the current base (`git fetch --unshallow` if shallow, otherwise `git fetch origin main`);
   compare against the remote base, never a stale local main.
7. Run the full runner suite from the runner directory:

   ```bash
   cd "$R"
   python3 -m unittest discover -p 'test_*.py'
   ```

8. Commit or push only verified, scoped work that belongs to the current owner. Before any public
   repository push, stage the candidate in a scratch directory, run
   `common/scripts/agentfiles-scan.sh <staged-dir>`, and obtain the required security review.
   A clean scanner result does not replace the security review or the communications gate.
9. Report unavailable independent review, blocked scans, moving HEAD, and unverified gates as
   residual risk. None of them may be relabeled as approval.

## 7. Recovery-proof checklist

Use precise verbs. These states are different:

| Claim | Minimum receipt | Does not prove |
| --- | --- | --- |
| Resumed | Recovery event or supervisor action and resulting lifecycle snapshot. | A fix was accepted, committed, pushed, or reviewed. |
| Accepted | Exact worker attempt accepted by runner guards for its task identity and slug. | A commit or publication occurred. |
| Committed | Exact local commit SHA and scoped diff. | The remote branch contains it. |
| Pushed | Remote branch evidence matches the claimed commit SHA. | CI, review, readiness, or Human Gate 1. |
| Verified Human Gate 1 | Open ready PR, evidence SHA matching the worktree HEAD, no active recovery or open decision, and the runner's recorded gate evidence. | Any later human review, merge, or deployment. |

Every recovery report must include:

- the original failure and exact failing gate or observed reason;
- recovery or repair identity, including the exact repair commit when one exists;
- the actual regenerated task ID/slug and verification command when recovery changed a manifest
  or guard, plus output and exit status proving the original failing gate passes; a formatter pass
  cannot establish an ESLint fix;
- normal commit and publication receipts, if claimed;
- gates that remain unverified;
- unavailable or rejected reviews; and
- a statement of what is not proved yet.

Do not compress these states into "fixed." If a receipt is unavailable, say so rather than
substituting a status label, a stale log, or an assumed remote result.

## 8. Regression standard

Run the full Python suite for runner changes:

```bash
R="$HOME/workspace/agentfiles/common/skills/agent-loop/scripts"
cd "$R"
python3 -m unittest discover -p 'test_*.py'
```

For ownership-extension changes, also run the native extension test:

```bash
REPO="$HOME/workspace/agentfiles"
cd "$REPO"
node --experimental-strip-types --test tools/pi/extensions/interactive-lane/index.test.ts
```

Use targeted modules during iteration when applicable:

```bash
cd "$R"
python3 -m unittest \
  test_config test_interactive test_lifecycle test_parallel test_contracts test_guards test_repair
```

A change touching scheduling, manifest acceptance, recovery, or hook classification needs evidence
for all relevant cases below. Add or preserve a behavioral test, not a source-string assertion.

- Serial and parallel execution of the same explicit dependency DAG must agree on accepted task
  identities and dependency ordering. Dependencies must land before dependents; runnable tasks use
  deterministic manifest order.
- Missing and cyclic prerequisites must dispatch no task and must enter the contract-repair path,
  not advance gates. Existing lifecycle coverage exercises a later prerequisite and a no-runnable
  cycle/missing-predecessor case.
- A re-planned task must match the accepted slug, not reuse an old acceptance for the same task ID.
- Reproduce the original failure with literal, realistic fixtures on the base, then pass with the
  fix. Mutate the relevant production guard and verify the regression fails again. A test that
  passes on the base proves nothing.
- Hook-classification changes need complete realistic hook output: failed, passed, skipped, and
  simultaneous Ruby/JavaScript/stylesheet failures. Prove the selected command matches the failing
  tool and selected paths. ESLint must never vacuously verify Ruby or stylesheet remediation.
  Preserve all original failed gates across generated fix tasks; fixing one does not clear the rest.
- Guard changes need an unrunnable-guard case and base-tree attribution evidence.

The existing suite has representative lifecycle fixtures for pre-commit lint, Reek, ESLint,
Prettier, skip handling, and mixed tool output; guard tests cover unrunnable guards and base-tree
attribution; repair tests reject a new test that passes on the base. It does **not** establish one
complete end-to-end mixed Ruby/JavaScript/stylesheet hook matrix or a serial-versus-parallel
same-DAG equivalence test. Do not claim either until it exists and passes.

## 9. Pressure scenarios

Before changing the policy or runner, reason through and, where behavior changes, test these
scenarios:

1. Two autonomous slots are full when a direct user message arrives for a parked worktree. The user
   can take foreground control without stopping either autonomous ticket or admitting a third.
2. A direct user message arrives while the same worktree still has a live writer. The handoff stays
   yielding and blocks foreground tools until the writer yields.
3. A legacy `HUMAN` takeover contains manual edits or an unresolved worker. Manual work is never
   restored over; a live worker prevents an unsafe handoff.
4. A planner warning, spent planning budget, malformed manifest, or ordinary gate issue occurs.
   It follows bounded recovery rather than inventing a Cody decision.
5. A manifest has a missing predecessor or a cycle. No task dispatches and no gate passes while the
   contract-repair path receives the evidence.
6. The repair service is active or lands a branch change during a maintenance suite or review. The
   candidate is revalidated at the moved HEAD without stopping or bypassing repair.
7. An opposite-vendor reviewer or provider is unavailable. The repair waits with evidence; it does
   not silently switch reviewer authority or claim review approval.
8. A coordinator starts while a supervisor or pane is absent. It reports that fact and opens or
   restarts nothing without explicit authorization.

Record the observed receipts and remaining uncertainty for each exercised scenario. A green unit
test alone does not authorize a communications, readiness, merge, or deployment action.
