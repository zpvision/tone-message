"""Small negative smoke test: real Ollama must refuse oversized synthetic input."""
import os
import json
from pathlib import Path
import sqlite3

import httpx

from app.context_budget import ContextBudget
from app.config import Settings


with sqlite3.connect('/var/lib/finkoper-ai/cabinet/cabinet.sqlite3') as db:
    assert not db.execute("SELECT 1 FROM jobs WHERE status IN ('preparing','warming','running','stopping','exporting')").fetchone()
system, user = 'Синтетическая проверка лимита.', 'проверь и\u0306 ' * 2500
params = {'thinking':False, 'num_ctx':4096, 'num_predict':128}
counter = ContextBudget(Settings().context_tokenizer_dir)
budget = counter.count(system,user,params)
assert budget['remaining_tokens'] < 0
response = httpx.post('http://127.0.0.1:11434/api/chat', timeout=60, json={
    'model':os.environ["FINKOPER_MODEL_ID"],'stream':False,'think':False,'truncate':False,'shift':False,'keep_alive':'30m',
    'options':{'num_ctx':4096,'num_predict':128,'temperature':0},
    'messages':[{'role':'system','content':system},{'role':'user','content':user}]})
assert response.status_code in (400,413), {'status':response.status_code,'keys':list(response.json())}
assert 'error' in response.json()
report = {'local_guard_rejects':True,'input_tokens':budget['input_tokens'],'context':4096,
          'ollama_http_status':response.status_code,'ollama_error':response.json()['error']}
Path('/tmp/context-overflow-check.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report))
