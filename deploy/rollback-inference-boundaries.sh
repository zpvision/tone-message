#!/usr/bin/env bash
set -euo pipefail
test "$(id -u)" = 0
backup=/var/backups/finkoper-ai/20261119-boundaries/before.tar.gz
test -r "$backup"
systemctl stop finkoper-ai
tar -xzf "$backup" -C /
systemctl start finkoper-ai
curl --retry 10 --retry-connrefused --retry-delay 1 -fsS http://127.0.0.1:8000/readyz
