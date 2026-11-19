#!/usr/bin/env bash
set -euo pipefail
test "$(id -u)" = 0
PYTHON=/opt/finkoper-ai/venv/bin/python
"$PYTHON" -m pip install --no-cache-dir -r /opt/finkoper-ai/requirements.in
# Stable CUDA 12.8 build, officially documented; GPU architecture is detected by the installed runtime; validate before changing dependencies.
# https://pytorch.org/get-started/previous-versions/#v2100
"$PYTHON" -m pip install --no-cache-dir torch==2.10.0 --index-url https://download.pytorch.org/whl/cu128
"$PYTHON" -m pip check
"$PYTHON" -m pip freeze > /opt/finkoper-ai/requirements.lock.txt
"$PYTHON" -m pip list --format=json > /opt/finkoper-ai/installed-python-packages.json
