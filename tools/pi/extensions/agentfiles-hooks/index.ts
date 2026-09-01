/**
 * agentfiles-hooks
 *
 * Bridges the tool-neutral `common/hooks/hooks.manifest.jsonc` into pi.
 *
 * pi has no native "hooks" concept (see pi's philosophy: hooks are just
 * extensions). This extension reads the same manifest that Claude Code and
 * Codex CLI consume and wires the shared event vocabulary onto pi events:
 *
 *   SessionStart -> run scripts once per session; their stdout is injected
 *                   into the first agent turn as additional context.
 *   Stop         -> run scripts when the agent settles (finishes all work);
 *                   their stdout is injected into the *next* agent turn as
 *                   context (matching "end-of-turn reminder" semantics
 *                   without spawning an extra turn).
 *
 * PreToolUse / PostToolUse / UserPromptSubmit are recognized but not yet
 * mapped; add handlers here if you start using them in the manifest.
 *
 * Script contract (same as the other tools): each script prints context to
 * stdout. A non-empty stdout is injected; stderr is surfaced as a pi
 * notification. Exit code is ignored (advisory hooks, never blocking).
 *
 * $REPO in manifest script paths is resolved to the agentfiles checkout,
 * discovered from this file's location (…/tools/pi/extensions/agentfiles-hooks)
 * or overridden with the AF_REPO environment variable.
 */

import { execFile } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

interface HookEntry {
	script: string;
}
type HookManifest = Record<string, HookEntry[]>;

function stripJsonc(text: string): string {
	// Remove /* */ block comments and // line comments (line comments only
	// when not inside a string). The manifest uses // comments exclusively,
	// but handle both defensively.
	let out = "";
	let inString = false;
	let inLine = false;
	let inBlock = false;
	for (let i = 0; i < text.length; i++) {
		const c = text[i];
		const next = text[i + 1];
		if (inLine) {
			if (c === "\n") {
				inLine = false;
				out += c;
			}
			continue;
		}
		if (inBlock) {
			if (c === "*" && next === "/") {
				inBlock = false;
				i++;
			}
			continue;
		}
		if (inString) {
			out += c;
			if (c === "\\") {
				out += text[i + 1] ?? "";
				i++;
			} else if (c === '"') {
				inString = false;
			}
			continue;
		}
		if (c === '"') {
			inString = true;
			out += c;
			continue;
		}
		if (c === "/" && next === "/") {
			inLine = true;
			i++;
			continue;
		}
		if (c === "/" && next === "*") {
			inBlock = true;
			i++;
			continue;
		}
		out += c;
	}
	return out;
}

function resolveRepoRoot(): string {
	if (process.env.AF_REPO && fs.existsSync(process.env.AF_REPO)) {
		return process.env.AF_REPO;
	}
	// this file lives at <repo>/tools/pi/extensions/agentfiles-hooks/index.ts
	const here = path.dirname(fileURLToPath(import.meta.url));
	return path.resolve(here, "..", "..", "..", "..");
}

function loadManifest(repoRoot: string): HookManifest {
	const manifestPath = path.join(repoRoot, "common", "hooks", "hooks.manifest.jsonc");
	if (!fs.existsSync(manifestPath)) return {};
	try {
		return JSON.parse(stripJsonc(fs.readFileSync(manifestPath, "utf-8"))) as HookManifest;
	} catch {
		return {};
	}
}

function resolveScript(repoRoot: string, script: string): string {
	return script.replace(/\$REPO/g, repoRoot);
}

function runScript(scriptPath: string, cwd: string): Promise<{ stdout: string; stderr: string }> {
	return new Promise((resolve) => {
		execFile("bash", [scriptPath], { cwd, timeout: 30_000 }, (_err, stdout, stderr) => {
			resolve({ stdout: (stdout ?? "").toString(), stderr: (stderr ?? "").toString() });
		});
	});
}

export default function agentfilesHooks(pi: ExtensionAPI) {
	const repoRoot = resolveRepoRoot();
	const manifest = loadManifest(repoRoot);

	const eventScripts = (event: string): string[] =>
		(manifest[event] ?? []).map((e) => resolveScript(repoRoot, e.script));

	let sessionStartDone = false;
	// Context captured from hook scripts, flushed into the next agent turn.
	let pendingContext: string[] = [];

	async function collect(event: string, ctx: { cwd: string; ui: { notify: (m: string, l?: string) => void } }) {
		for (const scriptPath of eventScripts(event)) {
			if (!fs.existsSync(scriptPath)) {
				ctx.ui.notify(`agentfiles-hooks: missing ${scriptPath}`, "warning");
				continue;
			}
			const { stdout, stderr } = await runScript(scriptPath, ctx.cwd);
			if (stderr.trim()) ctx.ui.notify(stderr.trim(), "info");
			if (stdout.trim()) pendingContext.push(stdout.trim());
		}
	}

	pi.on("before_agent_start", async (_event, ctx) => {
		if (!sessionStartDone) {
			sessionStartDone = true;
			await collect("SessionStart", ctx);
		}
		if (pendingContext.length === 0) return;
		const content = pendingContext.join("\n\n");
		pendingContext = [];
		return {
			message: {
				customType: "agentfiles-hooks",
				content,
				display: true,
			},
		};
	});

	pi.on("agent_settled", async (_event, ctx) => {
		await collect("Stop", ctx);
	});
}
