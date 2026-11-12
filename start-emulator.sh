#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
command -v aws-emulator >/dev/null || {
  echo 'Install the official AWS Emulator Linux package first. See README.en.md or README.es.md.' >&2
  exit 1
}
mkdir -p .lab
export EMULATOR_TELEMETRY=off
aws-emulator -b 127.0.0.1 -p 19397 2>&1 | tee .lab/emulator.log
