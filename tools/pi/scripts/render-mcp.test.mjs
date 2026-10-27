import assert from "node:assert/strict";
import fs from "node:fs";
import {
  existsSync,
  readFileSync,
  mkdtempSync as makeTempDir,
  writeFileSync,
  rmSync,
  symlinkSync,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import test from "node:test";
const here = path.dirname(fileURLToPath(import.meta.url));
const renderer = path.join(here, "render-mcp.mjs");
const mkdtempSync = (prefix) => fs.realpathSync(makeTempDir(prefix));
test("native MCP renderer exists", () =>
  assert.ok(
    existsSync(renderer),
    "Native preserving renderer is not implemented",
  ));
test("setup uses builtin MCP rather than installing the adapter", () => {
  const setup = readFileSync(path.resolve(here, "../setup.md"), "utf8");
  assert.doesNotMatch(setup, /pi install npm:pi-mcp-adapter/);
  assert.match(setup, /render-mcp\.mjs/);
  const catalog = readFileSync(
    path.resolve(here, "../../../common/mcp/servers.jsonc"),
    "utf8",
  );
  assert.match(catalog, /"enabled": false/);
});
if (existsSync(renderer)) {
  const { mergeMcpCatalog, parseJsonc, renderMcpCatalog } =
    await import("./render-mcp.mjs");
  test("JSONC retains URL and quoted slash values", () =>
    assert.deepEqual(
      parseJsonc(
        '{/*a*/"url":"https://example.test/mcp",//note\n"value":"a\\\"//b"}',
      ),
      { url: "https://example.test/mcp", value: 'a"//b' },
    ));
  test("JSONC trailing commas preserve quoted commas and reject array holes", () => {
    assert.deepEqual(parseJsonc('{"x":{"command":"tool",},}'), {
      x: { command: "tool" },
    });
    assert.deepEqual(
      parseJsonc('{"value":",}","list":[1,2,/* note */],//note\n}'),
      { value: ",}", list: [1, 2] },
    );
    assert.throws(() => parseJsonc("[,]"));
    assert.throws(() => parseJsonc("[1,,]"));
  });
  test("existing target symlinks are rejected without reading their referents", () => {
    const dir = mkdtempSync(path.join(tmpdir(), "native-mcp-test-"));
    const originalRead = fs.readFileSync;
    let reads = 0;
    try {
      const target = path.join(dir, "mcp.json");
      const referent = path.join(dir, "synthetic-private.json");
      const catalog = path.join(dir, "catalog.jsonc");
      writeFileSync(referent, '{"mcpServers":{}}');
      writeFileSync(catalog, "{}");
      symlinkSync(referent, target);
      fs.readFileSync = (file, ...args) => {
        if (file === target || typeof file === "number") reads++;
        return originalRead(file, ...args);
      };
      let error;
      try {
        renderMcpCatalog(catalog, target, "/repo", "/home");
      } catch (e) {
        error = e;
      }
      assert.equal(reads, 0);
      assert.match(error?.message ?? "", /ownership/);
    } finally {
      fs.readFileSync = originalRead;
      rmSync(dir, { recursive: true, force: true });
    }
  });
  test("non-regular and hard-linked targets are refused before reading", () => {
    const dir = mkdtempSync(path.join(tmpdir(), "native-mcp-test-"));
    const originalRead = fs.readFileSync;
    let reads = 0;
    try {
      const target = path.join(dir, "mcp.json");
      const catalog = path.join(dir, "catalog.jsonc");
      const referent = path.join(dir, "synthetic-private.json");
      writeFileSync(catalog, "{}");
      fs.mkdirSync(target);
      fs.readFileSync = (file, ...args) => {
        if (file === target || typeof file === "number") reads++;
        return originalRead(file, ...args);
      };
      assert.throws(
        () => renderMcpCatalog(catalog, target, "/repo", "/home"),
        /ownership/,
      );
      rmSync(target, { recursive: true });
      writeFileSync(referent, '{"mcpServers":{}}');
      fs.linkSync(referent, target);
      assert.throws(
        () => renderMcpCatalog(catalog, target, "/repo", "/home"),
        /ownership/,
      );
      assert.equal(reads, 0);
    } finally {
      fs.readFileSync = originalRead;
      rmSync(dir, { recursive: true, force: true });
    }
  });
  test("parent symlinks are refused for missing and existing targets without reads or writes", () => {
    const dir = mkdtempSync(path.join(tmpdir(), "native-mcp-test-"));
    const originalRead = fs.readFileSync;
    try {
      const external = path.join(dir, "external-owner");
      const alias = path.join(dir, "pi-dir");
      const catalog = path.join(dir, "catalog.jsonc");
      fs.mkdirSync(external);
      symlinkSync(external, alias);
      writeFileSync(catalog, "{}");
      const target = path.join(alias, "mcp.json");
      assert.throws(
        () => renderMcpCatalog(catalog, target, "/repo", "/home"),
        /ownership/,
      );
      assert.deepEqual(fs.readdirSync(external), []);
      writeFileSync(path.join(external, "mcp.json"), "PRIVATE_FIXTURE");
      let reads = 0;
      fs.readFileSync = (...args) => {
        reads++;
        return originalRead(...args);
      };
      assert.throws(
        () => renderMcpCatalog(catalog, target, "/repo", "/home"),
        /ownership/,
      );
      assert.equal(reads, 0);
    } finally {
      fs.readFileSync = originalRead;
      rmSync(dir, { recursive: true, force: true });
    }
  });
  test("missing parent directories need ownership preparation rather than recursive creation", () => {
    const dir = mkdtempSync(path.join(tmpdir(), "native-mcp-test-"));
    try {
      const catalog = path.join(dir, "catalog.jsonc");
      const target = path.join(dir, "missing", "mcp.json");
      writeFileSync(catalog, "{}");
      assert.throws(
        () => renderMcpCatalog(catalog, target, "/repo", "/home"),
        /ownership/,
      );
      assert.equal(existsSync(path.dirname(target)), false);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
  test("parent ownership mismatch is refused before reading target data", () => {
    const dir = mkdtempSync(path.join(tmpdir(), "native-mcp-test-"));
    const originalStat = fs.lstatSync;
    try {
      const catalog = path.join(dir, "catalog.jsonc");
      const target = path.join(dir, "mcp.json");
      writeFileSync(catalog, "{}");
      fs.lstatSync = (file, ...args) => {
        const result = originalStat(file, ...args);
        return file === dir
          ? Object.assign(Object.create(result), { uid: process.getuid() + 1 })
          : result;
      };
      assert.throws(
        () => renderMcpCatalog(catalog, target, "/repo", "/home"),
        /ownership/,
      );
      assert.equal(existsSync(target), false);
    } finally {
      fs.lstatSync = originalStat;
      rmSync(dir, { recursive: true, force: true });
    }
  });
  test("a parent link introduced after the missing-target probe is refused before writing", () => {
    const dir = mkdtempSync(path.join(tmpdir(), "native-mcp-test-"));
    const originalOpen = fs.openSync;
    try {
      const parent = path.join(dir, "agent");
      const external = path.join(dir, "external-owner");
      const catalog = path.join(dir, "catalog.jsonc");
      const target = path.join(parent, "mcp.json");
      fs.mkdirSync(parent);
      fs.mkdirSync(external);
      writeFileSync(catalog, "{}");
      let swapped = false;
      fs.openSync = (file, ...args) => {
        try {
          return originalOpen(file, ...args);
        } catch (error) {
          if (file === target && error.code === "ENOENT" && !swapped) {
            swapped = true;
            fs.rmdirSync(parent);
            symlinkSync(external, parent);
          }
          throw error;
        }
      };
      assert.throws(
        () => renderMcpCatalog(catalog, target, "/repo", "/home"),
        /ownership/,
      );
      assert.equal(swapped, true);
      assert.deepEqual(fs.readdirSync(external), []);
    } finally {
      fs.openSync = originalOpen;
      rmSync(dir, { recursive: true, force: true });
    }
  });
  test("disabled catalog example uses native semantics", () => {
    const catalog = parseJsonc(
      readFileSync(
        path.resolve(here, "../../../common/mcp/servers.jsonc"),
        "utf8",
      ),
    );
    const result = mergeMcpCatalog({}, catalog, "/repo", "/home");
    assert.equal(result.mcpServers["example-server"].enabled, false);
    assert.equal("disabled" in result.mcpServers["example-server"], false);
  });
  test("preserves personal servers and top-level settings without mutating inputs", () => {
    const existing = {
      autoEnableCodemode: false,
      mcpServers: {
        personal: { url: "https://example.test/mcp", enabled: false },
      },
    };
    const before = structuredClone(existing);
    const result = mergeMcpCatalog(
      existing,
      { extra: { command: "tool", enabled: false } },
      "/repo",
      "/home",
    );
    assert.deepEqual(existing, before);
    assert.deepEqual(result.mcpServers.personal, before.mcpServers.personal);
    assert.equal(result.autoEnableCodemode, false);
  });
  test("collisions are rejected instead of silently overwritten", () =>
    assert.throws(
      () =>
        mergeMcpCatalog(
          { mcpServers: { x: { command: "old" } } },
          { x: { command: "new" } },
          "/repo",
          "/home",
        ),
      /collision/,
    ));
  test("native hyphen/underscore aliases cannot shadow personal servers", () =>
    assert.throws(
      () =>
        mergeMcpCatalog(
          { mcpServers: { "a-b": { command: "old" } } },
          { a_b: { command: "new" } },
          "/repo",
          "/home",
        ),
      /collision/,
    ));
  test("identical entries are idempotent", () => {
    const input = { mcpServers: { x: { command: "tool", enabled: false } } };
    assert.deepEqual(
      mergeMcpCatalog(
        input,
        { x: { enabled: false, command: "tool" } },
        "/repo",
        "/home",
      ),
      input,
    );
  });
  test("expands catalog paths but retains runtime interpolation", () =>
    assert.equal(
      mergeMcpCatalog(
        {},
        { x: { command: "$HOME/bin/tool", args: ["$REPO/${TOKEN}"] } },
        "/repo",
        "/home",
      ).mcpServers.x.args[0],
      "/repo/${TOKEN}",
    ));
  test("rejects malformed maps and contradictory enablement", () => {
    assert.throws(() =>
      mergeMcpCatalog({ mcpServers: [] }, {}, "/repo", "/home"),
    );
    assert.throws(() =>
      mergeMcpCatalog(
        {},
        { x: { command: "tool", enabled: true, disabled: true } },
        "/repo",
        "/home",
      ),
    );
  });
  test("CLI preserves existing config bytes when a merge needs approval", () => {
    const dir = mkdtempSync(path.join(tmpdir(), "native-mcp-test-"));
    try {
      const target = path.join(dir, "mcp.json");
      const catalog = path.join(dir, "catalog.jsonc");
      const original =
        '{"autoEnableCodemode":false,"mcpServers":{"personal":{"command":"local"}}}\n';
      writeFileSync(target, original);
      writeFileSync(catalog, '{"new":{"command":"new-tool","enabled":false}}');
      const run = spawnSync(
        process.execPath,
        [renderer, catalog, target, "/repo", "/home"],
        { encoding: "utf8" },
      );
      assert.notEqual(run.status, 0);
      assert.match(run.stderr, /approval/);
      assert.equal(readFileSync(target, "utf8"), original);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
  test("CLI invoked through a source symlink still executes", () => {
    const dir = mkdtempSync(path.join(tmpdir(), "native-mcp-test-"));
    try {
      const alias = path.join(dir, "alias.mjs");
      const catalog = path.join(dir, "catalog.jsonc");
      const target = path.join(dir, "mcp.json");
      symlinkSync(renderer, alias);
      writeFileSync(catalog, '{"x":{"command":"local","enabled":false}}');
      assert.equal(
        spawnSync(process.execPath, [alias, catalog, target, "/repo", "/home"])
          .status,
        0,
      );
      assert.ok(existsSync(target));
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
  test("CLI errors do not echo malformed personal configuration values", () => {
    const dir = mkdtempSync(path.join(tmpdir(), "native-mcp-test-"));
    try {
      const catalog = path.join(dir, "catalog.jsonc");
      const target = path.join(dir, "mcp.json");
      writeFileSync(catalog, "{}");
      writeFileSync(target, "SYNTHETIC_PRIVATE_VALUE");
      const run = spawnSync(
        process.execPath,
        [renderer, catalog, target, "/repo", "/home"],
        { encoding: "utf8" },
      );
      assert.notEqual(run.status, 0);
      assert.doesNotMatch(run.stderr, /SYNTHETIC_PRIVATE_VALUE/);
      assert.equal(readFileSync(target, "utf8"), "SYNTHETIC_PRIVATE_VALUE");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
  const invalidServers = [
    ["missing transport", { enabled: false }],
    ["invalid args", { command: "not-executed", args: "PRIVATE_FIXTURE" }],
    ["legacy SSE", { type: "sse", url: "https://example.test/sse" }],
    [
      "invalid exposure",
      { command: "not-executed", exposure: "PRIVATE_FIXTURE" },
    ],
    ["invalid timeout", { command: "not-executed", timeout: 0 }],
    ["invalid description", { command: "not-executed", description: [] }],
    [
      "invalid tool exposure map",
      { command: "not-executed", toolExposure: [] },
    ],
    [
      "invalid tool exposure",
      { command: "not-executed", toolExposure: { x: "PRIVATE_FIXTURE" } },
    ],
    ["invalid env", { command: "not-executed", env: { TOKEN: 1 } }],
    ["invalid cwd", { command: "not-executed", cwd: [] }],
    ["invalid URL", { url: "file:///PRIVATE_FIXTURE" }],
    [
      "invalid headers",
      { url: "https://example.test/mcp", headers: { Authorization: 1 } },
    ],
    ["invalid OAuth", { url: "https://example.test/mcp", oauth: [] }],
    [
      "invalid OAuth client",
      { url: "https://example.test/mcp", oauth: { clientId: 1 } },
    ],
    [
      "invalid OAuth secret",
      { url: "https://example.test/mcp", oauth: { clientSecret: 1 } },
    ],
    [
      "invalid OAuth scope",
      { url: "https://example.test/mcp", oauth: { scope: [] } },
    ],
    [
      "invalid OAuth name",
      { url: "https://example.test/mcp", oauth: { clientName: " " } },
    ],
    [
      "invalid OAuth port",
      { url: "https://example.test/mcp", oauth: { callbackPort: 65536 } },
    ],
    [
      "invalid OAuth callback",
      {
        url: "https://example.test/mcp",
        oauth: { callbackUrl: "https://example.test/callback" },
      },
    ],
    [
      "conflicting OAuth ports",
      {
        url: "https://example.test/mcp",
        oauth: {
          callbackUrl: "http://localhost:8765/callback",
          callbackPort: 8766,
        },
      },
    ],
    [
      "invalid registration",
      {
        url: "https://example.test/mcp",
        oauth: { clientRegistration: "PRIVATE_FIXTURE" },
      },
    ],
    [
      "conflicting registration",
      {
        url: "https://example.test/mcp",
        oauth: { clientRegistration: "cimd", clientId: "PRIVATE_FIXTURE" },
      },
    ],
    [
      "invalid CIMD callback",
      {
        url: "https://example.test/mcp",
        oauth: {
          clientRegistration: "cimd",
          callbackUrl: "http://localhost/other",
        },
      },
    ],
    [
      "invalid OAuth metadata",
      {
        url: "https://example.test/mcp",
        oauth: { authServerMetadataUrl: "http://example.test/PRIVATE_FIXTURE" },
      },
    ],
    [
      "invalid provider auth",
      { url: "https://example.test/mcp", auth: { provider: "" } },
    ],
    [
      "insecure provider auth",
      { url: "http://example.test/mcp", auth: { provider: "PRIVATE_FIXTURE" } },
    ],
  ];
  for (const [label, entry] of invalidServers) {
    test(`CLI refuses ${label} before creating a configuration`, () => {
      const dir = mkdtempSync(path.join(tmpdir(), "native-mcp-test-"));
      try {
        const catalog = path.join(dir, "catalog.jsonc");
        const target = path.join(dir, "missing", "mcp.json");
        writeFileSync(
          catalog,
          JSON.stringify({ example: { enabled: false, ...entry } }),
        );
        const run = spawnSync(
          process.execPath,
          [renderer, catalog, target, "/repo", "/home"],
          { encoding: "utf8" },
        );
        assert.notEqual(run.status, 0);
        assert.equal(existsSync(target), false);
        assert.equal(existsSync(path.dirname(target)), false);
        assert.doesNotMatch(run.stderr, /PRIVATE_FIXTURE/);
        assert.match(run.stderr, /Invalid MCP server example:/);
      } finally {
        rmSync(dir, { recursive: true, force: true });
      }
    });
  }
  test("CLI leaves a native-valid BOM configuration byte-for-byte unchanged", () => {
    const dir = mkdtempSync(path.join(tmpdir(), "native-mcp-test-"));
    try {
      const catalog = path.join(dir, "catalog.jsonc");
      const target = path.join(dir, "mcp.json");
      const entry = { example: { command: "not-executed", enabled: false } };
      const original =
        "\uFEFF" + JSON.stringify({ mcpServers: entry }, null, 4) + "\n";
      writeFileSync(target, original);
      writeFileSync(catalog, "\uFEFF" + JSON.stringify(entry));
      const before = fs.statSync(target);
      const run = spawnSync(
        process.execPath,
        [renderer, catalog, target, "/repo", "/home"],
        { encoding: "utf8" },
      );
      assert.equal(run.status, 0, run.stderr);
      assert.match(run.stdout, /unchanged/);
      assert.equal(readFileSync(target, "utf8"), original);
      assert.equal(fs.statSync(target).mtimeMs, before.mtimeMs);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
  test("documented valid transports and OAuth fields survive rendering", () => {
    const entries = {
      local: {
        command: "not-executed",
        args: ["$REPO/tool"],
        env: { TOKEN: "${TOKEN}" },
        cwd: "$HOME",
        timeout: 1,
        description: "fixture",
        exposure: "codemode-deferred",
        toolExposure: { "read_*": "direct" },
      },
      remote: {
        type: "streamable-http",
        url: "https://example.test/mcp",
        headers: { Accept: "application/json" },
        oauth: {
          clientId: "fixture",
          clientSecret: "${SECRET}",
          callbackPort: 8765,
          callbackUrl: "http://127.0.0.1:8765/callback",
          scope: "read",
          clientName: "fixture",
          authServerMetadataUrl: "https://example.test/metadata",
          clientRegistration: "dcr",
        },
      },
      cimd: {
        url: "https://example.test/mcp",
        oauth: {
          clientRegistration: "cimd",
          callbackUrl: "http://localhost/callback",
        },
      },
      provider: { url: "http://[::1]/mcp", auth: { provider: "fixture" } },
    };
    const rendered = mergeMcpCatalog({}, entries, "/repo", "/home");
    assert.equal(rendered.mcpServers.local.cwd, "/home");
    assert.deepEqual(rendered.mcpServers.local.args, ["/repo/tool"]);
    assert.deepEqual(rendered.mcpServers.local.env, { TOKEN: "${TOKEN}" });
    assert.deepEqual(rendered.mcpServers.remote, entries.remote);
    assert.deepEqual(rendered.mcpServers.cimd, entries.cimd);
    assert.deepEqual(rendered.mcpServers.provider, entries.provider);
  });
  test("retains native-accepted stdio shapes without imposing HTTP validation", () => {
    for (const server of [
      { command: "", enabled: false },
      {
        command: "not-executed",
        enabled: false,
        headers: { Authorization: 7 },
        oauth: [],
        auth: { provider: "" },
      },
    ]) {
      assert.deepEqual(
        mergeMcpCatalog({}, { fixture: server }, "/repo", "/home").mcpServers
          .fixture,
        server,
      );
    }
  });
  test("CLI creates a disabled new installation and reruns without writing", () => {
    const dir = mkdtempSync(path.join(tmpdir(), "native-mcp-test-"));
    try {
      const target = path.join(dir, "mcp.json");
      const catalog = path.join(dir, "catalog.jsonc");
      writeFileSync(
        catalog,
        '{"example":{"command":"not-executed","enabled":false}}',
      );
      const args = [renderer, catalog, target, "/repo", "/home"];
      assert.equal(spawnSync(process.execPath, args).status, 0);
      const before = readFileSync(target, "utf8");
      assert.equal(JSON.parse(before).mcpServers.example.enabled, false);
      assert.equal(spawnSync(process.execPath, args).status, 0);
      assert.equal(readFileSync(target, "utf8"), before);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
}
