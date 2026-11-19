#!/usr/bin/env bash
set -euo pipefail
test "$(id -u)" = 0
cd /opt/finkoper-ai
ollama pull "${FINKOPER_MODEL_ID:?Set exact installed model ID explicitly}" > verification/ollama-pull.log 2>&1
ollama list
ollama show "${FINKOPER_MODEL_ID:?Set exact installed model ID explicitly}"
df -h /
du -sh /var/lib/finkoper-ai/models/ollama
curl -fsS http://127.0.0.1:11434/api/tags
