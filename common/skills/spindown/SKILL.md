---
name: spindown
description: Tear down a finished cmux worktree -- runs worktree-teardown (drops its DB/Redis/ES + frees the slot), force-removes the git worktree (keeping the branch), and closes the cmux workspace. With no argument, targets the currently-focused worktree workspace. Refuses the prime checkout.
---

# /spindown

Manual, instant teardown of a cmux worktree created by the spin-up chain.

## Usage
- `/spindown <name>` -- tear down `~/workspace/your-app-worktrees/<name>` (e.g. `/spindown proj-5864`).
- `/spindown` (no arg) -- tear down the **currently-focused** cmux worktree workspace.

## What it does
Runs: `python3 ~/.claude/skills/spinup/scripts/cmux_chain.py spindown [<name>]`

That calls `teardown_worktree`: hard-scope guard (only under `~/workspace/your-app-worktrees/`,
refuses the prime checkout) -> `bin/worktree-teardown` (DB/Redis/ES/slot) ->
`git worktree remove --force` (keeps the branch) -> `cmux close-workspace` -> `cmux notify`.

## Safety
- Only ever touches worktrees under `~/workspace/your-app-worktrees/`. Refuses the prime checkout.
- No dirty guard: uncommitted/untracked changes are destroyed; committed work survives via the branch.
