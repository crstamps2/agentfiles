# pi setup (mechanical -- execute, do not design)

You are configuring THIS machine's pi config directory from an agentfiles clone. Execute each
step below verbatim using the injected environment variables: `AF_HOME`, `AF_REPO`, `AF_OS`.

- `AF_HOME` -- the home directory whose pi config you are materializing. pi's config dir is
  `$AF_HOME/.pi/agent` (overridable via `PI_CODING_AGENT_DIR`; if that variable is set in the
  environment, use it verbatim instead of `$AF_HOME/.pi/agent`). Rendered paths below are
  written as `$PI_DIR/...` where `$PI_DIR` is that resolved config directory.
- `AF_REPO` -- the absolute path to the agentfiles checkout (source of everything under
  `common/`).
- `AF_OS` -- `macos` or `linux`. Only affects command syntax you use to create symlinks/copies;
  it does not change what gets linked.

Never invent a path, a file name, or a mapping not listed here. If a step's source path does
not exist, stop that step, report exactly which path was missing, and continue with the
remaining steps. Do not silently skip or substitute.

> **Runtime contract.** This setup requires pi 1.3.0 or later with built-in MCP support.
> Check the installed version and its native MCP documentation before proceeding; stop the
> MCP step on older or unsupported runtimes rather than installing an adapter fallback. The
> renderer's conservative catalog shape guards are not a full native schema validator or a
> compatibility promise for every release at or above this minimum.
>
> - `pi-subagents` provides delegation and reads `$PI_DIR/agents/*.md` (section 3).
> - Built-in MCP reads `$PI_DIR/mcp.json`; no MCP adapter package is required (section 6).
> - The vendored `agentfiles-hooks` extension maps the shared hooks onto pi events.
>
> Eligible ownership-verified instructions and supported skill formats map cleanly onto pi's
> native context-file and Agent-Skills support and can be symlinked.

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

Classify `$PI_DIR` and its parent chain before materializing any entry, not just the final
filename. A missing leaf does not authorize following a directory link. Stop a symlinked,
shared-writable, differently owned, or uncertain configuration path for migration approval.
An approved creation plan can prepare missing directories under a verified owner one component
at a time; verify each directory before continuing. The MCP renderer never prepares directories.

## 1. Instructions

pi loads `AGENTS.md` from its config dir as a global context file. Symlink it:

```
$PI_DIR/AGENTS.md  ->  $AF_REPO/common/instructions/AGENTS.md
```

For a missing or selected framework-managed destination, ensure this path resolves to the target
above. Replace a selected managed symlink that points elsewhere or is broken (see Idempotency,
section 8). If the destination is external or uncertain, including a private override or a real
file without framework provenance, leave it unchanged, report it separately, and stop this entry
for migration approval.

## 2. Skills (symlink each directory)

pi discovers skills from `$PI_DIR/skills/` natively (Agent Skills standard). For every directory
`$AF_REPO/common/skills/<name>/` whose destination is missing or selected framework-managed:

```
$PI_DIR/skills/<name>  ->  $AF_REPO/common/skills/<name>
```

Use one symlink per eligible skill directory, not per file inside it. Create `$PI_DIR/skills/`
only as needed for eligible destinations. Leave an external or uncertain same-name destination
untouched, report it separately, and stop that entry for migration approval. Skip (with a note in
the summary) any entry under `common/skills/` that is not a directory.

## 3. Agents (transform each `*.agent.md` -> `$PI_DIR/agents/<name>.md`)

pi has no native sub-agents; the `pi-subagents` package (installed in section 5) reads agent
definitions from `$PI_DIR/agents/*.md` and spawns an isolated `pi` child session per delegated
task. Each agent file is markdown with YAML frontmatter the package understands: `name`,
`description`, `tools` (comma-separated), `model`, `thinking`. The body is the agent's system
prompt. (`pi-subagents` also supports richer optional keys -- `aliases`, `systemPromptMode`,
`inheritProjectContext`, `inheritSkills`, `defaultReads`, `memory` -- but agentfiles sources do
not currently declare them, so this transform does not emit them.)

