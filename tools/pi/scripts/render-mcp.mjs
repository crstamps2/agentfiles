import fs from "node:fs";
import path from "node:path";
import { isDeepStrictEqual } from "node:util";
import { fileURLToPath } from "node:url";

export function parseJsonc(text) {
  let out = "",
    quoted = false,
    line = false,
    block = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i],
      next = text[i + 1];
    if (line) {
      if (c === "\n") {
        line = false;
        out += c;
      }
      continue;
    }
    if (block) {
      if (c === "*" && next === "/") {
        block = false;
        i++;
      }
      continue;
    }
    if (quoted) {
      out += c;
      if (c === "\\") out += text[++i] ?? "";
      else if (c === '"') quoted = false;
      continue;
    }
    if (c === '"') quoted = true;
    else if (c === "/" && next === "/") {
      line = true;
      i++;
      continue;
    } else if (c === "/" && next === "*") {
      block = true;
      out += " ";
      i++;
      continue;
    }
    out += c;
  }
  if (block) throw new Error("Unterminated JSONC comment");
  let normalized = "";
  quoted = false;
  for (let i = 0; i < out.length; i++) {
    const c = out[i];
    if (quoted) {
      normalized += c;
      if (c === "\\") normalized += out[++i] ?? "";
      else if (c === '"') quoted = false;
      continue;
    }
    if (c === '"') quoted = true;
    if (c === ",") {
      let next = i + 1,
        previous = i - 1;
      while (/\s/.test(out[next] ?? "") && next < out.length) next++;
      while (previous >= 0 && /\s/.test(out[previous])) previous--;
      if (
        (out[next] === "}" || out[next] === "]") &&
        previous >= 0 &&
        !"[{,:".includes(out[previous])
      )
        continue;
    }
    normalized += c;
  }
  return JSON.parse(normalized.replace(/^\uFEFF/, ""));
}
const record = (value) =>
  value !== null && typeof value === "object" && !Array.isArray(value);
const stringMap = (value) =>
  record(value) &&
  Object.values(value).every((entry) => typeof entry === "string");
const exposures = new Set([
  "codemode",
  "codemode-deferred",
  "deferred",
  "direct",
  "hidden",
]);
const loopback = new Set(["localhost", "127.0.0.1", "[::1]"]);
const parsedUrl = (value) =>
  typeof value === "string" && URL.canParse(value) ? new URL(value) : undefined;

