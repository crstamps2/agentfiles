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
If `$AF_HOME/.claude`, `skills/`, or `agents/` is externally owned or linked, preserve the whole
affected subtree; an absent instruction, skill, agent, or configuration leaf is not an exception.
Existing artifact symlinks below verified parents can still be classified by the ownership rules
above. These POSIX checks do not attest ACLs or provide a race-free sandbox: the operator must
trust the ancestors and exclude concurrent directory replacement, or stop.

### Parent-boundary acceptance cases

Use a synthetic `AF_HOME` only, without running bootstrap or a client. Apply the preflight to
each case before attempting the documented leaf operation:

| Synthetic destination parent | Required outcome |
| --- | --- |
| Linked `$AF_HOME/.claude`; instruction/configuration leaf missing | Stop the subtree; no referent file or directory is created. |
| Canonical harness root, linked `skills/`; skill missing | Stop that collection; no link or rendered skill is created in the referent. |
| Canonical harness root, linked `agents/`; agent missing | Stop that collection; no rendered agent is created in the referent. |
| Canonical, approved user-owned parent; selected leaf missing | The documented creation is eligible after its other checks pass. |

Record the classification and compare referent contents before/after. These are procedural
acceptance checks, not proof that a real client/bootstrap or installed profile was exercised.

## 1. Instructions

Symlink the top-level instructions file:

```
$AF_HOME/.claude/CLAUDE.md  ->  $AF_REPO/common/instructions/AGENTS.md
```

For a missing or selected framework-managed destination, ensure this path resolves to the target
above. Replace a selected managed symlink that points elsewhere or is broken (see Idempotency,
section 6). If the destination is external or uncertain, including a private override or a real
file without framework provenance, leave it unchanged, report it separately, and stop this entry
for migration approval.

## 2. Skills (symlink each directory)

For every directory `$AF_REPO/common/skills/<name>/` whose destination is missing or selected
framework-managed:

```
$AF_HOME/.claude/skills/<name>  ->  $AF_REPO/common/skills/<name>
```

Use one symlink per eligible skill directory, not per file inside it. Create
`$AF_HOME/.claude/skills/` only as needed for eligible destinations. Leave an external or
uncertain same-name destination untouched, report it separately, and stop that entry for
migration approval. Skip (with a note in the summary) any entry under `common/skills/` that is
not a directory.

## 3. Agents (transform each `*.agent.md` -> `.md`)

Source agents live at `$AF_REPO/common/agents/<name>.agent.md`. For each source agent whose
destination is missing or selected framework-managed, render
`$AF_HOME/.claude/agents/<name>.md` (plain `.md`, not a symlink -- this is a transform, not a
copy). Leave an external or uncertain destination untouched, report it separately, and stop that
agent for migration approval.

Transform rules, applied to the YAML frontmatter only. The body (everything after the closing
`---`) is copied verbatim, unchanged.

- `name:` -- copy through unchanged.
- `description:` -- copy through unchanged.
- `tier:` -- **drop** this key. Look up its value as a table name in
  `$AF_REPO/common/model-tiers.toml`, read that table's `claude` key, and emit a new
  `model: <value>` frontmatter key with that value.
- `access:` -- if present, rename the key to `tools:` (value copied through unchanged). If
  `access:` is absent, do not add a `tools:` key.
- `sandbox:` -- omit this key from the rendered frontmatter (it is not used here).
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

## 4. Hooks in settings.json; native MCP registration

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

Write only the approved hook merge to `$AF_HOME/.claude/settings.json`, preserving unrelated
keys. Do not write MCP server registrations into that settings file.

**MCP servers.** Read the shareable catalog at `$AF_REPO/common/mcp/servers.jsonc`.
Skip disabled catalog entries (`enabled: false` or legacy `disabled: true`); report the default
example as skipped, not connected. Reject contradictory enablement. Remove catalog-only flags
`enabled`, `disabled` and `tools` before native registration; honor a `tools` selection that
excludes `claude`. Resolve literal `$REPO`/`$HOME` paths from the setup inputs rather than
assuming a command executable is launched through a shell.

For an explicitly approved missing entry, verify the installed `claude mcp add-json --help`
contract and use `claude mcp add-json --scope user <name> <json>`. Let the native CLI validate
and own its user configuration. Do not infer storage paths, copy authentication material,
replace existing registrations, or migrate preexisting settings-file data automatically.
If existing-name ownership is uncertain, stop that entry for approval. Do not run
`claude mcp list` as an offline check: the installed command health-checks approved servers.

## 5. Plugins

Register the local plugin marketplace at `$AF_REPO/tools/claude/plugins` with Claude Code
(the directory containing marketplace manifests such as `local-lsp-marketplace/`). Use
whatever mechanism Claude Code exposes for adding a local marketplace path (settings entry or
CLI command) so plugins under that directory become installable/discoverable. Do not manually
copy plugin files into `$AF_HOME/.claude/` -- registering the marketplace path is sufficient.

## 6. Idempotency

Re-running this whole doc must be safe and convergent:

- **Selected framework-managed symlinks** (`CLAUDE.md`, each skill dir): if a selected link
  already points at the correct target, leave it untouched. If a selected managed link points
  elsewhere or is broken, recreate it. Preserve and report external or uncertain links rather
  than replacing them.
- **Rendered agent files**: regenerate only ownership-verified generated
  `$AF_HOME/.claude/agents/<name>.md` files from the current source. Preserve and report an
  external or uncertain agent file instead of overwriting it.
- **settings.json merges**: do not duplicate approved hook entries or overwrite unrelated settings.
- **MCP registrations**: disabled catalog entries remain skipped. Existing native registrations
  remain untouched; a changed or uncertain entry needs explicit approval, not replacement.
- **Plugin marketplace registration**: if already registered, leave it as-is; do not
  double-register.

## 7. Verify

Before reporting done, check:

- Every selected framework skill has a corresponding symlink under
  `$AF_HOME/.claude/skills/` that resolves (not broken). List preserved external or uncertain
  same-name skills separately; do not count them as missing or relink them.
- Every selected framework source agent has a corresponding
  `$AF_HOME/.claude/agents/<name>.md` containing a `model:` key (not `tier:`). List preserved
  external or uncertain agent files separately.
- For at least one read-only agent (e.g. `code-reviewer`), confirm its rendered `tools:` list
  contains no write-capable tool (no `Write`, `Edit`, or similar) -- it should match the
  source `access:` value exactly, unexpanded.
- The selected framework-managed `$AF_HOME/.claude/CLAUDE.md` resolves (symlink is not broken)
  and points at `$AF_REPO/common/instructions/AGENTS.md`. Report a preserved external or
  uncertain instruction file separately instead of replacing it.
- `$AF_HOME/.claude/settings.json` contains the expected approved hooks, not generated MCP keys.
- Report native MCP registrations as approved additions, preserved entries or skipped/unchecked
  items. Do not claim server connectivity from schema/help checks.
- The plugin marketplace at `$AF_REPO/tools/claude/plugins` is registered.

Report a summary: counts of skills linked, agents rendered, hooks merged, MCP servers merged,
plus preserved external or uncertain entries, and any items skipped or failed and why.

## Failure handling

If the filesystem/OS rejects a symlink for a selected framework-managed destination (e.g. no
symlink support, permission denied), fall back to copying the source file/directory to that
destination path instead, and note in the final summary that a copy was used in place of a
symlink for that item (copies will not auto-update on future source edits, unlike symlinks).
