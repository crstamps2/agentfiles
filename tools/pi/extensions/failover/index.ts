/**
 * Vendor failover for interactive pi sessions.
 *
 * Registers virtual models that run on their primary model and switch to the other vendor's
 * equivalent while the primary's provider is out of quota:
 *
 *   failover/sol   -> openai/gpt-6.1-sol, else anthropic/claude-opus-5-5
 *   failover/opus  -> anthropic/claude-opus-5-5, else openai/gpt-6.1-sol
 *
 * Provider health is shared with the agent loop through ~/.local/state/agent-loop/providers.json:
 * a usage-limit error in any session marks the provider until the reset time in the message
 * ("Try again in ~8405 min"), and every session and loop agent goes straight to the other vendor.
 *
 * A session on a concrete model (chosen with /model) has no router, so it is switched to the other
 * vendor's equivalent before it continues; if that is impossible it stops rather than re-sending
 * the request to the exhausted vendor.
 */

import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { ExtensionAPI, ExtensionContext, ModelRoute, ModelRouteRequest } from "@earendil-works/pi-coding-agent";

const HEALTH = path.join(os.homedir(), ".local", "state", "agent-loop", "providers.json");
const LIMIT = /usage limit|insufficient_quota|rate_limit_error|quota exceeded|\b429\b/i;
const RESET = /try again in ~?\s*(\d+)\s*(min|minute|hour|h\b|sec|s\b)/i;
const DEFAULT_COOL_OFF_S = 30 * 60;

type Health = Record<string, { until: number; message: string; marked: number }>;

function load(): Health {
	try {
		return JSON.parse(fs.readFileSync(HEALTH, "utf8")) as Health;
	} catch {
		return {};
	}
}

function exhausted(provider: string): boolean {
	return (load()[provider]?.until ?? 0) > Date.now() / 1000;
}

function mark(provider: string, message: string): void {
	const now = Date.now() / 1000;
	const m = RESET.exec(message);
	let secs = DEFAULT_COOL_OFF_S;
	if (m) {
		const n = Number(m[1]);
		const unit = m[2].toLowerCase();
		secs = n * (unit.startsWith("h") ? 3600 : unit.startsWith("s") ? 1 : 60);
	}
	const data = load();
	data[provider] = { until: Math.max(now + secs, data[provider]?.until ?? 0), message: message.slice(0, 300), marked: now };
	fs.mkdirSync(path.dirname(HEALTH), { recursive: true });
	const tmp = `${HEALTH}.tmp-${process.pid}`;
	fs.writeFileSync(tmp, JSON.stringify(data, null, 2));
	fs.renameSync(tmp, HEALTH);
}

interface Pair {
	id: string;
	name: string;
	primary: [string, string];
	backup: [string, string];
}

const PAIRS: Pair[] = [
	{ id: "sol", name: "Sol, Opus on OpenAI limit", primary: ["openai", "gpt-6.1-sol"], backup: ["anthropic", "claude-opus-5-5"] },
	{ id: "opus", name: "Opus, Sol on Anthropic limit", primary: ["anthropic", "claude-opus-5-5"], backup: ["openai", "gpt-6.1-sol"] },
];

// Tier family -> each vendor's model, matched on the id so newer releases (gpt-6.1-sol) still map.
const FAMILIES: { match: RegExp; openai: [string, string]; anthropic: [string, string] }[] = [
	{ match: /\bsol\b|opus/i, openai: ["openai", "gpt-6.1-sol"], anthropic: ["anthropic", "claude-opus-5-5"] },
	{ match: /astra|fable/i, openai: ["openai", "gpt-6-astra"], anthropic: ["anthropic", "claude-fable-5-1"] },
	{ match: /terra|sonnet/i, openai: ["openai", "gpt-5.6-terra"], anthropic: ["anthropic", "claude-sonnet-5-5"] },
	{ match: /luna|haiku/i, openai: ["openai", "gpt-6-luna"], anthropic: ["anthropic", "claude-haiku-4-5"] },
];

function equivalent(modelId: string, backup: string): [string, string] | undefined {
	const family = FAMILIES.find((f) => f.match.test(modelId));
	return family ? (backup === "openai" ? family.openai : family.anthropic) : undefined;
}

function find(ctx: ExtensionContext, [provider, id]: [string, string]) {
	const model = ctx.modelRegistry.find(provider, id);
	if (!model) throw new Error(`Model ${provider}/${id} is not in the catalog`);
	return model;
}

export default function (pi: ExtensionAPI) {
	// Pi does not retry a usage-limit error, so the router never sees it as a retry. Catch it before the
	// run settles instead: record the limit, then continue once; the router sends that request to the backup.
	pi.on("agent_before_settle", async (event, ctx) => {
		const msgs = event.context.contextMessages as { role?: string; stopReason?: string; errorMessage?: string; provider?: string; model?: string }[];
		const msg = [...msgs].reverse().find((m) => m.role === "assistant");
		if (msg?.role !== "assistant" || msg.stopReason !== "error" || !LIMIT.test(msg.errorMessage ?? "") || !msg.provider) return;
		mark(msg.provider, msg.errorMessage ?? "");
		const backup = msg.provider === "openai" ? "anthropic" : "openai";
		if (exhausted(backup)) return; // both vendors out: stop rather than loop
		if (ctx.model?.provider !== "failover") {
			// Continuing would resend to the exhausted vendor: switch first, or stop.
			const target = equivalent(msg.model ?? ctx.model?.id ?? "", backup);
			const model = target ? ctx.modelRegistry.find(target[0], target[1]) : undefined;
			if (!model || !(await pi.setModel(model))) {
				ctx.ui.notify(`${msg.provider} hit its usage limit and ${msg.model ?? "this model"} has no usable ${backup} equivalent; switch with /model and continue.`, "warning");
				return;
			}
		}
		// The failed message is not runnable context; a short note after it is, and tells the model why.
		return {
			entries: [{
				type: "custom_message",
				customType: "failover",
				content: `${msg.provider} hit its usage limit; continuing on the ${backup} equivalent. Resume the task where it stopped.`,
				display: true,
			}],
			continue: true,
		};
	});
	for (const pair of PAIRS) {
		pi.registerVirtualModel({
			provider: "failover",
			id: pair.id,
			name: pair.name,
			thinkingLevels: ["low", "medium", "high", "xhigh"],
			route(request: ModelRouteRequest, ctx: ExtensionContext): ModelRoute {
				const [pp] = pair.primary;
				const [bp] = pair.backup;
				const error = request.failed?.message?.errorMessage ?? "";
				if (request.reason === "retry" && request.failed && LIMIT.test(error)) {
					mark(request.failed.model.provider, error);
				}
				const useBackup = exhausted(pp) && !exhausted(bp);
				const target = find(ctx, useBackup ? pair.backup : pair.primary);
				// Keep tool follow-ups on the model that handled the turn while it is still healthy (prompt cache).
				const sticky = request.reason === "continuation" ? request.previous : undefined;
				if (sticky && !exhausted(sticky.model.provider)) {
					return { model: sticky.model, thinkingLevel: sticky.thinkingLevel ?? request.thinkingLevel };
				}
				return { model: target, thinkingLevel: request.thinkingLevel };
			},
		});
	}
}
