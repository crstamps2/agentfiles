#!/usr/bin/env python3
"""Helper for the /spinup skill. CLI entry points: derive-branch, list-pending,
spinup, mark-surfaced, spinup-pr, poll.
Adapt the constants below (GH_REPO, ATLAS_BARE_REPO, ATLAS_REPO_ID) to your project."""

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

CACHE_PATH = Path.home() / ".claude" / "cache" / "spinup-surfaced.json"
PR_CACHE_PATH = Path.home() / ".claude" / "cache" / "pr-surfaced.json"
ELIGIBLE_STATUSES = {"Selected for Work", "Triage"}
TYPE_PREFIX = {"Story": "feature", "Bug": "bugfix"}
DEFAULT_PREFIX = "internal"
# UUID of the atlas-managed repo (the bare repo at ~/.atlas/repos/your-app).
# Pinned by id when the registry has multiple rows with the same name.
# Run `atlas-cli --json workspaces list` to find your repo's id.
ATLAS_REPO_ID = "<atlas-repo-id>"                         # TODO: set to your Atlas repo UUID
ATLAS_CLI = "atlas-cli"
GH_REPO = "your-org/your-app"                                     # TODO: set to your GitHub repo
ATLAS_BARE_REPO = Path.home() / ".atlas" / "repos" / "your-app"  # TODO: set to your Atlas bare repo


def _slugify(title: str, max_len: int = 40) -> str:
    """Lowercase, strip apostrophes, replace non-alphanum with dashes, collapse, trim.

    If the max_len cap lands mid-word (i.e., the next char would also be a word char),
    trim back to the last dash so we don't emit half-words like 'show-o'."""
    lowered = title.lower().replace("'", "")
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    if len(slug) <= max_len:
        return slug.rstrip("-")
    truncated = slug[:max_len]
    next_char = slug[max_len] if max_len < len(slug) else ""
    # If we cut mid-word (the next char would still be word-content), back off to last dash.
    if next_char and next_char != "-":
        last_dash = truncated.rfind("-")
        if last_dash > 0:
            truncated = truncated[:last_dash]
    return truncated.rstrip("-")


def derive_branch(ticket_key: str, issue_type: str, title: str) -> str:
    """Build the branch name: <prefix>/<lowered-key>[-<slug>]."""
    prefix = TYPE_PREFIX.get(issue_type, DEFAULT_PREFIX)
    key_part = ticket_key.lower()
    slug = _slugify(title)
    if slug:
        return f"{prefix}/{key_part}-{slug}"
    return f"{prefix}/{key_part}"


def jira_work_prompt(ticket_key: str) -> str:
    """The prompt fired into the Jira work tab. admin.<workspace>.test is the dev URL."""
    workspace = ticket_key.lower()
    return (
        f"writing-plans for {ticket_key}, make sure to test any assumptions or "
        f"reproduce the issue and take screenshots on admin.{workspace}.test"
    )


def pr_work_prompt(pr_number: int) -> str:
    """The prompt fired into the PR review work tab."""
    return f"lens-review for PR #{pr_number}, QA on admin.pr-{pr_number}.test"


