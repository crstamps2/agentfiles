#!/usr/bin/env bash
set -eu
AF_TOOL=""; AF_HOME="${HOME}"; AF_REPO=""; AF_PRINT_ENV=0; AF_DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --tool=*) AF_TOOL="${arg#*=}" ;;
    --home=*) AF_HOME="${arg#*=}" ;;
    --repo=*) AF_REPO="${arg#*=}" ;;
    --print-env) AF_PRINT_ENV=1 ;;
    --dry-run) AF_DRY_RUN=1 ;;
  esac
done
case "$(uname)" in Darwin) AF_OS=macos ;; *) AF_OS=linux ;; esac
[ -z "$AF_REPO" ] && AF_REPO="${AF_HOME}/.agentfiles"
if [ "$AF_PRINT_ENV" = "1" ]; then
  printf 'AF_TOOL=%s\nAF_HOME=%s\nAF_REPO=%s\nAF_OS=%s\n' "$AF_TOOL" "$AF_HOME" "$AF_REPO" "$AF_OS"
  exit 0
fi

af_sync_repo() {
  if [ -d "$AF_REPO/.git" ]; then git -C "$AF_REPO" -c pull.rebase=false pull --ff-only --no-rebase >/dev/null 2>&1 || echo "warning: could not fast-forward $AF_REPO (diverged/dirty/offline); using existing checkout as-is" >&2
  else git clone https://github.com/crstamps2/agentfiles.git "$AF_REPO"; fi
}
af_dispatch() {
  case "$AF_TOOL" in claude|codex) : ;; *) echo "error: --tool must be claude|codex" >&2; exit 2 ;; esac
  SETUP_DOC="$AF_REPO/tools/$AF_TOOL/setup.md"
  [ -f "$SETUP_DOC" ] || { echo "error: missing $SETUP_DOC" >&2; exit 3; }
  if [ "${AF_DRY_RUN:-0}" = "1" ]; then
    printf 'SETUP_DOC=%s\nAF_HOME=%s\nAF_REPO=%s\nAF_OS=%s\n' "$SETUP_DOC" "$AF_HOME" "$AF_REPO" "$AF_OS"; return 0
  fi
  AF_HOME="$AF_HOME" AF_REPO="$AF_REPO" AF_OS="$AF_OS" "$AF_TOOL" "Read and mechanically execute $SETUP_DOC. Do not improvise."
}

if [ "$AF_DRY_RUN" != "1" ]; then
  af_sync_repo
fi
af_dispatch
