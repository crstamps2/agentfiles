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

> **Why pi differs from Claude Code / Codex CLI.** pi ships a minimal core with *no* built-in
> sub-agents, MCP, or hooks -- those are all provided by extensions/packages ("adapt pi to your
> workflows"). So where the Claude/Codex setups render native config, the pi setup:
>
> - installs the **`pi-subagents`** npm package, which provides the `subagent` delegation tool
>   and reads the agentfiles agent roster from `$PI_DIR/agents/*.md` (rendered in section 3);
> - installs the **`pi-mcp-adapter`** npm package, which exposes the MCP servers from
>   `common/mcp/servers.jsonc` through a single lazy proxy tool (rendered into `$PI_DIR/mcp.json`
>   in section 6);
> - symlinks the **`agentfiles-hooks`** extension (vendored in this repo under
>   `tools/pi/extensions/`), which reproduces the tool-neutral hooks behavior on pi's event API.
>
> Instructions and skills, by contrast, map cleanly onto pi's native context-file and
> Agent-Skills support and are just symlinked.

## 1. Instructions

pi loads `AGENTS.md` from its config dir as a global context file. Symlink it:

```
$PI_DIR/AGENTS.md  ->  $AF_REPO/common/instructions/AGENTS.md
```

If `$PI_DIR/AGENTS.md` already exists as a symlink pointing elsewhere, remove it and recreate it
pointing at the correct target (see Idempotency, section 8). If it exists as a real file (not a
symlink), stop and report -- do not overwrite a user's real file.

## 2. Skills (symlink each directory)

pi discovers skills from `$PI_DIR/skills/` natively (Agent Skills standard). For every directory
`$AF_REPO/common/skills/<name>/`:

```
$PI_DIR/skills/<name>  ->  $AF_REPO/common/skills/<name>
```

One symlink per skill directory (not per file inside it). Create `$PI_DIR/skills/` first if it
does not exist. Skip (with a note in the summary) any entry under `common/skills/` that is not a
directory.

## 3. Agents (transform each `*.agent.md` -> `$PI_DIR/agents/<name>.md`)

pi has no native sub-agents; the `pi-subagents` package (installed in section 5) reads agent
definitions from `$PI_DIR/agents/*.md` and spawns an isolated `pi` child session per delegated
task. Each agent file is markdown with YAML frontmatter the package understands: `name`,
`description`, `tools` (comma-separated), `model`, `thinking`. The body is the agent's system
prompt. (`pi-subagents` also supports richer optional keys -- `aliases`, `systemPromptMode`,
`inheritProjectContext`, `inheritSkills`, `defaultReads`, `memory` -- but agentfiles sources do
not currently declare them, so this transform does not emit them.)

Source agents live at `$AF_REPO/common/agents/<name>.agent.md`. For each one, render
`$PI_DIR/agents/<name>.md` (plain `.md`, not a symlink -- this is a transform, not a copy).
Create `$PI_DIR/agents/` first if it does not exist.

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
pi_model = "sonnet"
pi_thinking = "medium"
```

Rendered output, `$PI_DIR/agents/code-reviewer.md` frontmatter (body unchanged below the
second `---`):

```yaml
---
name: code-reviewer
description: Expert code reviewer. Use proactively after code changes to review for quality, security, performance, and adherence to project conventions. Read-only -- never modifies code.
tools: read, grep, find, bash
model: sonnet
thinking: medium
---
```

Note `tier: specialist` became `model: sonnet` + `thinking: medium`; `access: Read, Grep, Glob, Bash` mapped
to `tools: read, grep, find, bash` (no write-capable tool); `permissionMode`/`memory` were
dropped.

### Worked example: write-capable agent (absent `access:`)

Input, `$AF_REPO/common/agents/rails-engineer.agent.md` frontmatter (abridged):

```yaml
---
name: rails-engineer
description: Senior Ruby on Rails engineer for backend implementation.
tier: specialist
---
```

No `access:` key -> no `tools:` key rendered (full default tool set). Rendered output,
`$PI_DIR/agents/rails-engineer.md`:

```yaml
---
name: rails-engineer
description: Senior Ruby on Rails engineer for backend implementation.
model: sonnet
thinking: medium
---
```

## 4. Orchestrator model defaults -> settings.json

Target file: `$PI_DIR/settings.json`. Create it as `{}` first if it does not exist. Load it as
JSON.

Set the interactive (top-level orchestrator) model from the `orchestrator` tier in
`$AF_REPO/common/model-tiers.toml`:

- `defaultModel` = that table's `pi_model` value.
- `defaultThinkingLevel` = that table's `pi_thinking` value.

Do not overwrite any other existing keys in `settings.json`. If the user already set
`defaultModel`/`defaultThinkingLevel` to different values, update them to the tier values (these
are generated, tier-driven settings) but leave everything else untouched.

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

pi loads extensions from `$PI_DIR/extensions/`. Symlink the hooks extension vendored in this repo
(so edits to the repo take effect without re-running setup):

```
$PI_DIR/extensions/agentfiles-hooks  ->  $AF_REPO/tools/pi/extensions/agentfiles-hooks
```

Create `$PI_DIR/extensions/` first if it does not exist. This extension reads
`$AF_REPO/common/hooks/hooks.manifest.jsonc` (the same tool-neutral manifest Claude Code and
Codex CLI consume) and runs its `SessionStart` scripts once per session and `Stop` scripts when
the agent settles, injecting each script's stdout into the conversation as context. It discovers
the repo root from its own file location, so no extra configuration is needed.

## 6. MCP servers (via `pi-mcp-adapter`)

pi has **no built-in MCP support** by design (it favors CLI tools + skills). The
[`pi-mcp-adapter`](https://www.npmjs.com/package/pi-mcp-adapter) package bridges the gap: it
exposes all configured MCP servers through one lazy `mcp` proxy tool (~200 tokens) instead of
dumping every server's tool definitions into context, and only starts a server when a tool is
actually called. This is the pi-native way to reuse the MCP servers you run under Claude Code /
Codex CLI.

### 6a. Install the adapter

```
pi install npm:pi-mcp-adapter
```

Run via the shell. Idempotent -- if already present in `pi list`, skip and note it. If
`pi install` is unavailable, fall back to adding `"pi-mcp-adapter"` to the `packages` array in
`$PI_DIR/settings.json` (no duplicates) and note the fallback.

### 6b. Two-channel MCP config: public catalog vs. local secrets

MCP servers split into two categories, and each has its own home. **This split is a hard
security boundary, not a style preference** -- `common/` is a public repo gated by
`common/scripts/agentfiles-scan.sh` (deny-list + secret scanner), so anything with a real token,
credential, OAuth client secret, or an org-internal hostname must never be rendered from it.

The adapter reads *both* of these files and merges them (the local file wins on precedence):

| Channel | File | Contents | In git? |
|---------|------|----------|---------|
| **Public catalog** | `$PI_DIR/mcp.json` (rendered from `common/mcp/servers.jsonc`) | Shareable servers only: `$HOME`/`$REPO` placeholders, `${ENV_VAR}` interpolation, public URLs. No secrets, no internal hostnames. | yes (source) |
| **Local secrets** | `~/.config/mcp/mcp.json` (hand-authored, `chmod 600`) | Personal servers with inline tokens, OAuth, or internal/private hosts. | **no** |

#### Render the public catalog -> `$PI_DIR/mcp.json`

Render the shareable catalog from `$AF_REPO/common/mcp/servers.jsonc` so pi uses the *same*
single source of truth as the Claude/Codex MCP config (rather than importing from the derived
`~/.claude` / `~/.codex` trees):

1. Read `$AF_REPO/common/mcp/servers.jsonc` and strip `//` comments (it is JSONC). It is a flat
   object keyed by server name, each value holding that server's config (`command`, `args`,
   `env`, `url`, `headers`, `disabled`, etc.).
