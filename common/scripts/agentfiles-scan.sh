#!/usr/bin/env bash
set -eu
DIR="${1:?usage: agentfiles-scan.sh <dir>}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Resolve the deny-list file: env override, then a gitignored local file
# with real internal tokens, then the committed generic example.
if [ -n "${AF_DENYLIST:-}" ] && [ -r "${AF_DENYLIST:-}" ]; then
  DENYLIST_FILE="$AF_DENYLIST"
elif [ -r "$SCRIPT_DIR/denylist.local" ]; then
  DENYLIST_FILE="$SCRIPT_DIR/denylist.local"
else
  DENYLIST_FILE="$SCRIPT_DIR/denylist.example"
fi

DENY="$(grep -v '^[[:space:]]*#' "$DENYLIST_FILE" 2>/dev/null | grep -v '^[[:space:]]*$' | paste -sd '|' - 2>/dev/null || true)"
if [ -z "$DENY" ]; then
  # No usable tokens; fall back to a pattern that never matches (portable ERE).
  DENY='ZZZ_AGENTFILES_NO_DENYLIST_TOKENS_CONFIGURED_ZZZ'
fi

SECRET='AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|xox[baprs]-[0-9A-Za-z-]+|gh[pousr]_[0-9A-Za-z]{20,}|sk-[0-9A-Za-z]{20,}'

# Config files that hold deny-list tokens by design; never let them self-flag.
CONFIG_EXCLUDE='(^|/)denylist\.(local|example)$'

rc=0

if grep -rIiEl "$DENY" "$DIR" 2>/dev/null | grep -vE "$CONFIG_EXCLUDE"; then
  echo "DENY-LIST hit (above)"; rc=1
fi
if grep -rIEl "$SECRET" "$DIR" 2>/dev/null | grep -vE "$CONFIG_EXCLUDE"; then
  echo "SECRET pattern hit (above)"; rc=1
fi

# Check filenames only, relative to $DIR, so the invocation path itself
# (which may itself contain a deny-list token) isn't re-matched against
# its own tokens.
names=$(cd "$DIR" && find . )
filtered_names=$(printf '%s\n' "$names" | grep -vE "$CONFIG_EXCLUDE" || true)
if printf '%s\n' "$filtered_names" | grep -iE "$DENY" ; then echo "DENY-LIST filename hit (above)"; rc=1; fi
if printf '%s\n' "$filtered_names" | grep -iE "$SECRET" ; then echo "SECRET pattern filename hit (above)"; rc=1; fi
[ "$rc" = 0 ] && echo "scan clean: $DIR"
exit "$rc"
