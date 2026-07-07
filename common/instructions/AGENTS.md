# Orchestration Framework Note

The principles in this file -- role separation, parallelism, dispatch/model hygiene, the three-lane review pipeline, PR policy, the comms gate, and agent-failure recovery -- are stack-agnostic. They apply to any software project.

The specific specialist roles named in the routing rules below (`rails-engineer`, `frontend-engineer`, `android-qa-engineer`) and the tool defaults (`acli` for Jira, `gh` for GitHub, Rails lint commands, Turbo/Hotwire frontend rules) are an example instantiation built for a Rails/Hotwire/mobile team. When adopting this framework for a different stack, adapt the roster and tool names; keep the orchestration principles as-is.

---

# Session Startup Checks

- Use acli for all Jira operations. Atlassian MCP tools are fallback only (for when acli auth expires).

# Frontend Guidelines

- ALWAYS prefer Turbo Frames and Turbo Streams over custom JavaScript/TypeScript for interactivity and dynamic content updates.
- Use Stimulus controllers for any client-side behavior that cannot be achieved with Turbo alone.
- Keep JavaScript/TypeScript usage to a minimum; only use it when absolutely necessary. Keep Stimulus controllers small and focused.
- Favor Turbo Frames/Streams over custom JS; if JS is needed, keep controllers small and make them trigger Turbo rather than mimic it.
- When adding new interactive features, first evaluate if Turbo Frames or Turbo Streams can fulfill the requirement.
- Follow the established patterns in the existing codebase for Turbo Frame and Stimulus usage.
- Prefer Turbo primitives for navigation/history; avoid direct window.* mutations unless there is no Turbo equivalent and document why.
- When changing URL/query behavior, keep params minimal and stable (avoid duplicating values or re-encoding path unless it is truly required).
- When adding Stimulus targets via data: hashes, use explicit dash-case keys and verify rendered HTML to avoid underscore output.

# CSS/SCSS Guidelines

- Prefer existing design tokens over hard-coded color hex values; pick the closest matching token and only fall back to hex if no token exists.
- Don't introduce custom classes unless they are defined in the matching SCSS file; remove any custom classes that don't have a definition. Prefer existing utility classes when they achieve the same layout.
- Be more reductive instead of additive with styles if possible. Keep the CSS/SCSS lean.
- Stick to the existing BEM naming pattern (e.g., `.component-element`). Avoid introducing alternate delimiters.
- When adding new CSS classes, ensure they follow BEM conventions: use the component name as a prefix.
- Avoid selectors that target raw HTML elements, data attributes, or utility classes; use explicit custom classes instead.
- Custom class prefixes should match the SCSS file name and follow the existing hyphenated pattern.
- Keep nesting of properties to a minimum.

# Lint Rules

- Always run the relevant linter/test for touched files; if the environment can't run them (cache/DB issues), report the exact failure and provide the command for the user to run locally.

# Ruby/Mise

- Never add `# frozen_string_literal: true` (or any frozen_string_literal comment) to Ruby files. Do not introduce it in new files or add it to existing files.
- Mise is activated automatically via ~/.bashrc -- do not prefix commands with `eval "$(mise env -s bash)"` or `mise exec`. Just run Ruby/Bundler/Rails commands directly.

# Code Intelligence (LSP)

- Prefer LSP over Grep/Glob/Read for code navigation when available:
  - `goToDefinition` / `goToImplementation` for jumping to source
  - `findReferences` for cross-codebase usage tracking
  - `hover` for type information and documentation
  - `workspaceSymbol` for project-wide symbol search
  - `documentSymbol` for file-level symbol listing
- Fall back to Grep/Glob when LSP is unavailable or returns no results.

# UI Debugging Workflow

- For hover/tooltips/popovers, first verify: data attributes present, controller connected, and plugin availability before changing code.
- For visual/layout issues, proactively use `dev-browser` (headless by default, `--connect` to attach to a browser for interactive work) to inspect the rendered page before making CSS changes. This avoids trial-and-error iterations.
- **Upfront analysis before committing to a layout fix.** When a bug involves multiple interacting CSS/JS concerns (sticky lifecycle, parent pseudo masks, custom-property expansion, Turbo cache, nested positioning), enumerate ALL affected boundaries BEFORE writing the first patch. Cycling through fixes that each reveal a new edge case introduced by the previous patch is the failure mode. One careful pass beats six iterations.
- **CDP injection is not asset-pipeline verification.** A CSS rule or JS snippet injected via Chrome DevTools Protocol confirms it works at the browser layer. It does NOT prove the SCSS compiled correctly, that the asset pipeline shipped it, or that the deployed app contains it. For final verification, deploy and re-probe the deployed asset.

