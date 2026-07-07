---
name: audit
description: Audit all persistent artifacts (memory, AGENTS.md/CLAUDE.md, skills, agents, the agentfiles common/+tools/ source, claude-plugins, app-docs) for coherence and staleness. Verifies the derived ~/.claude and ~/.codex trees against the common/ source of truth, never the reverse. Auto-fixes safe issues, asks approval for substantive changes including PR-gated contributions back to shared marketplaces, and gates any public push behind the agentfiles-scan.sh scanner plus a security-analyst review. Run standalone or after /retro.
user_invocable: true
---

# Artifact Audit

Ensure all persistent artifacts across the development environment are coherent and up to date.

## Source of truth vs. derived trees

`~/workspace/agentfiles/common/` (plus the per-tool `tools/claude/` and `tools/codex/` overlays) is the **source of truth**. `~/.claude` and `~/.codex` are **DERIVED** -- they are materialized from `common/`+`tools/` by `bootstrap.sh`. Every layer below that compares "local" state against the repo must diff the rendered `~/.claude`/`~/.codex` trees **against** `common/` (and the relevant `tools/<tool>/` overlay), never the other way around. If a rendered tree has content the source lacks, that is drift to reconcile back into `common/`/`tools/`, not evidence that the source is wrong. Cover both tools -- don't assume `~/.claude`-only checks are sufficient; run the equivalent check against `~/.codex` wherever an artifact type exists for Codex CLI too.

## Arguments

- No args: audit all layers
- `<layer>`: audit a specific layer only (e.g., `/audit memory`, `/audit skills`)
- Valid layers: `memory`, `claude-md`, `skills`, `sync`, `plugins`, `app-docs`

## Tracking

Create a task for each layer to track progress.

## Phase 1: Scan (parallel)

Run layers 1-3 in parallel as background agents (`model: "haiku"`). They're read-only scans that don't depend on each other.

If a specific layer was requested, only run that one.

### Layer 1: Memory

**Agent prompt** (`subagent_type: "general-purpose"`, `model: "haiku"`):

> Audit the project's memory system.
>
> 1. Find the memory directory: `ls -d ~/.claude/projects/*/memory/ 2>/dev/null | head -1` (Claude Code stores it under the slugified git common dir path)
> 2. Read `MEMORY.md` index in that directory
> 3. For each referenced memory file:
>    - Verify the file exists (report broken index entries)
>    - Read the content
>    - If it references specific file paths, classes, or functions, spot-check 2-3 key ones via Glob/Grep against `$PWD` (the current working directory)
>    - Check the frontmatter `type` field matches the content
> 4. Check for orphan files (exist in directory but not referenced in MEMORY.md)
> 5. Check for duplicate or contradictory entries across memory files
>
> Return a structured report:
> ```
> broken_refs: [list of MEMORY.md lines pointing to nonexistent files]
> orphans: [files not in MEMORY.md]
> stale_refs: [memories referencing code that no longer exists, with details]
> duplicates: [pairs of memories covering the same topic]
> total_files: N
> healthy_files: N
> ```

### Layer 2: AGENTS.md / CLAUDE.md Files

**Agent prompt** (`subagent_type: "general-purpose"`, `model: "haiku"`):

> Audit the source-of-truth instructions file and its derived renders only. **Do not audit subdirectory-level or project-owner CLAUDE.md files** -- those are out of scope for this audit.
>
> 1. Read `~/workspace/agentfiles/common/instructions/AGENTS.md` (the shared source of truth) -- check for references to tools, paths, or conventions that may be stale.
> 2. Read the derived global renders -- `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` (or wherever the Codex CLI derived instructions land) -- and diff each against the `common/` source. Flag any derived content that isn't traceable back to `common/instructions/AGENTS.md` or a `tools/<tool>/` overlay; that's drift in the derived tree, not the source.
> 3. Determine the project root: `PROJECT=$(git -C "$PWD" rev-parse --show-toplevel 2>/dev/null || pwd)` -- works for both main workspace and atlas worktrees. Read `$PROJECT/CLAUDE.md` (project-level) -- check for stale references. Spot-check 2-3 file/class references via Glob/Grep.
>
> Return:
> ```
> stale_refs: [{file, reference, reason}]
> derived_drift: [{tool, file, reason}]
> total_files: N
> healthy_files: N
> ```

### Layer 3: Skills

**Agent prompt** (`subagent_type: "general-purpose"`, `model: "haiku"`):

