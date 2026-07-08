# Codex CLI setup (mechanical -- execute, do not design)

You are configuring THIS machine's `~/.codex` from an agentfiles clone. Execute each step
below verbatim using the injected environment variables: `AF_HOME`, `AF_REPO`, `AF_OS`.

- `AF_HOME` -- the home directory whose `.codex/` you are materializing (rendered paths below
  as `$AF_HOME/.codex/...`).
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
$AF_HOME/.codex/AGENTS.md  ->  $AF_REPO/common/instructions/AGENTS.md
```

If `$AF_HOME/.codex/AGENTS.md` already exists as a symlink pointing elsewhere, remove it and
recreate it pointing at the correct target (see Idempotency, section 7). If it exists as a
real file (not a symlink), stop and report -- do not overwrite a user's real file.

## 2. Skills (symlink each directory)

For every directory `$AF_REPO/common/skills/<name>/`:

```
$AF_HOME/.codex/skills/<name>  ->  $AF_REPO/common/skills/<name>
```

One symlink per skill directory (not per file inside it). Create `$AF_HOME/.codex/skills/`
first if it does not exist. Skip (with a note in the summary) any entry under
`common/skills/` that is not a directory.

Optionally, if a skill directory contains a sibling `openai.yaml` file next to its `SKILL.md`
(rendered from `$AF_REPO/tools/codex/openai.yaml.tmpl`), it travels with the symlinked
directory automatically -- no separate step is needed. Do not create an `openai.yaml` for a
skill that doesn't already have one; that file is optional per-skill metadata, not a required
artifact of this setup.

## 3. Agents (transform each `*.agent.md` -> `.toml`)

Source agents live at `$AF_REPO/common/agents/<name>.agent.md`. For each one, render
`$AF_HOME/.codex/agents/<name>.toml` (a TOML file, not a symlink -- this is a transform, not a
copy).

Transform rules:

- `name:` (frontmatter) -- copy through unchanged as `name = "<value>"`.
- `description:` (frontmatter) -- copy through unchanged as `description = "<value>"`.
- Body (everything after the closing `---` of the frontmatter) -- copied verbatim, unchanged,
  into a `developer_instructions = """<body>"""` triple-quoted string.
- `tier:` (frontmatter) -- **drop** this key. Look up its value as a table name in
  `$AF_REPO/common/model-tiers.toml`, read that table's `codex_model` key, and emit
  `model = "<value>"`. Read that same table's `codex_effort` key, and emit
  `model_reasoning_effort = "<value>"`.
- `access:` (frontmatter) -- if present, render an `[access]` table (see rule below). If
  `access:` is absent, emit `[access]` with `sandbox_mode = "workspace-write"` (see parity
  rationale below).
- Any other frontmatter key -- there is no general Codex TOML slot for it; omit it from the
  rendered file and note the omission in the final summary (do not invent a TOML field for a
  key with no defined mapping).

**`[access]` table rule (parity with the no-restriction default).** An absent `access:` key
means "no restriction was declared" -- the source agent gets every tool. Render that the same
way in both directions: absent `access:` -> full/write-capable. Only a declared `access:` list
that omits write-capable tools should render as read-only:

- **Override first.** If the source frontmatter defines a `sandbox:` key, use its value
  verbatim as `sandbox_mode` and skip the access-based inference entirely. Otherwise, infer
  from `access:` as described below (absent `access:` -> `workspace-write`; `access:` with a
  write/edit tool -> `workspace-write`; `access:` with only read-type tools -> `read-only`).
- If `access:` is **absent**, set `sandbox_mode = "workspace-write"` -- no restriction was
  declared, so the agent is write-capable (matching the "all tools" default when this same
  agent source has no `access:`/`tools:` key).
- If `access:` is **present** and contains `Write`, `Edit`, `NotebookEdit`, `*`, or "All
  tools" (i.e. any write-capable tool), set `sandbox_mode = "workspace-write"`.
- If `access:` is **present** and contains only read/inspection tools (e.g. `Read`, `Grep`,
  `Glob`, `Bash`, `WebFetch`, `WebSearch`), set `sandbox_mode = "read-only"`.
- In all cases also set `skills = true` and `mcp_servers = true` -- Codex agents get skills
  and MCP access by default regardless of sandbox mode; sandbox_mode governs filesystem
  writes, not skill/MCP availability.

If a `tier:` value has no matching table in `model-tiers.toml`, or the matching table has no
`codex_model`/`codex_effort` key, stop that agent's render, report the missing mapping, and
move on to the next agent.

### Worked example: read-only agent (declared `access:`, no write tools)

Input, `$AF_REPO/common/agents/code-reviewer.agent.md` frontmatter and body:

```yaml
---
name: code-reviewer
description: Expert code reviewer. Use proactively after code changes to review for quality, security, performance, and adherence to project conventions. Read-only -- never modifies code.
access: Read, Grep, Glob, Bash
tier: specialist
---
You are an expert code reviewer...
```

`model-tiers.toml` has:

```toml
[specialist]
codex_model = "gpt-5.4"
codex_effort = "medium"
```

Rendered output, `$AF_HOME/.codex/agents/code-reviewer.toml`:

```toml
name = "code-reviewer"
description = "Expert code reviewer. Use proactively after code changes to review for quality, security, performance, and adherence to project conventions. Read-only -- never modifies code."
developer_instructions = """
You are an expert code reviewer...
"""
model = "gpt-5.4"
model_reasoning_effort = "medium"

