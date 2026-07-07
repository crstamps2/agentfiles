---
name: standup
description: Status dashboard across Jira tickets, GitHub PRs, and Atlas workspaces. Use when the user says "standup", "status", or "check in".
user_invocable: true
---

# Standup / Status Dashboard

Give the user a consolidated view of their work by joining data from Jira, GitHub, and Atlas workspaces.

## Steps

### 0. Pre-resolve identifiers

Before dispatching agents, resolve these once (run in parallel):

```bash
gh repo view --json nameWithOwner --jq .nameWithOwner
```
```bash
gh api user --jq .login
```

Pass both values (e.g., `your-org/your-app` and `your-github-username`) to all agents explicitly -- agents should never resolve these themselves.

### 1. Gather data (2 agents in parallel)

Dispatch two `general-purpose` agents in parallel (`model: "haiku"`). Each gathers related data sources and returns structured results.

**Agent 1 -- Jira + Atlas workspaces:**

> 1. **Jira tickets:** Run `acli jira workitem search --jql 'assignee = currentUser() AND status in ("To Do", "Ready for Dev", "In Progress", "In Review") ORDER BY priority ASC, status DESC'`
>    Parse the table output into: key, type, priority, status, summary (one line per ticket).
>    If acli fails (auth expired), return the error and move on.
>
> 2. **Atlas workspaces:** For each workspace in `~/.atlas/workspaces/<your-app>/*/`:
>    - Get workspace name: `basename "$ws"`
>    - Get branch: `git -C "$ws" branch --show-current`
>    - Check for a PR on that branch: `gh pr list --state all --head "{branch}" --repo {repo} --json number,state,title,url --limit 1`
>    Return per workspace: name, branch, PR number/state (if any).

**Agent 2 -- GitHub PRs (mine + review requests):**

> 1. **My open PRs:** Run `gh pr list --author @me --state open --repo {repo} --json number,title,url,headRefName,reviewDecision,statusCheckRollup,reviews,isDraft`
>    For each PR: number, title, branch, draft?, CI status, review decision, reviewer names.
>
> 2. **Recently merged PRs** (to detect stale workspaces): `gh pr list --author @me --state merged --repo {repo} --json number,headRefName,mergedAt --limit 10`
>
> 3. **PRs requesting my review** -- run both queries and deduplicate by PR number:
>    - `gh pr list --search "user-review-requested:{username}" --state open --repo {repo} --json number,title,url,author,createdAt,updatedAt`
>    - `gh pr list --search "team-review-requested:your-org/your-team" --state open --repo {repo} --json number,title,url,author,createdAt,updatedAt`
>    Do NOT use `review-requested:@me` -- the `@me` shorthand silently returns empty results.
>    If 0 results after dedup, return a warning: "0 review requests found -- verify query resolved correctly"

### 2. Associate tickets, PRs, and workspaces

Once both agents return, build a unified map by extracting the ticket key from PR branch names and Atlas workspace branches (e.g., `bugfix/PROJ-3184-publisher-comments` -> `PROJ-3184`).

Join into: `PROJ-XXXX` -> `{jira_ticket, pr, workspace_name}`

Handle edge cases:
- PR with no matching ticket -> "Orphan PRs" group
- Ticket with no PR and no workspace -> "New / Unstarted" group
- Workspace with a merged PR -> "Stale Workspaces" group

### 3. Enrich PRs with review comments (conditional)

Only run this step if any PR has `reviewDecision` of `CHANGES_REQUESTED` or has reviews present. If all PRs are draft/approved/no-reviews, skip to step 4.

Spawn a single `general-purpose` agent (`model: "haiku"`) to fetch and analyze comments for reviewed PRs:

> For each PR number in this list: {pr_numbers}
>
> 1. **Fetch review threads:**
>    ```bash
>    bash ~/.claude/scripts/gh-pr-comments.sh {owner}/{repo} {number}
>    ```
>
> 2. **Filter threads:**
>    - Skip threads where `resolved: true` (reviewer marked resolved)
>    - Skip threads where `outdated: true` (code has changed since comment)
>
> 3. **Cross-reference remaining threads against commits** to catch addressed feedback:
>    ```bash
>    gh api repos/{owner}/{repo}/pulls/{number}/commits --paginate --jq '.[] | "\(.commit.committer.date) \(.commit.message | split("\n")[0])"'
>    ```
>    For each unresolved/not-outdated thread, check if any commit after `started_at` plausibly addresses the feedback. If so, mark as "likely addressed" and skip.
>
> 4. **Categorize remaining threads:**
>    - **Needs action**: reviewer asked a question or requested a change
>    - **FYI only**: positive comment (LGTM, nice) or no action requested
>
> 5. **Return per PR:** number, count of resolved/outdated (skipped), count of likely-addressed (skipped), count of actionable, and for each actionable thread: reviewer name, file path, one-line gist. Flag comments from the last 24 hours as "new".

### 4. Present

Format the output in these groups. Only show a section if it has data.

#### Active Work

Tickets with a PR (and optionally an Atlas workspace).

| Ticket | PR | Workspace | CI | Review | Comments |
|--------|----|-----------|----|--------|----------|

- Link tickets to your Jira instance: `[PROJ-XXXX](https://your-jira-instance/browse/PROJ-XXXX)`
- Link PRs: `[#NNNNN](pr_url)`
- Show workspace name if one exists
- CI: passing/failing/pending
- Review: approved/changes requested/pending
- Comments: count of actionable items, or "none"

#### Needs PR

Tickets In Progress with an Atlas workspace but no PR yet.

| Ticket | Priority | Workspace | Summary |
|--------|----------|-----------|---------|

#### New / Unstarted

Tickets in To Do or Ready for Dev with no workspace and no PR.

| Ticket | Priority | Summary |
|--------|----------|---------|

#### PRs Awaiting Your Review

PRs from other authors where your review is requested.

| PR | Author | Title | Stale | Age |
|----|--------|-------|-------|-----|

- **Stale** = time since `updatedAt`. Bold if 1+ day.
- **Age** = time since `createdAt`. Bold if 7+ days.

#### Stale Workspaces

Atlas workspaces whose branch's PR has been merged. Cleanup candidates.

| Workspace | Branch | PR | Note |
|-----------|--------|----|------|

#### Orphan PRs

Open PRs with no matching Jira ticket (no ticket key pattern in branch name).

| PR | Branch | Title |
|----|--------|-------|

### 5. Flag items needing attention

Call out anything requiring action:

- Failing CI on any PR
- Requested changes on PRs
- Unaddressed review comments (with reviewer name and gist)
- New unstarted tickets (especially P1/P2)

### 6. Suggest next actions

End with 2-4 concrete suggestions, e.g.:

- "PR #NNNNN has failing CI -- fix before it can merge"
- "PROJ-4001 is an unstarted P1 -- start work in Atlas"
- "3 PRs awaiting your review (oldest: 5 days)"
- "workspace-name has a merged PR -- clean up"

## Notes

- All data gathering happens in agents -- the orchestrator only synthesizes and presents
- If acli auth has expired, note the failure and continue with GitHub data only
- If "PRs Awaiting Your Review" returns 0, surface the warning in the dashboard
- Never chain bash commands with `&&` in agent prompts -- instruct agents to run each as a separate call
- Never use `2>&1` redirects on `gh` commands