2. In every value, substitute the literal string `$REPO` with `$AF_REPO`; leave `$HOME` and any
   `${ENV_VAR}` tokens as-is (the adapter/shell resolves them at launch).
3. Wrap the whole map under a top-level `mcpServers` key and write it to `$PI_DIR/mcp.json` as
   valid JSON:

   ```json
   {
     "mcpServers": {
       "<server-name>": { "command": "...", "args": ["..."], "env": {} }
     }
   }
   ```

   If `$PI_DIR/mcp.json` already exists, load it, replace its `mcpServers` object with the
   freshly rendered one (these are generated from source), and preserve any other top-level keys
   the adapter owns (e.g. `settings`). Do not preserve stale server entries that are no longer in
   `servers.jsonc`.

The default `servers.jsonc` ships only a `"disabled": true` placeholder `example-server`;
rendering it is harmless (the `disabled` flag stops the adapter from ever trying to connect).
Note in the summary that it is a disabled placeholder.

#### Local secrets -> `~/.config/mcp/mcp.json` (do NOT generate from `common/`)

Servers that carry secrets or internal hosts (databases behind a VPN, PATs, OAuth) are the ones
you already run under Claude Code / Codex CLI. Do **not** copy them into `common/mcp/servers.jsonc`
-- that would leak them into the public repo and trip the scanner. Instead they live in a local,
`chmod 600`, non-repo file at `~/.config/mcp/mcp.json`, which the adapter reads at higher
precedence than the rendered catalog. Setup does **not** own or overwrite this file.

This setup step must not write secrets. Handle the local file as follows:

