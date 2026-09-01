#!/usr/bin/env bash
set -eu
cd "$(dirname "$0")/.."
out=$(bash bootstrap.sh --tool=claude --home=/tmp/afhome --repo="$PWD" --dry-run)
echo "$out" | grep -q "SETUP_DOC=$PWD/tools/claude/setup.md"
echo "$out" | grep -q "AF_HOME=/tmp/afhome"
out_pi=$(bash bootstrap.sh --tool=pi --home=/tmp/afhome --repo="$PWD" --dry-run)
echo "$out_pi" | grep -q "SETUP_DOC=$PWD/tools/pi/setup.md"
bash bootstrap.sh --tool=bogus --home=/tmp/afhome --repo="$PWD" --dry-run && { echo "should have failed"; exit 1; } || true
echo "PASS"
