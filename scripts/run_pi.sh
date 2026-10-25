#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
if [[ ! -x .venv/bin/python ]]; then
  echo "Missing .venv. Create it and install requirements.txt first." >&2
  exit 1
fi
exec .venv/bin/python -m magic_mirror
