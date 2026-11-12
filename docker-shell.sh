#!/usr/bin/env bash
set -euo pipefail
mkdir -p .lab
export EMULATOR_TELEMETRY=off
aws-emulator -b 127.0.0.1 -p 19397 > .lab/emulator.log 2>&1 &
emulator_pid=$!
cleanup() {
  kill -TERM "$emulator_pid" 2>/dev/null || true
  wait "$emulator_pid" 2>/dev/null || true
}
trap cleanup EXIT
ready=false
for ((attempt = 0; attempt < 30; attempt++)); do
  if curl -fsS --cacert "$HOME/.aws/emulator/19397/ca.pem" https://127.0.0.1:19397/__admin/health >/dev/null 2>&1; then
    ready=true
    break
  fi
  kill -0 "$emulator_pid" 2>/dev/null || break
  sleep 1
done
if [[ "$ready" != true ]]; then
  echo 'Emulator did not become healthy. Inspect .lab/emulator.log.' >&2
  exit 1
fi
if (( $# )); then
  "$@"
else
  bash --norc -i
fi
