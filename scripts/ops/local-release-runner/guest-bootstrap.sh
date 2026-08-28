#!/usr/bin/env bash

set -Eeuo pipefail

exec >/var/log/cgm-release-runner-bootstrap.log 2>&1

systemctl enable --now docker

for tool in curl docker gh gcloud jq node npm python3; do
  command -v "$tool" >/dev/null || {
    echo "Required guest tool is missing: $tool" >&2
    exit 1
  }
done

cd /opt/actions-runner
test "$(cat .cgm-runner-version)" = "$CGM_RUNNER_VERSION"
test "$(jq -r '.github_runner.sha256' /opt/cgm-release-approved-artifacts.json)" = "$CGM_RUNNER_SHA256"

./config.sh \
  --unattended \
  --url "https://github.com/${CGM_REPOSITORY}" \
  --token "$CGM_RUNNER_TOKEN" \
  --name "$CGM_RUNNER_NAME" \
  --labels "cgm-release-local" \
  --work "_work" \
  --replace

unset CGM_RUNNER_TOKEN
exec ./run.sh
