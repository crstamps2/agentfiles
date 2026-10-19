import assert from "node:assert/strict";
import { test } from "node:test";
import register from "./index.ts";

function harness(mode = "interactive", initial = "autonomous", cwd = "/tmp/zip-3") {
	const handlers = new Map<string, (event: any, ctx: any) => any>();
	const commands = new Map<string, any>();
	const calls: string[][] = [];
	const notices: string[] = [];
	const timeouts: number[] = [];
	let killed = false;
	let fail = false;
	const pi = {
		on: (name: string, callback: any) => handlers.set(name, callback),
		registerCommand: (name: string, command: any) => commands.set(name, command),
		exec: async (_command: string, args: string[], options: { timeout: number }) => {
			calls.push(args);
			timeouts.push(options.timeout);
			if (fail) return { code: 2, stdout: "", stderr: "handoff failed" };
			const action = args[args.indexOf("interactive") + 1];
			return { code: 0, killed, stdout: JSON.stringify({ mode: action === "status" ? initial : action === "return" ? "queued" : mode, ticket: "ZIP-3" }), stderr: "" };
		},
	};
	register(pi as never);
	const ctx = { cwd, mode: "tui", sessionManager: { getSessionId: () => "session-a" }, ui: { notify: (text: string) => notices.push(text) }, waitForIdle: async () => {} };
	return {
		calls, notices, commands, ctx, timeouts,
		kill: () => { killed = true; },
		fail: () => { fail = true; },
		setMode: (value: string) => { mode = value; },
		emit: (name: string, event = {}, context = ctx) => handlers.get(name)?.(event, context),
	};
}

test("a real user message claims foreground immediately without sending the prompt to a shell", async () => {
	const h = harness();
	await h.emit("input", { source: "interactive", text: "fix it; $(touch /tmp/not-a-command)" });
	assert.equal(h.calls.length, 1);
	assert.deepEqual(h.calls[0].slice(-6), ["interactive", "enter", "--worktree", "/tmp/zip-3", "--session", "session-a"]);
	const start = await h.emit("before_agent_start", { messageType: "user" });
	assert.match(start.message.content, /outside.*cohort/i);
	assert.equal(await h.emit("tool_call", { toolName: "edit" }), undefined);
});

test("coordinator startup, extension wakes, RPC and headless workers never take a slot out of the cohort", async () => {
	const h = harness();
	for (const event of [
		{ source: "interactive", text: "/loop-watch ZIP-3" },
		{ source: "interactive", text: "/skill:loop-watch ZIP-3" },
		{ source: "extension", text: "check on this ticket" },
		{ source: "rpc", text: "advance work" },
	]) await h.emit("input", event);
	await h.emit("input", { source: "interactive", text: "worker task" }, { ...h.ctx, mode: "print" });
	assert.equal(h.calls.length, 0);
});

test("live runner handoff lets chat start but blocks tools until that writer has yielded", async () => {
	const h = harness("yielding");
	await h.emit("input", { source: "interactive", text: "fix the button" });
	assert.match((await h.emit("before_agent_start")).message.content, /yield/i);
	assert.equal((await h.emit("tool_call", { toolName: "bash" })).block, true);
	h.setMode("interactive");
	assert.equal(await h.emit("tool_call", { toolName: "edit" }), undefined);
});

test("failed ownership handoff cannot silently permit foreground writes", async () => {
	const h = harness();
	h.fail();
	await h.emit("input", { source: "interactive", text: "fix it" });
	assert.equal((await h.emit("tool_call", { toolName: "write" })).block, true);
});

test("reloading an interactive pane retains its foreground protection without claiming an autonomous one", async () => {
	const h = harness("yielding", "yielding");
	await h.emit("session_start");
	assert.equal((await h.emit("tool_call", { toolName: "edit" })).block, true);
	const observer = harness();
	await observer.emit("session_start");
	assert.equal(await observer.emit("tool_call", { toolName: "read" }), undefined);
	assert.equal(observer.calls.some(args => args.includes("enter")), false);
});

test("only explicit return queues autonomy and a later real message takes interactive control again", async () => {
	const h = harness();
	await h.emit("input", { source: "interactive", text: "fix it" });
	await h.emit("session_shutdown");
	assert.equal(h.calls.some(args => args.includes("return")), false);
	await h.commands.get("agent-loop-auto").handler("", h.ctx);
	assert.equal(h.calls.at(-1)?.includes("return"), true);
	assert.equal((await h.emit("tool_call", { toolName: "edit" })).block, true);
	await h.emit("input", { source: "interactive", text: "one more thing" });
	assert.equal(await h.emit("tool_call", { toolName: "edit" }), undefined);
});

test("an unattended wake cannot edit after the user's one-off turn has settled", async () => {
	const h = harness();
	await h.emit("input", { source: "interactive", text: "fix it" });
	assert.equal(await h.emit("tool_call", { toolName: "edit" }), undefined);
	await h.emit("agent_settled");
	await h.emit("input", { source: "extension", text: "resume coordinator work" });
	assert.equal((await h.emit("tool_call", { toolName: "edit" })).block, true);
	await h.emit("input", { source: "interactive", text: "now fix this too" });
	assert.equal(await h.emit("tool_call", { toolName: "edit" }), undefined);
});

test("handoff cleanup cannot be abandoned by a UI subprocess timeout while git may still write", async () => {
	const h = harness();
	await h.emit("input", { source: "interactive", text: "fix it" });
	await h.emit("tool_call", { toolName: "edit" });
	await h.commands.get("agent-loop-auto").handler("", h.ctx);
	assert.deepEqual(h.timeouts, [0, 0, 0]);
});

test("a killed helper is not treated as a successful ownership handoff", async () => {
	const h = harness();
	h.kill();
	await h.emit("input", { source: "interactive", text: "fix it" });
	assert.equal((await h.emit("tool_call", { toolName: "edit" })).block, true);
});

test("local user bash obeys the same handoff boundary", async () => {
	const h = harness("yielding");
	await assert.rejects(() => h.emit("user_bash", { command: "git status" }), /yield/i);
});
