import { randomUUID } from "node:crypto";
import {
  existsSync,
  readFileSync,
  realpathSync,
  statSync,
  writeFileSync,
  constants,
  openSync,
  fstatSync,
  closeSync,
} from "node:fs";
import { userInfo } from "node:os";
import { dirname, isAbsolute, join, resolve, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";

function readManagedText(path, agentDir, maxBytes) {
  const canonical = trustedManagedPath(path, agentDir);
  const fd = openSync(
    canonical,
    constants.O_RDONLY | constants.O_NONBLOCK | constants.O_NOFOLLOW,
  );
  try {
    const entry = fstatSync(fd);
    if (!entry.isFile() || entry.size > maxBytes)
      throw new Error("Expected a bounded regular managed file");
    return readFileSync(fd, "utf8");
  } finally {
    closeSync(fd);
  }
}

export function resolveSdkRoot({ agentDir }) {
  const install = join(agentDir, "install");
  const version = readManagedText(
    join(install, "current-version"),
    agentDir,
    256,
  ).trim();
  if (!/^[A-Za-z0-9][A-Za-z0-9._+-]*$/.test(version))
    throw new Error("Invalid managed Pi version");
  const root = resolve(
    install,
    "releases",
    version,
    "node_modules",
    "@earendil-works",
    "pi-coding-agent",
  );
  const manifest = JSON.parse(
    readManagedText(join(root, "package.json"), agentDir, 1048576),
  );
  if (manifest.name !== "@earendil-works/pi-coding-agent")
    throw new Error(
      `Invalid SDK: expected @earendil-works/pi-coding-agent at ${root}`,
    );
  if (!existsSync(join(root, "dist/index.js")))
    throw new Error(`SDK entry is missing at ${root}`);
  return root;
}

export function rpcRequest(bus, method, params = {}, timeoutMs = 5000) {
  return new Promise((resolveResult, reject) => {
    const requestId = randomUUID();
    const finish = (error, data) => {
      clearTimeout(timer);
      off();
      error ? reject(error) : resolveResult(data);
    };
    const off = bus.on(`subagents:rpc:v1:reply:${requestId}`, (reply) => {
      if (reply?.version !== 1 || reply.requestId !== requestId) return;
      finish(
        reply.success
          ? undefined
          : Error(reply.error?.message || "Native RPC failed"),
        reply.data,
      );
    });
    const timer = setTimeout(
      () =>
        finish(
          Error(
            `Native RPC ${method} timed out; request ${requestId}. Do not blindly respawn.`,
          ),
        ),
      timeoutMs,
    );
    try {
      bus.emit("subagents:rpc:v1:request", {
        version: 1,
        requestId,
        method,
        params,
      });
    } catch (error) {
      finish(error);
    }
  });
}

export function delegateLeaf(
  bus,
  { agent, task, cwd, timeoutMs = 900000, graceMs = 30000, signal, onRequest },
) {
  if (
    !agent?.trim() ||
    !task?.trim() ||
    !isAbsolute(cwd || "") ||
    !Number.isSafeInteger(timeoutMs) ||
    timeoutMs < 1 ||
    timeoutMs > 2147483647
  )
    throw new Error(
      "agent, task, absolute cwd and a positive bounded timeout are required",
    );
  if (!Number.isSafeInteger(graceMs) || graceMs < 0 || graceMs > 2147483647)
    throw new Error("Cancellation grace must be a bounded nonnegative integer");
  const identity = {
    requestId: randomUUID(),
    ownerRunId: randomUUID(),
    nodeId: "leaf",
  };
  return new Promise((resolveResult, reject) => {
    let timer,
      cancelling = false;
    const finish = (error, response) => {
      clearTimeout(timer);
      off();
      signal?.removeEventListener("abort", cancel);
      if (error) error.identity ??= identity;
      error ? reject(error) : resolveResult(response);
    };
    const cancel = () => {
      if (cancelling) return;
      cancelling = true;
      clearTimeout(timer);
      timer = setTimeout(
        () =>
          finish(
            Object.assign(
              Error(
                "Delegation timed out; cancellation requested, terminal response unconfirmed. Retain identity; do not blindly respawn.",
              ),
              { identity },
            ),
          ),
        graceMs,
      );
      try {
        bus.emit("prompt-template:subagent:cancel", identity);
      } catch (error) {
        finish(error);
      }
    };
    const off = bus.on("prompt-template:subagent:response", (response) => {
      if (
        response?.requestId !== identity.requestId ||
        response.ownerRunId !== identity.ownerRunId ||
        response.nodeId !== identity.nodeId
      )
        return;
      finish(undefined, response);
    });
    if (signal?.aborted) {
      finish(Error("Delegation aborted before request dispatch"));
      return;
    }
    timer = setTimeout(cancel, Math.min(timeoutMs + graceMs, 2147483647));
    signal?.addEventListener("abort", cancel, { once: true });
    try {
      onRequest?.(identity);
      bus.emit("prompt-template:subagent:request", {
        ...identity,
        agent,
        task,
        cwd,
        context: "fresh",
        timeoutMs,
        result: { kind: "text" },
        intercomBridge: { mode: "off" },
      });
    } catch (error) {
      finish(error);
    }
  });
}

export async function dispatchToReport(bus, options, path, metadata) {
  let response;
  try {
    response = await delegateLeaf(bus, options);
  } catch (error) {
    writeFileSync(
      path,
      JSON.stringify(
        {
          ...metadata,
          terminalConfirmed: false,
          error: { message: error.message, identity: error.identity },
        },
        null,
        2,
      ) + "\n",
      { flag: "wx", mode: 0o600 },
    );
    throw error;
  }
  writeFileSync(
    path,
    JSON.stringify(
      { ...metadata, terminalConfirmed: true, response },
      null,
      2,
    ) + "\n",
    { flag: "wx", mode: 0o600 },
  );
  return response;
}

export function trustedManagedPath(path, agentDir) {
  const boundary = realpathSync(agentDir);
  const canonical = realpathSync(path);
  let current = canonical;
  if (current !== boundary && !current.startsWith(boundary + sep))
    throw new Error("Managed Pi path escapes the operator install");
  while (true) {
    const entry = statSync(current);
    if (
      ((entry.mode & 0o022) !== 0 &&
        !(entry.isDirectory() && entry.mode & 0o1000)) ||
      (entry.uid !== process.getuid() && entry.uid !== 0)
    )
      throw new Error(
        `Managed Pi path has unsafe ownership or permissions: ${current}`,
      );
    const parent = dirname(current);
    if (parent === current) return canonical;
    current = parent;
  }
}

export async function openDispatchHost({ cwd, agentDir }) {
  agentDir = realpathSync(agentDir);
  const sdkRoot = trustedManagedPath(resolveSdkRoot({ agentDir }), agentDir);
  const sdkEntry = trustedManagedPath(join(sdkRoot, "dist/index.js"), agentDir);
  const sdkManifest = trustedManagedPath(
    join(sdkRoot, "package.json"),
    agentDir,
  );
  const extension = trustedManagedPath(
    join(agentDir, "npm/node_modules/pi-subagents/index.js"),
    agentDir,
  );
  const extensionManifest = trustedManagedPath(
    join(dirname(extension), "package.json"),
    agentDir,
  );
  process.env.PI_SUBAGENTS_PI_CODING_AGENT_PACKAGE_ROOT = sdkRoot;
  process.env.PI_CODING_AGENT_DIR = agentDir;
  const {
    DefaultResourceLoader,
    SettingsManager,
    SessionManager,
    createAgentSession,
  } = await import(pathToFileURL(sdkEntry).href);
  const settingsManager = SettingsManager.inMemory(
    { packages: [], extensions: [] },
    { projectTrusted: false },
  );
  let bus;
  const loader = new DefaultResourceLoader({
    cwd,
    agentDir,
    settingsManager,
    noSkills: true,
    noPromptTemplates: true,
    noThemes: true,
    noContextFiles: true,
    noExtensions: true,
    additionalExtensionPaths: [extension],
    extensionFactories: [
      (pi) => {
        bus = pi.events;
      },
    ],
  });
  await loader.reload();
  const errors = loader.getExtensions().errors;
  if (errors.length)
    throw new Error(`Native extension load failed: ${JSON.stringify(errors)}`);
  const { session } = await createAgentSession({
    cwd,
    agentDir,
    settingsManager,
    resourceLoader: loader,
    sessionManager: SessionManager.inMemory(cwd),
    tools: [],
  });
  const versions = {
    sdk: JSON.parse(readFileSync(sdkManifest, "utf8")).version,
    subagents: JSON.parse(readFileSync(extensionManifest, "utf8")).version,
  };
  return { session, bus, versions };
}

async function main() {
  const { values } = parseArgs({
    options: {
      agent: { type: "string" },
      "task-file": { type: "string" },
      cwd: { type: "string" },
      report: { type: "string" },
      probe: { type: "boolean" },
      "timeout-ms": { type: "string" },
    },
  });
  if (
    !values.probe &&
    (!values.agent || !values["task-file"] || !values.cwd || !values.report)
  )
    throw new Error(
      "Usage: node subagent-dispatch.mjs --probe | --agent ROLE --task-file FILE --cwd DIR --report FILE [--timeout-ms MS]",
    );
  if (values.report && existsSync(values.report))
    throw new Error(`Report already exists: ${values.report}`);
  const agentDir = join(userInfo().homedir, ".pi", "agent");
  const cwd = resolve(values.cwd || process.cwd());
  const { session, bus, versions } = await openDispatchHost({ cwd, agentDir });
  const abort = new AbortController();
  const cancel = () => abort.abort();
  process.once("SIGINT", cancel);
  process.once("SIGTERM", cancel);
  try {
    await session.bindExtensions({});
    const probe = await rpcRequest(bus, "ping");
    if (probe.version !== 1 || !probe.methods?.includes("spawn"))
      throw new Error(
        "Installed native provider does not support documented RPC v1",
      );
    if (values.probe) {
      console.log(JSON.stringify({ versions, probe }, null, 2));
      return;
    }
    const task = readFileSync(values["task-file"], "utf8");
    const response = await dispatchToReport(
      bus,
      {
        agent: values.agent,
        task,
        cwd,
        timeoutMs: Number(values["timeout-ms"] || 900000),
        signal: abort.signal,
        onRequest: (identity) =>
          console.log(
            JSON.stringify({
              kind: "delegation-request",
              ...identity,
              agent: values.agent,
              cwd,
            }),
          ),
      },
      values.report,
      { versions, probe },
    );
    console.log(
      JSON.stringify({
        status: response.status,
        runId: response.runId,
        model: response.model,
        thinking: response.thinking,
        report: values.report,
      }),
    );
    if (response.status !== "completed") process.exitCode = 1;
  } finally {
    process.removeListener("SIGINT", cancel);
    process.removeListener("SIGTERM", cancel);
    session.dispose();
  }
}
if (
  process.argv[1] &&
  realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url))
)
  main().catch((error) => {
    console.error(error.message);
    if (error.identity) console.error(JSON.stringify(error.identity));
    process.exitCode = 1;
  });
