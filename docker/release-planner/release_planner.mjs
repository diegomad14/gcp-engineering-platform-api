import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  chmodSync,
  closeSync,
  fsyncSync,
  lstatSync,
  mkdtempSync,
  openSync,
  readFileSync,
  renameSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";

import { analyzeCommits } from "@semantic-release/commit-analyzer";
import { generateNotes } from "@semantic-release/release-notes-generator";

const RESULT_KIND = "eng-platform-release-plan";
const SHA_PATTERN = /^[0-9a-f]{40}$/;
const HASH_PATTERN = /^[0-9a-f]{64}$/;
const TAG_PATTERN = /^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/;

const required = (name) => {
  const value = process.env[name] || "";
  if (!value) throw new Error(`Missing required planner identity: ${name}`);
  return value;
};

const identity = () => {
  const headSha = required("ENG_PLATFORM_RELEASE_HEAD_SHA").toLowerCase();
  const baseSha = required("ENG_PLATFORM_RELEASE_BASE_SHA").toLowerCase();
  const operation = required("ENG_PLATFORM_RELEASE_OPERATION");
  const plannerHash = required("ENG_PLATFORM_RELEASE_PLANNER_HASH");
  const plannerImage = required("ENG_PLATFORM_RELEASE_PLANNER_IMAGE");
  if (!SHA_PATTERN.test(headSha) || !SHA_PATTERN.test(baseSha) || headSha === baseSha) {
    throw new Error("Invalid release source identity");
  }
  if (operation !== "main_release") {
    throw new Error("Release plans are only allowed for main_release executions");
  }
  if (
    !HASH_PATTERN.test(plannerHash) ||
    !/@sha256:[0-9a-f]{64}$/.test(plannerImage)
  ) {
    throw new Error("Invalid release planner policy hash");
  }
  const packageJson = JSON.parse(
    readFileSync(new URL("./package.json", import.meta.url), "utf8"),
  );
  const commitAnalyzer = packageJson.dependencies?.["@semantic-release/commit-analyzer"];
  const notesGenerator =
    packageJson.dependencies?.["@semantic-release/release-notes-generator"];
  if (commitAnalyzer !== "13.0.1" || notesGenerator !== "14.1.1") {
    throw new Error("Baked release planner dependencies do not match policy");
  }
  const calculatedPolicyHash = canonicalHash({
    image: plannerImage,
    commit_analyzer: commitAnalyzer,
    release_notes_generator: notesGenerator,
    tag_format: "v${version}",
    branch: "main",
  });
  if (calculatedPolicyHash !== plannerHash) {
    throw new Error("Authorized planner hash does not match baked policy");
  }
  const providerRunId =
    process.env.ENG_PLATFORM_PROVIDER_RUN_ID || process.env.GITHUB_RUN_ID || "";
  if (!providerRunId) throw new Error("Missing provider run identity");
  const fingerprint = required("ENG_PLATFORM_RELEASE_FINGERPRINT");
  if (!HASH_PATTERN.test(fingerprint)) {
    throw new Error("Invalid release execution fingerprint");
  }
  return {
    execution_id: required("ENG_PLATFORM_RELEASE_EXECUTION_ID"),
    fingerprint,
    service_name: required("ENG_PLATFORM_RELEASE_SERVICE"),
    repository: required("ENG_PLATFORM_RELEASE_REPOSITORY"),
    head_sha: headSha,
    base_sha: baseSha,
    operation,
    planner_hash: plannerHash,
    planner_image: plannerImage,
    provider_run_id: providerRunId,
  };
};

const gitOutput = (cwd, args) =>
  execFileSync("git", args, {
    cwd,
    encoding: "utf8",
    env: {
      PATH: process.env.PATH || "/usr/bin:/bin",
      HOME: tmpdir(),
      LANG: "C.UTF-8",
      LC_ALL: "C.UTF-8",
      GIT_CONFIG_NOSYSTEM: "1",
      GIT_CONFIG_GLOBAL: "/dev/null",
      GIT_TERMINAL_PROMPT: "0",
    },
    maxBuffer: 8 * 1024 * 1024,
    stdio: ["ignore", "pipe", "pipe"],
  });

