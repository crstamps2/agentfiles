import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

type Ownership = { mode: "unmanaged" | "autonomous" | "yielding" | "interactive" | "queued" | "human" | "other-session"; ticket: string | null };
const REPO = process.env.AF_REPO ?? path.resolve(path.dirname(fs.realpathSync(fileURLToPath(import.meta.url))), "../../../..");
const RUNNER = path.join(REPO, "common/skills/agent-loop/scripts/runner.py");
const PYTHON = path.join(os.homedir(), ".local/bin/agent-loop-python");
const COORDINATOR = /^\/(?:skill:)?loop-watch(?:\s|$)/;

export default function (pi: ExtensionAPI) {
	let controlling = false;
	let userTurn = false;
	let returned = false;
	let last: Ownership | undefined;
	let failure = "";
	let checking: Promise<Ownership> | undefined;

	async function run(action: string, ctx: ExtensionContext): Promise<Ownership> {
		const args = [RUNNER, "interactive", action, "--worktree", ctx.cwd, "--session", ctx.sessionManager.getSessionId()];
		// Cleanup can restore an interrupted attempt. Pi's exec timeout kills only the helper,
		// not its git descendants, so never abandon a mutating handoff on a UI timeout.
		const result = await pi.exec(PYTHON, args, { cwd: ctx.cwd, timeout: action === "status" ? 15_000 : 0 });
		if (result.killed || result.code !== 0) throw new Error(result.stderr || "interactive handoff did not finish");
		const value = JSON.parse(result.stdout) as Ownership;
		if (!["unmanaged", "autonomous", "yielding", "interactive", "queued", "human", "other-session"].includes(value.mode)) {
			throw new Error("invalid ownership response");
		}
		last = value;
		failure = "";
		return value;
	}

	async function claim(ctx: ExtensionContext) {
		controlling = true; // A failed handoff must not silently permit edits.
		userTurn = true;
		returned = false;
		try {
			const value = await run("enter", ctx);
			if (value.mode === "yielding") ctx.ui.notify(`${value.ticket}: interactive handoff requested. The live attempt must yield before foreground tools can run; other tickets keep running.`, "warning");
			else if (value.mode === "interactive") ctx.ui.notify(`${value.ticket}: interactive control, outside the cohort limit. /agent-loop-auto returns it to the background queue.`, "info");
		} catch (error) {
			failure = String(error);
			ctx.ui.notify(failure, "error");
		}
	}

	async function guard(ctx: ExtensionContext): Promise<string | undefined> {
		if (returned) return "This workspace was returned to the background queue. Submit a new message to take interactive control before using tools.";
		try {
			// Coalesce simultaneous tool calls; the backend also serializes all ownership changes.
			checking ??= run(controlling ? "check" : "status", ctx).finally(() => { checking = undefined; });
			const value = await checking;
			if (value.mode === "unmanaged") return;
			if (value.mode === "autonomous" && !controlling) return;
			if (value.mode === "interactive" && controlling && userTurn) return;
			return value.mode === "yielding"
				? `${value.ticket}: the background writer has not yielded yet. Do not edit or run worktree commands until the handoff completes; respond to the user rather than polling.`
				: `${value.ticket}: foreground tools are not authorized in this session. A real user message must take interactive control first.`;
		} catch (error) {
			failure = String(error);
			return `Cannot verify exclusive worktree ownership: ${failure}. Do not edit until the handoff succeeds.`;
		}
	}

	pi.on("session_start", async (_event, ctx) => {
		if (ctx.mode !== "tui") return;
		try { await run("status", ctx); }
		catch (error) { failure = String(error); }
	});

	pi.on("input", async (event, ctx) => {
		if (ctx.mode !== "tui" || event.source !== "interactive" || COORDINATOR.test(event.text.trim())) return;
		await claim(ctx);
	});

	pi.on("before_agent_start", async (_event, ctx) => {
		if (ctx.mode !== "tui" || !controlling) return;
		let detail = "";
		if (failure) detail = `Ownership verification failed: ${failure}. Tools remain blocked until verified.`;
		else if (last?.mode === "yielding") detail = "The same-worktree runner is yielding; no foreground tools may execute before the handoff completes. You can respond to the user now.";
		return { message: {
			customType: "interactive-lane",
			content: "This is a foreground, user-directed session outside the autonomous cohort limit. Handle the user's one-off request here, not through cohort admission or a coordinator loop. Other autonomous tickets must continue running. Do not park another ticket or invoke runner.py resume to make room. Keep this workspace interactive until the user explicitly runs /agent-loop-auto. "
				+ detail,
			display: false,
		} };
	});

	pi.on("agent_settled", async () => { userTurn = false; });

	pi.on("tool_call", async (_event, ctx) => {
		if (ctx.mode !== "tui") return;
		const reason = await guard(ctx);
		if (reason) return { block: true, reason };
	});

	pi.on("user_bash", async (_event, ctx) => {
		if (ctx.mode !== "tui") return;
		await claim(ctx);
		const reason = await guard(ctx);
		if (reason) throw new Error(reason); // Pi blocks user_bash when its handler fails.
	});

	pi.registerCommand("agent-loop-auto", {
		description: "Return this interactive workspace to the autonomous queue (normal cohort limit applies)",
		handler: async (_args, ctx) => {
			await ctx.waitForIdle();
			try {
				const result = await run("return", ctx);
				returned = true;
				controlling = false;
				ctx.ui.notify(result.mode === "human" ? `${result.ticket}: the separate HUMAN takeover is still set; no background runner started.` : `${result.ticket ?? "Workspace"}: queued for autonomy; existing background work keeps running.`, "info");
			} catch (error) { ctx.ui.notify(String(error), "error"); }
		},
	});
}
