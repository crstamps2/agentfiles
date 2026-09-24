# lifecycle.step extraction — plan (not started)

**Status:** approved, deliberately NOT started. Cody's instruction 2026-09-23: wait until he has
reviewed PR #48066 (ZIP-7874) and PR #48020 (ZIP-7875). Do not begin before that.

## Why

`lifecycle.step` is 415 lines: one `if t.state == X` block per stage, sharing `cfg / t / wt / tdir`
and each returning a `Step`. It is a dispatch table pretending to be a function. Nearly every defect
found during the week of 2026-09-17..23 lived inside it:

- the local `ledger` name shadowing the module import for the whole function
- the stale-`revise` and stale-`approve` verdict reads
- the promise gate that let six "I will fix this" replies reach a human gate with zero commits
- the cached-adjudication replay
- publish ordering vs. a pending lint fix

The risk of leaving it is not complexity, it is that this is the live control path for every ticket.

## Scope

`lifecycle.step` only. Explicitly NOT `runner._attempt` (256 lines): it owns the attempt/fence/
crash-window invariants, and those deserve harder exercise before anything is rearranged around them.

## Method — mechanical extraction, one stage per commit, zero behaviour change

1. Introduce `_stage_handlers: dict[str, Callable]` and a small `StageCtx` carrying
   `cfg, t, wt, tdir, key, hopper_index, pr`.
2. Move ONE stage per commit, in this order (smallest and most self-contained first, so the harness
   is proven before the dangerous stages):
   `gates` -> `draft-pr` -> `ready` -> `bot-loop` -> `implement` -> `plan` -> the heal block.
3. Each commit is cut-and-paste plus handler registration. No logic edits at all.
4. After each commit: the full suite (721 tests as of 2026-09-23) AND `runner.py dry-run`, which
   exercises the state machine without paying a model.
5. Anything noticed mid-move gets written down here and fixed in a separate later commit. Mixing a
   refactor with a fix destroys the ability to bisect, and this is the file where bisecting matters.

## Preconditions before starting

- Cody has reviewed both open PRs (they are the loop's only unmerged output).
- The drip is stopped (`runner.py supervise drip-stop`) and no ticket supervisor is loaded, so no
  ticket can enter the state machine mid-refactor.
- Full suite green on the branch before the first extraction commit.

## Noticed during the branch review, deferred deliberately (do NOT fold into the refactor)

- The adversarial lane's deepest finding: "a deterministic runner decides, models only propose" is
  false at the semantic boundary — models still author acceptance criteria and may `decline` a valid
  review finding without machine-checkable evidence. That is a design change (repository-owned
  mandatory gates per change category), not a patch.
- 19 of ~200 commits on this branch touched `scripts/*.py` without a `test_` change in the same
  commit, against the repo's own rule.
