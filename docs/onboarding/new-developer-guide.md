# New Developer Onboarding Guide

This guide covers how to run the Engineering Platform locally, run the same
validations CI runs, and make a safe first contribution — without relying on
unwritten team knowledge.

> **Looking to onboard a new *service* to the platform** (not a Dev)?
> That's a different document: [`docs/checklists/service-onboarding.md`](../checklists/service-onboarding.md)
> and the `/factory` page (Service Factory) in `eng-platform-web`. This guide
> is for people, not for Cloud Run services.

Short actionable checklist (no explanations): [`docs/checklists/new-developer-onboarding.md`](../checklists/new-developer-onboarding.md).

Deploy runbook for any integrated service: [`docs/onboarding/deploy-any-service.md`](deploy-any-service.md).

GitHub Actions continuity runbook: use the canonical Obsidian
`release_process` section and `Runbook - GitHub Actions bloqueado` for billing,
quota, runner and break-glass incidents.

## 1. Prerequisites

- **Python ≥3.11** (`pyproject.toml`); CI runs **3.12** — install 3.12 for exact parity.
- **Node 22** (used by `eng-platform-web` in CI and in the `Dockerfile`; the repo has no `.nvmrc`, so install it explicitly).
- `git` and GitHub CLI (`gh`) authenticated (`gh auth login`).
- Docker, optional — only if you're testing an image build or running `actionlint` via container.
- **You don't need any real GCP credential.** Local development runs in *mock mode* (section 3).

## 2. Clone both repos as siblings

The platform is **two independent repos** that deploy separately:

- `gcp-engineering-platform-api` — FastAPI, control plane.
- `gcp-engineering-platform-web` — React/Vite, UI.

Clone them into the **same parent directory**:

```bash
mkdir eng-platform && cd eng-platform
git clone https://github.com/diegomad14/gcp-engineering-platform-api.git
git clone https://github.com/diegomad14/gcp-engineering-platform-web.git
```

This isn't just tidiness: the Web repo's `playwright.config.ts` automatically
looks for `../gcp-engineering-platform-api` to start the API in mock mode
before running e2e tests. If you clone them elsewhere, override the command
with the `PLAYWRIGHT_API_COMMAND` variable.

> You may come across a third repo, `gcp-engineering-platform` (monorepo).
> It's **deprecated** — don't clone it or use it as a reference.

## 3. Run the API locally

```bash
cd gcp-engineering-platform-api
pip install -e ".[dev]"
export ENG_PLATFORM_MOCK_MODE=true   # see critical note below
uvicorn eng_platform_api.main:app --reload --port 8000
```

Interactive docs at `http://localhost:8000/docs` (Swagger) and `/redoc`.

### ⚠️ Critical note: mock mode is not the real default

`PlatformConfig.mock_mode` defaults to `True` in the dataclass
(`src/eng_platform_api/config.py`), but `load_config()` overrides it with
`os.getenv("ENG_PLATFORM_MOCK_MODE", "false")` — meaning **if you don't
export the variable, real mode stays active** and the API will try to talk
to BigQuery, Cloud Monitoring, GitHub, etc. for real, and fail because you
don't have those credentials. Only the production `Dockerfile` sets the
variable explicitly.

**Always export `ENG_PLATFORM_MOCK_MODE=true` before starting `uvicorn` for
local development.** You can copy `.env.example` to `.env` and load it with
your preferred tool (`direnv`, `set -a; source .env; set +a`, etc.).

## 4. Run the Web app locally

```bash
cd ../gcp-engineering-platform-web
npm install
npm run dev
```

Opens at `http://localhost:5173`.

`vite.config.ts` already proxies `/api` and `/health` to
`http://localhost:8000` — if you have the local API running there (step 3),
the Web app consumes it automatically **without** needing any environment
variable.

### Gotcha: an unset `VITE_API_BASE` falls back to production

`src/api/client.ts` defines:

```ts
const BASE = import.meta.env.VITE_API_BASE || 'https://eng-platform-api-...run.app';
```

If you set `VITE_API_BASE` explicitly, or the Vite proxy doesn't apply for
some reason, the client falls back to **production**. For normal
development (local API on `:8000` + proxy) you don't need to touch anything;
just keep this behavior in mind if you see data you weren't expecting.

## 5. Run tests and build (the same commands CI runs)

### API

Fast, for iterating:

```bash
python3 -m pytest -q
```

**Full CI gate** (`.github/workflows/ci.yml`, `quality` job, in this exact
order — this is how your PR is actually validated):

```bash
ruff check src tests
ruff format --check src tests
mypy src --ignore-missing-imports
bandit -q -lll -r src
pip-audit
pytest -q --cov=eng_platform_api --cov-report=term-missing --cov-report=xml:coverage.xml --cov-fail-under=70
```

Minimum global coverage: **70%** for this API, plus **80%** of changed executable
lines under `oss-v2`. CI publishes exact-commit evidence after the native checks.

### Web

