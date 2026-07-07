#!/usr/bin/env bash
set -eu
cd "$(dirname "$0")/.."
out=$(bash bootstrap.sh --tool=claude --home=/tmp/afhome --repo="$PWD" --dry-run)
echo "$out" | grep -q "SETUP_DOC=$PWD/tools/claude/setup.md"
echo "$out" | grep -q "AF_HOME=/tmp/afhome"
bash bootstrap.sh --tool=bogus --home=/tmp/afhome --repo="$PWD" --dry-run && { echo "should have failed"; exit 1; } || true
echo "PASS"
