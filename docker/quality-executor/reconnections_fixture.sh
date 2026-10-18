#!/bin/bash
# Extracted from the pinned canonical image and run on the disposable CI host.
# Never mounts Docker or credentials into repository-test containers.
# Guards use explicit exits: a bare [[ ]] failure does not trip errexit on
# every supported bash (notably 3.2), so a wrong fixture could pass silently.
set -euo pipefail
fail() {
  echo "reconnections fixture: $1" >&2
  exit 1
}
if ! [[ "${GITHUB_RUN_ID:-}" =~ ^[0-9]+$ && "${GITHUB_RUN_ATTEMPT:-}" =~ ^[0-9]+$ ]]; then
  fail "invalid GitHub run identity"
fi
if ! [[ "${POSTGRES_IMAGE:-}" =~ ^postgres@sha256:[0-9a-f]{64}$ ]]; then
  fail "POSTGRES_IMAGE must be pinned by digest"
fi
fixture="eng-platform-pg-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
network="eng-platform-pg-net-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
cleanup_failed_start() {
  docker rm --force "$fixture" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
}
abort() {
  cleanup_failed_start
  fail "$1"
}
docker network create "$network" >/dev/null || fail "network creation failed"
docker run --detach --name "$fixture" --network "$network" \
  --network-alias eng-platform-postgres --user postgres \
  --cap-drop ALL --security-opt no-new-privileges --pids-limit 128 \
  --memory 640m --cpus 0.5 \
  --tmpfs /var/lib/postgresql/data:rw,nosuid,nodev,noexec,uid=999,gid=999,mode=0700,size=512m \
  --tmpfs /var/run/postgresql:rw,nosuid,nodev,uid=999,gid=999,size=16m \
  --env POSTGRES_PASSWORD=quality-only --env POSTGRES_DB=postgres \
  "$POSTGRES_IMAGE" >/dev/null || abort "container start failed"
for attempt in $(seq 1 30); do
  if docker exec "$fixture" pg_isready -U postgres -d postgres -h 127.0.0.1 >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker exec "$fixture" pg_isready -U postgres -d postgres -h 127.0.0.1 >/dev/null 2>&1 \
  || abort "PostgreSQL did not become ready"
version="$(docker exec "$fixture" psql -U postgres -d postgres -Atqc 'SHOW server_version_num')" \
  || abort "server version query failed"
if ! [[ "$version" =~ ^15[0-9]{4}$ ]]; then
  abort "unexpected PostgreSQL server_version_num"
fi