Source agents live at `$AF_REPO/common/agents/<name>.agent.md`. For each source agent whose
destination is missing or selected framework-managed, render `$PI_DIR/agents/<name>.md` (plain
`.md`, not a symlink -- this is a transform, not a copy). Leave an external or uncertain
destination untouched, report it separately, and stop that agent for migration approval. Create
`$PI_DIR/agents/` only as needed for eligible destinations.

Transform rules, applied to the YAML frontmatter only. The body (everything after the closing
`---`) is copied verbatim into the rendered file's body, unchanged.

- `name:` -- copy through unchanged.
- `description:` -- copy through unchanged.
- `tier:` -- **drop** this key. Look up its value as a table name in
  `$AF_REPO/common/model-tiers.toml`. Read that table's `pi_model` key and emit `model: <value>`;
  read that table's `pi_thinking` key and emit `thinking: <value>` (`pi-subagents` treats
  `model` and `thinking` as separate frontmatter fields).
- `access:` -- controls the rendered `tools:` key by mapping Claude-style tool names to pi's
  built-in tool names (see mapping table below):
  - If `access:` is **absent**, do **not** emit a `tools:` key. The agent then gets pi's full
    default tool set (`read, bash, edit, write, grep, find, ls`) -- parity with the "all tools"
    default this same agent source gets in the other tools.
  - If `access:` is **present**, translate each listed tool through the mapping table, drop
    any tool with no pi equivalent (noting it in the summary), de-duplicate, and emit
    `tools: <comma-separated pi tool names>`. A read-only source `access:` (no write/edit tool)
    therefore renders a read-only pi tool list, and the spawned `pi --tools <list>` process is
    constrained to exactly those tools.
- `sandbox:` -- omit (pi enforces tool restriction via the `tools:` allowlist, not a sandbox
  mode); note the omission only if present.
