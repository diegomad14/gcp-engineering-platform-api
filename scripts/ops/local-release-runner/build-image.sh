#!/usr/bin/env bash

set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 OUTPUT_QCOW2" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="$SCRIPT_DIR/approved-artifacts.json"
PROVISIONER="$SCRIPT_DIR/image-provision.sh"
OUTPUT="$1"
command -v jq >/dev/null || { echo "jq is required" >&2; exit 2; }
IMAGE_URL="$(jq -er '.base_image.url' "$MANIFEST")"
EXPECTED_SHA256="$(jq -er '.base_image.sha256' "$MANIFEST")"

case "$IMAGE_URL" in
  https://cloud-images.ubuntu.com/*/current/*)
    echo "Use a dated Ubuntu cloud image URL, not /current/" >&2
    exit 2
    ;;
  https://cloud-images.ubuntu.com/*) ;;
  *)
    echo "Only official Ubuntu cloud images are accepted" >&2
    exit 2
    ;;
esac
if [[ ! "$EXPECTED_SHA256" =~ ^[0-9a-fA-F]{64}$ ]]; then
  echo "SHA256 must be a 64-character hexadecimal value" >&2
  exit 2
fi
if [[ -e "$OUTPUT" ]]; then
  echo "Refusing to overwrite existing image: $OUTPUT" >&2
  exit 2
fi
command -v curl >/dev/null || { echo "curl is required" >&2; exit 2; }
command -v qemu-img >/dev/null || { echo "qemu-img is required" >&2; exit 2; }
command -v virt-customize >/dev/null || {
  echo "virt-customize is required to build the pre-provisioned image" >&2
  echo "Build on a trusted Linux host with libguestfs-tools installed" >&2
  exit 2
}

WORK_DIR="$(mktemp -d -t cgm-release-image.XXXXXX)"
trap 'rm -rf "$WORK_DIR"' EXIT
DOWNLOADED="$WORK_DIR/base.img"

curl --fail --location --retry 5 --retry-all-errors \
  --output "$DOWNLOADED" "$IMAGE_URL"
if command -v sha256sum >/dev/null; then
  echo "${EXPECTED_SHA256}  ${DOWNLOADED}" | sha256sum --check --status
elif command -v shasum >/dev/null; then
  ACTUAL_SHA256="$(shasum -a 256 "$DOWNLOADED" | awk '{print $1}')"
  [[ "$ACTUAL_SHA256" == "$EXPECTED_SHA256" ]]
else
  echo "sha256sum or shasum is required" >&2
  exit 2
fi
qemu-img convert -O qcow2 "$DOWNLOADED" "$OUTPUT"
chmod 600 "$OUTPUT"
virt-customize -a "$OUTPUT" \
  --copy-in "$MANIFEST:/tmp" \
  --run "$PROVISIONER" \
  --delete /etc/machine-id \
  --truncate /etc/machine-id

if command -v sha256sum >/dev/null; then
  IMAGE_SHA256="$(sha256sum "$OUTPUT" | awk '{print $1}')"
else
  IMAGE_SHA256="$(shasum -a 256 "$OUTPUT" | awk '{print $1}')"
fi
echo "Created pre-provisioned disposable image: $OUTPUT"
echo "IMAGE_SHA256=$IMAGE_SHA256"
