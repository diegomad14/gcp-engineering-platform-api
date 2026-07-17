#!/usr/bin/env bash
set -euo pipefail

SERVICE="${1:?service name required}"
PROJECT_ID="${2:?project id required}"
REGION="${3:?region required}"
REVISION="${4:?revision required}"

cat <<EOF
gcloud run services update-traffic $SERVICE \\
  --project=$PROJECT_ID \\
  --region=$REGION \\
  --to-revisions=$REVISION=100
EOF

