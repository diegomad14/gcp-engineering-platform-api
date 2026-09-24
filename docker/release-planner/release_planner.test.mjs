import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import assert from "node:assert/strict";

import { calculatePlan, gitArguments } from "./release_planner.mjs";

test("Git commands explicitly trust only the checked-out source directory", () => {
  assert.deepEqual(gitArguments("/workspace", ["rev-parse", "HEAD"]), [
    "-c",
    "safe.directory=/workspace",
    "rev-parse",
    "HEAD",
  ]);
});


const git = (cwd, ...args) =>
  execFileSync("git", args, {
    cwd,
    encoding: "utf8",
    env: {
      PATH: process.env.PATH || "/usr/bin:/bin",
      HOME: tmpdir(),
      GIT_CONFIG_NOSYSTEM: "1",
      GIT_CONFIG_GLOBAL: "/dev/null",
    },
  }).trim();

const repository = (commit, tag) => {
  const cwd = mkdtempSync(join(tmpdir(), "eng-platform-planner-test-"));
  git(cwd, "init", "-q", "-b", "main");
  git(cwd, "config", "user.name", "Planner Test");
  git(cwd, "config", "user.email", "planner@example.test");
  writeFileSync(join(cwd, "fixture.txt"), "base\n", "utf8");
  git(cwd, "add", "fixture.txt");
  git(cwd, "commit", "-q", "-m", "chore: initial fixture");
  const base = git(cwd, "rev-parse", "HEAD");
  if (tag) git(cwd, "tag", tag, base);
  writeFileSync(join(cwd, "fixture.txt"), `${commit.subject}\n`, "utf8");
  const args = ["commit", "-q", "-am", commit.subject];
  if (commit.body) args.push("-m", commit.body);
  git(cwd, ...args);
  return { cwd, base, head: git(cwd, "rev-parse", "HEAD") };
};

const cases = [
  { name: "fix", commit: { subject: "fix: repair fixture" }, type: "patch" },
  { name: "feat", commit: { subject: "feat: extend fixture" }, type: "minor" },
  {
    name: "breaking",
    commit: {
      subject: "feat!: replace fixture",
      body: "BREAKING CHANGE: the old fixture is incompatible",
    },
    type: "major",
  },
];

test("fix, feat and breaking changes all start untagged repositories at 1.0.0", async (t) => {
  for (const item of cases) {
    await t.test(item.name, async () => {
      const fixture = repository(item.commit, "");
      try {
        const plan = await calculatePlan(fixture.cwd, {
          repository: "test/example",
          head_sha: fixture.head,
          base_sha: fixture.base,
          planner_hash: "a".repeat(64),
        });
        assert.equal(plan.release_type, item.type);
        assert.equal(plan.next_version, "1.0.0");
        assert.equal(plan.git_tag, "v1.0.0");
      } finally {
        rmSync(fixture.cwd, { recursive: true, force: true });
      }
    });
  }
});

test("fix, feat and breaking changes bump an existing stable tag", async (t) => {
  const expected = { fix: "2.3.5", feat: "2.4.0", breaking: "3.0.0" };
  for (const item of cases) {
    await t.test(item.name, async () => {
      const fixture = repository(item.commit, "v2.3.4");
      try {
        const plan = await calculatePlan(fixture.cwd, {
          repository: "test/example",
          head_sha: fixture.head,
          base_sha: fixture.base,
          planner_hash: "b".repeat(64),
        });
        assert.equal(plan.release_type, item.type);
        assert.equal(plan.next_version, expected[item.name]);
        assert.equal(plan.git_tag, `v${expected[item.name]}`);
      } finally {
        rmSync(fixture.cwd, { recursive: true, force: true });
      }
    });
  }
});
