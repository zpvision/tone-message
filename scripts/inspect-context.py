"""Read-only diagnostics. Never print saved prompts or customer messages."""
import os
import json
import sqlite3
import httpx

with httpx.Client(base_url='http://127.0.0.1:11434', timeout=10) as client:
    show = client.post('/api/show', json={'model': os.environ["FINKOPER_MODEL_ID"]}).json()
    print('MODEL_TEMPLATE', show.get('template'))
    print('PARAMETERS', show.get('parameters'))
    for think in (False, True):
        rendered = client.post('/api/chat', json={'model': os.environ["FINKOPER_MODEL_ID"], 'stream': False, 'think': think,
            '_debug_render_only': True, 'truncate': False, 'options': {'num_ctx': 4096},
            'messages': [{'role': 'system', 'content': 'SYSTEM'}, {'role': 'user', 'content': 'USER'}]}).json()
        print('RENDER', think, repr(rendered['_debug_info']['rendered_template']))
with sqlite3.connect('/var/lib/finkoper-ai/cabinet/cabinet.sqlite3') as db:
    body = json.loads(db.execute('SELECT body FROM prompts ORDER BY id DESC LIMIT 1').fetchone()[0])
    print('SAVED_PROMPT_LENGTHS', {k: {'chars': len(v), 'utf8_bytes': len(v.encode())} for k,v in body.items()})
    snap = json.loads(db.execute('SELECT snapshot FROM jobs ORDER BY created DESC LIMIT 1').fetchone()[0])
    print('LAST_PARAMETERS', snap['parameters'])
