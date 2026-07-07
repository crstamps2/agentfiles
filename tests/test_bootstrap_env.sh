#!/usr/bin/env bash
set -eu
cd "$(dirname "$0")/.."
out=$(bash bootstrap.sh --tool=codex --home=/tmp/afhome --repo=/tmp/afrepo --print-env)
echo "$out" | grep -q "AF_TOOL=codex"
echo "$out" | grep -q "AF_HOME=/tmp/afhome"
echo "$out" | grep -q "AF_REPO=/tmp/afrepo"
echo "$out" | grep -qE "AF_OS=(macos|linux)"
echo "PASS"
