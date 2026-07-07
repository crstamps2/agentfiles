#!/bin/bash
# cmux-close-listener.sh -- launchd wrapper for the instant close-event listener.
# Long-running: streams workspace.closed and tears down the matching worktrees.
export HOME="${HOME:-$HOME}"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/usr/local/bin"
exec python3 "$HOME/.claude/skills/spinup/scripts/cmux_chain.py" close-listen \
    >> "$HOME/.claude/cache/cmux-close-listener.log" 2>&1