function validateServer(name, server) {
  const require = (valid, field) => {
    if (!valid) throw new Error(`Invalid MCP server ${name}: ${field}`);
  };
  if (server.exposure !== undefined)
    require(exposures.has(server.exposure), "exposure");
  if (server.description !== undefined)
    require(typeof server.description === "string", "description");
  if (server.timeout !== undefined)
    require(typeof server.timeout === "number" &&
      server.timeout > 0, "timeout");
  if (server.toolExposure !== undefined)
    require(record(server.toolExposure) &&
      Object.values(server.toolExposure).every((entry) =>
        exposures.has(entry),
      ), "toolExposure");
  require(server.type !== "sse", "type (legacy SSE is unsupported)");
  if (
    typeof server.url === "string" &&
    (server.type === undefined ||
      ["http", "streamable-http"].includes(server.type))
  ) {
    const url = parsedUrl(server.url);
    require(url && ["http:", "https:"].includes(url.protocol), "url");
    if (server.headers !== undefined)
      require(stringMap(server.headers), "headers");
    if (server.auth !== undefined) {
      require(record(server.auth) &&
        typeof server.auth.provider === "string" &&
        server.auth.provider.length > 0, "auth.provider");
      require(url.protocol === "https:" ||
        loopback.has(url.hostname), "auth transport");
    }
    if (server.oauth !== undefined) {
      const oauth = server.oauth;
      require(record(oauth), "oauth");
      for (const key of ["clientId", "clientSecret", "scope"])
        if (oauth[key] !== undefined)
          require(typeof oauth[key] === "string", `oauth.${key}`);
      if (oauth.clientName !== undefined)
        require(typeof oauth.clientName === "string" &&
          oauth.clientName.trim().length > 0, "oauth.clientName");
      const port = oauth.callbackPort;
      if (port !== undefined)
        require(Number.isInteger(port) &&
          port >= 1 &&
          port <= 65535, "oauth.callbackPort");
      const callback = parsedUrl(oauth.callbackUrl);
      if (oauth.callbackUrl !== undefined) {
        require(callback &&
          callback.protocol === "http:" &&
          loopback.has(callback.hostname) &&
          callback.search === "" &&
          callback.hash === "", "oauth.callbackUrl");
        require(!callback.port ||
          port === undefined ||
          Number(callback.port) === port, "oauth callback ports");
      }
      if (oauth.clientRegistration !== undefined) {
        require(["dcr", "cimd"].includes(
          oauth.clientRegistration,
        ), "oauth.clientRegistration");
        if (oauth.clientRegistration === "cimd") {
          require(oauth.clientId === undefined &&
            oauth.clientName === undefined, "oauth CIMD client");
          require(!callback ||
            (callback.hostname !== "[::1]" &&
              callback.pathname === "/callback"), "oauth CIMD callback");
        }
      }
      if (oauth.authServerMetadataUrl !== undefined) {
        const metadata = parsedUrl(oauth.authServerMetadataUrl);
        require(metadata &&
          (metadata.protocol === "https:" ||
            (metadata.protocol === "http:" &&
              loopback.has(metadata.hostname))), "oauth.authServerMetadataUrl");
      }
    }
    return;
  }
  require(typeof server.command === "string" &&
    (server.type === undefined ||
      server.type === "stdio"), "command or url transport");
  if (server.args !== undefined)
    require(Array.isArray(server.args) &&
      server.args.every((entry) => typeof entry === "string"), "args");
  if (server.env !== undefined) require(stringMap(server.env), "env");
  if (server.cwd !== undefined) require(typeof server.cwd === "string", "cwd");
}

export function mergeMcpCatalog(existing, catalog, repo, home) {
  if (
    !record(existing) ||
    !record(catalog) ||
    (existing.mcpServers !== undefined && !record(existing.mcpServers))
  )
    throw new Error("MCP configuration and server maps must be objects");
  const result = structuredClone(existing);
  result.mcpServers ??= {};
  const expand = (value) => {
    if (typeof value === "string")
      return value.replace(/\$(REPO|HOME)\b/g, (_, key) =>
        key === "REPO" ? repo : home,
      );
    if (Array.isArray(value)) return value.map(expand);
    if (record(value))
      return Object.fromEntries(
        Object.entries(value).map(([k, v]) => [k, expand(v)]),
      );
    return value;
  };
  for (const [name, raw] of Object.entries(catalog)) {
    if (!/^[A-Za-z0-9_-]+$/.test(name) || !record(raw))
      throw new Error("Invalid catalog server");
    if (
      raw.tools !== undefined &&
      (!Array.isArray(raw.tools) ||
        !raw.tools.every((x) => typeof x === "string"))
    )
      throw new Error("Invalid catalog tool selection");
    if (raw.tools && !raw.tools.includes("pi")) continue;
    const server = expand(raw);
    delete server.tools;
    if (server.enabled !== undefined && typeof server.enabled !== "boolean")
      throw new Error("enabled must be boolean");
    if (server.disabled !== undefined) {
      if (
        typeof server.disabled !== "boolean" ||
        (server.enabled !== undefined && server.enabled !== !server.disabled)
      )
        throw new Error("Contradictory catalog enablement");
      server.enabled = !server.disabled;
      delete server.disabled;
    }
    validateServer(name, server);
    const alias = Object.keys(result.mcpServers).find(
      (key) =>
        key !== name && key.replaceAll("-", "_") === name.replaceAll("-", "_"),
    );
    if (
      alias ||
      (Object.hasOwn(result.mcpServers, name) &&
        !isDeepStrictEqual(result.mcpServers[name], server))
    )
      throw new Error(`MCP server collision: ${name}; approval required`);
    Object.defineProperty(result.mcpServers, name, {
      value: server,
      enumerable: true,
      writable: true,
      configurable: true,
    });
  }
  return result;
}

