#!/usr/bin/env bash
set -euo pipefail
test "$(id -u)" = 0
backup=/var/backups/finkoper-ai/20261118-model-neutral/before.tar.gz
test -r "$backup"
systemctl stop finkoper-ai ollama ollama-embeddings
tar -xzf "$backup" -C /
helpers=/var/backups/finkoper-ai/20261118-model-neutral/helpers-before.tar.gz
if test -r "$helpers"; then tar -xzf "$helpers" -C /; fi
rm -f /etc/systemd/system/ollama.service.d/model-neutral.conf
rm -f /etc/systemd/system/ollama-embeddings.service.d/cpu-only.conf
# Added adapter is inert once the original app is restored.
systemctl daemon-reload
systemctl start ollama ollama-embeddings finkoper-ai
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:8000/readyz
