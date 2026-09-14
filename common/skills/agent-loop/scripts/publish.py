"""Publish stage: commit accepted work, push the loop-owned branch, ensure a DRAFT PR.

The runner is the only thing that touches git history or GitHub; workers never do. Every
GitHub verb goes through `github_write()`, which allowlists exactly what the design permits
the loop to do unattended: push its own branch, open a draft PR, later edit/mark-ready/
comment (Plan 3 gates). Merge, review-assignment, and force-push are NOT in the allowlist and
are rejected by name, not by convention.

Draft PR creation is the standing exception to the external-comms gate (AGENTS.md); the
`AGENT_PR_SKILL=1` envelope is what the project's PreToolUse hook expects from the
create-pull-request skill, and the body follows that skill's template.

All git/gh commands run under a login shell so mise activates the repo's Ruby: lefthook's
pre-commit rubocop/reek fail under the tool shell's default Ruby (observed 2026-09-12).
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

REPO = "retailzipline/zipline-app"
JIRA_BASE = "https://zipline.atlassian.net/browse"
FOOTER = "— opened by Cody's AI agent loop on his behalf"

# verb -> allowed. Anything not listed is refused. `merge`, `review`(assign), `--force` never.
GITHUB_ALLOW = {
    "push": ["git", "push", "-u", "origin", "HEAD"],
    "pr-create-draft": None,     # built per call; --draft is enforced below
    "pr-view": None,
    "pr-edit-body": None,
    "pr-ready": None,            # gated by config in Plan 3; present so the allowlist is one place
    "pr-comment": None,
    "checks": None,
}
FORBIDDEN_TOKENS = ("merge", "--force", "-f", "--force-with-lease", "review", "--reviewer", "delete")


class PublishError(RuntimeError):
    pass


def _sh(cmd: list[str], cwd, *, env=None, timeout=600) -> subprocess.CompletedProcess:
    """Run under `bash -lc` so mise/rbenv shims resolve the repo's Ruby for git hooks."""
    e = dict(os.environ); e.update(env or {})
    e.setdefault("GIT_TERMINAL_PROMPT", "0"); e.setdefault("GH_PROMPT_DISABLED", "1")
    joined = " ".join(shlex.quote(c) for c in cmd)
    return subprocess.run(["bash", "-lc", joined], cwd=str(cwd), env=e, capture_output=True, text=True, timeout=timeout)


def github_write(verb: str, argv: list[str], cwd) -> subprocess.CompletedProcess:
    """The single choke point for anything that mutates GitHub or the remote."""
    if verb not in GITHUB_ALLOW:
        raise PublishError(f"github_write: verb {verb!r} is not in the allowlist")
    for tok in argv:
        low = tok.lower()
        if low in FORBIDDEN_TOKENS or low.startswith("--reviewer"):
            raise PublishError(f"github_write: token {tok!r} is forbidden for the loop ({verb})")
    if verb == "pr-create-draft" and "--draft" not in argv:
        raise PublishError("github_write: pr-create-draft without --draft")
    env = {"AGENT_PR_SKILL": "1"} if verb.startswith("pr-") else None
    r = _sh(argv, cwd, env=env)
    if r.returncode:
        raise PublishError(f"{verb} failed rc={r.returncode}: {(r.stderr or r.stdout).strip()[:800]}")
    return r


# ----------------------------------------------------------------------------- git side

def current_branch(wt) -> str:
    r = _sh(["git", "branch", "--show-current"], wt)
    return r.stdout.strip()


def guard_branch(wt, ticket_key: str) -> str:
    """Refuse to publish from anything but the loop's own ticket branch."""
    b = current_branch(wt)
    if not b or b in ("main", "master") or b.startswith("agent-"):
        raise PublishError(f"refusing to publish from branch {b!r}")
    if ticket_key.lower() not in b.lower():
        raise PublishError(f"branch {b!r} does not belong to {ticket_key}")
    return b


def commit_paths(wt, paths: list[str], message: str) -> str | None:
    """Stage exactly `paths` and commit. Returns the new sha, or None if nothing to commit."""
    if not paths:
        return None
    r = _sh(["git", "add", "--", *paths], wt)
    if r.returncode:
        raise PublishError(f"git add failed: {r.stderr.strip()[:400]}")
    staged = _sh(["git", "diff", "--cached", "--quiet"], wt)
    if staged.returncode == 0:
        return None
    r = _sh(["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", message], wt, timeout=900)
    if r.returncode:
        raise PublishError(f"git commit failed (hooks?) rc={r.returncode}: {(r.stderr or r.stdout).strip()[-1200:]}")
    return _sh(["git", "rev-parse", "HEAD"], wt).stdout.strip()


