# ADR 0008 — OSS-only release quality

Status: accepted, implementation rollout in progress (2026-09-04).

Replace SonarCloud with mandatory OSS evidence across Engineering Platform,
SanPlat, communications-ms and cgm-bot-api. Keep current global and native
coverage minimums; require 80% changed-line coverage. Evidence is bound to the
repository, service, tested SHA, comparison base and policy version.

This supersedes ADR 0005 and extends ADR 0007. Source of operational truth:
[OSS quality policy](../quality/open-source-quality-gate.md). Preserve historical
reports and previously promoted rollback targets. Removing vendor integration
does not lower tests, coverage, lint, types/build or security requirements.