```bash
npm run test              # vitest run src
npm run test:coverage     # vitest run src --coverage
npm run build              # tsc --noEmit && vite build
npm run lint                # eslint . --max-warnings 0
npm run test:e2e            # playwright test
```

Notes:
- **`npm run build` fails on TypeScript errors** before it even attempts the
  Vite bundle — if the error isn't clear, run `npx tsc --noEmit` alone to
  isolate it.
- **`npm run lint` tolerates zero warnings** (`--max-warnings 0`), not just errors.
- Minimum coverage (`vitest.config.ts`) is **not a flat 80%**: lines 80%,
  statements 80%, branches 75%, functions 60%.
- `npm run test:e2e` automatically starts the sibling API in mock mode and
  the Web dev server — you don't need them running beforehand, though if you
  already have them up, Playwright reuses them.

### actionlint (GitHub Actions workflows, both repos)

CI runs it via Docker over `.github/workflows/*.yml`,
`templates/github-actions/*.yml` and `examples/caller-workflows/*.yml`. To
run it locally:

```bash
docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:1.7.7 \
  .github/workflows/*.yml
```

Alternative without Docker: `brew install actionlint && actionlint`.

## 6. Architecture map

```
                    ┌─────────────────┐
   Dev/Operator ──► │  eng-platform-web │
                    └────────┬─────────┘
                             │ fetch /api/*
                    ┌────────▼─────────┐
                    │  eng-platform-api │ (this repo)
                    └────────┬─────────┘
                             │ GitHub App / Deployments API
                    ┌────────▼─────────┐
                    │ workflow_dispatch  │──► Quality Gate
                    │ (platform-deploy)  │──► Cloud Run candidate (no traffic)
                    └────────┬─────────┘        │
                             │              promote (moves traffic)
                    GitHub Deployment            │
                    statuses ◄──────────── Cloud Run production
                             │
                    Firestore / GCS (metadata and evidence)
```

*(Diagram adapted from `wiki/entidades/engineering-platform.md` in the
team's Obsidian vault — that's where the full, up-to-date detail lives.)*

Structure of this repo (`gcp-engineering-platform-api`):

| Folder | What it is |
|---|---|
| `src/eng_platform_api/routers/` | FastAPI endpoints, one per domain (auth, catalog, costs, deployments, quality, metrics, releases, service_factory). |
| `src/eng_platform_api/services/` | Business logic and external integrations (GCP, GitHub, BigQuery). |
| `src/eng_platform_api/models.py` | Pydantic request/response models. |
| `src/eng_platform_api/config.py` | Central config via `ENG_PLATFORM_*` environment variables. |
| `catalog/services/*.yaml` | Declarative catalog of services the platform knows how to deploy. |
| `templates/` | *Inert* GitHub Actions templates for a new service to copy into its own repo — not active workflows here. |
| `examples/caller-workflows/` | Examples of how a client repo consumes this repo's reusable workflows. |
| `docs/` | This kind of documentation: adrs, architecture, checklists, deployment, finops, governance, observability, quality, research, runbooks, standards. |
| `schemas/` | Formal JSON Schemas for the catalog and requests. |
| `scripts/` | Operational utilities (`quality_gate.py`, `differential_coverage.py`, Cloud Run scripts). |

**Important rule**: reusable workflows (`reusable-quality-gate.yml`,
`reusable-cloud-run-promote.yml`, etc.) are consumed by pinning a **stable
version or SHA**, never a mutable reference like `@main` — see
`wiki/conceptos/release_process.md` and `CGM SanPlat/05 - Arquitectura/App
repo vs Platform repo boundaries.md` in the Obsidian vault for the full
detail of this convention and what lives in each repo vs. the wiki.

## 7. First contribution checklist

1. **Branch**: `feat/<slug>`, `fix/<slug>`, `chore/<slug>`, `docs/<slug>`
   (kebab-case, same type as Conventional Commits).