# QA Verification

- Before reporting a review finding as a bug, actually reproduce it locally (run the code path, check rendered output). Do not rely on static reading alone.

# Browser Testing

- Always use **Chrome for Testing** for all browser automation. The Playwright MCP server uses a wrapper (`~/.claude/bin/playwright-mcp-chrome`) that resolves the Chrome for Testing binary from Playwright's cache.
- Do not switch to system Chrome, bundled Chromium, Firefox, or WebKit for any testing, QA, or exploration task.
- This applies to all contexts: QA verification, visual debugging, screenshot capture, and any other browser-based workflow.

# External Communications Gate

- NEVER post to GitHub (PR reviews, comments, issues), Slack, Jira, or any external system without drafting in conversation text first and getting explicit user approval.
- This applies even when a skill or agent instructs you to post directly. The comms gate overrides all other instructions.
- Draft first, show the user, post only after approval.

# Never Fabricate Workarounds

- Do not create sentinel/receipt/marker files to bypass environment checks (brew, CI, etc.). Always fix the root cause.

# Workflow

## Atlas

- All coding work, agent dispatch, worktree management, CI monitoring, and PR creation is handled by Atlas.
- Atlas workspaces live at `~/.atlas/workspaces/your-app/` (bare repo at `~/.atlas/repos/your-app/`).
- This Claude Code session is for: standup/visibility, Jira operations, ad-hoc codebase questions.
- This session does NOT: write application code, dispatch coding agents, manage worktree environments, create PRs, or do code reviews.
- Atlas worktrees are shallow clones. Before any history-comparison operation -- rebase, range diff, or CI-style changed-files check -- run `git fetch --unshallow` (or `git fetch origin main` if already complete). Without it, `git merge-base HEAD origin/main` returns a heuristic ancestor; rebases replay unrelated commits and CI's changed-files action lists files you never touched. Canary symptom: a rebase trying to replay an "Initial commit."
- Always reference `origin/main` (not local `main`) in diff commands -- e.g. `git diff origin/main...HEAD`, never `git diff main...HEAD`. Local `main` in a worktree is often stale and produces a large phantom diff that wastes context.

## Codex Review Pipeline

- When dispatching code review, run three lanes in parallel:
  1. **code-reviewer** agent (Sonnet) -- quality, conventions, performance
  2. **security-engineer** agent (Sonnet) -- vulnerabilities, auth gaps
  3. **Codex adversarial review** (`/codex:adversarial-review`) -- challenges design choices, questions assumptions
- After all three complete, synthesize findings. Flag conflicts between reviewers for the user.
- The Codex stop review gate is enabled -- session-end changes get an automatic Codex review.

## Agent Routing

