# Release quality checks

Run the repository CI checks and the normalized OSS gate before declaring work complete.
Require the exact commit report with policy `oss-v2`: global coverage at the catalog threshold,
80% changed-line coverage, tests, lint, types/build and security checks.
Inspect every failure; do not lower thresholds or suppress findings without a documented technical reason.
Release and promotion must verify the same repository, service and artifact SHA.
Use the canonical release process; preserve existing runner restrictions.
