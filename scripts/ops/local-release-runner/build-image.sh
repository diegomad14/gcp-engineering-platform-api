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
OUTPUT_DIR="$(cd "$(dirname "$OUTPUT")" && pwd)"
OUTPUT="$OUTPUT_DIR/$(basename "$OUTPUT")"
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
command -v virt-resize >/dev/null || {
  echo "virt-resize is required to build the pre-provisioned image" >&2
  echo "Build on a trusted Linux host with libguestfs-tools installed" >&2
  exit 2
}

WORK_DIR="$(mktemp -d -t cgm-release-image.XXXXXX)"
BUILD_IMAGE="$(mktemp "$OUTPUT.tmp.XXXXXX")"
COMPRESSED_IMAGE="$(mktemp "$OUTPUT.compressed.XXXXXX")"
cleanup() {
  rm -rf "$WORK_DIR"
  rm -f "$BUILD_IMAGE" "$COMPRESSED_IMAGE"
}
trap cleanup EXIT
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
rm -f "$BUILD_IMAGE" "$COMPRESSED_IMAGE"
qemu-img create -f qcow2 "$BUILD_IMAGE" 20G
chmod 600 "$BUILD_IMAGE"
virt-resize --expand /dev/sda1 "$DOWNLOADED" "$BUILD_IMAGE"
virt-customize --network -a "$BUILD_IMAGE" \
  --copy-in "$MANIFEST:/tmp" \
  --run "$PROVISIONER" \
  --run-command 'install -m 0444 /dev/null /etc/machine-id'

qemu-img check "$BUILD_IMAGE"
qemu-img convert -O qcow2 -c "$BUILD_IMAGE" "$COMPRESSED_IMAGE"
chmod 600 "$COMPRESSED_IMAGE"
qemu-img check "$COMPRESSED_IMAGE"

if command -v sha256sum >/dev/null; then
  IMAGE_SHA256="$(sha256sum "$COMPRESSED_IMAGE" | awk '{print $1}')"
else
  IMAGE_SHA256="$(shasum -a 256 "$COMPRESSED_IMAGE" | awk '{print $1}')"
fi
mv "$COMPRESSED_IMAGE" "$OUTPUT"
echo "Created pre-provisioned disposable image: $OUTPUT"
echo "IMAGE_SHA256=$IMAGE_SHA256"
