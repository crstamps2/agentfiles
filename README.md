# agentfiles

One tool-neutral source of truth for AI agent-orchestration config -- agent
roles, skills, hooks, and MCP servers -- usable by **both Claude Code and
Codex CLI**.

## What it is

`agentfiles` holds your orchestration config once, in a tool-neutral format
under `common/`, and renders it into whichever coding CLI you're using. The
same agent roster, skills, and instructions back both `~/.claude` and
`~/.codex` -- no duplicated, drifting copies per tool.

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

**Clone-and-run alternative** (if you'd rather inspect the script first, or
already have a checkout):

```bash
git clone https://github.com/crstamps2/agentfiles.git
cd agentfiles
./bootstrap.sh --tool=claude   # or --tool=codex
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
  claude/plugins/         # Local Claude plugin marketplace (example: LSP)
  codex/openai.yaml.tmpl  # Template for per-skill Codex metadata

bootstrap.sh              # Entry point: resolve env, sync repo, dispatch setup
```

`~/.claude/` and `~/.codex/` are **derived output**, not source. Don't
hand-edit files under them directly -- edits get overwritten the next time
you run `bootstrap.sh`. Change `common/` (or the relevant `tools/<tool>/`
spec) instead, then re-run bootstrap.

## How it works

1. **`bootstrap.sh` resolves the environment.** It figures out `$HOME`, the
   clone directory (default `~/.agentfiles`, override with `--home=` /
   `--repo=`), and the OS (`macos`/`linux`), then clones or `pull --ff-only`s
   the repo.
2. **It dispatches to the tool's own AI.** `bootstrap.sh --tool=<claude|codex>`
   invokes that CLI with an instruction to read and mechanically execute
   `tools/<tool>/setup.md` -- the CLI configures itself using its own
   filesystem/shell access, with `AF_HOME`, `AF_REPO`, and `AF_OS` injected
   as environment variables.
3. **Setup is symlink-first, with a transform step for agent definitions.**
   Shared artifacts (instructions, skills) are symlinked straight from
   `common/` into the tool's home directory, so edits to `common/` show up
   immediately without re-running setup. Agent definitions
   (`common/agents/*.agent.md`) can't be symlinked as-is -- each tool has its
   own frontmatter shape (Claude wants `.md` with a `model:` key, Codex wants
   `.toml` with `model` / `model_reasoning_effort`) -- so setup renders a
   per-tool copy, looking up each agent's `tier:` in `model-tiers.toml` to
   pick the concrete model. See `tools/claude/setup.md` and
   `tools/codex/setup.md` for the exact transform rules.

## Updating

Re-run the same one-liner (or `git -C ~/.agentfiles pull --ff-only &&
~/.agentfiles/bootstrap.sh --tool=<claude|codex>` if you cloned manually).
Bootstrap is idempotent: symlinks are recreated only if they point at the
wrong target, rendered agent files are regenerated in place, and
hook/MCP-server merges skip entries that are already present. Nothing is
duplicated by re-running it.

## Supported platforms & prerequisites

- **macOS or Linux.** `bootstrap.sh` detects the OS via `uname` and adjusts
  symlink/copy syntax accordingly; there's no behavior difference otherwise.
- **`git`**, to clone/update the repo.
- **One of the CLIs**: [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
  or [Codex CLI](https://github.com/openai/codex), reachable on `$PATH` as
  `claude` or `codex` respectively (bootstrap invokes it directly).

## Customizing

Everything you add or edit lives under `common/`. Both tools pick it up next
time you run `bootstrap.sh`.

**Add or edit an agent** -- create/edit `common/agents/<name>.agent.md`.
Frontmatter needs at minimum `name:`, `description:`, and `tier:` (one of
the tables in `model-tiers.toml`); `access:` is optional and lists the
tools the agent may use. The body (below the closing `---`) is the agent's
system prompt/instructions, copied verbatim into both tools' rendered
output.

**Add or edit a skill** -- create a new directory under `common/skills/<name>/`
with a `SKILL.md`. Both tools' setup steps symlink the whole directory in,
one symlink per skill.

**Add or edit a hook** -- drop a script under `common/hooks/`, then add an
entry for it in `common/hooks/hooks.manifest.jsonc` under the relevant event
key (`SessionStart`, `Stop`, etc.). Both tools merge the manifest into their
own hook config on the next setup run.

**Add or edit an MCP server** -- add an entry to `common/mcp/servers.jsonc`,
keyed by server name, with its `command`/`args`/`env`. Use the `$REPO` and
`$HOME` placeholders instead of hardcoded paths so the entry stays portable
across machines.

**Adjust model tiers** -- edit `common/model-tiers.toml`. Each table (e.g.
`[specialist]`) maps a tier name to a concrete model per tool: `claude` for
Claude Code, `codex_model` + `codex_effort` for Codex CLI. Changing a
table's values here updates every agent that references that tier, in both
tools, on the next bootstrap run.

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
