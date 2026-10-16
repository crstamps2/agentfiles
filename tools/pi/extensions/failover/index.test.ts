// Run: node --test tools/pi/extensions/failover/index.test.ts
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { beforeEach, test } from "node:test";

const home = fs.mkdtempSync(path.join(os.tmpdir(), "failover-test-"));
process.env.HOME = home;
const health = path.join(home, ".local", "state", "agent-loop", "providers.json");
const { default: register } = await import("./index.ts");

const LIMIT_ERROR = "The ChatGPT user has reached their Subscription Sharing usage limit.";
const catalog = new Map<string, { provider: string; id: string }>(
	["openai/gpt-5.6-sol", "openai/gpt-6.1-sol", "anthropic/claude-opus-5-5", "anthropic/claude-sonnet-5-5", "openai/gpt-5.6-terra"].map((key) => {
		const [provider, id] = key.split("/");
		return [key, { provider, id }];
	}),
);

function harness(selected: { provider: string; id: string }, { authorized = true } = {}) {
	let settle: (event: unknown, ctx: unknown) => Promise<unknown>;
	const switched: string[] = [];
	const notices: string[] = [];
	const routers: Record<string, (request: unknown) => { model: { provider: string; id: string } }> = {};
	const pi = {
		on: (name: string, handler: typeof settle) => {
			if (name === "agent_before_settle") settle = handler;
		},
		registerVirtualModel: (spec: { id: string; route: (request: unknown, ctx: unknown) => { model: { provider: string; id: string } } }) => {
			routers[spec.id] = (request) => spec.route(request, ctx);
		},
		setModel: async (model: { provider: string; id: string }) => {
			switched.push(`${model.provider}/${model.id}`);
			return authorized;
		},
	};
	register(pi as never);
	const ctx = {
		model: selected,
		modelRegistry: { find: (provider: string, id: string) => catalog.get(`${provider}/${id}`) },
		ui: { notify: (message: string) => notices.push(message) },
	};
	const fail = (provider: string, model: string, errorMessage = LIMIT_ERROR) =>
		settle({ context: { contextMessages: [{ role: "assistant", stopReason: "error", provider, model, errorMessage }] } }, ctx);
	const route = (id: string) => {
		const { model } = routers[id]({ reason: "initial", thinkingLevel: "high" });
		return `${model.provider}/${model.id}`;
	};
	return { fail, switched, notices, route };
}

beforeEach(() => fs.rmSync(health, { force: true }));

function exhaust(provider: string) {
	fs.mkdirSync(path.dirname(health), { recursive: true });
	const data = fs.existsSync(health) ? JSON.parse(fs.readFileSync(health, "utf8")) : {};
	data[provider] = { until: Date.now() / 1000 + 3600, message: "usage limit", marked: 0 };
	fs.writeFileSync(health, JSON.stringify(data));
}

test("failover/sol runs on gpt-6.1-sol and moves to Opus while OpenAI is exhausted", () => {
	const { route } = harness({ provider: "failover", id: "sol" });
	assert.equal(route("sol"), "openai/gpt-6.1-sol");
	exhaust("openai");
	assert.equal(route("sol"), "anthropic/claude-opus-5-5");
});

test("failover/opus falls back to gpt-6.1-sol while Anthropic is exhausted", () => {
	const { route } = harness({ provider: "failover", id: "opus" });
	assert.equal(route("opus"), "anthropic/claude-opus-5-5");
	exhaust("anthropic");
	assert.equal(route("opus"), "openai/gpt-6.1-sol");
});

test("a concrete Opus session switches to gpt-6.1-sol on an Anthropic limit", async () => {
	const { fail, switched } = harness({ provider: "anthropic", id: "claude-opus-5-5" });
	await fail("anthropic", "claude-opus-5-5");
	assert.deepEqual(switched, ["openai/gpt-6.1-sol"]);
});

test("a concrete-model session is switched to the other vendor before it continues", async () => {
	const { fail, switched } = harness({ provider: "openai", id: "gpt-6.1-sol" });
	const result = (await fail("openai", "gpt-6.1-sol")) as { continue?: boolean };
	assert.deepEqual(switched, ["anthropic/claude-opus-5-5"]);
	assert.equal(result?.continue, true);
});

test("a concrete-model session whose switch fails stops instead of retrying the exhausted vendor", async () => {
	const { fail, switched, notices } = harness({ provider: "openai", id: "gpt-6.1-sol" }, { authorized: false });
	const result = (await fail("openai", "gpt-6.1-sol")) as { continue?: boolean } | undefined;
	assert.deepEqual(switched, ["anthropic/claude-opus-5-5"]);
	assert.notEqual(result?.continue, true);
	assert.match(notices.join("\n"), /\/model/);
});

test("a model with no known equivalent stops and says how to switch by hand", async () => {
	const { fail, switched, notices } = harness({ provider: "openai", id: "gpt-4o" });
	const result = (await fail("openai", "gpt-4o")) as { continue?: boolean } | undefined;
	assert.deepEqual(switched, []);
	assert.notEqual(result?.continue, true);
	assert.match(notices.join("\n"), /\/model/);
});

test("tier families map to their equivalents", async () => {
	const { fail, switched } = harness({ provider: "openai", id: "gpt-5.6-terra" });
	await fail("openai", "gpt-5.6-terra");
	assert.deepEqual(switched, ["anthropic/claude-sonnet-5-5"]);
});

test("a virtual failover session keeps routing through the router without a model switch", async () => {
	const { fail, switched } = harness({ provider: "failover", id: "sol" });
	const result = (await fail("openai", "gpt-5.6-sol")) as { continue?: boolean };
	assert.deepEqual(switched, []);
	assert.equal(result?.continue, true);
});

test("both vendors exhausted stops without switching", async () => {
	fs.mkdirSync(path.dirname(health), { recursive: true });
	fs.writeFileSync(health, JSON.stringify({ anthropic: { until: Date.now() / 1000 + 3600, message: "x", marked: 0 } }));
	const { fail, switched } = harness({ provider: "openai", id: "gpt-6.1-sol" });
	const result = (await fail("openai", "gpt-6.1-sol")) as { continue?: boolean } | undefined;
	assert.deepEqual(switched, []);
	assert.notEqual(result?.continue, true);
});