[access]
sandbox_mode = "read-only"
skills = true
mcp_servers = true
```

Note `tier: specialist` became `model = "gpt-5.4"` / `model_reasoning_effort = "medium"` (via
the `[specialist]` table's `codex_model`/`codex_effort` keys), and `access: Read, Grep, Glob,
Bash` -- present, but containing no write-capable tool -- became `sandbox_mode = "read-only"`.

### Worked example: write-capable agent (absent `access:`)

Input, `$AF_REPO/common/agents/rails-engineer.agent.md` frontmatter (abridged):

```yaml
---
name: rails-engineer
description: Senior Ruby on Rails engineer for backend implementation.
tier: specialist
---
```

There is no `access:` key at all -- the source declares no restriction, so this agent has every
tool. Rendered output, `$AF_HOME/.codex/agents/rails-engineer.toml` (abridged):

```toml
name = "rails-engineer"
description = "Senior Ruby on Rails engineer for backend implementation."
model = "gpt-5.4"
model_reasoning_effort = "medium"

[access]
sandbox_mode = "workspace-write"
skills = true
mcp_servers = true
```

Absent `access:` became `sandbox_mode = "workspace-write"` -- parity with the "all tools"
default this same agent source gets when rendered without an `access:`/`tools:` key.

## 4. Hooks + MCP -> config.toml

Target file: `$AF_HOME/.codex/config.toml`. Create it as an empty file first if it does not
exist.

**Hooks.** Read `$AF_REPO/common/hooks/hooks.manifest.jsonc`. It is keyed by hook event name
(e.g. `SessionStart`, `Stop`), each holding an array of `{ "script": "<repo-relative-path>" }`
entries. For each entry, substitute `$AF_REPO` for the literal string `$REPO` in the script
path, producing an absolute path. Merge the result into `config.toml`'s `[hooks]` table: for
each event key, ensure an array of script paths exists under that key, and ensure the
resolved script path is present (add it if missing; if the event key already has
non-manifest entries, leave those alone and just add/update the manifest-sourced ones).

**MCP servers.** Read `$AF_REPO/common/mcp/servers.jsonc`. It is a flat object keyed by server
name, each holding that server's config (`command`, `args`, `env`, etc.). Merge each entry
into `config.toml` as its own `[mcp_servers.<name>]` table, keyed by the same server name.
Where a server's config contains the placeholder `$REPO`, substitute `$AF_REPO`; where it
contains `$HOME`, leave it as `$HOME` (resolved by Codex CLI / the shell at launch time, not by
you).

Write the merged result back to `$AF_HOME/.codex/config.toml`, preserving any existing tables
in the file that are unrelated to `[hooks]` and `[mcp_servers.*]`.

## 5. `[agents]` config

Merge the following table into `$AF_HOME/.codex/config.toml`, preserving any other existing
keys in the file:

```toml
[agents]
delegation = "proactive"
max_depth = 1
```

`delegation = "proactive"` matches this project's dispatch-first orchestration style (agents
are expected to delegate sub-tasks rather than do everything inline). `max_depth = 1` (the
default) is sufficient for the agent roster rendered in section 3 -- none of them are defined
to spawn further sub-agents. Only raise `max_depth` if you observe (or are told) that a
rendered agent's `developer_instructions` itself directs it to dispatch sub-agents; do not
raise it speculatively.

## 6. Plugins

Discover sibling tool-plugin marketplace directories in this repo: glob
`$AF_REPO/tools/*/plugins`, excluding `$AF_REPO/tools/codex/plugins` (this doc's own tool).
For each marketplace manifest found under a matched directory:

- Check whether an equivalent plugin/extension exists in the Codex CLI marketplace ecosystem
  (e.g. a local-LSP-server plugin has a Codex extension counterpart). Where a real Codex
  equivalent exists, register it via `/plugin marketplace add <local-path-to-codex-equivalent>`
  followed by `/plugin install <plugin-name>@<marketplace-name>`, using Codex CLI's own plugin
  commands -- do not copy files by hand.
- Where no Codex equivalent exists for a discovered plugin, do not attempt a substitute or
  approximation. List it by name in the final verification report as skipped, with the reason
  "no Codex equivalent."

Do not manually copy plugin files into `$AF_HOME/.codex/` in either case.

## 7. Idempotency

Re-running this whole doc must be safe and convergent:

- **Symlinks** (`AGENTS.md`, each skill dir): if the symlink already exists and points at the
  correct target, leave it untouched. If it exists and points elsewhere (or is a broken
  symlink), remove and recreate it. Never leave two competing links for the same name.
- **Rendered agent files**: always regenerate `$AF_HOME/.codex/agents/<name>.toml` from the
  current source and overwrite it in place -- these are generated artifacts, not hand-edited
  files.
- **config.toml merges** (`[hooks]`, `[mcp_servers.*]`, `[agents]`): re-merging must not
  duplicate hook script entries or MCP server tables. An entry already present with the same
  resolved values is left as-is; only missing or changed entries are added/updated.
- **Plugin marketplace registration**: if a mapped plugin is already registered, leave it
  as-is; do not double-register.

## 8. Verify

Before reporting done, check:

- Every directory under `$AF_REPO/common/skills/` has a corresponding symlink under
  `$AF_HOME/.codex/skills/` that resolves (not broken).
- Every `$AF_REPO/common/agents/<name>.agent.md` has a corresponding
  `$AF_HOME/.codex/agents/<name>.toml` containing `model` and `model_reasoning_effort` keys
  (not a `tier` key).
- For at least one read-only agent (e.g. `code-reviewer`), confirm its rendered `[access]`
  table has `sandbox_mode = "read-only"` -- a Codex process running that agent should be
  constrained by the sandbox from writing to the filesystem, matching its source `access:`
  value (no write-capable tool listed).
- For at least one agent with no `access:` key at all (e.g. `rails-engineer`), confirm its
  rendered `[access]` table has `sandbox_mode = "workspace-write"` -- parity with the "all
  tools" default that same absent-`access:` agent gets elsewhere.
- `$AF_HOME/.codex/AGENTS.md` resolves (symlink is not broken) and points at
  `$AF_REPO/common/instructions/AGENTS.md`.
- `$AF_HOME/.codex/config.toml` is valid TOML and contains the expected `[hooks]`,
  `[mcp_servers.*]`, and `[agents]` tables.
- Every discovered sibling plugin marketplace (section 6) is either registered with a mapped
  Codex equivalent, or explicitly listed as skipped with "no Codex equivalent."

Report a summary: counts of skills linked, agents rendered, hooks merged, MCP servers merged,
plugins mapped vs. skipped, plus any items skipped or failed and why.

## Failure handling

If the filesystem/OS rejects a symlink (e.g. no symlink support, permission denied), fall back
to copying the source file/directory to the destination path instead, and note in the final
summary that a copy was used in place of a symlink for that item (copies will not auto-update
on future source edits, unlike symlinks).
