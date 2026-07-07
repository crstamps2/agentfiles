---
name: spinup
description: Spin up a Jira ticket or PR review in cmux (default) -- creates a git worktree, runs setup, opens a dev-server tab, launches an agent tab with the work prompt, and opens a browser tab. With --atlas, falls back to the legacy Atlas workspace path. With no argument, lists pending eligible tickets.
---

# /spinup

> **Note:** This is a worked example skill. The paths, repo references, and Jira project key below use placeholder values (`your-app`, `your-org/your-app`, `PROJ-XXXX`). Replace them with your own project details before use.

Personal workflow command. Four modes:

1. **`/spinup`** -- list newly-assigned tickets that are eligible to spin up (status: Selected for Work or Triage), excluding those already spun up. Each is shown with a `/spinup PROJ-XXXX` call-to-action.

2. **`/spinup PROJ-1234`** -- spin up a Jira ticket via **cmux** (default).

3. **`/spinup #<num>`** (or `/spinup pr-<num>`) -- spin up a PR for review via **cmux** (default).

4. **`/spinup --atlas PROJ-1234`** or **`/spinup --atlas #<num>`** -- escape hatch: use the legacy Atlas workspace path instead of cmux.

## Full spin-up chain (cmux default)

When spinning up a ticket or PR without `--atlas`, the chain does:

1. **git worktree** -- `git worktree add` from `~/workspace/your-app` into `~/workspace/your-app-worktrees/<name>` (idempotent: reuses path if it already exists).
2. **Setup tab** -- opens a cmux workspace with a terminal tab running `bin/worktree-setup` (with `WORKTREE_NAME` and `WORKTREE_ROOT_PATH` set). Writes an exit sentinel (`.cmux-setup-status`) when done. Blocks until setup succeeds or times out (10 min).
3. **Dev-server tab** -- opens a second terminal tab, sends `bin/dev` + Enter (worktree-aware: picks Procfile.dev.worktree via .env). Polls `https://admin.<name>.test` until it responds or times out (2 min).
4. **Agent tab** -- opens a third terminal tab, launches `claude`, waits briefly for it to start, then submits the work prompt already typed:
   - Jira: `writing-plans for PROJ-XXXX, ...`
   - PR: `lens-review for PR #<num>, QA on admin.pr-<num>.test`
5. **Browser tab** (only if dev server is up) -- opens a cmux browser surface at `https://admin.<name>.test`.
6. **Reference browser pane** (unconditional, separate from the test-app browser) -- opens a `new-pane` browser pane at the Jira ticket URL (Jira spin-ups) or GitHub PR URL (PR spin-ups). Best-effort: a failure here does not abort the chain.
7. **Notify** -- fires a `cmux notify` when the workspace is ready (or on failure).

### Failure modes

| Stage failed | Behavior |
|---|---|
| Setup fails / times out | Chain stops; no dev-server or agent tab; failure notify fired |
| Port conflict (after setup) | Chain auto-reassigns: calls `find_free_port(port+1)`, then re-runs `bin/worktree-setup` with `WORKTREE_PORT=<free>`. On success, fires a notify "moved to port N" and continues. On re-setup failure, fires "port reassign failed" and stops with status `port-reassign-failed`. |
| Dev server times out | Agent tab still opened; browser tab skipped; `serving-timeout` status |

## How to invoke

When the user runs `/spinup`, shell out to the appropriate script based on the mode.

### List mode (no argument)

Run:
```bash
python3 ~/.claude/skills/spinup/scripts/spinup_helper.py list-pending
```

The helper returns a JSON array of pending tickets, each with `key`, `title`, `issue_type`, `status`. Render them as a numbered list:

```
Pending spinup (N tickets):
  1. PROJ-1234 [Story]      -- Add login button
     Run: /spinup PROJ-1234
  2. PROJ-99   [Bug]        -- Fix avatar crash
     Run: /spinup PROJ-99
```

If the array is empty: print "No tickets pending spinup." If the helper exits non-zero, print the stderr message and stop.

### Spinup mode -- Jira ticket (cmux default)

Run:
```bash
python3 ~/.claude/skills/spinup/scripts/cmux_chain.py spinup PROJ-1234
```

On success it prints JSON with `ref`, `branch`, `status`, `workspace`, `worktree`, and optionally `suggested_prompt`. Render as a confirmation:

```
Spun up PROJ-1234 (cmux):
  Branch:    feature/proj-1234-add-login-button
  Workspace: proj-1234
  Worktree:  ~/workspace/your-app-worktrees/proj-1234
  Status:    ok
```

If `status` is `serving-timeout`: note "dev server did not respond -- agent tab opened, browser tab skipped." If `status` is `setup-failed` or `port-reassign-failed`: surface the failure and stop.