- Any other frontmatter key (e.g. `permissionMode:`, Claude's `memory:` shape) -- omit it; these
  are Claude/Codex-specific and have no clean `pi-subagents` equivalent in the agentfiles source.
  Note omitted keys in the summary.

**Tool name mapping (Claude-style `access:` -> pi built-in tool):**

| `access:` token         | pi tool  |
|-------------------------|----------|
| `Read`                  | `read`   |
| `Grep`                  | `grep`   |
| `Glob`                  | `find`   |
| `Bash`                  | `bash`   |
| `Write`                 | `write`  |
| `Edit`                  | `edit`   |
| `NotebookEdit`          | `edit`   |
| `WebFetch`, `WebSearch` | (no pi built-in -- drop, note in summary) |
| `*`, `All tools`        | (drop `tools:` entirely -> full default set) |

If a `tier:` value has no matching table in `model-tiers.toml`, or the matching table has no
`pi_model`/`pi_thinking` key, stop that agent's render, report the missing mapping, and move on
to the next agent.

> **Builtin-name collisions.** `pi-subagents` ships builtin agents (`scout`, `planner`,
> `reviewer`, `worker`, `researcher`, `oracle`, `context-builder`, `delegate`). A user-level
> agent rendered under `$PI_DIR/agents/` with the same `name` overrides the builtin. agentfiles'
> roster uses distinct names (`code-reviewer`, `rails-engineer`, ...) so no override occurs; if
> you add an agent whose name matches a builtin, that is an intentional override -- note it in
> the summary.

### Worked example: read-only agent (declared `access:`, no write tools)

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
pi_model = "openai/gpt-5.6-terra"
pi_thinking = "xhigh"
```

Rendered output, `$PI_DIR/agents/code-reviewer.md` frontmatter (body unchanged below the
second `---`):

```yaml
---
name: code-reviewer
description: Expert code reviewer. Use proactively after code changes to review for quality, security, performance, and adherence to project conventions. Read-only -- never modifies code.
tools: read, grep, find, bash
model: openai/gpt-5.6-terra
thinking: xhigh
---
```

Note `tier: specialist` became `model: openai/gpt-5.6-terra` + `thinking: xhigh`; `access: Read, Grep, Glob, Bash` mapped
to `tools: read, grep, find, bash` (no write-capable tool); `permissionMode`/`memory` were
dropped.

### Worked example: write-capable agent (absent `access:`)

Input, `$AF_REPO/common/agents/rails-engineer.agent.md` frontmatter (abridged):

```yaml
---
name: rails-engineer
description: Senior Ruby on Rails engineer for backend implementation.
tier: specialist-execution
---
```

No `access:` key -> no `tools:` key rendered (full default tool set). Rendered output,
`$PI_DIR/agents/rails-engineer.md`:

```yaml
---
name: rails-engineer
description: Senior Ruby on Rails engineer for backend implementation.
model: openai/gpt-5.6-terra
thinking: max
---
```

## 4. Orchestrator model defaults -> settings.json

Target file: `$PI_DIR/settings.json`. Create it as `{}` first if it does not exist. Load it as
JSON.

Set the interactive (top-level orchestrator) model from the `orchestrator` tier in
`$AF_REPO/common/model-tiers.toml`:

- When `pi_model` is `provider/model`, split it at the first slash: set `defaultProvider` to
  `provider` and `defaultModel` to `model`. Never write the full `provider/model` string into
  `defaultModel` while also retaining `defaultProvider`; that constructs an invalid doubled model.
- When `pi_model` has no slash, set only `defaultModel` and retain the existing `defaultProvider`.
- `defaultThinkingLevel` = that table's `pi_thinking` value.

Do not overwrite unrelated settings. The top-level model and `subagents.defaultModel` are
independent: the latter must contain one registered `provider/model` identifier, not a doubled
provider or an assumed CLI/plugin namespace. When absent, use the verified `specialist.pi_model`
as the builtin-agent fallback. Preserve valid explicit custom overrides. If an existing fallback
uses an unavailable provider, report its exact identifier and propose the registered tier value
for scoped repair; never rename every occurrence of a provider-like string or plugin name.

## 5. Subagent package + hooks extension

### 5a. `pi-subagents` package (agent delegation)

Install the delegation package that provides the `subagent` tool and consumes the agent roster
rendered in section 3:

```
pi install npm:pi-subagents
```

Run that command via the shell. It is idempotent -- if `pi-subagents` is already installed (check
`pi list`), skip the install and note it. `pi-subagents` reads user-level agents from
`$PI_DIR/agents/*.md` (and `~/.agents/*.md`) by default, so the agents rendered in section 3 are
picked up automatically. It ships its own builtin agents and workflow prompt shortcuts; leave
those as-is (see the builtin-name-collision note in section 3).

If `pi install` is unavailable in this environment, fall back to adding `"pi-subagents"` to the
`packages` array in `$PI_DIR/settings.json` (create the array if absent, do not duplicate the
entry) so pi installs it on next trusted startup, and note the fallback in the summary.

### 5b. `agentfiles-hooks` extension (hooks bridge)

pi loads extensions from `$PI_DIR/extensions/`. For a missing or selected framework-managed
destination, symlink the hooks extension vendored in this repo (so edits to the repo take effect
without re-running setup):

```
$PI_DIR/extensions/agentfiles-hooks  ->  $AF_REPO/tools/pi/extensions/agentfiles-hooks
```

Create `$PI_DIR/extensions/` only as needed for this eligible destination. Leave an external or
uncertain `agentfiles-hooks` extension untouched, report it separately, and stop that entry for
migration approval. This extension reads `$AF_REPO/common/hooks/hooks.manifest.jsonc` (the same
tool-neutral manifest Claude Code and Codex CLI consume) and runs its `SessionStart` scripts once
per session and `Stop` scripts when the agent settles, injecting each script's stdout into the
conversation as context. It discovers the repo root from its own file location, so no extra
configuration is needed.

### 5c. Foreground interactive lane

On macOS, when the agent-loop runner and `~/.local/bin/agent-loop-python` launcher are installed
and the destination is missing or selected framework-managed, link:

```
$PI_DIR/extensions/interactive-lane  ->  $AF_REPO/tools/pi/extensions/interactive-lane
```

A direct user message in a TUI pane takes foreground ownership of its tracked worktree, outside
cohort capacity. Coordinator startup, extension wakeups, RPC and headless workers never claim it.
The workspace stays interactive across reloads and exits; `/agent-loop-auto` explicitly returns it
to the background queue. Existing panes need `/reload` once after installing this extension.
If the launcher is absent, report this extension as skipped rather than installing a broken hook.
If an existing `interactive-lane` extension is external or uncertain, leave it untouched, report
it separately, and stop that entry for migration approval.

## 6. Built-in MCP servers

### 6a. Select the built-in implementation

Built-in MCP must remain enabled. Preserve unrelated package and extension settings.
If an extension registers `/mcp` (including `pi-mcp-adapter`), it replaces the builtin in
sessions even though shell `pi mcp` commands still use the builtin. If such a replacement or
`-builtin:mcp` is configured, stop this step and request a scoped migration. Do not install
an adapter, remove unrelated packages, restart sessions, or import credentials automatically.

### 6b. Preserve user configuration

Native user configuration is `$PI_DIR/mcp.json`, with servers under `mcpServers`. Trusted
projects can additionally define `.pi/mcp.json`; do not generate project overrides here.
Personal servers and private hosts belong in the user file, never this public catalog.

Use the renderer for the shareable catalog forms it supports:

```bash
node "$AF_REPO/tools/pi/scripts/render-mcp.mjs" \
  "$AF_REPO/common/mcp/servers.jsonc" "$PI_DIR/mcp.json" "$AF_REPO" "$AF_HOME"
```

It expands catalog `$REPO`/`$HOME` paths, preserves runtime `${ENV_VAR}` interpolation,
and translates legacy catalog disablement to native `enabled: false`. The shared example
retains compatibility metadata for other consumers; native Pi output omits `disabled`.
Before creating a missing target, it applies conservative offline shape guards to the catalog
forms it supports. A guard failure stops before file creation. These guards do not validate every
native field or promise compatibility with future Pi schemas; the installed runtime remains the
version-specific authority.

- A missing target that passes the supported-shape guards is created with mode `600` only in
  an already prepared canonical parent chain. The renderer rejects parent symlinks, differently
  owned directories, and group/other-writable directories within the user's tree. Root-owned
  system ancestors above that tree are permitted. Missing parents require separately approved
  ownership preparation, not recursive creation; the disabled example is not connected.
- These POSIX checks do not attest ACLs or provide a race-free sandbox against a malicious
  same-account process. The operator must trust the canonical path's ancestors and exclude
  concurrent directory replacement; otherwise stop rather than claim safe setup.
- An identical existing target is left byte-for-byte unchanged. A native-valid existing JSON
  configuration with a UTF-8 BOM is parsed with the BOM removed only for parsing, then remains
  byte-for-byte unchanged on a no-op. Target symlinks, hard links, non-regular files and files
  owned by another account are refused before reading their contents; stop for ownership approval
  rather than following a link into a credential store.
- A different same-name server or native hyphen/underscore alias is a collision, not permission
  to overwrite. Stop and report the server identifier only.
- Any other merge into an existing file also requires explicit approval; the renderer leaves
  the entire file unchanged. Present a redacted metadata-level proposal, then use native
  `pi mcp add` or `/mcp` only for the approved entries. Never replace `mcpServers`, prune
  personal entries, or silently change existing exposure/enabled state.

### 6c. Authentication and verification boundaries

Native OAuth is managed through `/mcp login <server>` or `pi mcp login <server>` and stored
in `$PI_DIR/mcp-auth.json`. Do not read or render this credential file. Native MCP does not
automatically read an adapter's `~/.config/mcp/mcp.json`; leave any legacy file untouched,
and let the user authorize migration of individual server definitions. Do not copy tokens
from other clients, inspect browser accounts, or initiate sign-in during setup.

Use the installed runtime's native schema validator for offline checks where available; it is
the authoritative version-specific check. The renderer's guards cover only the documented
catalog forms above. `pi mcp list` connects every enabled server; it is not an offline validation
command.
Only run connection/login checks after the user approves those side effects. External
changes take effect with a user-controlled `/reload`; do not reload busy panes automatically.

## 7. Packages / plugins

Discover sibling tool-plugin marketplace directories in this repo: glob `$AF_REPO/tools/*/plugins`,
excluding this doc's own tool (`$AF_REPO/tools/pi/plugins`, which does not exist). For each
marketplace manifest found:

- If a real pi package equivalent exists (an npm/git pi-package providing the same capability),
  install it with `pi install <source>` (or add it to the `packages` array in
  `$PI_DIR/settings.json`). Do not copy plugin files by hand.
- Where no pi equivalent exists (e.g. a Claude-specific LSP plugin marketplace), do not
  approximate it. List it by name in the final summary as skipped, with the reason "no pi
  equivalent."

## 8. Idempotency

Re-running this whole doc must be safe and convergent:

- **Selected framework-managed symlinks** (`AGENTS.md`, each skill dir, the
  `agentfiles-hooks` extension dir): if a selected link already points at the correct target,
  leave it untouched. If a selected managed link points elsewhere or is broken, recreate it.
  Preserve and report external or uncertain links rather than replacing them.
- **`pi-subagents` package**: skip installation when already present. Do not add an MCP adapter.
- **`$PI_DIR/mcp.json`**: create a missing file with the preserving renderer only after its
  supported-shape guards pass. An identical existing configuration, including a native-valid
  UTF-8-BOM configuration, is unchanged; any required merge or collision stops this step for
  explicit approval. Never replace the existing `mcpServers` map or remove personal entries.
- **Authentication and legacy files**: never read, generate, import, or overwrite credential
  files, including `mcp-auth.json` and any legacy adapter secrets file.
- **Rendered agent files**: regenerate only ownership-verified generated
  `$PI_DIR/agents/<name>.md` files from the current source. Preserve and report an external or
  uncertain agent file instead of overwriting it.
- **settings.json**: re-applying tier-driven `defaultModel`/`defaultThinkingLevel` must not
  disturb unrelated keys.
- **Packages/MCP**: if already installed/registered, leave as-is; do not double-register.

## 9. Verify

Before reporting done, check:

- Every selected framework skill has a corresponding symlink under `$PI_DIR/skills/` that
  resolves (not broken). List preserved external or uncertain same-name skills separately; do
  not count them as missing or relink them.
- Every selected framework source agent has a corresponding `$PI_DIR/agents/<name>.md`
  containing a `model:` key (not `tier:`). List preserved external or uncertain agent files
  separately.
- For at least one read-only agent (e.g. `code-reviewer`), confirm its rendered `tools:` list
  contains no write-capable tool (no `write` or `edit`) -- the spawned subagent process will be
  constrained to that allowlist.
- For at least one agent with no `access:` key (e.g. `rails-engineer`), confirm the rendered file
  has **no** `tools:` key (full default tool set).
- The selected framework-managed `$PI_DIR/AGENTS.md` resolves (symlink not broken) and points
  at `$AF_REPO/common/instructions/AGENTS.md`. Report a preserved external or uncertain
  instruction file separately instead of replacing it.
- `pi-subagents` is registered, and built-in MCP is not disabled or replaced by an extension.
- `$PI_DIR/mcp.json` parses. Report the supported-shape guard outcome, source entries that
  require a separately approved merge, and the limitation that this is not complete future-schema
  validation. The example is disabled using native `enabled: false`, not an adapter-only flag.
- Existing personal server entries, top-level configuration and credential files are preserved.
  No private host, token, header value or authentication material was copied into this repo.
- The selected framework-managed `$PI_DIR/extensions/agentfiles-hooks` resolves and contains an
  `index.ts`; report a preserved external or uncertain extension separately.
- When the agent-loop launcher is installed on macOS, the selected framework-managed
  `$PI_DIR/extensions/interactive-lane` resolves and contains an `index.ts`; report a preserved
  external or uncertain extension separately.
- `$PI_DIR/settings.json` is valid JSON with `defaultModel` / `defaultThinkingLevel` set from the
  `orchestrator` tier.

Report skills linked, agents rendered, delegation package status, hook links, built-in MCP
selection, catalog supported-shape-guard plus creation/no-op/approval-required outcome,
preserved external or uncertain entries, and skipped or failed steps. Separate offline schema
checks from user-approved connection checks; do not expose personal server arguments,
environment/header values, authentication files or private logs.

## Failure handling

If the filesystem/OS rejects a symlink for a selected framework-managed destination (e.g. no
symlink support, permission denied), fall back to copying the source file/directory to that
destination path instead, and note in the final summary that a copy was used in place of a
symlink for that item (copies will not auto-update on future source edits, unlike symlinks).
