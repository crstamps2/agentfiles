---
name: audit
description: Audit all persistent artifacts (per-harness memory stores, AGENTS.md/CLAUDE.md, skills, agents, the agentfiles common/+tools/ source, claude-plugins, app-docs) for coherence and staleness. Audits every harness's memory store (Claude Code, Codex CLI, pi) and reconciles durable facts across them, and verifies the derived ~/.claude, ~/.codex and ~/.pi trees against the common/ source of truth, never the reverse. Auto-fixes safe issues, asks approval for substantive changes including PR-gated contributions back to shared marketplaces, and gates any public push behind the agentfiles-scan.sh scanner plus a security-analyst review. Run standalone or after /retro.
user_invocable: true
---

# Artifact Audit

Ensure all persistent artifacts across the development environment are coherent and up to date.

## Source of truth vs. derived trees

`~/workspace/agentfiles/common/` (plus the per-tool `tools/claude/`, `tools/codex/` and `tools/pi/` overlays) is the **source of truth**. The rendered config trees -- `~/.claude`, `~/.codex`, `~/.pi/agent` -- are **DERIVED**: `bootstrap.sh` materializes them from `common/`+`tools/`. Every layer below that compares "local" state against the repo must diff the rendered trees **against** `common/` (and the relevant `tools/<tool>/` overlay), never the other way around. If a rendered tree has content the source lacks, that is drift to reconcile back into `common/`/`tools/`, not evidence that the source is wrong.

Cover every harness, not just the one you're running in: enumerate the config roots that exist (`~/.claude`, `~/.codex`, `~/.pi/agent`, and any newer one) and run each artifact-type check against all of them. A harness whose artifacts silently stopped being audited is exactly how the trees drift apart.

Some derived paths are **symlinks back into the source** rather than copies -- for example a harness's global instructions file, or a skill directory, pointing straight at `common/`. Resolve links before diffing (`readlink`): a symlinked artifact cannot drift, so report it as linked, not as identical-by-luck, and don't propose a sync for it. Conversely, a path that *used to* be a symlink and is now a real file is drift worth flagging.

The **memory stores are not derived** and are never rendered by `bootstrap.sh` -- they are per-harness runtime state, each in its own format. They're handled by Layers 1 and 1b, which reconcile them against each other instead of against `common/`.

## Arguments

- No args: audit all layers
- `<layer>`: audit a specific layer only (e.g., `/audit memory`, `/audit skills`)
- Valid layers: `memory`, `claude-md`, `skills`, `sync`, `plugins`, `app-docs`
- `memory` covers every harness store plus the cross-harness reconciliation; scope it to one harness with `/audit memory <harness>`

## Tracking

Create a task for each layer to track progress.

## Phase 1: Scan (parallel)

Run layers 1-3 in parallel as background agents (`model: "haiku"`). They're read-only scans that don't depend on each other. Layer 1 itself fans out one agent per harness memory store, so dispatch those in the same turn as layers 2 and 3 -- all of them in one batch, not one layer at a time.

Layer 1b is the exception: it consumes every Layer 1 report, so it runs in the orchestrator after the batch returns.

If a specific layer was requested, only run that one.

### Layer 1: Memory (every harness)

Each harness keeps its own persistent memory store, in its own shape. Audit all of them that exist on this machine, then reconcile them against each other in Layer 1b. Fan out one agent per store.

| Harness | Store | Shape |
|---|---|---|
| Claude Code | `~/.claude/projects/<slug>/memory/`, plus `~/.claude/agent-memory/<role>/` for per-agent memory | One fact per file with `name` / `description` / `metadata.type` frontmatter; `MEMORY.md` is the index; `[[slug]]` links between facts |
| Codex CLI | `~/.codex/memories/` (its own git repo) | `MEMORY.md` task groups (`scope` / `applies_to` headers, per-task sections), `raw_memories.md`, `memory_summary.md` (rolled-up profile + preferences), `rollout_summaries/` (generated, one per session), `extensions/` |
| pi | `~/.pi/agent/memory/` | `MEMORY.md` tagged one-liners (`#preference` / `#lesson` / `#note`, each with a `[[slug]]`) grouped under topic `##` headings and appended in timestamped blocks; `daily/<YYYY-MM-DD>.md` handoffs; `SCRATCHPAD.md` open items; `imported/` search-only archive; `recovery/` |