> Audit skills at the source of truth, `~/workspace/agentfiles/common/skills/`, and cross-check the derived trees `~/.claude/skills/` and `~/.codex/skills/` (or the Codex CLI equivalent) against it.
>
> For each skill's SKILL.md in `common/skills/`:
> 1. Check for references to file paths -- verify they exist
> 2. Check for references to CLI tools/scripts -- verify they're available (e.g., `which <tool>`, `ls <path>`)
> 3. Note the line count -- flag if over 500 lines
> 4. Confirm the derived copy in `~/.claude/skills/<name>/` and `~/.codex/skills/<name>/` (where applicable) matches `common/skills/<name>/` -- any mismatch is derived-tree drift to reconcile back into `common/`, never edited in place on the derived side.
>
> Skip `peon-ping-*` skills (machine-specific audio alerts).
>
> Return:
> ```
> broken_refs: [{skill, reference, type}]
> oversized: [{skill, line_count}]
> derived_drift: [{tool, skill, reason}]
> total_skills: N
> healthy_skills: N
> ```

## Phase 2: Sync (sequential, orchestrator)

This layer diffs the DERIVED `~/.claude` and `~/.codex` trees against the `~/workspace/agentfiles/common/` (+`tools/`) source of truth. Run after Phase 1 completes (or standalone if `/audit sync` was requested). The direction of every diff below is derived-tree -> source; a derived-only file is sync-candidate or drift, never grounds for changing `common/` to match it without review.

### Layer 4: Agentfiles Sync

Check these artifact types between the derived trees and the agentfiles `common/` source:

**Agents:**
```bash
# List agents in the derived Claude Code tree and the common/ source
ls ~/.claude/agents/*.md | xargs -I{} basename {}
ls ~/workspace/agentfiles/common/agents/*.md 2>/dev/null | xargs -I{} basename {}
# Codex CLI derived tree, if agents render there too
ls ~/.codex/agents/*.md 2>/dev/null | xargs -I{} basename {}
```

For each agent file present in both a derived tree and `common/agents/`, diff them. Categorize:
- **Identical**: skip
- **Derived-ahead** (the derived `~/.claude` or `~/.codex` copy has changes `common/agents/` doesn't): auto-sync candidate -- reconcile back into `common/agents/`
- **Source-ahead**: expected steady state after a `common/` edit not yet re-bootstrapped; re-run `bootstrap.sh` rather than editing the derived copy
- **Derived-only**: flag as needing a sync decision (should it move into `common/agents/`, or is it a legitimate machine-local override?)
- **Source-only**: flag (bootstrap may not have run since the agent was added to `common/agents/`)

**Skills** (generic ones only -- those in a derived tree that also exist in `~/workspace/agentfiles/common/skills/`):
```bash
# Find skills that exist in both a derived tree and the common/ source
for d in ~/workspace/agentfiles/common/skills/*/; do
  name=$(basename "$d")
  [ -d ~/.claude/skills/"$name" ] && echo "claude|$name"
  [ -d ~/.codex/skills/"$name" ] 2>/dev/null && echo "codex|$name"
done
```

Same diff logic as agents. Skip skills only in a derived tree that aren't in `common/skills/` (they're local-only by design).

**Instructions (AGENTS.md / CLAUDE.md):**
- Diff `~/workspace/agentfiles/common/instructions/AGENTS.md` against the derived `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`
- Flag generic rules present in a derived render but missing from the `common/` source

**Hooks, MCP config, model tiers:**
- Diff `~/workspace/agentfiles/common/hooks/`, `common/mcp/`, and `common/model-tiers.toml` against whatever the bootstrap materializes into each tool's config dir. Same derived-ahead/source-ahead/derived-only/source-only categorization.

**README.md coherence:**
- Verify agent count and skill count in README match actual files under `common/`
- Verify directory tree listing matches reality (`common/`, `tools/claude/`, `tools/codex/`)

**Repo hygiene:**
```bash
git -C ~/workspace/agentfiles status --short
git -C ~/workspace/agentfiles log origin/main..HEAD --oneline
```

**Security gate (mandatory before any commit/push to the public agentfiles repo).** `agentfiles` is a public repo, so every derived-ahead auto-fix candidate from this layer must clear the leak scan before it's committed or pushed:

1. Stage the candidate content in a scratch directory (not the live repo).
2. Run `~/workspace/agentfiles/common/scripts/agentfiles-scan.sh <staged-dir>`. Any deny-list or secret-pattern hit **BLOCKS** the push -- no auto-push, no auto-remediate. Surface the exact hit to the user and stop; do not attempt to strip/redact and retry automatically.
3. If the scan is clean, still route the candidate through a `security-analyst` review (`model: "sonnet"`) before committing. The security-analyst reviews for information leakage and data-classification issues the deny-list/regex scan can't catch (e.g. paraphrased internal details, indirect references).
4. Only after both the scanner and the security-analyst review pass does the auto-fix (copy into `common/`/`tools/<tool>/`, commit, push) proceed. A block from either gate halts the layer and is reported under "Needs Approval," not silently skipped or auto-remediated.

**Auto-fix:** For derived-ahead files that clear the security gate above, copy the change back into `common/` (or the relevant `tools/<tool>/` overlay), commit, push.
**Approval needed:** Diverged files, new sync candidates, README updates, and any candidate blocked by the security gate.