### PR review mode (cmux default)

Run:
```bash
python3 ~/.claude/skills/spinup/scripts/cmux_chain.py spinup-pr 45647
```

On success it prints JSON with `pr`, `decision`, `branch`, `status`, `workspace`, `worktree`, and optionally `suggested_prompt`. Render as a confirmation:

```
Spinning up PR #45647 for review (cmux):
  Branch:    internal/foo-bar
  Workspace: pr-45647
  Worktree:  ~/workspace/your-app-worktrees/pr-45647
  Status:    ok
```

Apply the same `status` rendering rules as the Jira spinup mode above.

### --atlas escape hatch

Use when cmux is unavailable or the user explicitly passes `--atlas`.

#### --atlas Jira ticket

Run:
```bash
python3 ~/.claude/skills/spinup/scripts/spinup_helper.py spinup PROJ-1234
```

The helper fetches the ticket, derives the branch, transitions to In Progress, then creates an Atlas workspace (idempotent via `atlas-cli workspaces new --use-existing`) and runs `bin/worktree-setup` in a visible PTY tab. On success it prints JSON with `ticket`, `branch`, `workspace_id`, `workspace_name`, `worktree_path`, `suggested_prompt`, `transitioned`. Render as a confirmation:

```
Spun up PROJ-1234 (Atlas):
  Branch:           feature/proj-1234-add-login-button
  Workspace:        proj-1234
  Worktree:         ~/.atlas/workspaces/your-app/proj-1234
  Suggested prompt: writing-plans for PROJ-1234, make sure to test...
  Transition:       To Do -> In Progress
```

If `transitioned: false`, replace the Transition line with: `Transition: already In Progress (skipped)`.
Open the workspace in Atlas and paste the suggested prompt into a visible Claude tab to start planning.

There is NO auto-launched Claude work tab on the Atlas path. The dev server is NOT auto-started; `bin/conductor-setup` configures puma-dev, which lazy-boots `admin.<workspace>.test` on the first request.

#### --atlas PR review

Run:
```bash
python3 ~/.claude/skills/spinup/scripts/spinup_helper.py spinup-pr 45647

# Skip a PR (logs decision, no workspace created)
python3 ~/.claude/skills/spinup/scripts/spinup_helper.py spinup-pr 45647 --skip
```

On success (`review`), prints JSON with `pr`, `decision`, `branch`, `workspace_id`, `workspace_name`, `worktree_path`, `suggested_prompt`. Render as confirmation:

```
Spinning up PR #45647 for review (Atlas):
  Branch:           internal/foo-bar
  Workspace:        pr-45647
  Worktree:         ~/.atlas/workspaces/your-app/pr-45647
  Suggested prompt: lens-review for PR #45647, QA on admin.pr-45647.test
```

Open the workspace in Atlas and paste the suggested prompt into a visible Claude tab to start the review.

On `--skip`, prints `{"pr": 45647, "decision": "skip"}` and logs the decision.

## poll subcommand (used by the local launchd work-listener)

Not for manual use. The launchd job calls:

```bash
python3 ~/.claude/skills/spinup/scripts/spinup_helper.py poll
```

`poll` auto-spins all newly-assigned eligible Jira tickets (create workspace + run setup) and surfaces new PR review requests. Prints JSON:

```json
{
  "jira_spun_up": [{"key": "PROJ-1", "branch": "bugfix/proj-1-t", "workspace": "proj-1"}],
  "prs_to_surface": [{"number": 45647, "title": "...", "author": "alice", "url": "..."}]
}
```

When either `jira_spun_up` or `prs_to_surface` is non-empty, `poll` fires a single **macOS notification** (via terminal-notifier or osascript) telling you workspaces are prepped and ready:

- **Title**: `Atlas: {J} ready, {P} PRs` (omitting the zero side -- e.g. `Atlas: 2 ready` or `Atlas: 1 PR review`)
- **Body**: one line per spun-up ticket (`v PROJ-XXXX ready to plan`) then one line per PR (`PR #<num> (<author>) -- /spinup #<num>`)

When both lists are empty, no notification fires. After seeing the notification, open the relevant workspace in Atlas and paste the suggested prompt into a visible Claude tab to start planning or reviewing.

### launchd runtime

The listener runs as a local launchd job -- NOT a remote /schedule routine, NOT a Claude session. It runs every 20 minutes on weekdays (Mon-Fri) from 08:00 with a 16:00 (4pm) cutoff, local time.

Source-of-truth files:

- **Wrapper**: `~/.claude/skills/spinup/scripts/cmux-poller.sh` -- sets PATH for bare launchd environment, then `exec python3 ... poll`
- **Plist**: `~/.claude/skills/spinup/scripts/inc.example.cmux-work-listener.plist` -- source of truth; copy to `~/Library/LaunchAgents/` to install