Discover stores rather than assuming this list is complete: a harness added since this skill was last touched still has a memory directory under its own config root, and finding it is part of the audit. Report any config root that has no store entry in the table above.

**Audit-only, never rewrite:** `~/.codex/memories/rollout_summaries/` and `~/.pi/agent/memory/{imported,recovery}/` are generated or archival. Read them for contradictions, but land every correction in the curated file (`MEMORY.md`, `memory_summary.md`) instead.

**Agent prompt** (`subagent_type: "general-purpose"`, `model: "haiku"`), one per store, parameterized by the table row:

> Audit the `<harness>` memory store at `<path>`.
>
> 1. Read the store's index or curated file (see its Shape column) and enumerate every entry it declares.
> 2. For each entry:
>    - Verify anything it points at exists (report broken index entries)
>    - Read the content
>    - If it references file paths, classes, functions, commands, or CLI flags, spot-check 2-3 key ones via Glob/Grep/`which` against `$PWD` and the paths named
>    - Verify the entry's classification is valid for this store: Claude frontmatter `type` in {user, feedback, project, reference}; pi tag in {`#preference`, `#lesson`, `#note`} carrying a `[[slug]]`; Codex task group carrying `scope` and `applies_to`
> 3. Report orphans (present in the directory, absent from the index) and index entries with no backing content
> 4. Report duplicate or contradictory entries WITHIN this store
> 5. Report entries whose named subject no longer exists, and any curated file grown too large to read in one pass (give the byte count; do not truncate it yourself)
>
> Return a structured report:
> ```
> harness: <name>
> broken_refs: [index entries pointing at nothing]
> orphans: [files not in the index]
> stale_refs: [{entry, reference, reason}]
> duplicates: [pairs covering the same topic]
> misclassified: [{entry, declared_type, reason}]
> oversized: [{file, bytes}]
> total_entries: N
> healthy_entries: N
> ```

### Layer 1b: Cross-harness memory reconciliation

Layer 1 checks each store against reality. This layer checks the stores **against each other** -- a durable fact learned in one harness is worthless if the others never hear about it. Run it in the orchestrator once the Layer 1 agents return, since it needs all their reports at once.

