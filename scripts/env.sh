#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${JAVA_HOME:-}" ]]; then
  export PATH="${JAVA_HOME}/bin:${PATH}"
fi

if [[ -d ".venv" ]]; then
  export VIRTUAL_ENV="$(pwd)/.venv"
  export PATH="${VIRTUAL_ENV}/bin:${PATH}"
fi
