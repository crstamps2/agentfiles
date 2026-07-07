# Claude Code setup (mechanical -- execute, do not design)

You are configuring THIS machine's `~/.claude` from an agentfiles clone. Execute each step
below verbatim using the injected environment variables: `AF_HOME`, `AF_REPO`, `AF_OS`.

- `AF_HOME` -- the home directory whose `.claude/` you are materializing (rendered paths below
  as `$AF_HOME/.claude/...`).
- `AF_REPO` -- the absolute path to the agentfiles checkout (source of everything under
  `common/`).
- `AF_OS` -- `macos` or `linux`. Only affects command syntax you use to create symlinks/copies;
  it does not change what gets linked.

Never invent a path, a file name, or a mapping not listed here. If a step's source path does
not exist, stop that step, report exactly which path was missing, and continue with the
remaining steps. Do not silently skip or substitute.

## 1. Instructions

Symlink the top-level instructions file:

```
$AF_HOME/.claude/CLAUDE.md  ->  $AF_REPO/common/instructions/AGENTS.md
```

If `$AF_HOME/.claude/CLAUDE.md` already exists as a symlink pointing elsewhere, remove it and
recreate it pointing at the correct target (see Idempotency, section 6). If it exists as a
real file (not a symlink), stop and report -- do not overwrite a user's real file.

## 2. Skills (symlink each directory)

For every directory `$AF_REPO/common/skills/<name>/`:

```
$AF_HOME/.claude/skills/<name>  ->  $AF_REPO/common/skills/<name>
```

One symlink per skill directory (not per file inside it). Create `$AF_HOME/.claude/skills/`
first if it does not exist. Skip (with a note in the summary) any entry under
`common/skills/` that is not a directory.

## 3. Agents (transform each `*.agent.md` -> `.md`)

Source agents live at `$AF_REPO/common/agents/<name>.agent.md`. For each one, render
`$AF_HOME/.claude/agents/<name>.md` (plain `.md`, not a symlink -- this is a transform, not a
copy).

Transform rules, applied to the YAML frontmatter only. The body (everything after the closing
`---`) is copied verbatim, unchanged.

- `name:` -- copy through unchanged.
- `description:` -- copy through unchanged.
- `tier:` -- **drop** this key. Look up its value as a table name in
  `$AF_REPO/common/model-tiers.toml`, read that table's `claude` key, and emit a new
  `model: <value>` frontmatter key with that value.
- `access:` -- if present, rename the key to `tools:` (value copied through unchanged). If
  `access:` is absent, do not add a `tools:` key.
- Any other frontmatter key (e.g. `permissionMode:`, `memory:`) -- copy through unchanged,
  same key name, same value.
- Preserve the original key order as closely as possible, with `model:` taking the position
  `tier:` occupied.

If a `tier:` value has no matching table in `model-tiers.toml`, or the matching table has no
`claude` key, stop that agent's render, report the missing mapping, and move on to the next
agent.

### Worked example

Input, `$AF_REPO/common/agents/code-reviewer.agent.md` frontmatter:

```yaml
---
name: code-reviewer
description: Expert code reviewer. Use proactively after code changes to review for quality, security, performance, and adherence to project conventions. Read-only -- never modifies code.
access: Read, Grep, Glob, Bash
tier: specialist
permissionMode: dontAsk
memory: user
---
```

`model-tiers.toml` has:

```toml
[specialist]
claude = "sonnet"
```

Rendered output, `$AF_HOME/.claude/agents/code-reviewer.md` frontmatter (body unchanged below
the second `---`):

```yaml
---
name: code-reviewer
description: Expert code reviewer. Use proactively after code changes to review for quality, security, performance, and adherence to project conventions. Read-only -- never modifies code.
tools: Read, Grep, Glob, Bash
model: sonnet
permissionMode: dontAsk
memory: user
---
```

