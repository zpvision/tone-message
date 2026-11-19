"""Export the explicitly selected, immutable classification preset."""
import hashlib
import json
import sqlite3
from pathlib import Path

source = '2435a5e62d9243b09eec55eb55d6c712'
with sqlite3.connect('/var/lib/finkoper-ai/cabinet/cabinet.sqlite3') as db:
    snapshot = json.loads(db.execute('SELECT snapshot FROM jobs WHERE id=?', (source,)).fetchone()[0])
system = snapshot['prompts']['system']
document = {'version':'sentiment-v1','source_job':source,'system':system,
            'user_template':snapshot['prompts']['template'],'system_sha256':hashlib.sha256(system.encode()).hexdigest()}
Path('/tmp/finkoper-api-preset.json').write_text(json.dumps(document,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'chars':len(system),'sha256':document['system_sha256']}))
