#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
mkdir -p storage
nohup .venv/bin/python -m deploy.codespaces > storage/codespaces.log 2>&1 < /dev/null &
