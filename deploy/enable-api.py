"""Enable the existing service, preserving all unrelated env settings."""
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess

env=Path('/etc/finkoper-ai/finkoper-ai.env')
backup=Path('/etc/finkoper-ai/backups')/('api-env-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
backup.mkdir(mode=0o700)
shutil.copy2(env,backup/'finkoper-ai.env')
updates={'FINKOPER_API_ENABLED':'true','FINKOPER_API_DATA_DIR':'/var/lib/finkoper-ai/api',
         'FINKOPER_API_MAX_ITEMS':'1000','FINKOPER_API_MAX_BODY_BYTES':str(5*1024*1024),
         'FINKOPER_API_MAX_WAITING_BATCHES':'5','FINKOPER_API_MAX_WAITING_MESSAGES':'2000',
         'FINKOPER_API_QUEUE_TIMEOUT':'30','FINKOPER_API_DEADLINE':'600','FINKOPER_API_INFERENCE_TIMEOUT':'180'}
lines=[line for line in env.read_text().splitlines() if line.split('=',1)[0] not in updates]
lines.extend(f'{key}={value}' for key,value in updates.items())
env.write_text('\n'.join(lines)+'\n')
subprocess.run(['install','-d','-m','0700','-o','finkoper-ai','-g','finkoper-ai','/var/lib/finkoper-ai/api'],check=True)
print('API enabled; unrelated environment values preserved; no secrets added')
