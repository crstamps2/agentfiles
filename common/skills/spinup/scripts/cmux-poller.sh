#!/bin/bash
# cmux-poller.sh -- launchd wrapper for the cmux work listener.
#
# launchd runs with a bare environment (no ~/.bashrc, minimal PATH).
# This wrapper bakes in the absolute locations of tools on this machine so
# every tool the helper shells out to resolves correctly.
#
# Adapt tool paths for your machine (run `which python3`, `which cmux`, etc.):
#   python3  -> /opt/homebrew/bin/python3
#   cmux     -> /opt/homebrew/bin/cmux
#   git      -> /opt/homebrew/bin/git
#   acli     -> /opt/homebrew/bin/acli
#   gh       -> /opt/homebrew/bin/gh

export HOME="${HOME:-$HOME}"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/usr/local/bin"

# The work-hours gate has moved into cmux_chain.py (within_work_hours()).
# Only Jira auto-spin is gated; PR notifications and Archive sweeps run 24/7.
exec python3 "$HOME/.claude/skills/spinup/scripts/cmux_chain.py" cmux-poll \
    >> "$HOME/.claude/cache/cmux-work-listener.log" 2>&1
