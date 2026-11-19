#!/usr/bin/env bash
set -euo pipefail
test "$(id -u)" = 0
if command -v ollama >/dev/null || systemctl cat ollama.service >/dev/null 2>&1; then
    echo 'STOP: existing Ollama installation; inspect before changing it.' >&2
    exit 1
fi
nvidia-smi >/dev/null
for path in /var/lib/finkoper-ai/models /etc/systemd/system/ollama.service.d; do
    test ! -e "$path" || { echo "STOP: existing $path" >&2; exit 1; }
done
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l
apt-get install -y --no-install-recommends zstd acl
# Official stable archive, checked against the official GitHub release digest.
python3 /opt/finkoper-ai/scripts/download_ollama.py
bash /opt/finkoper-ai/deploy/finish-ollama-manual.sh