### Layer 5: Claude-plugins Sync

Diffs local artifacts against `~/workspace/claude-plugins/` (the shared team marketplace). Unlike agentfiles, this is a team-facing repo -- **all changes go via PR**, never direct push to main. Run after Layer 4 (or standalone via `/audit plugins`).

**Repo hygiene first:**
```bash
git -C ~/workspace/claude-plugins status --short
git -C ~/workspace/claude-plugins log origin/main..HEAD --oneline
```
If the repo has uncommitted work or unpushed commits, surface it before proposing new changes.

**Build the mapping table.** For each `~/workspace/claude-plugins/plugins/<plugin>/{skills,agents,commands}/<item>`:
```bash
# Plugin skills
for d in ~/workspace/claude-plugins/plugins/*/skills/*/; do
  pname=$(echo "$d" | sed -E 's|.*/plugins/([^/]+)/.*|\1|')
  sname=$(basename "$d")
  [ -d ~/.claude/skills/"$sname" ] && echo "skill|$pname|$sname"
done

# Plugin agents
for f in ~/workspace/claude-plugins/plugins/*/agents/*.md; do
  pname=$(echo "$f" | sed -E 's|.*/plugins/([^/]+)/.*|\1|')
  aname=$(basename "$f")
  [ -f ~/.claude/agents/"$aname" ] && echo "agent|$pname|$aname"
done
```

For each match, diff `~/workspace/claude-plugins/plugins/<plugin>/<kind>/<item>` against the local equivalent. Categorize:

- **Identical**: skip
- **Plugin-specific specialization** (different `description` or `model` in frontmatter signaling this is a scoped variant): skip -- these are intentional forks, not drift candidates. Note them so a future audit doesn't re-flag.
- **Local-ahead, name-shared, same purpose** (local has new content the plugin variant lacks AND the purpose statements align): contribution candidate. Surface for PR.
- **Plugin-ahead**: flag (unexpected -- the plugin was updated outside this machine; pull locally first).

**README and marketplace coherence:**
- Verify `.claude-plugin/marketplace.json` lists every directory under `plugins/`
- Verify `README.md`'s Available Plugins table matches `marketplace.json` entries (use `bin/check-marketplace` if present)
- Surface drift without auto-fixing

**No auto-fix.** All proposed changes here require a PR:
1. Create branch: `internal/audit-plugins-YYYY-MM-DD` from `origin/main`
2. Apply changes
3. Run `bin/check-marketplace` (and `bin/verify-plugin <name>` if a plugin's installable shape changed)
4. Commit, push, open **draft PR** via the `/create-pull-request` skill
5. Surface PR URL for user review

**Skip detection caveat.** Most plugin skills have no local equivalent at `~/.claude/skills/` -- they're installed via the marketplace. Don't flag them as missing locally. The audit only fires when a same-named local file exists AND has drifted.

**Security gate (mandatory before any public push).** `claude-plugins` is a public/team-facing repo, so every contribution candidate from this layer must clear the leak scan before it's staged for a PR:

1. Stage the candidate content in a scratch directory (not the live repo).
2. Run `~/workspace/agentfiles/common/scripts/agentfiles-scan.sh <staged-dir>`. Any deny-list or secret-pattern hit **BLOCKS** the push -- no auto-push, no auto-remediate. Surface the exact hit to the user and stop; do not attempt to strip/redact and retry automatically.
3. If the scan is clean, still route the candidate through a `security-analyst` review (`model: "sonnet"`) before opening the PR. The security-analyst reviews for information leakage and data-classification issues the deny-list/regex scan can't catch (e.g. paraphrased internal details, indirect references).
4. Only after both the scanner and the security-analyst review pass does Step 4 below (branch/commit/PR) proceed. A block from either gate halts the layer and is reported under "Needs Approval," not silently skipped.

## Phase 3: App-docs (conditional)

Only run if this session produced doc-worthy work (architecture decisions, debugging guides, setup procedures). If nothing doc-worthy, skip.

If changes needed:
1. Create branch: `internal/audit-docs-YYYY-MM-DD`
2. Write or update relevant docs
3. Commit, push, create draft PR

## Output

After all layers complete, present the summary table:

```
| Layer          | Scanned | Healthy | Issues | Auto-fixed | Needs Approval |
|----------------|---------|---------|--------|------------|----------------|
| Memory         |         |         |        |            |                |
| CLAUDE.md      |         |         |        |            |                |
| Skills         |         |         |        |            |                |
| Sync           |         |         |        |            |                |
| Claude-plugins |         |         |        |            |                |
| App-docs       |         |         |        |            |                |
```

Then list auto-fixes already applied, followed by each item needing approval with the proposed change.

If this audit was run after `/retro`, note which retro recommendations were addressed.
