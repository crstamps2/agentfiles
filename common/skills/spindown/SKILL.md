---
name: spindown
description: Tear down a finished cmux worktree -- runs worktree-teardown (drops its DB/Redis/ES + frees the slot), force-removes the git worktree (keeping the branch), and closes the cmux workspace. With no argument, targets the currently-focused worktree workspace. Refuses the prime checkout.
---

# /spindown

## Resolve the script owner

Resolve this `SKILL.md` to its real source path before running commands. Set
`SPINUP_SKILL_DIR` to the sibling `spinup` directory under that same source's `skills/` directory.
Require `$SPINUP_SKILL_DIR/scripts/cmux_chain.py` to exist; stop if it is absent.
Do not infer ownership from the current working directory or invoke another harness's copy.

These commands are for explicitly requested lifecycle actions, not wiring verification.
Do not seed, poll, install/load listeners, start workspaces or tear down resources merely
to test these paths. Existing helper behavior and cache locations are unchanged; apply
current communication, Jira-tool and foreground-work policies before invoking it.

Manual, instant teardown of a cmux worktree created by the spin-up chain.

## Usage
- `/spindown <name>` -- tear down `~/workspace/your-app-worktrees/<name>` (e.g. `/spindown proj-5864`).
- `/spindown` (no arg) -- tear down the **currently-focused** cmux worktree workspace.

## What it does
Runs: `python3 "$SPINUP_SKILL_DIR/scripts/cmux_chain.py" spindown [<name>]`

That calls `teardown_worktree`: hard-scope guard (only under `~/workspace/your-app-worktrees/`,
refuses the prime checkout) -> `bin/worktree-teardown` (DB/Redis/ES/slot) ->
`git worktree remove --force` (keeps the branch) -> `cmux close-workspace` -> `cmux notify`.

## Safety
- Only ever touches worktrees under `~/workspace/your-app-worktrees/`. Refuses the prime checkout.
- No dirty guard: uncommitted/untracked changes are destroyed; committed work survives via the branch.
