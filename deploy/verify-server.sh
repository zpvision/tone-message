#!/usr/bin/env bash
set -euo pipefail
test "$(id -u)" = 0
cd /opt/finkoper-ai
echo 'ACCEPTANCE HTTP / PROCESS / LISTENERS'
venv/bin/python scripts/acceptance.py
echo 'NGINX'
nginx -t
echo 'OLLAMA'
ollama --version
ollama list
ollama show "${FINKOPER_MODEL_ID:?Set exact installed model ID explicitly}"
ollama ps
systemctl show ollama.service -p User -p Group -p ActiveState -p UnitFileState -p Restart -p MainPID
stat -c '%U:%G %a %n' /var/lib/finkoper-ai/models /var/lib/finkoper-ai/models/ollama
getfacl /var/lib/finkoper-ai
echo 'DEPENDENCIES'
venv/bin/python -m pip check
venv/bin/python --version
dpkg-query -W python3 python3-venv python3-pip nginx git curl ca-certificates systemd
echo 'SERVICE'
systemctl show finkoper-ai.service -p User -p Group -p MainPID -p NRestarts -p ActiveState -p UnitFileState -p PrivateDevices -p NoNewPrivileges -p ProtectSystem -p ProtectHome -p MemoryCurrent
echo 'GPU / DISK / RAM'
nvidia-smi
df -hT /
free -h
echo 'FIREWALL / TIME'
ufw status verbose
nft list ruleset
iptables-save
ip6tables-save
timedatectl
echo 'DIRECTORY AND SECRET PERMISSIONS (NO CONTENTS)'
stat -c '%U:%G %a %n' /opt/finkoper-ai /opt/finkoper-ai/venv /var/lib/finkoper-ai /var/lib/finkoper-ai/* /etc/finkoper-ai /etc/finkoper-ai/finkoper-ai.env
echo 'ALL LISTENERS'
ss -lntup
echo 'JOURNAL LIMITS'
journalctl --disk-usage
systemd-analyze cat-config systemd/journald.conf
echo 'RECENT BACKEND LIFECYCLE'
journalctl -u finkoper-ai.service -n 40 --no-pager