Note `tier: specialist` became `model: sonnet` (via the `[specialist]` table's `claude` key),
and `access:` became `tools:` with the same value.

## 4. Hooks + MCP -> settings.json

Target file: `$AF_HOME/.claude/settings.json`. Create it as `{}` first if it does not exist.
Load it as JSON (strip `//` comments from the source files below before parsing -- they are
JSONC).

**Hooks.** Read `$AF_REPO/common/hooks/hooks.manifest.jsonc`. It is keyed by hook event name
(e.g. `SessionStart`, `Stop`), each holding an array of `{ "script": "<repo-relative-path>" }`
entries. For each entry, substitute `$AF_REPO` for the literal string `$REPO` in the script
path, producing an absolute path. Merge the result into `settings.json`'s top-level `"hooks"`
object: for each event key, ensure an array exists, and ensure an entry running that resolved
script path is present (add it if missing; if the event key already has non-manifest entries,
leave those alone and just add/update the manifest-sourced ones).

**MCP servers.** Read `$AF_REPO/common/mcp/servers.jsonc`. It is a flat object keyed by server
name, each holding that server's config (`command`, `args`, `env`, etc.). Merge each entry into
`settings.json`'s top-level `"mcpServers"` object, keyed by the same server name. Where a
server's config contains the placeholder `$REPO`, substitute `$AF_REPO`; where it contains
`$HOME`, leave it as `$HOME` (resolved by Claude Code / the shell at launch time, not by you).

Write the merged result back to `$AF_HOME/.claude/settings.json`, preserving any existing keys
in the file that are unrelated to `"hooks"` and `"mcpServers"`.

## 5. Plugins

Register the local plugin marketplace at `$AF_REPO/tools/claude/plugins` with Claude Code
(the directory containing marketplace manifests such as `local-lsp-marketplace/`). Use
whatever mechanism Claude Code exposes for adding a local marketplace path (settings entry or
CLI command) so plugins under that directory become installable/discoverable. Do not manually
copy plugin files into `$AF_HOME/.claude/` -- registering the marketplace path is sufficient.

## 6. Idempotency

Re-running this whole doc must be safe and convergent:

- **Symlinks** (`CLAUDE.md`, each skill dir): if the symlink already exists and points at the
  correct target, leave it untouched. If it exists and points elsewhere (or is a broken
  symlink), remove and recreate it. Never leave two competing links for the same name.
- **Rendered agent files**: always regenerate `$AF_HOME/.claude/agents/<name>.md` from the
  current source and overwrite it in place -- these are generated artifacts, not
  hand-edited files.
- **settings.json merges**: re-merging must not duplicate hook entries or MCP server entries.
  An entry already present with the same resolved values is left as-is; only missing or
  changed entries are added/updated.
- **Plugin marketplace registration**: if already registered, leave it as-is; do not
  double-register.

## 7. Verify

Before reporting done, check:

- Every directory under `$AF_REPO/common/skills/` has a corresponding symlink under
  `$AF_HOME/.claude/skills/` that resolves (not broken).
- Every `$AF_REPO/common/agents/<name>.agent.md` has a corresponding
  `$AF_HOME/.claude/agents/<name>.md` containing a `model:` key (not `tier:`).
- For at least one read-only agent (e.g. `code-reviewer`), confirm its rendered `tools:` list
  contains no write-capable tool (no `Write`, `Edit`, or similar) -- it should match the
  source `access:` value exactly, unexpanded.
- `$AF_HOME/.claude/CLAUDE.md` resolves (symlink is not broken) and points at
  `$AF_REPO/common/instructions/AGENTS.md`.
- `$AF_HOME/.claude/settings.json` is valid JSON and contains the expected hook and MCP
  server keys.
- The plugin marketplace at `$AF_REPO/tools/claude/plugins` is registered.

Report a summary: counts of skills linked, agents rendered, hooks merged, MCP servers merged,
plus any items skipped or failed and why.

## Failure handling

If the filesystem/OS rejects a symlink (e.g. no symlink support, permission denied), fall back
to copying the source file/directory to the destination path instead, and note in the final
summary that a copy was used in place of a symlink for that item (copies will not
auto-update on future source edits, unlike symlinks).
