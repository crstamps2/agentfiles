#!/bin/bash

# Stop hook: provide advisory commit hygiene without granting mutation authority.

# Only run inside a git repo
git rev-parse --is-inside-work-tree &>/dev/null || exit 0

# Count uncommitted changed files (staged + unstaged, excluding untracked)
CHANGED_FILES=$(git diff --name-only HEAD 2>/dev/null | wc -l | tr -d ' ')
STAGED_FILES=$(git diff --cached --name-only 2>/dev/null | wc -l | tr -d ' ')
TOTAL=$((CHANGED_FILES + STAGED_FILES))

# If fewer than 2 files changed, not significant enough to prompt
[ "$TOTAL" -lt 2 ] && exit 0

# Output context for Claude to act on
cat <<'EOF'
There are uncommitted tracked changes in the working tree. This reminder does not authorize staging, committing, or changing task scope. During a read-only audit, review, or status request, leave them untouched. Pre-existing changes may belong to the user or another session; never absorb, stash, reset, or commit them merely to clean the checkout. Only when the current task owns the exact changes and the user or applicable policy authorizes commits, verify them and make small focused commits using the project's conventions. Otherwise report the pending changes without modifying them.
EOF
