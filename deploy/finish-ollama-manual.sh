#!/usr/bin/env bash
set -euo pipefail
test "$(id -u)" = 0
test ! -e /etc/systemd/system/ollama.service
! getent passwd ollama >/dev/null
archive=/opt/finkoper-ai/downloads/ollama-0.40.5.tar.zst
echo 'dc5eabc292f9949546c90a97c820f924b035e05243b0de13c7f366182562169c  /opt/finkoper-ai/downloads/ollama-0.40.5.tar.zst' | sha256sum -c -
tar --zstd -tf "$archive" | head -15 || true
tar --zstd -xf "$archive" -C /usr/local --no-same-owner
useradd -r -s /usr/sbin/nologin -U -m -d /usr/share/ollama ollama
for group in render video; do
    if getent group "$group" >/dev/null; then usermod -aG "$group" ollama; fi
done
install -d -o root -g ollama -m 0750 /var/lib/finkoper-ai/models
install -d -o ollama -g ollama -m 0750 /var/lib/finkoper-ai/models/ollama
setfacl -m u:ollama:--x /var/lib/finkoper-ai
install -d -o root -g root -m 0755 /etc/systemd/system/ollama.service.d
install -o root -g root -m 0644 /opt/finkoper-ai/deploy/ollama.service /etc/systemd/system/ollama.service
install -o root -g root -m 0644 /opt/finkoper-ai/deploy/ollama-override.conf /etc/systemd/system/ollama.service.d/finkoper-ai.conf
systemctl daemon-reload
systemctl enable --now ollama.service
for attempt in {1..30}; do
    if curl -fsS http://127.0.0.1:11434/api/version; then break; fi
    sleep 1
done
curl -fsS http://127.0.0.1:11434/api/version
ollama --version
systemctl is-active ollama.service
systemctl is-enabled ollama.service
id ollama
ollama serve --help
