#!/usr/bin/env bash
set -eu
cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

# Self-contained synthetic deny-list: a fake token, not a real internal
# identifier, so this test never leaks anything if the repo goes public.
DENYLIST_TMP="$(mktemp -t agentfiles-denylist.XXXXXX)"
SYNTHETIC_PATH_TMP=""
export AF_DENYLIST="$DENYLIST_TMP"

cleanup() {
  rm -f "$DENYLIST_TMP"
  if [[ -n "$SYNTHETIC_PATH_TMP" ]]; then
    rm -rf "$SYNTHETIC_PATH_TMP"
  fi
}
trap cleanup EXIT

printf '# synthetic test deny-list\nACME-SECRET-TOKEN\n' > "$DENYLIST_TMP"

mkdir -p tests/fixtures/clean tests/fixtures/dirty tests/fixtures/dirty_filename \
  tests/fixtures/dirty_denyname tests/fixtures/excluded_config

echo "generic orchestration docs" > tests/fixtures/clean/ok.md
echo "contact us re token ACME-SECRET-TOKEN and key AKIAIOSFODNN7EXAMPLE" > tests/fixtures/dirty/leak.md
echo "generic orchestration docs" > tests/fixtures/dirty_filename/AKIAIOSFODNN7EXAMPLE.txt
echo "generic orchestration docs" > tests/fixtures/dirty_denyname/ACME-SECRET-TOKEN.txt

# Config files that carry deny-list/secret-looking tokens by design must
# never self-flag, whether via content or filename.
echo "ACME-SECRET-TOKEN" > tests/fixtures/excluded_config/denylist.local
echo "AKIAIOSFODNN7EXAMPLE" > tests/fixtures/excluded_config/denylist.example

bash common/scripts/agentfiles-scan.sh tests/fixtures/clean

bash common/scripts/agentfiles-scan.sh tests/fixtures/dirty && { echo "dirty (deny content) should fail"; exit 1; } || true

# Regression: invoking with an ABSOLUTE path whose prefix contains a deny-list
# token (ACME-SECRET-TOKEN from the synthetic AF_DENYLIST) must work correctly
# without false-positives. This verifies that the invocation path itself is
# excluded from the token scan.
SYNTHETIC_PATH_TMP="$(mktemp -d -t agentfiles-path-token.XXXXXX)"
SYNTHETIC_SCAN_DIR="$SYNTHETIC_PATH_TMP/ACME-SECRET-TOKEN/clean"
mkdir -p "$SYNTHETIC_SCAN_DIR"
echo "generic orchestration docs" > "$SYNTHETIC_SCAN_DIR/ok.md"
bash common/scripts/agentfiles-scan.sh "$SYNTHETIC_SCAN_DIR"

# Regression: a filename (not just file contents) containing a secret pattern
# must be caught.
bash common/scripts/agentfiles-scan.sh tests/fixtures/dirty_filename && { echo "dirty_filename (secret) should fail"; exit 1; } || true

# Regression: a filename containing a deny-list token must be caught too.
bash common/scripts/agentfiles-scan.sh tests/fixtures/dirty_denyname && { echo "dirty_denyname (deny) should fail"; exit 1; } || true

# Regression: the scanner's own config files (denylist.local / denylist.example)
# must never self-flag on content or filename, even though they intentionally
# hold deny-list/secret-shaped tokens.
bash common/scripts/agentfiles-scan.sh tests/fixtures/excluded_config

echo "PASS"
