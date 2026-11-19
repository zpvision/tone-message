#!/usr/bin/env bash
set -euo pipefail
test "$(id -u)" = 0
test -f /etc/nginx/finkoper-ai.htpasswd
python3 - <<'PY'
import sqlite3
from pathlib import Path
path = Path('/var/lib/finkoper-ai/cabinet/cabinet.sqlite3')
if path.exists():
    with sqlite3.connect(path) as db:
        active = db.execute("SELECT count(*) FROM jobs WHERE status IN ('preparing','warming','running','stopping','exporting')").fetchone()[0]
    if active:
        raise SystemExit('Active cabinet job: finish or stop it before deployment')
PY
backup="/etc/finkoper-ai/backups/cabinet-update-$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 0700 "$backup"
cp -a /opt/finkoper-ai/app "$backup/app"
systemctl stop finkoper-ai.service
cp -a /opt/finkoper-ai/stage-cabinet/app/. /opt/finkoper-ai/app/
systemctl start finkoper-ai.service
echo "Cabinet code updated; backup: $backup"