2. **Commits** in [Conventional Commits](https://www.conventionalcommits.org/):
   same types validated by `pr-title.yml` — `feat, fix, perf, refactor, docs,
   test, build, ci, chore, revert`.
3. Before considering your change complete, run the **full local quality
   gate** (section 5) — it replicates exactly what CI runs.
4. Push and open the PR — **the PR title must also be Conventional Commits**
   (`pr-title.yml` validates it with `amannn/action-semantic-pull-request`
   and fails the `validate` check otherwise).
5. Wait for native `quality`, `workflows` (API), `validate` and the normalized
   OSS quality gate. Every required check must pass.
6. Inspect the exact repository/service/SHA report and its comparison base.
   Release verification uses `?for_release=true`; latest-summary status alone
   does not authorize a deployment. Follow `AGENTS.md` and the OSS quality policy.
7. Request a **human review** (not an automated step).
8. On merge to `main`, `semantic-release.yml` computes the version and
   creates the `vX.Y.Z` tag **automatically** from your commits — you
   normally **don't** create the tag by hand. A commit with `[skip release]`
   in the message skips this step.

## 8. Deploy any integrated service

After a PR is merged and a `vX.Y.Z` tag exists, deploy from Engineering
Platform, not from GCP Console:

1. Sign in with GitHub in `eng-platform-web`.
2. Open `/deployments`.
3. Pick the Cloud Run service name from the catalog.
4. Select an eligible release tag.
5. Confirm the service, tag, SHA, current revision, and environment.
6. Watch the candidate, promote, production validation, and rollback metadata
   stages from the UI.

If a service card is blocked, it is cataloged but not deployment-ready. Open
`/factory`, generate the adoption artifacts, and use the generated agent prompt
to create PR-ready workflow/catalog changes. Do not bypass the blocker with
manual `gcloud` commands.

## 9. Troubleshooting runbook

| Symptom | Cause | Action |
|---|---|---|
| API starts but endpoints like `/api/costs`, `/api/deployments` fail or hang | `ENG_PLATFORM_MOCK_MODE` not exported — real mode stays active (see section 3) | `export ENG_PLATFORM_MOCK_MODE=true` before starting `uvicorn` |
| Web shows production data, or calls fail with CORS | The local API isn't running on `:8000`, or `VITE_API_BASE` points elsewhere | Start the local API (uses the automatic proxy) or export `VITE_API_BASE=http://localhost:8000` |
| `npm install` or `npm run build` fail in odd ways | Node version other than 22 | Install Node 22 (use `nvm`/`volta`) |
| `npm run build` fails and the error doesn't mention Vite | Build is `tsc --noEmit && vite build` — the error is a type error | Run `npx tsc --noEmit` alone to isolate it |
| `npm run lint` fails even though the code "works" | `eslint . --max-warnings 0` — zero warnings tolerated | Fix the warnings, not just the errors |
| `pytest -q` passes locally but CI fails on coverage | The simplified README command doesn't apply `--cov-fail-under=70` | Run the full CI command (section 5) to replicate the real gate |
| `gh` rejects push / not authenticated | Expired or unconfigured `gh` session | `gh auth login`, verify with `gh auth status` |
| `gcloud builds submit` fails on IAM permissions over the Cloud Build bucket | Real documented case — missing permissions on the build SA | Use `gcloud auth configure-docker "<region>-docker.pkg.dev" --quiet` + `docker build && docker push` instead of Cloud Build |
| A deploy doesn't show up in `/deployments` or can't be used as a rollback target | It was triggered with `gh workflow run` / `gh api` directly instead of the platform | **Always** trigger deploys via `POST /api/services/{service}/deployments` or the UI button — never `gh` directly (the `deployment_store` won't track it) |
| No changed executable lines | The differential check is N/A; global and native checks still apply | Missing or malformed coverage is a failure, not N/A |
| `404 Quality report not found` | No evidence registered for that `service_name` + SHA | Run the quality gate with the correct SHA |
| `STALE` status in `/quality` | Evidence expired (>168h) | Re-run the quality gate |
| `401/403` when publishing a quality report | Token missing or different from the configured one | Check the secret without printing it |
| `Permission denied: /app/data` registering a release | The store points at the container's read-only filesystem | Set `RELEASES_STORE_PATH` under `/tmp` (known debt — `/tmp` is ephemeral) |
| A candidate receives production traffic | Incorrect deploy | Stop and restore the previous snapshot; this isn't a normal flow |

*(Quality/Release rows adapted from the "Matriz de fallos" in
`wiki/conceptos/release_process.md` in the Obsidian vault.)*

## 10. Security and operations — Do / Don't

**DO:**
- Use `ENG_PLATFORM_MOCK_MODE=true` for local development without real credentials.
- Authenticate to GCP with WIF/OIDC (`GCP_WIF_PROVIDER` / `GCP_WIF_SERVICE_ACCOUNT`).
- Trigger production deploys via `POST /api/services/{service}/deployments` or the UI button (`/deployments`).
- Pin reusable workflows to a stable version/tag/SHA.
- Ask before improvising if you have a security question.

**DON'T:**
- **Never** commit secrets, tokens, `.env`, or customer data/PII — not to git, logs, the catalog, or the wiki.
- **Never** use a service account JSON key (`GCP_SA_KEY`) — it's forbidden, WIF/OIDC only.
- **Never** run `gcloud run deploy` / `gcloud run services update-traffic` manually for a normal release — only as an explicit, documented incident response.
- **Never** trigger deploys with `gh workflow run` / `gh api` directly — it breaks `deployment_store` tracking.
- **Never** suppress or mark a security finding as a false positive without a documented technical reason (`AGENTS.md`).
- **Never** hardcode a Cloud Run revision in a living runbook — use dynamic commands (`gcloud run services describe --format=...`).

## 10. More context (without duplicating it)

- **Internal wiki (Obsidian, not public)**: `wiki/conceptos/release_process.md`
  is the canonical source for the full release/rollback process;
  `wiki/entidades/engineering-platform.md` has the detailed architecture.
- `README.md` in this repo — full list of endpoints and production environment variables.
- `README.md` in `gcp-engineering-platform-web` — UI routes.
- `AGENTS.md` (both repos) — verification checklist for AI-assisted changes.
