#!/usr/bin/env bash
# Keep ordinary command failures observable until the final, mandatory gate.
set -uo pipefail
step=$1
shift
mkdir -p /workspace/evidence
chmod a+rwx /workspace /workspace/evidence
started=$(date +%s)
"$@" 2>&1 | tee "/workspace/evidence/$step.log"
code=${PIPESTATUS[0]}
finished=$(date +%s)
printf '{"step":"%s","exit_code":%s,"duration_seconds":%s,"started_at":%s,"finished_at":%s}\n' \
  "$step" "$code" "$((finished - started))" "$started" "$finished" \
  > "/workspace/evidence/$step.json"
exit 0
