import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const hook = fileURLToPath(new URL("./commit-reminder.sh", import.meta.url));

function fixture(run) {
  const cwd = fs.mkdtempSync(path.join(tmpdir(), "commit-reminder-test-"));
  const env = {
    ...process.env,
    GIT_CONFIG_NOSYSTEM: "1",
    GIT_CONFIG_GLOBAL: "/dev/null",
  };
  for (const key of Object.keys(env)) {
    if (
      key.startsWith("GIT_") &&
      !["GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL"].includes(key)
    )
      delete env[key];
  }
  const git = (...args) => {
    const result = spawnSync("git", args, { cwd, env, encoding: "utf8" });
    assert.equal(result.status, 0, result.stderr);
    return result.stdout;
  };
  try {
    run({
      cwd,
      git,
      invoke: () => spawnSync("bash", [hook], { cwd, env, encoding: "utf8" }),
    });
  } finally {
    fs.rmSync(cwd, { recursive: true, force: true });
  }
}

function baseline({ cwd, git }) {
  git("init", "--quiet");
  fs.writeFileSync(path.join(cwd, "first"), "first\n");
  fs.writeFileSync(path.join(cwd, "second"), "second\n");
  git("add", "first", "second");
  git(
    "-c",
    "user.name=Fixture",
    "-c",
    "user.email=fixture@example.test",
    "commit",
    "--quiet",
    "--no-gpg-sign",
    "-m",
    "baseline",
  );
}

test("reminder outside a repository is silent and creates no files", () =>
  fixture(({ cwd, invoke }) => {
    const result = invoke();
    assert.equal(result.status, 0);
    assert.equal(result.stdout, "");
    assert.deepEqual(fs.readdirSync(cwd), []);
  }));

test("clean repositories and untracked-only work are left untouched", () =>
  fixture((ctx) => {
    baseline(ctx);
    const clean = ctx.invoke();
    assert.equal(clean.status, 0);
    assert.equal(clean.stdout, "");
    fs.writeFileSync(path.join(ctx.cwd, "unowned"), "preserve\n");
    const before = ctx.git("status", "--porcelain=v1");
    const result = ctx.invoke();
    assert.equal(result.status, 0);
    assert.equal(result.stdout, "");
    assert.equal(ctx.git("status", "--porcelain=v1"), before);
    assert.equal(
      fs.readFileSync(path.join(ctx.cwd, "unowned"), "utf8"),
      "preserve\n",
    );
  }));

test("dirty-repository reminder never stages, commits or changes existing work", () =>
  fixture((ctx) => {
    baseline(ctx);
    fs.writeFileSync(
      path.join(ctx.cwd, "first"),
      "changed by another session\n",
    );
    fs.writeFileSync(path.join(ctx.cwd, "second"), "changed by the user\n");
    fs.writeFileSync(path.join(ctx.cwd, "unowned"), "untracked\n");
    ctx.git("add", "first");
    const before = {
      head: ctx.git("rev-parse", "HEAD"),
      status: ctx.git("status", "--porcelain=v1"),
      index: fs.readFileSync(path.join(ctx.cwd, ".git/index")),
      first: fs.readFileSync(path.join(ctx.cwd, "first")),
      second: fs.readFileSync(path.join(ctx.cwd, "second")),
      unowned: fs.readFileSync(path.join(ctx.cwd, "unowned")),
    };
    const result = ctx.invoke();
    assert.equal(result.status, 0);
    assert.match(result.stdout, /does not authorize staging, committing/);
    assert.match(result.stdout, /read-only audit, review, or status request/);
    assert.match(result.stdout, /current task owns the exact changes/);
    assert.match(
      result.stdout,
      /report the pending changes without modifying them/,
    );
    assert.equal(ctx.git("rev-parse", "HEAD"), before.head);
    assert.equal(ctx.git("status", "--porcelain=v1"), before.status);
    assert.deepEqual(
      fs.readFileSync(path.join(ctx.cwd, ".git/index")),
      before.index,
    );
    for (const name of ["first", "second", "unowned"])
      assert.deepEqual(fs.readFileSync(path.join(ctx.cwd, name)), before[name]);
  }));
