# Codex User Skill Path Migration

## Goal

Update agentfiles and the user's live Codex setup to use the current user-scoped
skill discovery directory, `~/.agents/skills`, instead of the legacy
`~/.codex/skills` directory.

## Scope

The repository changes are limited to:

- `tools/codex/setup.md`, which defines Codex materialization.
- `common/skills/audit/SKILL.md`, which checks derived Codex skills.

The live migration covers the five agentfiles-managed Codex skills:

- `audit`
- `retro`
- `spindown`
- `spinup`
- `standup`

Unrelated personal skills, bundled `.system` skills, agents, hooks, MCP
configuration, and instruction paths are outside this migration.

## Design

### Repository contract

Codex instructions, rendered agents, and `config.toml` remain under
`$AF_HOME/.codex`. User-scoped skills move to
`$AF_HOME/.agents/skills`.

The setup contract will:

1. Link each `$AF_REPO/common/skills/<name>` directory at
   `$AF_HOME/.agents/skills/<name>`.
2. Create `$AF_HOME/.agents/skills` when needed.
3. Verify skill links at the new location.
4. Treat an existing real skill directory as user-owned content: report it and
   do not replace it with a symlink.

The audit skill will use `~/.agents/skills/<name>` for Codex-side discovery and
drift checks. Claude paths remain unchanged.

### Live migration

The current five Codex skill directories differ from their `common/skills`
sources and contain Codex-specific adaptations. They will therefore be moved
unchanged from `~/.codex/skills/<name>` to
`~/.agents/skills/<name>`, rather than replaced by repository symlinks.

Before each move:

1. Confirm the legacy source is a real directory.
2. Confirm the destination does not exist.
3. Record the source content hash.

After each move:

1. Confirm the new directory exists and is not a symlink.
2. Confirm its content hash matches the recorded source hash.
3. Confirm the legacy path is absent.

Other entries under `~/.codex/skills` remain untouched.

## Error Handling

- If a destination already exists, stop that skill's migration and report the
  collision without overwriting either copy.
- If a source is missing or is a symlink, stop that skill's migration and
  report the unexpected state.
- If post-move verification fails, preserve both the observed state and exact
  command output for recovery; do not delete additional files.
- Existing unrelated worktree changes remain untouched.

## Verification

Repository verification:

- Search the Codex setup and audit skill for stale `~/.codex/skills` or
  `$AF_HOME/.codex/skills` references.
- Review the focused diff and confirm only the setup contract, audit skill,
  and this design document changed as part of this migration.

Live verification:

- All five agentfiles-managed skills exist under `~/.agents/skills`.
- Their post-move hashes match their pre-move hashes.
- Their old `~/.codex/skills` paths are absent.
- Unrelated legacy and `.system` entries under `~/.codex/skills` are unchanged.
- No `[[skills.config]]` entry disables the migrated skills.