const git = (cwd, args) => gitOutput(cwd, args).trim();

const semanticVersion = (tag) => {
  const match = TAG_PATTERN.exec(tag);
  if (!match) return null;
  return {
    tag,
    version: `${match[1]}.${match[2]}.${match[3]}`,
    parts: match.slice(1).map((part) => Number.parseInt(part, 10)),
  };
};

const compareVersions = (left, right) => {
  for (let index = 0; index < 3; index += 1) {
    if (left.parts[index] !== right.parts[index]) {
      return left.parts[index] - right.parts[index];
    }
  }
  return left.tag.localeCompare(right.tag, "en");
};

const latestRelease = (cwd, headSha) => {
  const output = git(cwd, ["tag", "--merged", headSha, "--list", "v*"]);
  const versions = output
    .split("\n")
    .filter(Boolean)
    .map(semanticVersion)
    .filter((value) => value !== null)
    .sort(compareVersions);
  if (versions.length === 0) {
    return { version: "0.0.0", gitTag: "", gitHead: "" };
  }
  const latest = versions.at(-1);
  return {
    version: latest.version,
    gitTag: latest.tag,
    gitHead: git(cwd, ["rev-list", "-n", "1", latest.tag]).toLowerCase(),
  };
};

const commitsSince = (cwd, lastRelease, headSha) => {
  const revision = lastRelease.gitTag ? `${lastRelease.gitTag}..${headSha}` : headSha;
  const fields = gitOutput(cwd, [
    "log",
    "-z",
    "--reverse",
    "--format=%H%x00%aN%x00%aE%x00%cI%x00%D%x00%B",
    revision,
  ]).split("\0");
  if (fields.at(-1) === "") fields.pop();
  if (fields.length % 6 !== 0) {
    throw new Error("Git returned malformed commit history");
  }
  const commits = [];
  for (let index = 0; index < fields.length; index += 6) {
    commits.push({
      hash: fields[index],
      message: fields[index + 5].trim(),
      gitTags: fields[index + 4],
      committerDate: fields[index + 3],
      author: { name: fields[index + 1], email: fields[index + 2] },
    });
  }
  return commits;
};

const nextVersion = (current, releaseType) => {
  const match = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.exec(current);
  if (!match) throw new Error("Last release is not a stable semantic version");
  let [major, minor, patch] = match.slice(1).map((part) => Number.parseInt(part, 10));
  if (releaseType === "major") {
    major += 1;
    minor = 0;
    patch = 0;
  } else if (releaseType === "minor") {
    minor += 1;
    patch = 0;
  } else if (releaseType === "patch") {
    patch += 1;
  } else {
    throw new Error(`Unsupported semantic release type: ${releaseType}`);
  }
  return `${major}.${minor}.${patch}`;
};

const logger = Object.freeze({
  log: () => {},
  success: () => {},
  error: () => {},
});

