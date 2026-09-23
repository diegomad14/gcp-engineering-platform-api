#!/bin/sh
set -eu

scanner="$(basename "$0")"
exec python3 /opt/eng-platform/trusted_scanner.py "$scanner" "$@"
