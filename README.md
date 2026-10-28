# agentfiles

One tool-neutral source of truth for AI agent-orchestration config -- agent
roles, skills, hooks, and MCP servers -- usable by **Claude Code, Codex CLI,
and pi**.

## What it is

`agentfiles` keeps reusable orchestration content in `common/` and tool-native
setup and rendering rules in `tools/`. It materializes selected
framework-managed artifacts for `~/.claude`, `~/.codex`, and `~/.pi/agent`
without creating another policy fork for private or package-owned overrides.

## Why

If you use more than one agentic CLI, you've felt this: an agent definition
gets tuned in one tool and the other quietly falls behind. `agentfiles`
keeps reusable framework content in `common/` plus native `tools/` overlays.
Private organization overrides and externally owned packages retain their own sources;
resolve ownership before comparing or synchronizing installed artifacts.

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
common/                   # Shared tool-neutral source, edit framework content here
  instructions/AGENTS.md  # Orchestration rules, role boundaries, conventions
  agents/*.agent.md       # Agent role definitions (tool-neutral frontmatter)
  skills/                 # Orchestration skills (agent-loop, loop-watch, standup, retro, audit, spinup, spindown)
  hooks/                  # Shared and harness-scoped scripts + hooks.manifest.jsonc
  mcp/servers.jsonc       # MCP server catalog
  model-tiers.toml        # Tier -> concrete model, per tool
  scripts/agentfiles-scan.sh  # Deny-list + secret scan for public pushes

tools/
  claude/setup.md         # Ownership-aware setup and native Claude rules
  codex/setup.md          # Ownership-aware setup and native Codex rules
  pi/setup.md             # Ownership-aware setup and native pi rules
  pi/extensions/          # pi extensions that reproduce hooks on pi's event API
  claude/plugins/         # Local Claude plugin marketplace (example: LSP)
  codex/openai.yaml.tmpl  # Template for per-skill Codex metadata

bootstrap.sh              # Entry point: resolve env, sync repo, dispatch setup
```

Only selected framework-managed artifacts under harness roots are derived output. Before
changing a destination, setup resolves links and registrations: missing or confirmed
framework-managed entries can converge to the documented source; private overrides,
package/shared-marketplace content, personal configuration, runtime memory, and uncertain
owners are preserved and reported.

A preserved external entry is not a setup failure to "fix" by repointing it. If a requested
change needs to replace one, setup stops that entry for explicit migration approval. Host
dotfiles should wire external owners, not maintain another policy fork.

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
3. **Setup is ownership-aware and symlink-first where native formats permit.**
   For a missing or confirmed framework-managed destination, eligible shared
   instructions and skills can symlink directly from `common/`; an external
   same-name target is preserved and reported instead of repointed. Native
   overlays render artifacts when the destination format differs. For example,
   Codex skills with unsupported generic frontmatter render only supported
   frontmatter while retaining the source body, and agent definitions
   (`common/agents/*.agent.md`) render to each tool's native agent format:
   Claude uses `.md` with `model:`, Codex uses `.toml` with `model` /
   `model_reasoning_effort`, and pi uses `.md` with `model` / `thinking` read
   by `pi-subagents`. Setup regenerates only ownership-verified generated
   renders, looking up each agent's `tier:` in `model-tiers.toml`. See
   `tools/claude/setup.md`, `tools/codex/setup.md`, and `tools/pi/setup.md`
   for the exact transform rules.

**Note on pi.** Setup requires pi 1.3.0 or later with **built-in MCP**. Delegation
uses `pi-subagents`, and the vendored `agentfiles-hooks` extension maps shared hooks
onto pi events. No MCP adapter package is required. An extension registering `/mcp`
replaces the builtin; setup stops for a separately approved migration if one is found.
Instructions and supported skill formats use native context-file/Agent-Skills support.
The MCP renderer applies conservative checks to the catalog forms it supports before
creating a missing file; the installed pi release remains authoritative, and this does
not promise complete support for future native schemas.

## Updating

Re-run the same one-liner (or `git -C ~/.agentfiles pull --ff-only &&
~/.agentfiles/bootstrap.sh --tool=<claude|codex|pi>` if you cloned manually).
Bootstrap is repeatable for entries it owns: selected framework-managed links
converge, ownership-verified generated renders regenerate in place, and approved
hook/MCP operations do not duplicate entries. It preserves external or uncertain
targets and reports any needed migration for approval, so a rerun can stop with an
approval-required outcome rather than materializing every similarly named artifact.

## Supported platforms & prerequisites

- **macOS or Linux.** `bootstrap.sh` detects the OS via `uname` and adjusts
  symlink/copy syntax accordingly; there's no behavior difference otherwise.
- **`git`**, to clone/update the repo.
- **One of the CLIs**: [Claude Code](https://docs.anthropic.com/en/docs/claude-code),
  [Codex CLI](https://github.com/openai/codex), or [pi](https://pi.dev),
  reachable on `$PATH` as `claude`, `codex`, or `pi` respectively (bootstrap
  invokes it directly).
- **For pi:** pi 1.3.0 or later and Node.js; delegation setup may install
  `pi-subagents` on first run. MCP itself is built in, not an adapter dependency.

## Customizing

Shared framework content belongs under `common/`; native setup, rendering, and
integration behavior belongs under `tools/`. A tool picks up selected
framework-managed outputs on the next `bootstrap.sh` run. Private, package-owned,
and other external artifacts retain their native owner and are not source-edit targets.

**Add or edit an agent** -- create/edit `common/agents/<name>.agent.md`.
Frontmatter needs at minimum `name:`, `description:`, and `tier:` (one of
the tables in `model-tiers.toml`); `access:` is optional and lists the
tools the agent may use. The body (below the closing `---`) is the agent's
system prompt/instructions, copied verbatim into every tool's rendered
output.

**Add or edit a skill** -- create a new directory under `common/skills/<name>/`
with a `SKILL.md`. Each setup selects only ownership-verified destinations, then
uses a direct link or the tool's documented native rendering. It does not repoint an
external same-name skill.

**Add or edit a hook** -- drop a script under `common/hooks/`, then add an
entry for it in `common/hooks/hooks.manifest.jsonc` under the relevant event
key (`SessionStart`, `Stop`, etc.). Entries are shared by default; add an
optional `tools` array such as `"tools": ["pi"]` when a hook depends on one
harness's native registry or configuration. Each setup path skips entries that
do not select its harness and rejects malformed selectors rather than widening
their scope. Each setup maps only events with a verified native semantic
equivalent and reports unsupported events instead of approximating them. Claude
Code merges its supported entries; Codex 0.164 maps `SessionStart` but skips the
shared end-of-turn `Stop` because `SessionEnd` is not equivalent. pi runs eligible
entries via the `agentfiles-hooks` extension, which maps `SessionStart`/`Stop`
onto pi's event API (extend that extension only after verifying native semantics
for additional events).

The Pi-only `model-route-check.sh` hook validates active model routes against
`pi --list-models` at session start. Its default mode is advisory so registry
unavailability does not block startup. Run `common/hooks/model-route-check.sh --strict`
in setup, CI, or manual verification when an unresolved route must fail the command.

**Add or edit an MCP server.** MCP config splits into two channels by
sensitivity:

- **Shareable servers** go in `common/mcp/servers.jsonc`, keyed by server
  name, with `command`/`args`/`env`. Use `$REPO`/`$HOME` placeholders and
  `${ENV_VAR}` interpolation -- **no inline secrets or internal hostnames**
  (this repo is public and scanner-gated). Claude Code and Codex CLI render
  this into native MCP config. Pi uses `~/.pi/agent/mcp.json` and built-in MCP;
  its preserving renderer checks the catalog forms it supports before creating a
  missing file in a prepared canonical, ownership-verified parent chain. It does not
  create parent directories or claim complete native-schema support across pi versions.
  It requires approval before any change to an existing configuration and never
  replaces personal server entries.
- **Personal/secret servers** (inline tokens, OAuth, internal/private hosts)
  never go in the repo. Pi uses its native user-level `mcp.json`; OAuth credentials
  are separately managed in `mcp-auth.json`. Setup does not read or migrate secrets
  or legacy adapter files. Use native `/mcp` or `pi mcp add` for approved changes.
  `pi mcp list` connects servers, so it is not an offline schema check.
  See `tools/pi/setup.md` section 6 for preservation and migration rules.

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