def _now_iso() -> str:
    """UTC timestamp in ISO-8601 with 'Z'."""
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_state() -> dict:
    """Read cache file. Missing or corrupted -> empty dict."""
    try:
        with open(CACHE_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    """Persist state to cache file. Creates parent dir if needed."""
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def mark_surfaced(ticket_key: str) -> None:
    """Record first-seen timestamp; idempotent (won't overwrite existing)."""
    state = load_state()
    if ticket_key not in state:
        state[ticket_key] = {"first_surfaced": _now_iso(), "spunup_at": None}
        save_state(state)


def mark_spunup(ticket_key: str) -> None:
    """Record spunup-at timestamp. Creates entry if missing."""
    state = load_state()
    entry = state.setdefault(
        ticket_key, {"first_surfaced": _now_iso(), "spunup_at": None}
    )
    entry["spunup_at"] = _now_iso()
    save_state(state)


def pending_keys() -> set:
    """Tickets surfaced but not yet spunup."""
    return {k for k, v in load_state().items() if v.get("spunup_at") is None}


class JiraError(RuntimeError):
    pass


def fetch_ticket(ticket_key: str) -> dict:
    """Return {'key', 'title', 'issue_type', 'status'} for a Jira ticket via acli.

    Raises JiraError on failure."""
    result = subprocess.run(
        ["acli", "jira", "workitem", "view", ticket_key, "--json"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise JiraError(f"acli failed for {ticket_key}: {result.stderr.strip()}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise JiraError(f"acli output not JSON for {ticket_key}: {e}")
    fields = data.get("fields", {}) or {}
    return {
        "key": data.get("key", ticket_key),
        "title": fields.get("summary", ""),
        "issue_type": (fields.get("issuetype") or {}).get("name", ""),
        "status": (fields.get("status") or {}).get("name", ""),
    }


def list_assigned_eligible() -> list:
    """Return tickets currently assigned to the user with eligible status.

    Uses JQL: assignee = currentUser() AND status in ('Selected for Work', 'Triage').
    acli's --json on workitem search returns a flat array, not {issues: [...]}."""
    jql = (
        "assignee = currentUser() AND "
        "status in ('Selected for Work', 'Triage')"
    )
    result = subprocess.run(
        ["acli", "jira", "workitem", "search", "--jql", jql, "--limit", "50", "--json"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise JiraError(f"acli search failed: {result.stderr.strip()}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise JiraError(f"acli search output not JSON: {e}")
    # acli returns a plain array. Defensive fallback if the response is wrapped.
    issues = data if isinstance(data, list) else data.get("issues", [])
    out = []
    for i in issues:
        fields = i.get("fields", {}) or {}
        out.append({
            "key": i.get("key"),
            "title": fields.get("summary", ""),
            "issue_type": (fields.get("issuetype") or {}).get("name", ""),
            "status": (fields.get("status") or {}).get("name", ""),
        })
    return out


def transition_to_in_progress(ticket_key: str, current_status: str) -> bool:
    """Transition ticket to 'In Progress'. Idempotent if already there.

    Returns True if a transition was performed, False if skipped."""
    if current_status == "In Progress":
        return False
    result = subprocess.run(
        [
            "acli", "jira", "workitem", "transition",
            "--key", ticket_key,
            "--status", "In Progress",
            "--yes",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise JiraError(f"transition failed for {ticket_key}: {result.stderr.strip()}")
    return True


class AtlasError(RuntimeError):
    pass


def _atlas_json(args: list) -> dict:
    """Run `atlas-cli --json <args>` and parse stdout. Raises AtlasError on failure."""
    try:
        result = subprocess.run([ATLAS_CLI, "--json", *args], capture_output=True, text=True)
    except FileNotFoundError:
        raise AtlasError(
            f"{ATLAS_CLI} not found on PATH. Symlink "
            "/Applications/atlas-workspaces.app/Contents/MacOS/atlas-cli into ~/.local/bin."
        )
    if result.returncode != 0:
        raise AtlasError(f"atlas-cli {' '.join(args)} failed: {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise AtlasError(f"atlas-cli output not JSON: {e}")


def term_new(workspace: str) -> str:
    """Create a visible PTY terminal tab in the workspace; return its terminal id."""
    return _atlas_json(["term", "new", workspace])["id"]


def term_exec(terminal_id: str, command: str) -> str:
    """Inject a command into a running PTY and BLOCK until it completes (sentinel-based).
    Use for setup so dependencies are installed before later steps run."""
    result = subprocess.run(
        [ATLAS_CLI, "term", "exec", terminal_id, command], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise AtlasError(f"term exec failed: {result.stderr.strip()}")
    return result.stdout


def term_send(terminal_id: str, data: str) -> None:
    """Write keystrokes to a terminal (caller controls newlines). Use for long-running
    processes like the dev server where we don't want to block."""
    result = subprocess.run(
        [ATLAS_CLI, "term", "send", terminal_id, data], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise AtlasError(f"term send failed: {result.stderr.strip()}")


def claude_new(workspace: str, prompt: str) -> str:
    """Spawn a visible, attended-capable Claude session tab with an initial prompt.
    Returns the session id."""
    return _atlas_json(["claude", "new", workspace, "--prompt", prompt])["id"]


def _workspace_name_from_key(ticket_key: str) -> str:
    """`PROJ-1234` -> `proj-1234`. Matches the Atlas naming convention."""
    return ticket_key.lower()


def create_atlas_workspace_named(branch: str, name: str, repo_id: str = ATLAS_REPO_ID) -> dict:
    """Create an Atlas workspace with an explicit name on the given branch (idempotent)."""
    return _atlas_json(["workspaces", "new", "--repo", repo_id, "--branch", branch, "--use-existing", name])


def create_atlas_workspace(branch: str, ticket_key: str, repo_id: str = ATLAS_REPO_ID) -> dict:
    """Back-compat: derive name from the ticket key, delegate to the named creator."""
    return create_atlas_workspace_named(branch, _workspace_name_from_key(ticket_key), repo_id)


def run_spinup_chain(workspace_name: str, branch: str) -> dict:
    """Create the workspace, open it, then run the setup tab (blocking).
    puma-dev lazy-boots admin.<workspace>.test on demand; no dev-server tab is needed.
    Returns a summary dict. The caller provides a suggested_prompt for the user to run
    manually once they open the workspace in Atlas."""
    ws = create_atlas_workspace_named(branch, workspace_name)
    open_atlas_workspace(ws["id"])

    setup_tid = term_new(workspace_name)
    term_exec(setup_tid, "bin/conductor-setup")  # blocks until bundle/yarn/etc. finish

    return {
        "workspace_id": ws["id"],
        "workspace_name": ws.get("name", workspace_name),
        "worktree_path": ws.get("worktree_path"),
        "setup_terminal_id": setup_tid,
    }


def open_atlas_workspace(workspace_id: str) -> None:
    """Best-effort focus the workspace in the running Atlas app. Failure is non-fatal."""
    try:
        subprocess.run(
            [ATLAS_CLI, "workspaces", "open", workspace_id],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        pass


def cmd_derive_branch(args) -> int:
    print(derive_branch(args.ticket, args.issue_type, args.title))
    return 0


def cmd_list_pending(args) -> int:
    """Print eligible-and-not-yet-spunup tickets as JSON for /standup to consume."""
    try:
        eligible = list_assigned_eligible()
    except JiraError as e:
        print(f"jira-error: {e}", file=sys.stderr)
        return 2
    spunup = {k for k, v in load_state().items() if v.get("spunup_at") is not None}
    pending = [t for t in eligible if t["key"] not in spunup]
    # Mark each as surfaced (idempotent).
    for t in pending:
        mark_surfaced(t["key"])
    print(json.dumps(pending, indent=2))
    return 0


def cmd_spinup(args) -> int:
    try:
        ticket = fetch_ticket(args.ticket)
    except JiraError as e:
        print(f"could not fetch {args.ticket}: {e}", file=sys.stderr)
        return 2
    branch = derive_branch(ticket["key"], ticket["issue_type"], ticket["title"])
    try:
        transitioned = transition_to_in_progress(ticket["key"], ticket["status"])
    except JiraError as e:
        print(f"transition failed: {e}", file=sys.stderr)
        return 3
    try:
        chain = run_spinup_chain(_workspace_name_from_key(ticket["key"]), branch)
    except AtlasError as e:
        print(f"atlas spin-up chain failed: {e}", file=sys.stderr)
        return 4
    mark_spunup(ticket["key"])
    print(json.dumps({
        "ticket": ticket["key"],
        "branch": branch,
        "workspace_id": chain["workspace_id"],
        "workspace_name": chain["workspace_name"],
        "worktree_path": chain["worktree_path"],
        "suggested_prompt": jira_work_prompt(ticket["key"]),
        "transitioned": transitioned,
    }, indent=2))
    return 0


class GitHubError(RuntimeError):
    pass


def list_review_requests() -> list:
    """Open PRs in GH_REPO where the current user is a requested reviewer.
    NOTE: round-robin/team assignments appear here identically to direct
    requests -- the design surfaces all and lets the user decide per-PR."""
    result = subprocess.run(
        [
            "gh", "search", "prs", "--review-requested=@me", "--state=open",
            "--repo", GH_REPO, "--json", "number,title,author,url,createdAt", "--limit", "50",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GitHubError(f"gh search failed: {result.stderr.strip()}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise GitHubError(f"gh output not JSON: {e}")
    out = []
    for pr in (data if isinstance(data, list) else []):
        out.append({
            "number": pr.get("number"),
            "title": pr.get("title", ""),
            "author": (pr.get("author") or {}).get("login", ""),
            "url": pr.get("url", ""),
            "created_at": pr.get("createdAt", ""),
        })
    return out


def resolve_pr_head_branch(pr_number: int) -> str:
    """The PR's head branch name -- used as the Atlas workspace branch."""
    result = subprocess.run(
        ["gh", "pr", "view", str(pr_number), "--repo", GH_REPO, "--json", "headRefName"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GitHubError(f"gh pr view {pr_number} failed: {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)["headRefName"]
    except (json.JSONDecodeError, KeyError) as e:
        raise GitHubError(f"gh pr view {pr_number} output unparseable: {e}")


def fetch_branch(branch: str) -> None:
    """Best-effort fetch of a branch into the atlas-managed bare repo so
    `workspaces new --use-existing` can resolve it. Non-fatal on failure --
    workspaces new will surface a clear error if the branch is truly absent."""
    subprocess.run(
        ["git", "-C", str(ATLAS_BARE_REPO), "fetch", "origin", branch],
        capture_output=True,
        text=True,
    )


def load_pr_state() -> dict:
    try:
        with open(PR_CACHE_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_pr_state(state: dict) -> None:
    PR_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PR_CACHE_PATH, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def mark_pr_surfaced(pr_number: int, meta: dict) -> None:
    """Record first-seen + metadata; idempotent (won't overwrite an existing entry)."""
    state = load_pr_state()
    key = str(pr_number)
    if key not in state:
        state[key] = {
            "first_surfaced": _now_iso(),
            "decision": "pending",
            "decided_at": None,
            "author": meta.get("author", ""),
            "title": meta.get("title", ""),
            "url": meta.get("url", ""),
        }
        save_pr_state(state)


def surfaced_pr_numbers() -> set:
    return {int(k) for k in load_pr_state().keys()}


def record_pr_decision(pr_number: int, decision: str) -> None:
    """decision in {'review','skip'}. Feeds the corpus for a future auto-classifier."""
    state = load_pr_state()
    key = str(pr_number)
    entry = state.setdefault(key, {"first_surfaced": _now_iso(), "author": "", "title": "", "url": ""})
    entry["decision"] = decision
    entry["decided_at"] = _now_iso()
    save_pr_state(state)


def cmd_spinup_pr(args) -> int:
    number = args.number
    if getattr(args, "skip", False):
        record_pr_decision(number, "skip")
        print(json.dumps({"pr": number, "decision": "skip"}, indent=2))
        return 0
    try:
        branch = resolve_pr_head_branch(number)
    except GitHubError as e:
        print(f"could not resolve PR #{number}: {e}", file=sys.stderr)
        return 2
    fetch_branch(branch)
    name = f"pr-{number}"
    try:
        chain = run_spinup_chain(name, branch)
    except AtlasError as e:
        print(f"atlas spin-up chain failed: {e}", file=sys.stderr)
        return 4
    record_pr_decision(number, "review")
    print(json.dumps({
        "pr": number,
        "decision": "review",
        "branch": branch,
        "workspace_id": chain["workspace_id"],
        "workspace_name": chain["workspace_name"],
        "worktree_path": chain["worktree_path"],
        "suggested_prompt": pr_work_prompt(number),
    }, indent=2))
    return 0


def notify_macos(title: str, message: str) -> None:
    """Fire a native macOS notification. Best-effort: never raises.

    Prefers terminal-notifier (works from launchd/CLI contexts where osascript
    notifications are silently dropped). Falls back to osascript when unavailable."""
    try:
        if shutil.which("terminal-notifier"):
            subprocess.run(
                ["terminal-notifier", "-title", title, "-message", message, "-sound", "default"],
                capture_output=True,
                text=True,
            )
        else:
            script = f'display notification {json.dumps(message)} with title {json.dumps(title)}'
            subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    except Exception:
        pass


def cmd_poll(args) -> int:
    jira_spun_up = []
    try:
        eligible = list_assigned_eligible()
    except JiraError as e:
        print(f"jira-error: {e}", file=sys.stderr)
        eligible = []
    already = {k for k, v in load_state().items() if v.get("spunup_at") is not None}
    for t in eligible:
        if t["key"] in already:
            continue
        branch = derive_branch(t["key"], t["issue_type"], t["title"])
        try:
            transition_to_in_progress(t["key"], t["status"])
            run_spinup_chain(_workspace_name_from_key(t["key"]), branch)
        except (JiraError, AtlasError) as e:
            # Intentionally do NOT mark_spunup on failure: the ticket re-spins next
            # cycle. This is self-healing for transient failures -- create_atlas_workspace
            # uses --use-existing, so re-running the chain is idempotent.
            print(f"jira-spinup-failed {t['key']}: {e}", file=sys.stderr)
            continue
        mark_spunup(t["key"])
        jira_spun_up.append({
            "key": t["key"],
            "branch": branch,
            "workspace": _workspace_name_from_key(t["key"]),
        })

    prs_to_surface = []
    try:
        prs = list_review_requests()
    except GitHubError as e:
        print(f"gh-error: {e}", file=sys.stderr)
        prs = []
    surfaced = surfaced_pr_numbers()
    for pr in prs:
        if pr["number"] in surfaced:
            continue
        mark_pr_surfaced(pr["number"], pr)
        prs_to_surface.append(pr)

    if jira_spun_up or prs_to_surface:
        j = len(jira_spun_up)
        p = len(prs_to_surface)
        if j and p:
            title = f"Atlas: {j} ready, {p} PR{'s' if p != 1 else ''}"
        elif j:
            title = f"Atlas: {j} ready"
        else:
            title = f"Atlas: {p} PR review{'s' if p != 1 else ''}"
        lines = [f"✓ {item['key']} ready to plan" for item in jira_spun_up]
        lines += [f"PR #{pr['number']} ({pr['author']}) -- /spinup #{pr['number']}" for pr in prs_to_surface]
        notify_macos(title, "\n".join(lines))

    print(json.dumps({"jira_spun_up": jira_spun_up, "prs_to_surface": prs_to_surface}, indent=2))
    return 0


def cmd_mark_surfaced(args) -> int:
    mark_surfaced(args.ticket)
    return 0


def main():
    parser = argparse.ArgumentParser(prog="spinup_helper")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list-pending")
    p_spin = sub.add_parser("spinup")
    p_spin.add_argument("ticket")
    p_db = sub.add_parser("derive-branch")
    p_db.add_argument("ticket")
    p_db.add_argument("issue_type")
    p_db.add_argument("title")
    p_ms = sub.add_parser("mark-surfaced")
    p_ms.add_argument("ticket")
    p_pr = sub.add_parser("spinup-pr")
    p_pr.add_argument("number", type=int)
    p_pr.add_argument("--skip", action="store_true")
    sub.add_parser("poll")
    args = parser.parse_args()
    handlers = {
        "derive-branch": cmd_derive_branch,
        "list-pending": cmd_list_pending,
        "spinup": cmd_spinup,
        "mark-surfaced": cmd_mark_surfaced,
        "spinup-pr": cmd_spinup_pr,
        "poll": cmd_poll,
    }
    sys.exit(handlers[args.cmd](args))


if __name__ == "__main__":
    main()
