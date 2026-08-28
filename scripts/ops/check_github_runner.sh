#!/usr/bin/env bash

set -euo pipefail

REPOSITORY="${1:-${GITHUB_REPOSITORY:-}}"
LABEL="${2:-cgm-release-local}"

if [[ -z "$REPOSITORY" ]]; then
  echo "Usage: $0 OWNER/REPOSITORY [runner-label]" >&2
  exit 2
fi

RUNNERS_JSON=$(gh api "repos/${REPOSITORY}/actions/runners?per_page=100")

printf '%s\n' "$RUNNERS_JSON" | python3 -c '
import json
import sys

label = sys.argv[1]
payload = json.load(sys.stdin)
runners = payload.get("runners", [])
available = [
    runner
    for runner in runners
    if runner.get("status") == "online"
    and not runner.get("busy", False)
    and label in {item.get("name") for item in runner.get("labels", [])}
]

if not available:
    print(
        f"No online idle runner with label {label!r} is available",
        file=sys.stderr,
    )
    raise SystemExit(1)

names = ", ".join(
    f"{runner.get('name', 'unknown')}#{runner.get('id', 'unknown')}"
    for runner in available
)
print(f"OK: {names} ({label})")
' "$LABEL"