1. **Build the union.** Collapse every store's entries into one list of facts keyed by subject, not by wording -- the same preference is phrased differently in each store's idiom.
2. **Find the gaps.** For each fact, record which stores hold it. A fact in one store and missing from another is a propagation gap, unless it is genuinely harness-specific: a fact about one CLI's own flags, config, or launch behavior belongs only in that CLI's store. Say which case applies rather than propagating by default.
3. **Find the conflicts.** A fact asserted one way in one store and another way elsewhere is a **HIGH-severity** finding -- whichever harness runs next acts on the wrong instruction. Resolve to one truth first (ask the user when the session doesn't settle it), then propagate the resolved version everywhere.
4. **Harvest this session.** Review the conversation for durable facts not yet in ANY store: user corrections and confirmed approaches, project decisions and constraints, new tools or references. Each becomes a propagation candidate for every store.
5. **Propagate**, translating into each store's native shape instead of pasting one store's wording everywhere:
   - **Claude Code** -- one file per fact in the project memory dir with `name` / `description` / `metadata.type` frontmatter, plus one `- [Title](file.md) -- hook` line in `MEMORY.md`; link relatives with `[[slug]]`. Update the existing file when one already covers the subject.
   - **Codex CLI** -- append a one-line entry to the matching `##` section of `memory_summary.md` (profile, preferences, general tips) or to the relevant task group in `MEMORY.md`, in the store's existing voice. The store is a git repo: commit the edit there with a short message so it stays attributable. Nothing in this layer touches credentials, session logs, or the CLI's own config file.
   - **pi** -- append a tagged one-liner (`#preference` / `#lesson` / `#note` plus a fresh `[[slug]]`) under the right topic heading in `MEMORY.md`, following the existing timestamped-block convention. Leave `daily/` and `SCRATCHPAD.md` to the harness itself.
6. Where Layer 1 flagged a curated file `oversized`, propose consolidation (merge duplicates, retire facts whose subject is gone) as its own approval item -- never fold it silently into a propagation edit.

**Auto-fix:** Removing an index line that points at a file which does not exist.
**Approval needed:** Every memory write -- new facts, propagations, conflict resolutions, consolidations. Show the exact per-store text before writing it.

Report per fact: `subject`, `present_in`, `missing_from`, `harness_specific`, `conflict`, and the proposed text for each store it is missing from.

### Layer 2: AGENTS.md / CLAUDE.md Files

**Agent prompt** (`subagent_type: "general-purpose"`, `model: "haiku"`):

> Audit the source-of-truth instructions file and its derived renders only. **Do not audit subdirectory-level or project-owner CLAUDE.md files** -- those are out of scope for this audit.
>
> 1. Read `~/workspace/agentfiles/common/instructions/AGENTS.md` (the shared source of truth) -- check for references to tools, paths, or conventions that may be stale.
> 2. Read the derived global renders -- `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, `~/.pi/agent/AGENTS.md`, and any other harness's global instructions file -- and diff each against the `common/` source. Run `readlink` on each first: one that symlinks back to `common/instructions/AGENTS.md` is by definition in sync, so report it as linked and move on. For the real-file renders, flag any content not traceable back to `common/instructions/AGENTS.md` or a `tools/<tool>/` overlay; that's drift in the derived tree, not the source. Also flag a stale `.orig`/`.bak` copy sitting next to a render -- it's a leftover from a hand-edit and hides which file the harness actually reads.
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

> Audit skills at the source of truth, `~/workspace/agentfiles/common/skills/`, and cross-check every harness's derived skills tree against it -- `~/.claude/skills/`, `~/.codex/skills/`, `~/.pi/agent/skills/`, and any other harness config root present.
>
> For each skill's SKILL.md in `common/skills/`:
> 1. Check for references to file paths -- verify they exist
> 2. Check for references to CLI tools/scripts -- verify they're available (e.g., `which <tool>`, `ls <path>`)
> 3. Note the line count -- flag if over 500 lines
> 4. For each harness, resolve `<harness-skills>/<name>` with `readlink`. A symlink into `common/skills/<name>/` is in sync by construction -- report it as linked. A real directory must be diffed against `common/skills/<name>/`; any mismatch is derived-tree drift to reconcile back into `common/`, never edited in place on the derived side.
> 5. Report per skill which harnesses carry it and which don't. A skill in `common/` that never rendered into a harness that supports skills is a bootstrap gap, and the same-named skill diverging between two harnesses is a fork to reconcile -- both matter more than any single tree's health.
>
> Skip `peon-ping-*` skills (machine-specific audio alerts).
>
> Return:
> ```
> broken_refs: [{skill, reference, type}]
> oversized: [{skill, line_count}]
> derived_drift: [{tool, skill, reason}]
> linked: [{tool, skill}]
> coverage_gaps: [{skill, missing_from: [tools]}]
> forks: [{skill, tools, reason}]
> total_skills: N
> healthy_skills: N
> ```

## Phase 2: Sync (sequential, orchestrator)

This layer diffs the DERIVED harness trees -- `~/.claude`, `~/.codex`, `~/.pi/agent`, and any other harness config root present -- against the `~/workspace/agentfiles/common/` (+`tools/`) source of truth. Run after Phase 1 completes (or standalone if `/audit sync` was requested). The direction of every diff below is derived-tree -> source; a derived-only file is sync-candidate or drift, never grounds for changing `common/` to match it without review.

### Layer 4: Agentfiles Sync

Check these artifact types between the derived trees and the agentfiles `common/` source:

Enumerate the harness trees once, up front, and drive every check below off that list rather than hardcoding two tools:

```bash
# Derived harness roots present on this machine (extend as harnesses are added)
for root in ~/.claude ~/.codex ~/.pi/agent; do [ -d "$root" ] && echo "$root"; done
```

**Agents:**
```bash
ls ~/workspace/agentfiles/common/agents/*.md 2>/dev/null | xargs -I{} basename {}
# then, per harness root from the list above
ls <root>/agents/*.md 2>/dev/null | xargs -I{} basename {}
```

Note that a harness may render agents in its own format (a `.toml` per agent, say) rather than copying the `.md` verbatim. Match on the agent NAME, not the filename, and compare the semantic content: roster membership, model tier, and role boundaries. A format difference is not drift; a missing or contradictory role is.

For each agent present in both a derived tree and `common/agents/`, categorize:
- **Identical**: skip
- **Derived-ahead** (the harness copy has changes `common/agents/` doesn't): auto-sync candidate -- reconcile back into `common/agents/`
- **Source-ahead**: expected steady state after a `common/` edit not yet re-bootstrapped; re-run `bootstrap.sh` rather than editing the derived copy
- **Derived-only**: flag as needing a sync decision (should it move into `common/agents/`, or is it a legitimate machine-local override?)
- **Source-only**: flag (bootstrap may not have run since the agent was added to `common/agents/`)
- **Roster asymmetry**: an agent that exists for one harness and not another, where both support agents -- flag it, since it silently changes which roles are dispatchable depending on which CLI is running

**Skills** (generic ones only -- those in a harness tree that also exist in `~/workspace/agentfiles/common/skills/`):
```bash
for d in ~/workspace/agentfiles/common/skills/*/; do
  name=$(basename "$d")
  for root in ~/.claude ~/.codex ~/.pi/agent; do
    [ -e "$root/skills/$name" ] && echo "$root|$name|$(readlink "$root/skills/$name" || echo copy)"
  done
done
```

Same diff logic as agents, minus anything the `readlink` column shows as a symlink into `common/` (in sync by construction). Skip skills present only in a harness tree and absent from `common/skills/` -- they're local-only by design.

**Instructions (AGENTS.md / CLAUDE.md):**
- Diff `~/workspace/agentfiles/common/instructions/AGENTS.md` against each harness's global render (`~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, `~/.pi/agent/AGENTS.md`, ...), skipping any that symlinks to the source
- Flag generic rules present in a derived render but missing from the `common/` source

