#!/usr/bin/env bash
# Initial provisioning only. Never overwrite an existing installation.
set -euo pipefail
test "$(id -u)" = 0
for target in /opt/finkoper-ai /var/lib/finkoper-ai /etc/finkoper-ai; do
    if test -e "$target"; then
        echo "STOP: existing path $target; inspect manually" >&2
        exit 1
    fi
done
if getent passwd finkoper-ai >/dev/null; then
    echo 'STOP: user already exists; inspect manually' >&2
    exit 1
fi
if dpkg-query -W -f='${Status}' nginx 2>/dev/null | grep -q 'install ok installed'; then
    echo 'STOP: nginx already installed; inspect manually' >&2
    exit 1
fi
if test -e /etc/systemd/system/nginx.service || test -L /etc/systemd/system/nginx.service; then
    echo 'STOP: nginx systemd override exists; inspect manually' >&2
    exit 1
fi
echo 'Existing firewall rules (unchanged):'
ufw status verbose
nft list ruleset
iptables-save
ip6tables-save
echo 'CUDA-related installed packages:'
dpkg-query -W | grep -Ei 'cuda|nvidia|cudnn' || true
systemctl mask nginx.service
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=l
apt-get update
apt-get install -y --no-install-recommends python3 python3-venv python3-pip git curl ca-certificates nginx
useradd --system --user-group --home-dir /var/lib/finkoper-ai --shell /usr/sbin/nologin finkoper-ai
install -d -o root -g root -m 0755 /opt/finkoper-ai
install -d -o root -g finkoper-ai -m 0750 /etc/finkoper-ai
install -d -o finkoper-ai -g finkoper-ai -m 0750 /var/lib/finkoper-ai
for name in huggingface torch cache tmp; do
    install -d -o finkoper-ai -g finkoper-ai -m 0700 "/var/lib/finkoper-ai/$name"
done
python3 -m venv /opt/finkoper-ai/venv
echo 'Base packages and directories prepared; nginx remains masked until configuration.'