export const calculatePlan = async (cwd, executionIdentity) => {
  const actualHead = git(cwd, ["rev-parse", "HEAD"]).toLowerCase();
  if (actualHead !== executionIdentity.head_sha) {
    throw new Error("Checkout does not match the authorized head SHA");
  }
  const resolvedBase = git(cwd, [
    "rev-parse",
    `${executionIdentity.base_sha}^{commit}`,
  ]).toLowerCase();
  if (resolvedBase !== executionIdentity.base_sha) {
    throw new Error("Authorized base SHA is missing from release history");
  }
  try {
    git(cwd, [
      "merge-base",
      "--is-ancestor",
      executionIdentity.base_sha,
      executionIdentity.head_sha,
    ]);
  } catch {
    throw new Error("Authorized release base is not an ancestor of head");
  }
  const lastRelease = latestRelease(cwd, executionIdentity.head_sha);
  const commits = commitsSince(cwd, lastRelease, executionIdentity.head_sha);
  const context = {
    cwd,
    env: {},
    logger,
    commits,
    lastRelease,
    branch: { name: "main", type: "release", channel: null },
    branches: [{ name: "main", type: "release", channel: null }],
    options: {
      branches: ["main"],
      repositoryUrl: `https://github.com/${executionIdentity.repository}.git`,
      tagFormat: "v${version}",
    },
  };
  const releaseType = await analyzeCommits({}, context);
  if (!releaseType) {
    return {
      next_version: "",
      git_tag: "",
      release_type: "none",
      notes: "",
      config_hash: executionIdentity.planner_hash,
    };
  }
  if (!["patch", "minor", "major"].includes(releaseType)) {
    throw new Error(`Unsupported semantic release type: ${releaseType}`);
  }
  // semantic-release starts a repository at 1.0.0; subsequent releases use
  // the analyzed type against the highest reachable stable tag.
  const version = lastRelease.gitTag
    ? nextVersion(lastRelease.version, releaseType)
    : "1.0.0";
  const nextRelease = {
    type: releaseType,
    version,
    gitTag: `v${version}`,
    gitHead: executionIdentity.head_sha,
  };
  const notes = await generateNotes({}, { ...context, nextRelease });
  if (typeof notes !== "string" || notes.length > 100_000) {
    throw new Error("Generated release notes exceed the public contract");
  }
  return {
    next_version: version,
    git_tag: nextRelease.gitTag,
    release_type: releaseType,
    notes,
    config_hash: executionIdentity.planner_hash,
  };
};

const canonicalHash = (value) => {
  const ordered = (item) => {
    if (Array.isArray(item)) return item.map(ordered);
    if (item && typeof item === "object") {
      return Object.fromEntries(
        Object.keys(item)
          .sort()
          .map((key) => [key, ordered(item[key])]),
      );
    }
    return item;
  };
  return createHash("sha256").update(JSON.stringify(ordered(value))).digest("hex");
};

const atomicJson = (path, value) => {
  const directory = dirname(path);
  const temporaryDirectory = mkdtempSync(join(directory, ".release-plan-"));
  const temporary = join(temporaryDirectory, "result.json");
  try {
    const descriptor = openSync(temporary, "wx", 0o600);
    try {
      writeFileSync(descriptor, `${JSON.stringify(value, null, 2)}\n`, "utf8");
      fsyncSync(descriptor);
    } finally {
      closeSync(descriptor);
    }
    chmodSync(temporary, 0o444);
    renameSync(temporary, path);
  } finally {
    rmSync(temporaryDirectory, { recursive: true, force: true });
  }
};

const identityToken = async (audience) => {
  const requestUrl = process.env.ACTIONS_ID_TOKEN_REQUEST_URL || "";
  const requestToken = process.env.ACTIONS_ID_TOKEN_REQUEST_TOKEN || "";
  if (requestUrl && requestToken) {
    const url = new URL(requestUrl);
    url.searchParams.set("audience", "engineering-platform-release-orchestrator");
    const response = await fetch(url, {
      headers: { Authorization: `Bearer ${requestToken}` },
    });
    if (!response.ok) throw new Error("Unable to obtain GitHub workflow identity");
    const value = await response.json();
    if (typeof value.value !== "string" || !value.value) {
      throw new Error("GitHub did not issue a workflow identity");
    }
    return value.value;
  }
  const url = new URL(
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity",
  );
  url.searchParams.set("audience", audience);
  const response = await fetch(url, { headers: { "Metadata-Flavor": "Google" } });
  if (!response.ok) throw new Error("Unable to obtain managed build identity");
  return response.text();
};

const eventToken = (controlDirectory) => {
  const path = join(controlDirectory, "event-token");
  const stat = lstatSync(path);
  if (!stat.isFile() || stat.isSymbolicLink() || (stat.mode & 0o077) !== 0) {
    throw new Error("Execution event token permissions are unsafe");
  }
  const value = readFileSync(path, "utf8").trim();
  if (value.length < 32 || value.length > 512 || /\s/.test(value)) {
    throw new Error("Execution event token is invalid");
  }
  return value;
};

