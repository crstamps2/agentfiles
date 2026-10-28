import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const hook = fileURLToPath(new URL("./model-route-check.sh", import.meta.url));

const REGISTRY = `provider      model                 context  max-out  thinking  images
anthropic     claude-sonnet-5-5   1M       128K     yes       yes
failover      sol                   0          0     yes       yes
ollama-local  gpt-oss:20b        128K        32K     yes       no
openai        gpt-5.6-terra      272K       128K     yes       yes
`;

function fixture(run) {
  const root = fs.mkdtempSync(path.join(tmpdir(), "model-route-check-test-"));
  const home = path.join(root, "home");
  const repo = path.join(root, "agentfiles");
  const bin = path.join(root, "bin");
  const agentDir = path.join(home, ".pi", "agent");
  fs.mkdirSync(path.join(repo, "common"), { recursive: true });
  fs.mkdirSync(path.join(agentDir, "agents"), { recursive: true });
  fs.mkdirSync(bin, { recursive: true });

  fs.writeFileSync(
    path.join(bin, "pi"),
    `#!/bin/sh\nprintf '%s' '${REGISTRY.replaceAll("'", "'\\''")}'\n`,
    { mode: 0o755 },
  );
  fs.writeFileSync(
    path.join(agentDir, "settings.json"),
    JSON.stringify({
      defaultProvider: "failover",
      defaultModel: "sol",
      subagents: { defaultModel: "openai/gpt-5.6-terra" },
    }),
  );
  fs.writeFileSync(
    path.join(agentDir, "models.json"),
    JSON.stringify({
      providers: {
        openai: { modelOverrides: { "gpt-5.6-terra": { contextWindow: 272000 } } },
      },
    }),
  );
  fs.writeFileSync(
    path.join(agentDir, "agents", "worker.md"),
    "---\nname: worker\nmodel: openai/gpt-5.6-terra:max\nthinking: max\n---\n",
  );
  fs.writeFileSync(
    path.join(agentDir, "agents", "local.md"),
    "---\nname: local\nmodel: ollama-local/gpt-oss:20b\nthinking: low\n---\n",
  );
  fs.writeFileSync(
    path.join(repo, "common", "model-tiers.toml"),
    '[specialist]\npi_model = "openai/gpt-5.6-terra"\npi_thinking = "xhigh"\n',
  );

  const baseEnv = {
    ...process.env,
    HOME: home,
    AF_REPO: repo,
    PATH: `${bin}:${process.env.PATH}`,
  };
  const invoke = (...args) =>
    spawnSync("bash", [hook, ...args], { env: baseEnv, encoding: "utf8" });
  const invokeWithEnv = (extraEnv, ...args) =>
    spawnSync("bash", [hook, ...args], {
      env: { ...baseEnv, ...extraEnv },
      encoding: "utf8",
    });

  try {
    run({ root, home, repo, agentDir, invoke, invokeWithEnv });
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}

test("shared hook is executable by tool-native hook runners", () => {
  assert.notEqual(fs.statSync(hook).mode & 0o111, 0);
});

test("valid configured routes are silent, including model ids containing a colon", () =>
  fixture(({ invoke }) => {
    const result = invoke("--strict");
    assert.equal(result.status, 0, result.stderr);
    assert.equal(result.stdout, "");
    assert.equal(result.stderr, "");
  }));

test("stale settings and model overrides report their exact configuration paths", () =>
  fixture(({ agentDir, invoke }) => {
    fs.writeFileSync(
      path.join(agentDir, "settings.json"),
      JSON.stringify({
        defaultProvider: "failover",
        defaultModel: "sol",
        subagents: { defaultModel: "openai-codex/gpt-5.6-terra" },
      }),
    );
    fs.writeFileSync(
      path.join(agentDir, "models.json"),
      JSON.stringify({
        providers: {
          "openai-codex": {
            modelOverrides: { "gpt-5.6-terra": { contextWindow: 1050000 } },
          },
        },
      }),
    );

    const result = invoke();
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /^MODEL ROUTE CHECK: unresolved provider\/model routes:/m);
    assert.match(
      result.stdout,
      /~\/\.pi\/agent\/settings\.json:subagents\.defaultModel -> openai-codex\/gpt-5\.6-terra/,
    );
    assert.match(
      result.stdout,
      /~\/\.pi\/agent\/models\.json:providers\.openai-codex\.modelOverrides\.gpt-5\.6-terra -> openai-codex\/gpt-5\.6-terra/,
    );
  }));

test("strict mode exits nonzero when a configured route is unresolved", () =>
  fixture(({ agentDir, invoke }) => {
    fs.writeFileSync(
      path.join(agentDir, "settings.json"),
      JSON.stringify({ subagents: { defaultModel: "openai-codex/gpt-5.6-terra" } }),
    );
    const result = invoke("--strict");
    assert.equal(result.status, 1);
    assert.match(result.stdout, /settings\.json:subagents\.defaultModel/);
  }));

test("a failed Pi registry lookup reports a diagnostic without dumping configuration", () =>
  fixture(({ root, invoke }) => {
    fs.writeFileSync(
      path.join(root, "bin", "pi"),
      "#!/bin/sh\necho registry unavailable >&2\nexit 7\n",
      { mode: 0o755 },
    );
    const result = invoke("--strict");
    assert.equal(result.status, 1);
    assert.match(result.stdout, /^MODEL ROUTE CHECK: could not read active Pi model registry:/);
    assert.doesNotMatch(result.stdout, /contextWindow|defaultModel/);
  }));

test("a hung Pi registry lookup times out with a concise diagnostic", () =>
  fixture(({ root, invokeWithEnv }) => {
    fs.writeFileSync(path.join(root, "bin", "pi"), "#!/bin/sh\nsleep 2\n", {
      mode: 0o755,
    });
    const result = invokeWithEnv({ PI_MODEL_ROUTE_CHECK_TIMEOUT_S: "0.05" }, "--strict");
    assert.equal(result.status, 1);
    assert.match(
      result.stdout,
      /^MODEL ROUTE CHECK: could not read active Pi model registry: timed out after 0\.05s/,
    );
    assert.equal(result.stderr, "");
  }));