**Hooks, MCP config, model tiers:**
- Diff `~/workspace/agentfiles/common/hooks/`, `common/mcp/`, and `common/model-tiers.toml` against whatever the bootstrap materializes into each tool's config dir. Same derived-ahead/source-ahead/derived-only/source-only categorization.

**README.md coherence:**
- Verify agent count and skill count in README match actual files under `common/`
- Verify the directory tree listing matches reality, including one `tools/<tool>/` entry per supported harness -- a harness that `bootstrap.sh` accepts as a `--tool=` value but that the README never mentions (or vice versa) is the most common form of staleness here
- Verify the documented bootstrap invocations cover every supported harness, and that each one's setup doc (`tools/<tool>/setup.md`) exists

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
| Layer                | Scanned | Healthy | Issues | Auto-fixed | Needs Approval |
|----------------------|---------|---------|--------|------------|----------------|
| Memory: claude       |         |         |        |            |                |
| Memory: codex        |         |         |        |            |                |
| Memory: pi           |         |         |        |            |                |
| Memory: cross-harness|         |         |        |            |                |
| AGENTS.md/CLAUDE.md  |         |         |        |            |                |
| Skills               |         |         |        |            |                |
| Sync                 |         |         |        |            |                |
| Claude-plugins       |         |         |        |            |                |
| App-docs             |         |         |        |            |                |
```

One memory row per store found, so a harness whose store went unaudited shows up as a missing row rather than disappearing into an aggregate. The cross-harness row counts facts reconciled, gaps found, and conflicts.

Then list auto-fixes already applied, followed by each item needing approval with the proposed change. Group the memory-propagation approvals by fact (showing the per-store text side by side), not by store -- the user is approving one fact at a time, not three unrelated edits.

If this audit was run after `/retro`, note which retro recommendations were addressed.