// Node has no portable descriptor-relative creation; require prepared parents.
function validateParents(targetPath, requireExisting = false) {
  const refused = () =>
    new Error("MCP parent path requires ownership approval; unchanged");
  const uid = process.getuid?.();
  if (uid === undefined) throw refused();
  const parent = path.dirname(targetPath);
  const root = path.parse(parent).root;
  let cursor = root;
  let owned = false;
  for (const part of path
    .relative(root, parent)
    .split(path.sep)
    .filter(Boolean)) {
    cursor = path.join(cursor, part);
    let stat;
    try {
      stat = fs.lstatSync(cursor);
    } catch (error) {
      if (error.code === "ENOENT" && !requireExisting && owned) return;
      throw refused();
    }
    const inOwnedTree = owned || stat.uid === uid;
    if (
      !stat.isDirectory() ||
      stat.isSymbolicLink() ||
      (stat.uid !== uid && (owned || stat.uid !== 0)) ||
      (inOwnedTree && (stat.mode & 0o022) !== 0)
    )
      throw refused();
    owned = inOwnedTree;
  }
  if (!owned) throw refused();
}

export function renderMcpCatalog(catalogPath, targetPath, repo, home) {
  targetPath = path.resolve(targetPath);
  validateParents(targetPath);
  let descriptor;
  try {
    descriptor = fs.openSync(
      targetPath,
      fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK,
    );
  } catch (error) {
    if (error.code !== "ENOENT")
      throw new Error(
        "Existing MCP target requires ownership approval; unchanged",
      );
  }
  const exists = descriptor !== undefined;
  let existing = {},
    catalog;
  if (exists) {
    try {
      validateParents(targetPath, true);
      const stat = fs.fstatSync(descriptor);
      if (
        !stat.isFile() ||
        stat.nlink !== 1 ||
        (process.getuid && stat.uid !== process.getuid())
      )
        throw new Error(
          "Existing MCP target requires ownership approval; unchanged",
        );
      const text = fs.readFileSync(descriptor, "utf8");
      try {
        existing = JSON.parse(text.replace(/^\uFEFF/, ""));
      } catch {
        throw new Error(
          "Existing MCP configuration is not valid JSON; unchanged",
        );
      }
    } finally {
      fs.closeSync(descriptor);
    }
  }
  const text = fs.readFileSync(catalogPath, "utf8");
  try {
    catalog = parseJsonc(text);
  } catch {
    throw new Error("MCP catalog is not valid JSONC");
  }
  const merged = mergeMcpCatalog(existing, catalog, repo, home);
  if (exists) {
    if (!isDeepStrictEqual(existing, merged))
      throw new Error(
        "Existing MCP configuration requires explicit merge approval; no changes written",
      );
    return "unchanged";
  }
  validateParents(targetPath, true);
  fs.writeFileSync(targetPath, JSON.stringify(merged, null, 2) + "\n", {
    flag: "wx",
    mode: 0o600,
  });
  return "created";
}
if (
  process.argv[1] &&
  fs.existsSync(process.argv[1]) &&
  fs.realpathSync(process.argv[1]) === fileURLToPath(import.meta.url)
) {
  try {
    const args = process.argv.slice(2);
    if (args.length !== 4)
      throw new Error("Usage: render-mcp.mjs <catalog> <target> <repo> <home>");
    console.log(`MCP catalog: ${renderMcpCatalog(...args)}`);
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
