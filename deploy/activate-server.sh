#!/usr/bin/env bash
# Initial activation only; inspect existing files before using on an older server.
set -euo pipefail
test "$(id -u)" = 0
cd /opt/finkoper-ai
for target in /etc/finkoper-ai/finkoper-ai.env /etc/systemd/system/finkoper-ai.service /etc/nginx/sites-available/finkoper-ai /etc/systemd/journald.conf.d/finkoper-ai.conf; do
    if test -e "$target"; then
        echo "STOP: existing configuration $target" >&2
        exit 1
    fi
done
test "$(find /etc/nginx/sites-enabled -mindepth 1 -maxdepth 1 | wc -l)" = 1
test -L /etc/nginx/sites-enabled/default
test "$(readlink -f /etc/nginx/sites-enabled/default)" = /etc/nginx/sites-available/default
test -f /etc/nginx/sites-available/default
install -d -m 0700 -o root -g root /etc/finkoper-ai/backups
cp -a /etc/nginx/sites-available/default /etc/finkoper-ai/backups/nginx-default.available
cp -a /etc/nginx/sites-enabled/default /etc/finkoper-ai/backups/nginx-default.enabled
install -m 0640 -o root -g finkoper-ai .env.example /etc/finkoper-ai/finkoper-ai.env
install -m 0644 -o root -g root deploy/finkoper-ai.service /etc/systemd/system/finkoper-ai.service
install -m 0644 -o root -g root deploy/nginx-loopback.conf /etc/nginx/sites-available/finkoper-ai
unlink /etc/nginx/sites-enabled/default
ln -s /etc/nginx/sites-available/finkoper-ai /etc/nginx/sites-enabled/finkoper-ai
nginx -t
install -d -m 0755 /etc/systemd/journald.conf.d
install -m 0644 -o root -g root deploy/journald-finkoper-ai.conf /etc/systemd/journald.conf.d/finkoper-ai.conf
systemctl restart systemd-journald
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/finkoper-ai.service
systemctl enable --now finkoper-ai.service
systemctl unmask nginx.service
systemctl enable --now nginx.service
systemctl restart finkoper-ai.service
for attempt in {1..30}; do
    if curl -fsS http://127.0.0.1:8000/healthz; then break; fi
    sleep 1
done
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:8080/healthz
echo
systemctl is-active finkoper-ai.service nginx.service
systemctl is-enabled finkoper-ai.service nginx.service
ss -lntup
