#!/usr/bin/env bash
set -euo pipefail
echo 'VERSION AND MODEL'
ollama --version
ollama list
ollama show "${FINKOPER_MODEL_ID:?Set exact installed model ID explicitly}"
ollama ps
echo 'SERVICE'
systemctl is-active ollama.service finkoper-ai.service nginx.service
systemctl is-enabled ollama.service finkoper-ai.service nginx.service
systemctl cat ollama.service
echo 'GPU AND RAM'
nvidia-smi
ps -C ollama,llama-server -o pid,user,rss,pcpu,args
free -h
df -h /
echo 'PORTS'
ss -lntup
echo 'GPU LOG EVIDENCE'
journalctl -u ollama.service --no-pager | grep -E 'offload|CUDA0|n_ctx|n_seq|flash_attn|inference compute|warming up|prompt eval time|eval time' || true
echo 'READINESS / HEALTH'
curl -fsS http://127.0.0.1:8000/healthz
echo
curl -fsS http://127.0.0.1:8000/readyz
echo
curl -sS -o /dev/null -w 'Nginx unauthenticated healthz HTTP %{http_code} (expected 401)' http://127.0.0.1:8080/healthz
echo
curl -sS -o /dev/null -w 'Nginx unauthenticated readyz HTTP %{http_code} (expected 401)' http://127.0.0.1:8080/readyz
echo
nginx -t
