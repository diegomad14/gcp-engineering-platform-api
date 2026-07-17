#!/usr/bin/env bash
set -euo pipefail

SERVICE="${1:?service name required}"
PROJECT_ID="${2:?project id required}"
REGION="${3:?region required}"

gcloud run services describe "$SERVICE" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --format="yaml(status.url,status.latestReadyRevisionName,status.latestCreatedRevisionName,status.traffic)"

