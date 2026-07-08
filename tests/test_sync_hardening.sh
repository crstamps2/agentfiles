#!/usr/bin/env bash
# Regression for af_sync_repo hardening: a plain `git pull --ff-only` aborts
# when the repo has pull.rebase=true configured and unstaged changes exist
# ("cannot pull with rebase: You have unstaged changes."). The hardened
# invocation (`git -c pull.rebase=false pull --ff-only --no-rebase`) must
# succeed in the same scenario instead of erroring out and aborting the
# whole bootstrap under `set -eu`.
set -eu
cd "$(dirname "$0")/.."

bare=""
clone=""
cleanup() {
  [ -n "$clone" ] && rm -rf "$clone"
  [ -n "$bare" ] && rm -rf "$bare"
}
trap cleanup EXIT

bare="$(mktemp -d)/origin.git"
clone="$(mktemp -d)/clone"

git init --quiet --bare "$bare"

git clone --quiet "$bare" "$clone"
git -C "$clone" config user.email "test@example.com"
git -C "$clone" config user.name "Test"
printf 'hello\n' > "$clone/file.txt"
git -C "$clone" add file.txt
git -C "$clone" commit --quiet -m "initial commit"
git -C "$clone" push --quiet origin HEAD

# Simulate the common footgun config plus a dirty working tree.
git -C "$clone" config pull.rebase true
printf 'unstaged change\n' >> "$clone/file.txt"

# Baseline: prove the ORIGINAL bug reproduces -- plain `pull --ff-only`
# fails with rebase-vs-dirty-tree conflict when pull.rebase=true.
if git -C "$clone" pull --ff-only >/tmp/af-sync-hardening-baseline.$$ 2>&1; then
  echo "expected baseline 'git pull --ff-only' to fail with pull.rebase=true + dirty tree, but it succeeded"
  cat /tmp/af-sync-hardening-baseline.$$
  rm -f /tmp/af-sync-hardening-baseline.$$
  exit 1
fi
baseline_output="$(cat /tmp/af-sync-hardening-baseline.$$)"
rm -f /tmp/af-sync-hardening-baseline.$$
echo "$baseline_output" | grep -qi "rebase" || {
  echo "expected baseline failure to mention rebase, got: $baseline_output"
  exit 1
}

# Hardened: the af_sync_repo invocation must succeed (exit 0) in the exact
# same scenario, despite there being nothing new to fast-forward to.
git -C "$clone" -c pull.rebase=false pull --ff-only --no-rebase >/tmp/af-sync-hardening-hardened.$$ 2>&1
status=$?
hardened_output="$(cat /tmp/af-sync-hardening-hardened.$$)"
rm -f /tmp/af-sync-hardening-hardened.$$
[ "$status" -eq 0 ] || { echo "expected hardened pull to succeed, got exit $status: $hardened_output"; exit 1; }

echo "PASS"
