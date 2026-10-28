import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { loadManifest, scriptsForEvent } from "./index.ts";

test("hook tool selectors keep Pi-only checks out of Claude and Codex", () => {
	const manifest = {
		SessionStart: [
			{ script: "$REPO/common/hooks/model-route-check.sh", tools: ["pi"] },
			{ script: "$REPO/common/hooks/startup-checklist.sh" },
			{ script: "$REPO/common/hooks/claude-only.sh", tools: ["claude"] },
		],
	};

	assert.deepEqual(scriptsForEvent(manifest, "SessionStart", "pi", "/repo"), [
		"/repo/common/hooks/model-route-check.sh",
		"/repo/common/hooks/startup-checklist.sh",
	]);
	assert.deepEqual(scriptsForEvent(manifest, "SessionStart", "claude", "/repo"), [
		"/repo/common/hooks/startup-checklist.sh",
		"/repo/common/hooks/claude-only.sh",
	]);
	assert.deepEqual(scriptsForEvent(manifest, "SessionStart", "codex", "/repo"), [
		"/repo/common/hooks/startup-checklist.sh",
	]);
});

test("malformed tool selectors fail closed instead of widening hook scope", () => {
	const manifest = {
		SessionStart: [
			{ script: "$REPO/common/hooks/not-an-array.sh", tools: "pi" as unknown as string[] },
			{ script: "$REPO/common/hooks/not-strings.sh", tools: ["pi", 42] as unknown as string[] },
		],
	};

	assert.deepEqual(scriptsForEvent(manifest, "SessionStart", "pi", "/repo"), []);
	assert.deepEqual(scriptsForEvent(
		null as unknown as Parameters<typeof scriptsForEvent>[0],
		"SessionStart",
		"pi",
		"/repo",
	), []);
	assert.deepEqual(scriptsForEvent(
		{ SessionStart: "not-an-array" as unknown as never[] },
		"SessionStart",
		"pi",
		"/repo",
	), []);
	assert.deepEqual(scriptsForEvent(
		{ SessionStart: [{ script: 42 as unknown as string }] },
		"SessionStart",
		"pi",
		"/repo",
	), []);
});

test("the checked-in model route preflight is Pi-only", () => {
	const repoRoot = fileURLToPath(new URL("../../../..", import.meta.url));
	const manifest = loadManifest(repoRoot);
	const piScripts = scriptsForEvent(manifest, "SessionStart", "pi", repoRoot);
	const claudeScripts = scriptsForEvent(manifest, "SessionStart", "claude", repoRoot);
	const codexScripts = scriptsForEvent(manifest, "SessionStart", "codex", repoRoot);

	assert.equal(piScripts.some((script) => script.endsWith("/model-route-check.sh")), true);
	assert.equal(claudeScripts.some((script) => script.endsWith("/model-route-check.sh")), false);
	assert.equal(codexScripts.some((script) => script.endsWith("/model-route-check.sh")), false);
});