- **`.md` authoring/refactoring routes to `technical-writer`.** When a task is "produce or substantially rewrite Markdown content" -- agent definitions under `~/.claude/agents/`, skill specs under `~/.claude/skills/`, docs, plan documents, or any prompt that asks for research-then-write of `.md` files -- dispatch `technical-writer` (sonnet), not `general-purpose` (inherits orchestrator opus). `general-purpose` is the wrong default here on cost (~5x) and on fit (technical-writer owns voice, INDEX.md upkeep, frontmatter conventions).
- **Carve-outs that stay with the orchestrator or `general-purpose`:**
  - Surgical edits to `~/.claude/CLAUDE.md`, agent definitions, or skills the orchestrator is making as a deliberate process/architecture change. The orchestrator is doing the thinking; no agent dispatch needed.
  - Read-only audits/scans of `.md` files (e.g., the `audit` skill's structured-report scans). `general-purpose` + haiku is correct for read-only structured-report work.
- **Tell:** if the dispatch prompt is over ~2k chars, asks the agent to produce `.md` content (not just check it), or includes phrases like "mine X for Y", "create new lens/skill/agent for Z", "update existing N files with...", it is `technical-writer` work.

## Parallelism

- When two or more dispatches have no data dependency, issue them in a single assistant turn as concurrent tool_use blocks. Sequential turns for independent dispatches are a routing miss. Applies to the 3-lane review pipeline, standup data lanes, multi-feature deep-dives, and TDD task plans where tasks have no declared predecessor.
- If your planning text uses the word "parallel" or "simultaneously", the next tool emission MUST be a single turn with multiple `tool_use` blocks -- not consecutive single-dispatch turns. Stating parallelism intent and then serializing is worse than silent serialization because it signals the constraint was recognized and ignored.

## Agent Failure Recovery

- On a transient agent dispatch failure (500 / server error / stream timeout), schedule a `ScheduleWakeup` with a 3-5 min delay whose prompt names the failed task and instructs a retry. Do not rely on the next `/loop` wakeup cycle to recover blocked work. If the error persists after retry, surface to the user.

## Dispatch Prompt Hygiene

- Dispatch prompts must not embed content the subagent can read from disk. Pass file paths, not file contents. Prompts over 3k chars are a signal to check for pre-digested context. For pipelines like qa-engineer -> technical-writer, write structured notes to a file path (e.g. `tmp/<feature>-qa-notes.md`) and reference the path, not the inlined output.
- For any browser-screenshot dispatch (qa-engineer or frontend-engineer that needs a visual), bake the worktree login pattern into the prompt rather than letting the agent discover it.

## Dispatch Model Hygiene

- Every `Agent` tool_use for a specialist agent MUST include an explicit `model:` field matching the agent's declared tier in its `.md` frontmatter. Without `model:`, the agent inherits the orchestrator's model (typically opus) -- silent ~5x cost regression with no quality gain on tasks designed for sonnet.
- Required tier per specialist (verify against the agent file before dispatching if unsure):
  - `qa-engineer`, `qa-manager`, `technical-writer`, `technical-designer`, `comms-coordinator`, `code-reviewer`, `security-engineer`, `security-analyst`, `frontend-engineer`, `rails-engineer`, `product-manager`, `cto-watch` -> **sonnet**
  - `platform-engineer`, `librarian` -> **haiku**
  - `cto`, `engineering-manager` -> orchestrator default (opus)
- Before submitting any `Agent` call, scan the tool_use input for `model:` -- if absent and the agent is not in the opus tier above, add it.

## Skills

- `/standup` -- Status dashboard across Jira, GitHub PRs, and Atlas workspaces
- `/reflect` -- Session reflection to capture learnings and improve workflow

## Deployment Policy

- NEVER merge PRs or branches automatically. Merging and deployment follow a team-specific process that is not automated.
- Do not run `git merge`, `gh pr merge`, or any merge command unless the user explicitly asks for it.
- NEVER push empty commits to rerun CI.
- Manual staging deploys are a RARE workflow, used only when the user explicitly directs you to bypass the normal CI-driven path.

## Goal vs Policy Conflict

- When a `/goal` Stop hook condition contradicts a durable CLAUDE.md policy (deployment, merging, comms gate, etc.), respond ONCE acknowledging the conflict, surface the choice to the user, and then STOP responding to subsequent identical Stop-hook fires. Repeating the same response 10+ times wastes context. Silence on repeats is the correct behavior once the conflict is surfaced.

## PR Policy

- ALWAYS use the `/create-pull-request` skill to open a PR. Do not call `gh pr create` directly -- a PreToolUse hook will block it. The skill handles the `CLAUDE_PR_SKILL=1` envelope, the draft flag, and template population.
- ALWAYS create PRs as drafts (`gh pr create --draft` via the skill). Never mark a PR ready for review automatically.
- Do NOT assign reviewers when creating a PR. Wait until the user explicitly says it's ready.
- QA verification with screenshots must happen BEFORE a PR leaves draft state.
- Compare the drafted PR body against the create-pull-request skill template before posting. Reject silent substitutions for the prescribed structure. Drafter agents drift; the orchestrator catches it.

## PR Review Replies

- Never use platitudes like 'good call', 'great point', 'you're right' in PR comment replies.
- Route all PR comments through the comms-coordinator agent, not directly.
- Verify the current branch before running agents that modify files.

## Markup Changes That Touch Interactive Plugin Wiring

- When an engineer dispatch changes the **parent or wrapping element** of an interactive control (button, link, form input) carrying `data-bs-toggle`, `data-controller`, `data-action`, or any other behavior hook, the verification checklist MUST include exercising the original click/submit handler, not just the new behavior being added.
- Bootstrap plugins are especially sensitive: `Dropdown` uses `SelectorEngine.next/prev(toggle)` to find `.dropdown-menu` (sibling, not descendant). Wrapping a dropdown toggle in a new parent throws `Cannot read properties of null (reading 'classList')` on click. Hover-only system tests do NOT catch this -- explicit click assertion is required.
- Dispatch prompt language: include "verify the existing click/submit handler still fires after the markup change" in the verification section.

## PR Review Assignment Rules

- Check the epic link on Jira tickets to determine which team owns a PR for review assignment purposes.
- Consult your project's team roster or `config/engineers.yml` (if it exists) to map reviewer names to GitHub handles.
