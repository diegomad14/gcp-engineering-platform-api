#!/usr/bin/env bash
set -euo pipefail

# The Python package's OpenTelemetry dependencies can be inconsistent on the
# hosted runner. Run the same pinned, prebuilt scanner for every repository.
workspace="${GITHUB_WORKSPACE:?GITHUB_WORKSPACE is required}"
exec docker run --rm \
  --user "$(id -u):$(id -g)" \
  --volume "$workspace:$workspace" \
  --workdir "$PWD" \
  --env HOME=/tmp \
  semgrep/semgrep@sha256:cda1b566fafbf6010a02a3ea1d265b1c8eba4380e489a13891a102243d81ca6f \
  semgrep "$@"
