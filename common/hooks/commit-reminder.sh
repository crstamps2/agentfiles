#!/bin/bash

# Stop hook: remind Claude to make small logical commits when there are
# significant uncommitted changes (implementation + tests).

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
There are uncommitted code changes in the working tree. If the current task has produced a logical, self-contained set of changes (with tests when applicable), make a small focused commit now before continuing. Follow the project's commit conventions for branch prefix and message format. Don't bundle unrelated changes together — prefer multiple small commits over one large one.
EOF
