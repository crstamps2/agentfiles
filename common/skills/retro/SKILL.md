---
name: retro
description: Run a session retrospective — reviews what was accomplished, what went well, what didn't, and recommends improvements to skills, memory, and CLAUDE.md.
user_invocable: true
---

# Session Retrospective

Review the current session to capture learnings and improve future sessions.

## Steps

### 1. Gather session context

Review the conversation history for:

- Tasks requested and completed
- PRs created, reviewed, or updated
- Tools/skills used
- Agents dispatched (and what they did)
- Where context was lost (compaction, continuation)

Also check:
- Atlas workspaces: `for ws in ~/.atlas/workspaces/your-app/*/; do echo "$(basename "$ws"): $(git -C "$ws" log --oneline -3 2>/dev/null)"; done`
- Any background tasks that were running

### 2. Categorize outcomes

Present a table of work done:

| Area | What was done | PR | Status |
|------|---------------|----|--------|

Include worktree/branch and ticket references where applicable.

### 3. What went well

Identify 3-5 things that worked effectively — efficient tool usage, good parallelism, skills that saved time, clean investigation cycles.

### 4. What didn't go well

Identify 3-5 friction points — context exhaustion, wrong first attempts, tool limitations, wasted effort. Be honest; the goal is to improve, not to look good.

### 5. Recommendations

For each friction point, propose a concrete fix. Categorize as:

- **Memory** — patterns to save to `~/.claude/projects/*/memory/`
- **CLAUDE.md** — new rules or conventions (global, project, or subdirectory level)
- **Skills** — new skills to create or improvements to existing ones
- **Agents** — role definitions or prompts to adjust
- **Workflow** — better approaches for common tasks

### 6. Apply approved changes

Ask the user which recommendations to apply. Then make the changes.

**Where changes land:**
- **Memory**: `~/.claude/projects/<project-slug>/memory/` (update MEMORY.md index too)
- **Skills**: `~/.claude/skills/{name}/SKILL.md`
- **Agents**: `~/.claude/agents/{role}.md`
- **CLAUDE.md**: at the appropriate level (global `~/.claude/CLAUDE.md`, project, or subdirectory)

After applying, suggest running `/audit` to sync changes to agentfiles and verify coherence across all artifacts. The retro captures learnings; the audit propagates them.

## Notes

- Keep the retro scannable — tables and bullet points, not walls of text
- Focus on actionable recommendations, not vague observations
- If the session was continued from a compacted conversation, note what was lost
- Some sessions are coordination-only (standup, ad-hoc questions) — a short retro is fine
