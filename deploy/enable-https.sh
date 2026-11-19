#!/bin/bash
set -euo pipefail
test "$(id -u)" = 0
test -f /etc/letsencrypt/live/ai.example.com/fullchain.pem
backup="/etc/finkoper-ai/backups/https-$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 0700 "$backup"
cp -a /etc/nginx/sites-available/finkoper-ai "$backup/nginx.conf"
install -m 0644 /opt/finkoper-ai/deploy/nginx-api-proxy.conf /etc/nginx/snippets/finkoper-ai-api.conf
install -m 0644 /opt/finkoper-ai/deploy/nginx-cabinet-proxy.conf /etc/nginx/snippets/finkoper-ai-cabinet.conf
install -m 0644 /opt/finkoper-ai/deploy/nginx-cabinet.conf /etc/nginx/sites-available/finkoper-ai
if test -L /etc/nginx/sites-enabled/finkoper-acme; then
    rm /etc/nginx/sites-enabled/finkoper-acme
fi
nginx -t
systemctl reload nginx
install -m 0755 /opt/finkoper-ai/deploy/renew-nginx.sh /etc/letsencrypt/renewal-hooks/deploy/finkoper-nginx
systemctl enable --now certbot.timer
echo "HTTPS enabled; backup: $backup"
