#!/usr/bin/env bash

set -eu

export DEBIAN_FRONTEND=noninteractive
MANIFEST=/tmp/approved-artifacts.json

apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates \
  curl \
  docker.io \
  git \
  gh \
  gnupg \
  jq \
  nodejs \
  npm \
  python3 \
  unzip

install -m 0755 -d /etc/apt/keyrings
curl --fail --location --retry 5 --retry-all-errors \
  --output /tmp/cloud.google.gpg \
  https://packages.cloud.google.com/apt/doc/apt-key.gpg
gpg --dearmor --yes --output /etc/apt/keyrings/cloud.google.gpg /tmp/cloud.google.gpg
rm -f /tmp/cloud.google.gpg
echo "deb [signed-by=/etc/apt/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
  > /etc/apt/sources.list.d/google-cloud-sdk.list
apt-get update
apt-get install -y --no-install-recommends google-cloud-cli

runner_version=$(jq -er '.github_runner.version' "$MANIFEST")
runner_sha256=$(jq -er '.github_runner.sha256' "$MANIFEST")
archive="actions-runner-linux-x64-${runner_version}.tar.gz"

install -d -m 0755 /opt/actions-runner
cd /opt/actions-runner
curl --fail --location --retry 5 --retry-all-errors \
  --output "$archive" \
  "https://github.com/actions/runner/releases/download/v${runner_version}/${archive}"
echo "${runner_sha256}  ${archive}" | sha256sum --check --status
tar -xzf "$archive"
rm -f "$archive"
./bin/installdependencies.sh
printf '%s\n' "$runner_version" > .cgm-runner-version
chmod 0444 .cgm-runner-version

for tool in curl docker gh gcloud git jq node npm python3; do
  command -v "$tool" >/dev/null
done

cp "$MANIFEST" /opt/cgm-release-approved-artifacts.json
chmod 0444 /opt/cgm-release-approved-artifacts.json
systemctl enable docker
apt-get clean
rm -rf /var/lib/apt/lists/* /tmp/approved-artifacts.json
