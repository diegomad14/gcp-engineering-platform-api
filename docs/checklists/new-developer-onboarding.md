# Checklist: New Developer Onboarding

Actionable version, no explanations. For the "why" behind each step, see
[`docs/onboarding/new-developer-guide.md`](../onboarding/new-developer-guide.md).

## Setup

- [ ] Clone `gcp-engineering-platform-api` and `gcp-engineering-platform-web` as **sibling** repos (same parent directory)
- [ ] API: `pip install -e ".[dev]"`
- [ ] API: `cp .env.example .env` (or export the variables directly)
- [ ] API: `export ENG_PLATFORM_MOCK_MODE=true`
- [ ] API: `uvicorn eng_platform_api.main:app --reload --port 8000` → verify `http://localhost:8000/docs`
- [ ] Web: `npm install`
- [ ] Web: `npm run dev` → verify `http://localhost:5173` shows mock data from the local API

## Local validation (replicates the CI gate)

- [ ] API: `ruff check src tests && ruff format --check src tests`
- [ ] API: `mypy src --ignore-missing-imports`
- [ ] API: `bandit -q -lll -r src && pip-audit`
- [ ] API: `pytest -q --cov=eng_platform_api --cov-fail-under=70` passes
- [ ] Web: `npm run lint` passes (0 warnings)
- [ ] Web: `npm run test:coverage` passes (lines/statements 80%, branches 75%, functions 60%)
- [ ] Web: `npm run build` passes
- [ ] `actionlint` passes on `.github/workflows/*.yml` (both repos)

## First contribution

- [ ] Branch: `feat|fix|chore|docs/<slug-kebab-case>`
- [ ] Commits in Conventional Commits
- [ ] Push + PR with a Conventional Commits title
- [ ] `quality` / `workflows` / `validate` / `SonarCloud Code Analysis` checks green
- [ ] `python scripts/quality/sonar_agent_check.py --pull-request <PR_NUMBER>` run and reviewed
- [ ] Human review approved
- [ ] Merged to `main` (tag/release is generated automatically — don't create the tag by hand)

## Deploy an integrated service

- [ ] Signed in to Engineering Platform with GitHub
- [ ] GitHub user is allowlisted to deploy
- [ ] Service card in `/deployments` shows no readiness blockers
- [ ] Eligible `vX.Y.Z` tag exists for the selected service
- [ ] Deploy started from `/deployments/{service}` only
- [ ] Candidate, promote, and production validation stages reviewed
- [ ] GitHub workflow logs reviewed if any stage fails
- [ ] Rollback action used only from deployment history to a known-good revision

## Adopt a blocked or new service

- [ ] Service Factory plan generated
- [ ] Generated platform deploy, rollback, CI/quality, and semantic-release workflows reviewed
- [ ] Generated catalog entry added to Engineering Platform API
- [ ] Agent handoff prompt used only for PR-ready repo changes
- [ ] Adoption PRs pass actionlint and repo quality checks
- [ ] Service returns to `/deployments` with `deployment_ready=true`

## Security (read and understood)

- [ ] Never commit secrets/tokens/.env/PII to git, logs, the catalog, or the wiki
- [ ] Never use `GCP_SA_KEY` (JSON key) — WIF/OIDC only
- [ ] Never run `gcloud run deploy` / `update-traffic` manually for normal releases or production traffic
- [ ] Production deploys only via `POST /api/services/{service}/deployments` or the UI — never `gh workflow run`/`gh api` directly