def ahead_of_remote(wt, branch: str) -> int | None:
    """Commits ahead of origin/<branch>, or None when the branch has no remote yet."""
    r = _sh(["git", "rev-parse", "--verify", "-q", f"origin/{branch}"], wt)
    if r.returncode:
        return None
    return int(_sh(["git", "rev-list", "--count", f"origin/{branch}..HEAD"], wt).stdout.strip() or 0)


def push(wt, branch: str) -> bool:
    """Push if there is anything to push. Never force."""
    ahead = ahead_of_remote(wt, branch)
    if ahead == 0:
        return False
    github_write("push", GITHUB_ALLOW["push"], wt)
    return True


# ----------------------------------------------------------------------------- PR side

def existing_pr(wt, branch: str) -> dict | None:
    r = _sh(["gh", "pr", "list", "--repo", REPO, "--head", branch, "--state", "open",
             "--json", "number,url,isDraft,title"], wt)
    if r.returncode:
        raise PublishError(f"gh pr list failed: {r.stderr.strip()[:400]}")
    import json
    items = json.loads(r.stdout or "[]")
    return items[0] if items else None


def pr_title(ticket_key: str, summary: str) -> str:
    """`CATEGORY: Short description ZIP-XXXX` per the create-pull-request skill."""
    s = re.sub(r"\s+", " ", summary).strip().rstrip(".")
    return f"INTERNAL: {s} {ticket_key}"


def pr_body(ticket_key: str, ticket_summary: str, why: str, review_start: list[str],
            qa: list[str], ai: dict) -> str:
    """Populate the repo's PULL_REQUEST_TEMPLATE.md structure. Screenshots section is kept
    (Well touches views); the QA gate fills it later via `pr-edit-body`."""
    rs = "\n".join(f"{i}. `{p}`" for i, p in enumerate(review_start, 1)) or "_Small enough to read top to bottom._"
    qa_lines = "\n".join(f"- [x] {q}" for q in qa)
    tokens = ai.get("tokens", "— (not yet measured by the loop; ledger lands in Plan 2)")
    return f"""🎁 Summary
---

{why}

🖼 Screenshots
---

_Pending: the loop's QA gate adds before/after captures before this PR leaves draft._

☎️ Related links and discussions
---

- [{ticket_key}]({JIRA_BASE}/{ticket_key}) — {ticket_summary}
- Epic: [ZIP-6774]({JIRA_BASE}/ZIP-6774) — ZUI design-system containers

✋ Deployment Dependencies
---

None. Additive component; no migrations, no flags, no translations.

🗺 Where should a review start?
---

{rs}

📝 Documentation
---

Lookbook preview for the component is part of this ticket and lands in a later commit on this branch.

✅ Quality Assurances
---

{qa_lines}
- [ ] Lookbook preview renders (pending)
- [ ] Figma parity check (pending)

<details>
<summary><h2>🤖 AI Conversation Summary</h2></summary>

### Intent
{ai.get("intent", "")}

### Key Decisions
{ai.get("decisions", "")}

### Discovery
{ai.get("discovery", "")}

### Problems Encountered
{ai.get("problems", "None.")}

### Token Usage & Costs
| Metric | Value |
|--------|-------|
| Total tokens | {tokens} |
| Tool uses | {ai.get("tool_uses", "—")} |
| Duration | {ai.get("duration", "—")} |
| Estimated cost | {ai.get("cost", "—")} |

</details>

{FOOTER}
"""


def ensure_draft_pr(wt, branch: str, title: str, body: str, base: str = "main") -> dict:
    """Idempotent: returns the open PR for `branch`, creating a DRAFT one if none exists."""
    pr = existing_pr(wt, branch)
    if pr:
        return pr
    argv = ["gh", "pr", "create", "--repo", REPO, "--draft", "--base", base, "--head", branch,
            "--title", title, "--body", body]
    r = github_write("pr-create-draft", argv, wt)
    url = r.stdout.strip().splitlines()[-1]
    pr = existing_pr(wt, branch)
    if not pr:
        raise PublishError(f"PR created ({url}) but not found by branch afterwards")
    return pr


def record_pr_on_worktree(wt, number: int) -> None:
    """`bin/wt update --pr N` is how the worktree tooling learns its PR; best-effort."""
    if (Path(wt) / "bin" / "wt").exists():
        _sh(["bin/wt", "update", "--pr", str(number)], wt, timeout=120)
