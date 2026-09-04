# OSS release quality policy

Policy `oss-v2` replaces SonarCloud for the six active CGM services. The normalized
report and its exact repository/service/commit identity authorize new releases.
Historical `oss-v1` reports remain readable but cannot authorize a new candidate.

## Required controls

Preserve native CI checks, tests, lint, formatting, types/build, Semgrep ERROR
findings and Trivy HIGH/CRITICAL dependency, secret and configuration findings.
Global minimums: Engineering Platform API, SanPlat API/Web and communications-ms
70%; Engineering Platform Web and cgm-bot-api 80%. Preserve stricter native
branch/function/statement thresholds. The server checks catalog policy itself.

Changed executable lines require 80% coverage. PRs use the merge base with the
PR target; main pushes use the event's previous SHA, including multi-commit
pushes. Manual reruns must preserve the original base SHA. No executable changes
produce N/A, never a fabricated 100%. Missing or incomplete coverage fails.
`.quality-sources.json` lists source roots and justified exclusions relative to
the service working directory. Instrument the entire source set, including
unimported files. Detailed Python JSON or LCOV is required.

## Evidence and release

`POST /api/quality/reports` accepts additive policy/base/count/coverage fields.
`GET /api/quality/services/{service}/commits/{sha}?for_release=true` validates
current catalog requirements. Historical reads omit `for_release`.
Semantic release waits for evidence for its exact main SHA. Candidate creation
and promotion both require PASSED oss-v2 evidence younger than 168 hours.
Provider failures, missing evidence and mismatched commits fail closed.
Only the workflow that tested the checked-out commit may publish its report;
PR merge evidence cannot authorize a different main commit.

Rollback uses `/api/quality/services/{service}/rollback-targets/{revision}`:
require an originally successful production deployment, original PASSED evidence,
and compare the actual Cloud Run revision digest against the recorded release
image. Evidence age and new policy requirements do not requalify an old release.
Automatic rollback to the just-recorded previous revision remains available.

## Rollout

First deploy additive report support with current checks and the new policy
checks both passing. Preserve original evidence and new report artifacts for
that bootstrap release. Then pin callers and the implementation to the released
immutable platform revision, run each service gate and activate its new path.
Remove Sonar checks and scans as part of the replacement, then remove exclusive
credentials once no active consumer remains. Do not delete analysis history.
No subscription/billing changes are part of this migration.

Runner restrictions and the existing candidate/promote model remain unchanged.
GitHub branch protection, where available, must require OSS checks instead of
Sonar. Release workflow verification remains mandatory independently of billing
or branch-protection availability. See the canonical wiki `release_process`.

## Compatibility rollout record

The v0.19.0 control-plane bootstrap preserves the complete oss-v2 JSON artifact
and publishes a legacy projection accepted by the preceding API. Its exact main
SHA passed native checks and the differential gate before tagging and candidate
validation. Normal releases now publish oss-v2 directly and require server-verified
evidence. The one-time bootstrap workflow is removed after this transition;
ordinary releases use signed Platform Deploy authorization.
