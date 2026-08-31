#!/usr/bin/env bash

set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 OUTPUT_QCOW2" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="$SCRIPT_DIR/approved-artifacts.json"
OUTPUT="$1"

command -v jq >/dev/null || { echo "jq is required" >&2; exit 2; }
command -v oras >/dev/null || { echo "oras is required" >&2; exit 2; }
command -v sha256sum >/dev/null || command -v shasum >/dev/null || {
  echo "sha256sum or shasum is required" >&2
  exit 2
}

if [[ -e "$OUTPUT" ]]; then
  echo "Refusing to overwrite existing image: $OUTPUT" >&2
  exit 2
fi

REFERENCE="$(jq -er '.runner_image.reference' "$MANIFEST")"
DIGEST="$(jq -er '.runner_image.digest' "$MANIFEST")"
FILENAME="$(jq -er '.runner_image.filename' "$MANIFEST")"
EXPECTED_SHA256="$(jq -er '.runner_image.sha256' "$MANIFEST")"
MEDIA_TYPE="$(jq -er '.runner_image.media_type' "$MANIFEST")"

[[ "$REFERENCE" == ghcr.io/diegomad14/cgm-release-runner-image ]] || {
  echo "Unapproved runner image registry reference" >&2
  exit 2
}
[[ "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || {
  echo "Approved runner image digest is missing or invalid" >&2
  exit 2
}
[[ "$EXPECTED_SHA256" =~ ^[0-9a-f]{64}$ ]] || {
  echo "Approved runner image SHA-256 is missing or invalid" >&2
  exit 2
}
[[ "$FILENAME" == cgm-release-local-ubuntu-24.04-amd64.qcow2 ]] || {
  echo "Unexpected runner image filename" >&2
  exit 2
}
[[ "$MEDIA_TYPE" == application/vnd.cgm.release-runner.qcow2 ]] || {
  echo "Unexpected runner image media type" >&2
  exit 2
}

WORK_DIR="$(mktemp -d -t cgm-release-pull.XXXXXX)"
trap 'rm -rf "$WORK_DIR"' EXIT
oras pull "$REFERENCE@$DIGEST" --output "$WORK_DIR"
DOWNLOADED="$WORK_DIR/$FILENAME"
[[ -f "$DOWNLOADED" ]] || { echo "OCI artifact did not contain $FILENAME" >&2; exit 2; }

if command -v sha256sum >/dev/null; then
  ACTUAL_SHA256="$(sha256sum "$DOWNLOADED" | awk '{print $1}')"
else
  ACTUAL_SHA256="$(shasum -a 256 "$DOWNLOADED" | awk '{print $1}')"
fi
[[ "$ACTUAL_SHA256" == "$EXPECTED_SHA256" ]] || {
  echo "Downloaded runner image SHA-256 does not match approval manifest" >&2
  exit 2
}

mkdir -p "$(dirname "$OUTPUT")"
chmod 600 "$DOWNLOADED"
mv "$DOWNLOADED" "$OUTPUT"
echo "Downloaded approved runner image: $OUTPUT"
echo "IMAGE_SHA256=$ACTUAL_SHA256"
