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

## Ownership before setup

All following replacement rules apply only to **ownership-verified** framework-managed
artifacts. Resolve existing links and source registrations first, then classify each destination:

- **Missing:** eligible for the documented creation only after the parent-chain preflight below passes.
- **Framework-managed:** a source registration, link, or generated-artifact provenance confirms
  this `AF_REPO` lineage, rather than a private, package, or shared owner, owns it. Select it for
  convergence to this checkout's documented source.
- **External or uncertain:** private overrides, package/shared-marketplace content, intentional
  native profiles, personal configuration, and any target without that provenance. Preserve it
  and report its owner or uncertainty.

An alternate target or matching name is not ownership evidence. Stop the affected step for
explicit migration approval if ownership is external or uncertain; never bulk-repoint it to this
checkout.

Before any directory creation, linking, rendering, or configuration write, inspect the harness
root and every existing destination-parent component with `lstat` or equivalent no-follow
metadata inspection. Use a canonical path; resolving an unexpected directory link does not
approve its referent. Stop the affected subtree if a parent is symlinked, shared-writable,
differently owned, externally managed, or uncertain. Trusted root-owned system ancestors above
the verified user tree are permitted; UID ownership alone is not framework provenance.

Only below approved, ownership-verified parents may missing directories be created, one
component at a time. Verify each new directory and recheck the parents before each write.
If `$AF_HOME/.codex`, `skills/`, or `agents/` is externally owned or linked, preserve the whole
affected subtree; an absent instruction, skill, agent, or configuration leaf is not an exception.
Existing artifact symlinks below verified parents can still be classified by the ownership rules
above. These POSIX checks do not attest ACLs or provide a race-free sandbox: the operator must
trust the ancestors and exclude concurrent directory replacement, or stop.

### Parent-boundary acceptance cases

Use a synthetic `AF_HOME` only, without running bootstrap or a client. Apply the preflight to
each case before attempting the documented leaf operation:

| Synthetic destination parent | Required outcome |
| --- | --- |
| Linked `$AF_HOME/.codex`; instruction/configuration leaf missing | Stop the subtree; no referent file or directory is created. |
| Canonical harness root, linked `skills/`; skill missing | Stop that collection; no link or rendered skill is created in the referent. |
| Canonical harness root, linked `agents/`; agent missing | Stop that collection; no rendered agent is created in the referent. |
| Canonical, approved user-owned parent; selected leaf missing | The documented creation is eligible after its other checks pass. |

Record the classification and compare referent contents before/after. These are procedural
acceptance checks, not proof that a real client/bootstrap or installed profile was exercised.

## 1. Instructions

Symlink the top-level instructions file:

```
$AF_HOME/.codex/AGENTS.md  ->  $AF_REPO/common/instructions/AGENTS.md
```

For a missing or selected framework-managed destination, ensure this path resolves to the target
above. Replace a selected managed symlink that points elsewhere or is broken (see Idempotency,
section 6). If the destination is external or uncertain, including a private override or a real
file without framework provenance, leave it unchanged, report it separately, and stop this entry
for migration approval.

## 2. Skills (native frontmatter, unchanged body)

Classify each same-name destination under Ownership before setup. For every directory
`$AF_REPO/common/skills/<name>/` whose destination is missing or selected framework-managed:

```
$AF_HOME/.codex/skills/<name>  ->  $AF_REPO/common/skills/<name>
```

Create `$AF_HOME/.codex/skills/` only as needed for eligible destinations. Check the installed
native skill contract before linking: the verified frontmatter fields are `name`, `description`,
`license`, `allowed-tools` and `metadata`. A newer installed contract takes precedence;
unchecked fields are not proof of invalidity.

- If the source frontmatter is supported, symlink the directory as above.
- Otherwise render a real skill directory: copy `SKILL.md` with only supported frontmatter
  fields, and preserve the complete body verbatim. For example, omit `user_invocable` from
  the generic audit's Codex rendering. Link its other files/directories back to the same
  source rather than creating an unrelated helper fork.
- Leave an external or uncertain same-name skill untouched, report it separately, and stop that
  entry for migration approval. Update only an ownership-verified generated rendering.
- Run the installed native validator if available. A missing validator dependency is an
  unchecked native validation, not a rejected skill; report bounded alternative assertions.

Skip non-directory source entries with a note.

Optionally, if a skill directory contains a sibling `openai.yaml` file next to its `SKILL.md`
(rendered from `$AF_REPO/tools/codex/openai.yaml.tmpl`), it travels with the symlinked
directory automatically -- no separate step is needed. Do not create an `openai.yaml` for a
skill that doesn't already have one; that file is optional per-skill metadata, not a required
artifact of this setup.

