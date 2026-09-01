# agentfiles

One tool-neutral source of truth for AI agent-orchestration config -- agent
roles, skills, hooks, and MCP servers -- usable by **Claude Code, Codex CLI,
and pi**.

## What it is

`agentfiles` holds your orchestration config once, in a tool-neutral format
under `common/`, and renders it into whichever coding CLI you're using. The
same agent roster, skills, and instructions back `~/.claude`, `~/.codex`, and
`~/.pi/agent` -- no duplicated, drifting copies per tool.

## Why

If you use more than one agentic CLI, you've felt this: an agent definition
gets tuned in one tool and the other quietly falls behind. `agentfiles`
fixes that by making `common/` the only place you edit. Both tools consume
the same source and stay consistent.

## Quick start

Pick your tool and run the one-liner. It clones (or updates) this repo to
`~/.agentfiles` and has your tool's own AI execute that tool's setup steps.

**Claude Code:**

```bash
curl -fsSL https://raw.githubusercontent.com/crstamps2/agentfiles/main/bootstrap.sh | bash -s -- --tool=claude
```

**Codex CLI:**

```bash
curl -fsSL https://raw.githubusercontent.com/crstamps2/agentfiles/main/bootstrap.sh | bash -s -- --tool=codex
```

**pi:**

```bash
curl -fsSL https://raw.githubusercontent.com/crstamps2/agentfiles/main/bootstrap.sh | bash -s -- --tool=pi
```

**Clone-and-run alternative** (if you'd rather inspect the script first, or
already have a checkout):

```bash
git clone https://github.com/crstamps2/agentfiles.git
cd agentfiles
./bootstrap.sh --tool=claude   # or --tool=codex, --tool=pi
```

## Layout

```
common/                   # Source of truth -- tool-neutral, edit here
  instructions/AGENTS.md  # Orchestration rules, role boundaries, conventions
  agents/*.agent.md       # Agent role definitions (tool-neutral frontmatter)
  skills/                 # Orchestration skills (standup, retro, audit, spinup, spindown)
  hooks/                  # Shared hook scripts + hooks.manifest.jsonc
  mcp/servers.jsonc       # MCP server catalog
  model-tiers.toml        # Tier -> concrete model, per tool
  scripts/agentfiles-scan.sh  # Deny-list + secret scan for public pushes

tools/
  claude/setup.md         # Mechanical setup spec: common/ -> ~/.claude
  codex/setup.md          # Mechanical setup spec: common/ -> ~/.codex
  pi/setup.md             # Mechanical setup spec: common/ -> ~/.pi/agent
  pi/extensions/          # pi extensions that reproduce hooks on pi's event API
  claude/plugins/         # Local Claude plugin marketplace (example: LSP)
  codex/openai.yaml.tmpl  # Template for per-skill Codex metadata

bootstrap.sh              # Entry point: resolve env, sync repo, dispatch setup
```

`~/.claude/`, `~/.codex/`, and `~/.pi/agent/` are **derived output**, not source. Don't
hand-edit files under them directly -- edits get overwritten the next time
you run `bootstrap.sh`. Change `common/` (or the relevant `tools/<tool>/`
spec) instead, then re-run bootstrap.

## How it works

1. **`bootstrap.sh` resolves the environment.** It figures out `$HOME`, the
   clone directory (default `~/.agentfiles`, override with `--home=` /
   `--repo=`), and the OS (`macos`/`linux`), then clones or `pull --ff-only`s
   the repo.
2. **It dispatches to the tool's own AI.** `bootstrap.sh --tool=<claude|codex|pi>`
   invokes that CLI with an instruction to read and mechanically execute
   `tools/<tool>/setup.md` -- the CLI configures itself using its own
   filesystem/shell access, with `AF_HOME`, `AF_REPO`, and `AF_OS` injected
   as environment variables. (pi is invoked in non-interactive print mode,
   `pi -a -p`.)
3. **Setup is symlink-first, with a transform step for agent definitions.**
   Shared artifacts (instructions, skills) are symlinked straight from
   `common/` into the tool's home directory, so edits to `common/` show up
   immediately without re-running setup. Agent definitions
   (`common/agents/*.agent.md`) can't be symlinked as-is -- each tool has its
   own frontmatter shape (Claude wants `.md` with a `model:` key, Codex wants
   `.toml` with `model` / `model_reasoning_effort`, pi wants `.md` with
   `model` / `thinking` keys read by the `pi-subagents` package) -- so setup
   renders a per-tool copy, looking up each agent's `tier:` in
   `model-tiers.toml` to pick the concrete model. See `tools/claude/setup.md`,
   `tools/codex/setup.md`, and `tools/pi/setup.md` for the exact transform
   rules.

