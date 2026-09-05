#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
sudo apt-get update
sudo apt-get install -y --no-install-recommends ffmpeg fonts-noto-core libasound2 libgomp1
python3 -m pip install --user uv==0.11.33
python3 -m uv sync --frozen --all-extras --python 3.11
.venv/bin/python -m deploy.codespaces --prepare
