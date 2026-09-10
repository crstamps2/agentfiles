#!/bin/bash
# launchd entrypoint for the agent-loop idle tick. Bare env -> explicit PATH. Secrets come from
# Keychain via models.json, never from this file or the plist.
export HOME="${HOME:-/Users/cody}"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin"
exec /opt/homebrew/bin/python3 "$HOME/.pi/agent/skills/agent-loop/scripts/runner.py" run-once \
  >> "$HOME/.local/state/agent-loop/tick.log" 2>&1