To go live (install and load):

```bash
cp ~/.claude/skills/spinup/scripts/inc.example.cmux-work-listener.plist \
   ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/inc.example.cmux-work-listener.plist
```

To unload:

```bash
launchctl unload ~/Library/LaunchAgents/inc.example.cmux-work-listener.plist
```

Log output (stdout + stderr merged): `~/.claude/cache/cmux-work-listener.log`

## If the helper exits non-zero

Do not retry; surface the error to the user. Common failures:

- cmux not running or `automation.socketControlMode` not set to `"automation"` -- open cmux settings and set it, then restart cmux
- `atlas-cli` not on PATH (--atlas path only) -- symlink from `/Applications/atlas-workspaces.app/Contents/MacOS/atlas-cli` into `~/.local/bin`
- `acli` auth expired -- run `acli auth login`
- `gh` auth expired -- run `gh auth login`

## Requirements

- `acli` installed and authenticated
- `gh` (GitHub CLI) installed and authenticated
- `~/workspace/your-app` must be a valid git repo (the worktree source for the cmux default path)
- cmux running with `automation.socketControlMode` set to `"automation"` -- required for the cmux default path
- Atlas Workspaces app installed (`--atlas` path only; the CLI ships inside the bundle at `/Applications/atlas-workspaces.app/Contents/MacOS/atlas-cli`)
- `atlas-cli` on PATH (`--atlas` path only; symlink the bundled binary into `~/.local/bin`)

## State

- Jira spinup state: `~/.claude/cache/spinup-surfaced.json` (ticket key -> `first_surfaced`, `spunup_at`)
- PR decision state: `~/.claude/cache/pr-surfaced.json` (PR number -> `first_surfaced`, `decision`, `decided_at`, `author`, `title`, `url`)

Do not edit these files directly.

## cmux listener (Phase 2)

The cmux listener runs `cmux_chain.py cmux-poll` as a local launchd job on weekdays (Mon-Fri) from 08:00 to 16:00, every 20 minutes.

**What it does each poll cycle:**

- **Auto-spins newly-assigned Jira tickets** -- for every ticket in status `Selected for Work` or `Triage` that has not already been spun up, the listener runs the full cmux chain (git worktree, worktree-setup, dev-server tab, agent tab, browser tab). Only one `cmux notify` fires per cycle covering all new items.
- **Surfaces new PR review requests** -- PRs where you are a requested reviewer are surfaced via `cmux notify` (body lists the PR number and author). PRs are NOT auto-spun. Act with `cmux-spinup-pr <number>` after seeing the notification.

**Deduplification:** State is shared with the Atlas listener via `spinup_helper`: `~/.claude/cache/spinup-surfaced.json` (Jira) and `~/.claude/cache/pr-surfaced.json` (PRs). Items already seen by either listener are not re-notified.

**Prereqs:**

- cmux running with `automation.socketControlMode` set to `"automation"`.
- `acli` and `gh` authenticated.
- `~/workspace/your-app` is a valid git repo.

**Seed before going live (prevent first-poll burst):**

Run once to mark all currently-eligible tickets and open PRs as already handled, so the first real poll only surfaces genuinely new items:

```bash
python3 ~/.claude/skills/spinup/scripts/cmux_chain.py seed-backlog
```

**Source-of-truth files:**

- **Wrapper**: `~/.claude/skills/spinup/scripts/cmux-poller.sh` -- sets a bare-env-safe PATH, then `exec python3 ... cmux-poll`
- **Plist**: `~/.claude/skills/spinup/scripts/inc.example.cmux-work-listener.plist` -- source of truth; copy to `~/Library/LaunchAgents/` to install

**Go live (human in loop):**

```bash
# 1. Seed the backlog
python3 ~/.claude/skills/spinup/scripts/cmux_chain.py seed-backlog

# 2. Dry-run once (expect empty jira_spun_up and prs_to_surface)
python3 ~/.claude/skills/spinup/scripts/cmux_chain.py cmux-poll

# 3. Install and load
cp ~/.claude/skills/spinup/scripts/inc.example.cmux-work-listener.plist \
   ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/inc.example.cmux-work-listener.plist
```

To unload:

```bash
launchctl unload ~/Library/LaunchAgents/inc.example.cmux-work-listener.plist
```

**Log output** (stdout + stderr merged): `~/.claude/cache/cmux-work-listener.log`

**Note:** Confirm the Atlas listener plist is NOT already loaded before going live -- running both listeners simultaneously would double-spin tickets.

## Spec

Original design documents live in `~/.claude/specs/` alongside this skill's development history.
