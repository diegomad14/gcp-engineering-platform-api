import crypto from "node:crypto";

const env = process.env;
const decode = (value) => Buffer.from(value, "base64url");

function fail(message) {
  console.error(message);
  process.exit(1);
}

const token = env.PLATFORM_AUTHORIZATION || "";
const parts = token.split(".");
if (parts.length !== 3) fail("Invalid platform release authorization");

let header;
let claims;
try {
  header = JSON.parse(decode(parts[0]).toString("utf8"));
  claims = JSON.parse(decode(parts[1]).toString("utf8"));
} catch {
  fail("Malformed platform release authorization");
}
if (header.alg !== "EdDSA" || header.typ !== "JWT" || header.kid !== "release-v1") {
  fail("Unsupported platform release authorization");
}
const publicKey = env.ENG_PLATFORM_RELEASE_SIGNING_PUBLIC_KEY || "";
if (!publicKey) fail("Release authorization public key is not configured");
const validSignature = crypto.verify(
  null,
  Buffer.from(`${parts[0]}.${parts[1]}`),
  crypto.createPublicKey(publicKey),
  decode(parts[2]),
);
if (!validSignature) fail("Invalid platform release authorization signature");

const now = Math.floor(Date.now() / 1000);
if (
  claims.iss !== "engineering-platform" ||
  claims.aud !== "github-release-workflow" ||
  !Number.isInteger(claims.iat) ||
  !Number.isInteger(claims.exp) ||
  claims.iat > now + 30 ||
  claims.exp < now ||
  claims.exp - claims.iat > 300 ||
  !claims.jti
) fail("Expired platform release authorization");

const expected = {
  repository: env.EXPECTED_REPOSITORY,
  service_name: env.EXPECTED_SERVICE_NAME,
  tag: env.EXPECTED_TAG,
  sha: env.EXPECTED_SHA,
  github_deployment_id: env.EXPECTED_DEPLOYMENT_ID,
  kind: env.EXPECTED_KIND,
};
for (const [key, value] of Object.entries(expected)) {
  if (String(claims[key] || "") !== String(value || "")) {
    fail(`Platform release authorization mismatch: ${key}`);
  }
}
if (env.EXPECTED_TARGET_REVISION && claims.target_revision !== env.EXPECTED_TARGET_REVISION) {
  fail("Platform release authorization mismatch: target_revision");
}

const response = await fetch(
  `${env.PLATFORM_API_URL.replace(/\/$/, "")}/api/internal/release-authorizations/consume`,
  {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      token,
      repository: env.EXPECTED_REPOSITORY,
      service_name: env.EXPECTED_SERVICE_NAME,
      tag: env.EXPECTED_TAG,
      sha: env.EXPECTED_SHA,
      github_deployment_id: env.EXPECTED_DEPLOYMENT_ID,
      kind: env.EXPECTED_KIND,
    }),
  },
);
if (!response.ok) fail(`Platform release authorization rejected (${response.status})`);