const callback = async (executionIdentity, status, controlDirectory, fields = {}) => {
  const api = required("ENG_PLATFORM_API_URL").replace(/\/$/, "");
  const token = await identityToken(api);
  const response = await fetch(
    `${api}/api/internal/release-executions/${executionIdentity.execution_id}/events`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
        "X-Eng-Platform-Event-Token": eventToken(controlDirectory),
      },
      body: JSON.stringify({
        execution_id: executionIdentity.execution_id,
        provider_run_id: executionIdentity.provider_run_id,
        fingerprint: executionIdentity.fingerprint,
        sequence: 3,
        status,
        ...fields,
      }),
    },
  );
  if (!response.ok) {
    throw new Error(`Release plan callback failed: ${response.status}`);
  }
};

const verifyManifest = (path, executionIdentity) => {
  const value = JSON.parse(readFileSync(path, "utf8"));
  const immutable = {
    kind: RESULT_KIND,
    execution_id: executionIdentity.execution_id,
    fingerprint: executionIdentity.fingerprint,
    service_name: executionIdentity.service_name,
    repository: executionIdentity.repository,
    head_sha: executionIdentity.head_sha,
    base_sha: executionIdentity.base_sha,
    operation: executionIdentity.operation,
    planner_hash: executionIdentity.planner_hash,
    planner_image: executionIdentity.planner_image,
    provider_run_id: executionIdentity.provider_run_id,
  };
  if (
    value.schema_version !== 1 ||
    Object.entries(immutable).some(([key, expected]) => value[key] !== expected)
  ) {
    throw new Error("Release plan manifest identity mismatch");
  }
  if (!value.release_plan || typeof value.release_plan !== "object") {
    throw new Error("Release plan manifest does not contain a plan");
  }
  const plan = value.release_plan;
  if (plan.config_hash !== executionIdentity.planner_hash) {
    throw new Error("Release plan policy hash mismatch");
  }
  if (plan.release_type === "none") {
    if (plan.next_version || plan.git_tag || plan.notes) {
      throw new Error("No-release plan contains release output");
    }
  } else if (
    !["patch", "minor", "major"].includes(plan.release_type) ||
    plan.git_tag !== `v${plan.next_version}` ||
    semanticVersion(plan.git_tag) === null ||
    typeof plan.notes !== "string" ||
    plan.notes.length > 100_000
  ) {
    throw new Error("Release plan is invalid");
  }
  if (value.plan_hash !== canonicalHash(plan)) {
    throw new Error("Release plan manifest hash mismatch");
  }
  return plan;
};

const main = async () => {
  const options = parseArgs({
    options: {
      mode: { type: "string", default: "plan" },
      source: { type: "string", default: "/workspace" },
      output: { type: "string", default: "/eng-platform-output/release-plan.json" },
      manifest: { type: "string" },
      "control-dir": { type: "string", default: "/eng-platform-control" },
    },
    strict: true,
  }).values;
  const executionIdentity = identity();
  if (options.mode === "plan") {
    const source = resolve(options.source);
    const plan = await calculatePlan(source, executionIdentity);
    const manifest = {
      schema_version: 1,
      kind: RESULT_KIND,
      ...executionIdentity,
      status: plan.release_type === "none" ? "no_release" : "release_planned",
      plan_hash: canonicalHash(plan),
      release_plan: plan,
    };
    atomicJson(resolve(options.output), manifest);
    console.log(`Release plan manifest: ${resolve(options.output)}`);
    return;
  }
  if (options.mode !== "publish") {
    throw new Error(`Unsupported planner mode: ${options.mode}`);
  }
  let plan;
  try {
    plan = verifyManifest(resolve(options.manifest || options.output), executionIdentity);
  } catch (error) {
    await callback(
      executionIdentity,
      "failed",
      resolve(options["control-dir"]),
      {
        error: String(error instanceof Error ? error.message : error).slice(0, 1000),
      },
    );
    throw error;
  }
  await callback(
    executionIdentity,
    plan.release_type === "none" ? "no_release" : "release_planned",
    resolve(options["control-dir"]),
    { release_plan: plan },
  );
};

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main();
}