- If `~/.config/mcp/mcp.json` already exists, leave it untouched and note it in the summary.
- If it does not exist and the machine has host-specific MCP configs (`~/.claude.json`,
  `~/.codex/config.toml`, etc.) with secret-bearing servers, do **not** silently copy secrets.
  Report that personal servers were found and point the user at either:
  - the adapter's own interactive importer -- `/mcp setup` or
    `pi-mcp-adapter init --discover-host-configs` -- which previews and adopts host configs
    with the user's consent; or
  - hand-authoring `~/.config/mcp/mcp.json` (same `{ "mcpServers": { ... } }` shape as the public
    catalog, `chmod 600`), keeping tokens inline locally or via `${ENV_VAR}` / OS keychain.
- Prefer `${ENV_VAR}` interpolation for tokens where an env var is actually populated; only inline
  a literal token when it exists nowhere else (e.g. already inline in `~/.claude.json`) and the
  local file is `chmod 600` -- this is a lateral move at the same local-plaintext security level,
  never an addition to the repo.

HTTP/OAuth servers (bare `url`, no inline secret) are repo-safe in principle, but if their URL is
an internal/private host they still belong in the local file. Public OAuth endpoints
(e.g. `https://mcp.figma.com/mcp`) may go in either file; if placed in the local file they
authenticate via `/mcp-auth <server>` (browser flow, credentials stored in the OS keychain).

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

- **Symlinks** (`AGENTS.md`, each skill dir, the `agentfiles-hooks` extension dir): if the
  symlink already exists and points at the correct target, leave it untouched. If it exists and
  points elsewhere (or is a broken symlink), remove and recreate it. Never leave two competing
  links for the same name.
- **`pi-subagents` / `pi-mcp-adapter` packages**: if already installed (`pi list`), leave as-is;
  do not reinstall.
- **`$PI_DIR/mcp.json`** (public catalog): always regenerate the `mcpServers` object from
  `common/mcp/servers.jsonc` and overwrite it in place; preserve unrelated adapter-owned top-level
  keys.
- **`~/.config/mcp/mcp.json`** (local secrets): never generated or overwritten by setup; if it
  exists, leave it untouched.
- **Rendered agent files**: always regenerate `$PI_DIR/agents/<name>.md` from the current source
  and overwrite it in place -- these are generated artifacts, not hand-edited files.
- **settings.json**: re-applying tier-driven `defaultModel`/`defaultThinkingLevel` must not
  disturb unrelated keys.
- **Packages/MCP**: if already installed/registered, leave as-is; do not double-register.

## 9. Verify

Before reporting done, check:

- Every directory under `$AF_REPO/common/skills/` has a corresponding symlink under
  `$PI_DIR/skills/` that resolves (not broken).
- Every `$AF_REPO/common/agents/<name>.agent.md` has a corresponding `$PI_DIR/agents/<name>.md`
  containing a `model:` key (not `tier:`).
- For at least one read-only agent (e.g. `code-reviewer`), confirm its rendered `tools:` list
  contains no write-capable tool (no `write` or `edit`) -- the spawned subagent process will be
  constrained to that allowlist.
- For at least one agent with no `access:` key (e.g. `rails-engineer`), confirm the rendered file
  has **no** `tools:` key (full default tool set).
- `$PI_DIR/AGENTS.md` resolves (symlink not broken) and points at
  `$AF_REPO/common/instructions/AGENTS.md`.
- `pi list` shows `pi-subagents` and `pi-mcp-adapter` installed (or `$PI_DIR/settings.json`
  `packages` contains them if the install fell back to settings).
- `$PI_DIR/mcp.json` is valid JSON with an `mcpServers` object containing every server from
  `common/mcp/servers.jsonc` (with `$REPO` resolved to `$AF_REPO`).
- No secret or internal hostname was written into `$PI_DIR/mcp.json` or `common/`; any such server
  is in `~/.config/mcp/mcp.json` (local, `chmod 600`) or was left to the user to import.
- `$PI_DIR/extensions/agentfiles-hooks` resolves and contains an `index.ts`.
- `$PI_DIR/settings.json` is valid JSON with `defaultModel` / `defaultThinkingLevel` set from the
  `orchestrator` tier.

Report a summary: counts of skills linked, agents rendered, the `pi-subagents` and
`pi-mcp-adapter` install results, the `agentfiles-hooks` link result, public MCP servers rendered
into `$PI_DIR/mcp.json`, whether a local `~/.config/mcp/mcp.json` was found (never its contents),
plugins mapped vs. skipped, plus any items skipped or failed and why.

## Failure handling

If the filesystem/OS rejects a symlink (e.g. no symlink support, permission denied), fall back to
copying the source file/directory to the destination path instead, and note in the final summary
that a copy was used in place of a symlink for that item (copies will not auto-update on future
source edits, unlike symlinks).
