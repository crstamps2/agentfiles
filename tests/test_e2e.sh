#!/usr/bin/env bash
set -eu
cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

# ---------------------------------------------------------------------------
# DEFERRED to the manual live smoke test (see task-12-brief.md Step 3; record
# results in the PR, NOT here). This harness is 100% offline/hermetic:
#   - real materialization into the actual ~/.claude and ~/.codex directories
#     on macOS AND Linux
#   - a real network clone of https://github.com/crstamps2/agentfiles.git
#   - actually launching the real `claude` / `codex` CLIs
#   - the tool actually discovering skills/agents/hooks/MCP at runtime
#   - a symlinked skill being found by the tool
#   - a read-only agent actually being constrained at runtime
#   - confirming a second real run is a true no-op end-to-end
# Everything below only exercises bootstrap.sh's argument parsing, exit
# codes, and clone-vs-pull branch selection via shimmed fakes on PATH.
# ---------------------------------------------------------------------------

TMP_ROOT="$(mktemp -d -t agentfiles-e2e.XXXXXX)"
cleanup() { rm -rf "$TMP_ROOT"; }
trap cleanup EXIT

# ---------------------------------------------------------------------------
# 1. Dry-run resolution: each tool resolves its own setup doc + prints AF_* env
# ---------------------------------------------------------------------------
for t in claude codex; do
  out=$(bash bootstrap.sh --tool="$t" --home="$TMP_ROOT/af-$t" --repo="$REPO_ROOT" --dry-run)
  echo "$out" | grep -q "SETUP_DOC=$REPO_ROOT/tools/$t/setup.md"
  echo "$out" | grep -q "AF_HOME=$TMP_ROOT/af-$t"
  echo "$out" | grep -q "AF_REPO=$REPO_ROOT"
  echo "$out" | grep -qE "AF_OS=(macos|linux)"
done

# ---------------------------------------------------------------------------
# 2. Invalid tool exits 2
# ---------------------------------------------------------------------------
set +e
bash bootstrap.sh --tool=bogus --home="$TMP_ROOT/af-bogus" --repo="$REPO_ROOT" --dry-run
rc=$?
set -e
[ "$rc" = 2 ] || { echo "expected exit 2 for bogus tool, got $rc"; exit 1; }

# ---------------------------------------------------------------------------
# 3. Missing setup doc exits 3 (deferred exit-3 coverage)
# ---------------------------------------------------------------------------
EMPTY_REPO="$TMP_ROOT/empty-repo"
mkdir -p "$EMPTY_REPO"
set +e
bash bootstrap.sh --tool=claude --home="$TMP_ROOT/af-empty" --repo="$EMPTY_REPO" --dry-run
rc=$?
set -e
[ "$rc" = 3 ] || { echo "expected exit 3 for missing setup doc, got $rc"; exit 1; }

# ---------------------------------------------------------------------------
# 4. Security gate: common/ source is clean
# ---------------------------------------------------------------------------
bash common/scripts/agentfiles-scan.sh common

# ---------------------------------------------------------------------------
# 5. Clean separation between tool setup docs
# ---------------------------------------------------------------------------
! grep -iE "codex|\.codex" tools/claude/setup.md
! grep -iE "claude" tools/codex/setup.md
! grep -iE "claude" tools/codex/openai.yaml.tmpl

# ---------------------------------------------------------------------------
# 6. Idempotency + dispatch: offline shim test for af_sync_repo's
#    clone-vs-pull branch and af_dispatch's CLI invocation.
#    (This is the deferred af_sync_repo network coverage, done hermetically.)
# ---------------------------------------------------------------------------
SHIM_BIN="$TMP_ROOT/bin"
mkdir -p "$SHIM_BIN"
GIT_LOG="$TMP_ROOT/git.log"
: > "$GIT_LOG"

cat > "$SHIM_BIN/git" <<'EOS'
#!/usr/bin/env bash
# Fake git: logs whether it was invoked for clone or pull, and for clone
# materializes a minimal fake checkout (.git dir + a stub tools/<t>/setup.md
# for both tools) so the caller's subsequent file-existence checks pass,
# mirroring what a real `git clone` of this repo would leave on disk.
args="$*"
case "$args" in
  *clone*)
    echo clone >> "$GIT_LOG"
    # Portable (bash 3.2-safe) way to grab the last positional arg (the
    # clone target dir) without relying on ${@: -1} / ${!#}.
    target=""
    for a in "$@"; do target="$a"; done
    mkdir -p "$target/.git" "$target/tools/claude" "$target/tools/codex"
    echo "# stub setup doc (shim)" > "$target/tools/claude/setup.md"
    echo "# stub setup doc (shim)" > "$target/tools/codex/setup.md"
    ;;
  *pull*)
    echo pull >> "$GIT_LOG"
    ;;
  *)
    echo "unexpected git invocation: $args" >&2
    exit 99
    ;;
esac
exit 0
EOS
chmod +x "$SHIM_BIN/git"

for fake_cli in claude codex; do
  cat > "$SHIM_BIN/$fake_cli" <<EOS
#!/usr/bin/env bash
echo "dispatched $fake_cli \$*"
exit 0
EOS
  chmod +x "$SHIM_BIN/$fake_cli"
done

# GIT_LOG is read inside the fake git shim via the exported env var below.
export GIT_LOG

FRESH_REPO="$TMP_ROOT/fresh-repo"

# (a) First run against a repo path that does not exist yet -> must clone,
#     then dispatch the fake CLI.
out_a=$(PATH="$SHIM_BIN:$PATH" bash bootstrap.sh --tool=claude --home="$TMP_ROOT/af-home" --repo="$FRESH_REPO" 2>&1)
echo "$out_a" | grep -q "dispatched claude"
[ "$(cat "$GIT_LOG")" = "clone" ] || { echo "expected only a clone on first run, got: $(cat "$GIT_LOG")"; exit 1; }

# (b) Second run against the SAME repo path (now has .git + setup.md from the
#     clone shim above) -> must pull, not clone again, proving idempotent
#     re-run selects the pull branch.
: > "$GIT_LOG"
out_b=$(PATH="$SHIM_BIN:$PATH" bash bootstrap.sh --tool=claude --home="$TMP_ROOT/af-home" --repo="$FRESH_REPO" 2>&1)
echo "$out_b" | grep -q "dispatched claude"
[ "$(cat "$GIT_LOG")" = "pull" ] || { echo "expected only a pull on second (idempotent) run, got: $(cat "$GIT_LOG")"; exit 1; }

echo "PASS"