**Note on pi.** pi's core has no built-in sub-agents, MCP, or hooks -- it
favors a minimal core extended by packages/extensions. So the pi setup installs
the [`pi-subagents`](https://www.npmjs.com/package/pi-subagents) package for the
agent roster (`subagent` delegation tool), the
[`pi-mcp-adapter`](https://www.npmjs.com/package/pi-mcp-adapter) package to
expose your `common/mcp/servers.jsonc` servers through one lazy proxy tool, and
symlinks a small vendored `agentfiles-hooks` extension that runs the same
`common/hooks/` manifest on pi's event API. Instructions and skills map onto
pi's native context-file and Agent-Skills support and are symlinked directly.

## Updating

Re-run the same one-liner (or `git -C ~/.agentfiles pull --ff-only &&
~/.agentfiles/bootstrap.sh --tool=<claude|codex|pi>` if you cloned manually).
Bootstrap is idempotent: symlinks are recreated only if they point at the
wrong target, rendered agent files are regenerated in place, and
hook/MCP-server merges skip entries that are already present. Nothing is
duplicated by re-running it.

## Supported platforms & prerequisites

- **macOS or Linux.** `bootstrap.sh` detects the OS via `uname` and adjusts
  symlink/copy syntax accordingly; there's no behavior difference otherwise.
- **`git`**, to clone/update the repo.
- **One of the CLIs**: [Claude Code](https://docs.anthropic.com/en/docs/claude-code),
  [Codex CLI](https://github.com/openai/codex), or [pi](https://pi.dev),
  reachable on `$PATH` as `claude`, `codex`, or `pi` respectively (bootstrap
  invokes it directly).
- **For pi:** an internet connection on first run so the setup can
  `pi install` the `pi-subagents` (agent delegation) and `pi-mcp-adapter`
  (MCP bridge) packages.

## Customizing

Everything you add or edit lives under `common/`. Every tool picks it up next
time you run `bootstrap.sh`.

**Add or edit an agent** -- create/edit `common/agents/<name>.agent.md`.
Frontmatter needs at minimum `name:`, `description:`, and `tier:` (one of
the tables in `model-tiers.toml`); `access:` is optional and lists the
tools the agent may use. The body (below the closing `---`) is the agent's
system prompt/instructions, copied verbatim into every tool's rendered
output.

**Add or edit a skill** -- create a new directory under `common/skills/<name>/`
with a `SKILL.md`. Every tool's setup step symlinks the whole directory in,
one symlink per skill.

**Add or edit a hook** -- drop a script under `common/hooks/`, then add an
entry for it in `common/hooks/hooks.manifest.jsonc` under the relevant event
key (`SessionStart`, `Stop`, etc.). Claude Code and Codex CLI merge the
manifest into their own hook config on the next setup run; pi runs it via the
`agentfiles-hooks` extension, which maps `SessionStart`/`Stop` onto pi's event
API (extend that extension if you start using `PreToolUse`/`PostToolUse`/
`UserPromptSubmit`).

**Add or edit an MCP server.** MCP config splits into two channels by
sensitivity:

- **Shareable servers** go in `common/mcp/servers.jsonc`, keyed by server
  name, with `command`/`args`/`env`. Use `$REPO`/`$HOME` placeholders and
  `${ENV_VAR}` interpolation -- **no inline secrets or internal hostnames**
  (this repo is public and scanner-gated). Claude Code and Codex CLI render
  this into their native MCP config; pi renders it into `~/.pi/agent/mcp.json`
  and serves it through the `pi-mcp-adapter` package (one lazy proxy tool
  instead of every server's tool definitions in context).
- **Personal/secret servers** (inline tokens, OAuth, internal/private hosts)
  never go in the repo. For pi they live in a local, `chmod 600`,
  non-repo file at `~/.config/mcp/mcp.json`, which `pi-mcp-adapter` reads at
  higher precedence and merges with the rendered catalog. You can hand-author
  it or adopt existing host configs with `/mcp setup` /
  `pi-mcp-adapter init --discover-host-configs`. See `tools/pi/setup.md` §6 for
  the full rules.

**Adjust model tiers** -- edit `common/model-tiers.toml`. Each table (e.g.
`[specialist]`) maps a tier name to a concrete model per tool: `claude` for
Claude Code, `codex_model` + `codex_effort` for Codex CLI, and `pi_model` +
`pi_thinking` for pi. Changing a table's values here updates every agent that
references that tier, across every tool, on the next bootstrap run.

## Private/public and contributing

This repo is meant to be pushed publicly. Before anything crosses that
boundary, run the security gate:

```bash
common/scripts/agentfiles-scan.sh <dir>
```

It scans file contents and filenames for two things: a deny-list of
organization-specific identifiers (internal tool names, domains, personal
paths, usernames) and common secret patterns (AWS keys, private key
headers, Slack/GitHub/OpenAI-style tokens). A clean scan prints
`scan clean: <dir>` and exits `0`; any hit prints the match and exits
non-zero.

Contributions are vetted before publishing: run the scan against any new or
changed files, keep agent rosters and examples stack-agnostic (see the
framing note at the top of `common/instructions/AGENTS.md`), and don't
introduce hardcoded personal or organizational values anywhere under
`common/` or `tools/`.

## License

Do whatever you want with these. No license needed.
