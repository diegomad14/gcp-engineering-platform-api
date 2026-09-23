#!/usr/bin/env bash
# Trusted wrapper: repository content is only a Docker build context. Neither the
# built image nor its runtime container receives this helper's Docker socket.
set -uo pipefail

source_dir="${1:-/workspace}"
output_dir="${2:-/eng-platform-output/external}"
correlation="${BUILD_ID:-${GITHUB_RUN_ID:-local}}-${RANDOM}-$$"
safe_correlation="$(printf '%s' "$correlation" | tr -cd 'a-zA-Z0-9_.-' | cut -c1-36)"
container_name="eng-platform-api-smoke-${safe_correlation}"
network_name="eng-platform-api-smoke-net-${safe_correlation}"
image_name="eng-platform-api:quality-${safe_correlation}"
log_file="$(mktemp)"
response_file="$(mktemp)"
started="$(date +%s)"
status="FAILED"
details="API container smoke did not complete."

# shellcheck disable=SC2329  # invoked by the EXIT trap
cleanup() {
  docker rm --force "$container_name" >/dev/null 2>&1 || true
  docker network rm "$network_name" >/dev/null 2>&1 || true
  docker image rm --force "$image_name" >/dev/null 2>&1 || true
  rm -f "$log_file" "$response_file"
}
trap cleanup EXIT

if [[ ! -d "$source_dir/.git" || ! -f "$source_dir/Dockerfile" ]]; then
  details="Container smoke source checkout is invalid."
elif ! git -c "safe.directory=$source_dir" -C "$source_dir" \
    archive --format=tar HEAD 2>>"$log_file" \
    | docker build --tag "$image_name" - >"$log_file" 2>&1; then
  details="Container image build failed: $(tail -n 1 "$log_file" | cut -c1-360)"
elif ! docker network create --internal "$network_name" >/dev/null 2>&1; then
  details="Unable to create the isolated smoke network."
elif ! docker run --detach --rm \
    --name "$container_name" \
    --network "$network_name" \
    --publish 127.0.0.1::8000 \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
    "$image_name" >"$log_file" 2>&1; then
  details="Unable to start the isolated API container."
else
  port="$(docker port "$container_name" 8000/tcp 2>/dev/null | sed -n '1s/.*://p')"
  if [[ ! "$port" =~ ^[0-9]+$ ]]; then
    details="Docker did not publish the API smoke port."
  else
    ready="false"
    for _ in $(seq 1 30); do
      if curl --fail --silent --max-time 2 "http://127.0.0.1:${port}/health" >/dev/null; then
        ready="true"
        break
      fi
      sleep 1
    done
    if [[ "$ready" != "true" ]]; then
      details="API container did not become healthy."
    elif ! curl --fail-with-body --silent --show-error --max-time 10 --request POST \
      "http://127.0.0.1:${port}/api/service-factory/plan" \
      --header 'Content-Type: application/json' \
      --data '{"repository":"test-org/test-repo","service_name":"test-api","service_type":"api","runtime":"python","gcp_project":"test-project","owner":"platform"}' \
      --output "$response_file" 2>>"$log_file"; then
      details="Service factory smoke request failed: $(tail -n 1 "$log_file" | cut -c1-360)"
    elif ! grep --quiet 'platform_deploy_workflow' "$response_file"; then
      details="Service factory contract was not present in the API response."
    else
      status="PASSED"
      details="Container health and service factory contract passed."
    fi
  fi
fi

duration="$(( $(date +%s) - started ))"
mkdir -p "$output_dir"
python3 - "$output_dir/api-container-smoke.json" "$status" "$duration" "$details" <<'PY'
import json
import os
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
status = sys.argv[2]
value = {
    "schema_version": 1,
    "name": "API container smoke",
    "category": "container_smoke",
    "status": status,
    "findings": int(status == "FAILED"),
    "blocking_findings": int(status == "FAILED"),
    "duration_seconds": float(sys.argv[3]),
    "details": sys.argv[4][:500],
    "report_path": "",
}
path.parent.mkdir(parents=True, exist_ok=True)
os.chown(path.parent, 0, 0, follow_symlinks=False)
os.chmod(path.parent, 0o755)
descriptor, temporary_name = tempfile.mkstemp(prefix=".api-smoke.", dir=path.parent)
with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
    json.dump(value, handle, sort_keys=True, separators=(",", ":"))
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.chown(temporary_name, 0, 0, follow_symlinks=False)
os.chmod(temporary_name, 0o444)
os.replace(temporary_name, path)
os.chmod(path.parent, 0o555)
PY

# Quality status is intentionally decided by the manifest publisher.
exit 0
