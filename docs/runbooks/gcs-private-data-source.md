# Runbook - Private GCS Data Source

## Goal

Store sensitive data files outside Git and expose them to Cloud Run services through a private, least-privilege GCS pattern.

Use this for data sources such as local database files, reference datasets, or operational exports that must not be committed to a repository.

## Non-Negotiables

- Do not commit `.db`, `.sqlite`, `.csv`, dumps, or PII to Git.
- Do not print row-level data, addresses, coordinates, IDs, or credentials in logs.
- Do not grant `allUsers` or `allAuthenticatedUsers`.
- Do not use service account JSON keys.
- Do not store data file contents in Secret Manager.

## Placeholders

Replace these placeholders per service:

- `<PROJECT_ID>`
- `<REGION>`
- `<BUCKET_NAME>`
- `<SERVICE_NAME>`
- `<RUNTIME_SERVICE_ACCOUNT>`
- `<DATA_OBJECT>`
- `<MOUNT_PATH>`
- `<ENV_VAR_NAME>`

Example runtime path:

```text
<ENV_VAR_NAME>=<MOUNT_PATH>/<DATA_OBJECT>
```

For a Perseo-style SQLite data file, the application might receive:

```text
PERSEO_DB_PATH=/mnt/perseo/kpi_cgm.db
```

## Bucket Baseline

Create a private bucket with uniform bucket-level access enabled:

```bash
gcloud storage buckets create gs://<BUCKET_NAME> \
  --project=<PROJECT_ID> \
  --location=<REGION> \
  --uniform-bucket-level-access
```

Verify public access is absent:

```bash
gcloud storage buckets get-iam-policy gs://<BUCKET_NAME> \
  --format=json
```

The IAM policy must not include `allUsers` or `allAuthenticatedUsers`.

## Runtime Service Account Access

Grant the runtime service account read-only object access:

```bash
gcloud storage buckets add-iam-policy-binding gs://<BUCKET_NAME> \
  --member=serviceAccount:<RUNTIME_SERVICE_ACCOUNT> \
  --role=roles/storage.objectViewer
```

Do not grant writer, admin, project-wide owner, or editor roles for this pattern.

## Upload Data

Upload the data object from an approved workstation or pipeline without printing data:

```bash
gcloud storage cp <LOCAL_DATA_FILE> gs://<BUCKET_NAME>/<DATA_OBJECT>
```

Do not commit `<LOCAL_DATA_FILE>` to Git.

## Cloud Run Read-Only Mount

Attach the bucket as a read-only Cloud Run volume in a candidate revision:

```bash
gcloud run services update <SERVICE_NAME> \
  --project=<PROJECT_ID> \
  --region=<REGION> \
  --add-volume=name=data-source,type=cloud-storage,bucket=<BUCKET_NAME>,readonly=true \
  --add-volume-mount=volume=data-source,mount-path=<MOUNT_PATH> \
  --update-env-vars=<ENV_VAR_NAME>=<MOUNT_PATH>/<DATA_OBJECT> \
  --no-traffic
```

Use the platform release-candidate workflow when adopting this pattern. The candidate must be validated through a tagged URL before promotion.

## Validation

Before promotion:

1. Snapshot current service and revision state.
2. Deploy a no-traffic candidate revision.
3. Validate the candidate tagged URL.
4. Hit a safe endpoint that proves the data source is available without returning PII.
5. Confirm logs do not print data contents.
6. Promote only after smoke and behavior checks pass.

## Rollback

Rollback must move traffic to the previous known-good revision:

```bash
gcloud run services update-traffic <SERVICE_NAME> \
  --project=<PROJECT_ID> \
  --region=<REGION> \
  --to-revisions=<PREVIOUS_READY_REVISION>=100
```

Do not delete the bucket, object, previous revision, or revision tags during incident response unless a separate approved data-removal procedure requires it.

## Security Checklist

- [ ] Data file is outside Git.
- [ ] Bucket uses uniform bucket-level access.
- [ ] No public principals exist in bucket IAM.
- [ ] Runtime service account has only `roles/storage.objectViewer`.
- [ ] Candidate revision is deployed with no production traffic.
- [ ] Validation endpoint does not expose PII.
- [ ] Rollback revision is known before promotion.
- [ ] No service account JSON keys are created or used.
