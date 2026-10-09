# Reconnections Go onboarding

`catalog/services/cgm-reconnections-api.yaml` is a proposal for the SHA-pinned
private catalog, not an active production entry. Do not modify public catalog
fixtures to pretend onboarding has completed. Validate and append with
`catalog_registry.py --catalog <private-catalog.json> --check/--write`, review the
complete result, publish its configured private source and update its digest.

## Managed quality and release

Go 1.27.2 uses a dedicated credential-free quality image with native atomic
coverage over `-coverpkg=./...`, race detection, go vet, gofmt and build/type
checks. The gate reads native statement blocks and trusted Go syntax to count
changed executable statement-start lines; comments and declarations are not
manufactured as covered lines. All executable source files in the configured
roots require evidence. Set `.quality-sources.json` roots to `cmd` and `internal`,
excluding `*_test.go`; do not exclude untested production packages.

Merge the platform support through its existing oss-v2 process, publish
`quality-go` and `eng-platform-release-executor` using Publish Release Tooling,
and configure `ENG_PLATFORM_QUALITY_GO_IMAGE` to its actual OCI digest. Existing
image pins are preserved; never invent a bundle digest before publication.
Controller and engine profile hashes must agree before processing this service.
Go profiles are additive in `release_quality_profiles.go.json`; the reviewed
primary manifest, existing per-service hashes and bundle image pins stay intact.

The private default remains Cloud Build for existing repositories. Add only
`cgm-reconnections-api` to `ENG_PLATFORM_GITHUB_FIRST_SERVICES` for GitHub-first
quality, SemVer publication and deployment with verified billing fallback.
Cloud Build-only catalog/config overrides still take precedence. Keep existing
provider reservations and drain them before changing routing; unknown deployment
providers still require reconciliation. This exception does not alter billing
circuits or other repositories' executor hints.

Copy the current `eng-platform-quality.yml` and `eng-platform-release.yml`
transport workflows, replacing the service name and removing API container-smoke
steps (this Go profile has `container_smoke=false`). The exact PR fetch uses
the read-only GitHub workflow token via command-scoped Git headers, then clears
it before checking out source; never persist it in Git config or mount it into
the credential-free gate. Main checkout already uses `persist-credentials=false`. Copy canonical Platform
Deploy/Rollback plus their authorization action. These are transport callers;
commands, quality policy and publication stay controlled by eng-platform.

Set repository variables `ENG_PLATFORM_RELEASE_ORCHESTRATOR_ENABLED=true`,
`ENG_PLATFORM_CI_EXECUTOR=github_actions`, `ENG_PLATFORM_API_URL`,
`GCP_QUALITY_WIF_PROVIDER`, `GCP_QUALITY_WIF_SERVICE_ACCOUNT`,
`GCP_PUBLICATION_WIF_PROVIDER`, `GCP_PUBLICATION_WIF_SERVICE_ACCOUNT`,
`GCP_RELEASE_WIF_PROVIDER`, `GCP_WIF_SERVICE_ACCOUNT`,
`ENG_PLATFORM_RELEASE_EXECUTOR_IMAGE` (actual digest) and
`ENG_PLATFORM_RELEASE_SIGNING_PUBLIC_KEY`. Extend the GitHub App installation and
WIF repository condition to the new private repository. Quality and publication use separate image-reader-only identities/providers
bound respectively to the canonical PR workflow (`pull_request_target`, main
base) and publication workflow (`push`, main ref). Their only GCP role is shared
tooling Artifact Registry reader; report/planner callbacks use GitHub OIDC and
server-side eng-platform publishes tags/Releases. Existing callers retain a
compatibility fallback, but the new service must configure both dedicated reader
identities explicitly. Release/rollback keep the separate workflow-dispatch WIF
and release account with app-registry writer/Cloud Run deployment grants. Never
install a service-account key.

Enroll the service in release and deployment enabled-services configuration,
provide the canonical Cloud Build repository connection for fallback, and add
its dedicated project to catalog and deployment provider IAM. Review the private
catalog separately from any example file. Preserve oss-v2 coverage 80% global and
80% differential. PR quality and main publication always bind the exact SHA.

## Dedicated infrastructure

Use `cgm-reconnections-prod`, `us-central1`, registry `cgm-reconnections-repo`,
Cloud Run service `cgm-reconnections-api` and health `/healthz`. Bootstrap runtime
IAM, Firestore, Storage, Tasks, Scheduler and secret bindings via the service's
Terraform. The platform executor uses `gcloud run services update`; it requires
a baseline Cloud Run resource with approved runtime configuration. First artifact
build can finish before an absent-runtime candidate fails; bootstrap the baseline
with that exact immutable artifact, then retry managed candidate validation.
Do not claim a failed candidate is a successful deployment or bypass promotion.

The controller/deployer requires Cloud Run administration, app Artifact Registry
write, and `iam.serviceAccountUser` on
`reconnections-runtime@cgm-reconnections-prod.iam.gserviceaccount.com`.
GitHub release WIF uses
`reconnections-release@cgm-reconnections-prod.iam.gserviceaccount.com`.
Internal Tasks/Scheduler use
`reconnections-task-invoker@cgm-reconnections-prod.iam.gserviceaccount.com`.

Initialize in simulation mode, validate real fixtures and candidate health, then
promote through the platform. Provider and communications credentials are secret
values outside catalog and source. Production activation requires its real pilot;
GitHub Release publication alone does not deploy or enable reconnections.