## 3. Agents (transform each `*.agent.md` -> `.toml`)

Source agents live at `$AF_REPO/common/agents/<name>.agent.md`. For each source agent whose
destination is missing or selected framework-managed, render
`$AF_HOME/.codex/agents/<name>.toml` (a TOML file, not a symlink -- this is a transform, not a
copy). Leave an external or uncertain destination untouched, report it separately, and stop that
agent for migration approval.

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

**MCP servers.** Read `$AF_REPO/common/mcp/servers.jsonc` as a flat server map.
Skip disabled catalog entries (`enabled: false` or legacy `disabled: true`) without removing
or disabling any existing personal registration. Reject contradictory enablement. Strip
catalog-only flags `enabled`, `disabled` and `tools`; honor selections that exclude `codex`.
For approved missing entries, use the installed native contract to render the supported
fields into `[mcp_servers.<name>]`. A different existing entry requires explicit approval,
not an automatic overwrite. The default disabled example creates no Codex registration.
Resolve literal `$REPO` and `$HOME` paths from `$AF_REPO` and `$AF_HOME`; a native server
executable is not assumed to run through a shell. Apply the installed native contract for
other field types and credential references rather than copying another client's schema.

Write the merged result back to `$AF_HOME/.codex/config.toml`, preserving any existing tables
in the file that are unrelated to `[hooks]` and `[mcp_servers.*]`.

Do not infer a Codex `[agents]` delegation setting from dispatch policy, source prompts, or
startup hooks. This setup does not create or update an `[agents]` table. Preserve any existing
native agent settings as external configuration unless a separately verified Codex contract and
explicit approval cover a change.

## 5. Plugins

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

## 6. Idempotency

Re-running this whole doc must be safe and convergent:

- **Selected framework-managed symlinks** (`AGENTS.md`, each skill dir): if a selected link
  already points at the correct target, leave it untouched. If a selected managed link points
  elsewhere or is broken, recreate it. Preserve and report external or uncertain links rather
  than replacing them.
- **Rendered agent files**: regenerate only ownership-verified generated
  `$AF_HOME/.codex/agents/<name>.toml` files from the current source. Preserve and report an
  external or uncertain agent file instead of overwriting it.
- **config.toml merges** (`[hooks]`, `[mcp_servers.*]`): do not duplicate entries. Preserve
  existing MCP tables and personal configuration; changed MCP entries require explicit approval.
  Update only ownership-verified framework-managed hook entries. Do not create or replace an
  `[agents]` table.
- **Plugin marketplace registration**: if a mapped plugin is already registered, leave it
  as-is; do not double-register.

## 7. Verify

Before reporting done, check:

- Every selected framework skill resolves as a directory link or native-frontmatter rendering;
  transformed bodies match source exactly and auxiliary links resolve. Report external overrides
  separately instead of relinking them. Recheck generated renderings on subsequent setup runs.
- Every selected framework source agent has a corresponding
  `$AF_HOME/.codex/agents/<name>.toml` containing `model` and `model_reasoning_effort` keys
  (not a `tier` key). List preserved external or uncertain agent files separately.
- For at least one read-only agent (e.g. `code-reviewer`), confirm its rendered `[access]`
  table has `sandbox_mode = "read-only"` -- a Codex process running that agent should be
  constrained by the sandbox from writing to the filesystem, matching its source `access:`
  value (no write-capable tool listed).
- For at least one agent with no `access:` key at all (e.g. `rails-engineer`), confirm its
  rendered `[access]` table has `sandbox_mode = "workspace-write"` -- parity with the "all
  tools" default that same absent-`access:` agent gets elsewhere.
- The selected framework-managed `$AF_HOME/.codex/AGENTS.md` resolves (symlink is not broken)
  and points at `$AF_REPO/common/instructions/AGENTS.md`. Report a preserved external or
  uncertain instruction file separately instead of replacing it.
- `$AF_HOME/.codex/config.toml` is valid TOML and contains the expected `[hooks]` and
  `[mcp_servers.*]` tables while preserving unrelated tables, including any pre-existing
  `[agents]` table.
- Every discovered sibling plugin marketplace (section 5) is either registered with a mapped
  Codex equivalent, or explicitly listed as skipped with "no Codex equivalent."

Report a summary: counts of skills linked, agents rendered, hooks merged, MCP servers merged,
plugins mapped vs. skipped, preserved external or uncertain entries, and any items skipped or
failed and why.

## Failure handling

If the filesystem/OS rejects a symlink for a selected framework-managed destination (e.g. no
symlink support, permission denied), fall back to copying the source file/directory to that
destination path instead, and note in the final summary that a copy was used in place of a
symlink for that item (copies will not auto-update on future source edits, unlike symlinks).
