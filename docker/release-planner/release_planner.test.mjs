import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import assert from "node:assert/strict";

import { calculatePlan, gitArguments } from "./release_planner.mjs";

const plannerHandlebars = () => {
  const writerRequire = createRequire(import.meta.resolve("conventional-changelog-writer"));
  return writerRequire("handlebars");
};

test("the note writer rejects invalid AST block parameters in compile and precompile", () => {
  // GHSA-8r5x-fm3f-whwj: an object length must never become generated code.
  const handlebars = plannerHandlebars();
  for (const mode of ["compile", "precompile"]) {
    const ast = handlebars.parse("{{#if enabled}}ready{{/if}}");
    ast.body[0].program.blockParams = { length: "(1 + 1)" };
    assert.throws(() => {
      const template = handlebars[mode](ast);
      if (mode === "compile") template({ enabled: true });
    }, /Invalid AST: Program blockParams must be an array/);
  }
});

test("the note writer blocks prototype constructors while preserving ordinary context data", () => {
  // GHSA-p8wg-vrv2-v86f: prototype objects have their own constructor property.
  const handlebars = plannerHandlebars();
  const template = handlebars.compile('{{lookup (lookup fn "__proto__") "constructor"}}');
  assert.equal(template({ fn: () => {} }, { allowProtoMethodsByDefault: true }), "");
  assert.equal(
    handlebars.compile("{{constructor.name}}")({ constructor: { name: "release" } }),
    "release",
  );
});

test("the scoped analyzer matcher only needs the pinned isMatch contract", () => {
  const packageJson = JSON.parse(readFileSync(new URL("./package.json", import.meta.url)));
  assert.deepEqual(packageJson.overrides, {
    "@semantic-release/commit-analyzer@13.0.1": {
      micromatch: "npm:picomatch@2.3.2",
    },
  });
  const require = createRequire(import.meta.url);
  const analyzerEntry = require.resolve("@semantic-release/commit-analyzer");
  const analyzerDir = dirname(analyzerEntry);
  const analyzerRequire = createRequire(analyzerEntry);
  const matcherEntry = analyzerRequire.resolve("micromatch");
  const matcherPackage = JSON.parse(readFileSync(join(dirname(matcherEntry), "package.json")));
  assert.equal(matcherPackage.name, "picomatch");
  assert.equal(matcherPackage.version, "2.3.2");
  assert.throws(() => analyzerRequire.resolve("braces"), { code: "MODULE_NOT_FOUND" });
  const consumers = readdirSync(analyzerDir, { recursive: true })
    .filter((path) => path.endsWith(".js") && !path.startsWith("node_modules/"))
    .filter((path) => /\bmicromatch\b/.test(readFileSync(join(analyzerDir, path), "utf8")));
  assert.deepEqual(consumers, ["lib/analyze-commit.js"]);
  const source = readFileSync(join(analyzerDir, consumers[0]), "utf8");
  assert.equal(source.match(/\bmicromatch\b/g).length, 3);
  assert.deepEqual(source.match(/\bmicromatch\.\w+/g), ["micromatch.isMatch"]);
  const matcher = analyzerRequire("micromatch");
  for (const [input, pattern, options, expected] of [
    ["main", "main", {}, true],
    ["release/v1", ["main", "release/*"], {}, true],
    ["docs", "!docs", {}, false],
    ["feat", "@(feat|fix)", {}, true],
    ["src/api/file.js", "src/{api,web}/**", {}, true],
    ["FIX", "fix", { nocase: true }, true],
    [".hidden", "*", { dot: true }, true],
    ["fix", "@(feat|fix)", { noext: true }, false],
  ]) {
    assert.equal(matcher.isMatch(input, pattern, options), expected);
  }
  assert.throws(() => matcher.isMatch(null, "main"), TypeError);
  assert.throws(() => matcher.isMatch("main", null), TypeError);
});

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

test("the planner generates release notes with the locked template writer", async () => {
  const fixture = repository(
    { subject: "fix(planner): render release notes (#42)", body: "Closes #42" },
    "v2.3.4",
  );
  try {
    const plan = await calculatePlan(fixture.cwd, {
      repository: "test/example",
      head_sha: fixture.head,
      base_sha: fixture.base,
      planner_hash: "c".repeat(64),
    });
    assert.equal(plan.next_version, "2.3.5");
    assert.match(plan.notes, /### Bug Fixes/);
    assert.match(plan.notes, /\*\*planner:\*\* render release notes/);
    assert.ok(plan.notes.includes("https://github.com/test/example/compare/v2.3.4...v2.3.5"));
    assert.ok(plan.notes.includes(`https://github.com/test/example/commit/${fixture.head}`));
    assert.ok(plan.notes.includes("https://github.com/test/example/issues/42"));
  } finally {
    rmSync(fixture.cwd, { recursive: true, force: true });
  }
});

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
