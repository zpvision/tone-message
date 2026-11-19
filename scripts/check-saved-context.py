"""Read saved prompts and latest job rows; report counts only. No prompt changes."""
import os
import hashlib
import json
import sqlite3
import time
from pathlib import Path
import httpx
from tokenizers import Tokenizer

tokenizer = Tokenizer.from_file('/var/lib/finkoper-ai/tokenizers/qwen3-8b/tokenizer.json')
tokenizer.no_truncation()
tokenizer.no_padding()
with sqlite3.connect('/var/lib/finkoper-ai/cabinet/cabinet.sqlite3') as db:
    saved_raw = db.execute('SELECT body FROM prompts ORDER BY id DESC LIMIT 1').fetchone()[0]
    prompts = json.loads(saved_raw)
    job_id, snapshot_raw = db.execute('SELECT id,snapshot FROM jobs ORDER BY created DESC LIMIT 1').fetchone()
    snapshot = json.loads(snapshot_raw)
    rows = db.execute('SELECT row_num,text FROM rows WHERE job_id=? ORDER BY row_num', (job_id,)).fetchall()
report = {'source_job': job_id, 'saved_prompts_sha256': hashlib.sha256(saved_raw.encode()).hexdigest(),
          'context': 4096, 'output_reserve': 128, 'rows': [], 'generation': []}
with httpx.Client(base_url='http://127.0.0.1:11434', timeout=180) as client:
    for index, (number, text) in enumerate(rows):
        if not text:
            continue
        payload = {'model': os.environ["FINKOPER_MODEL_ID"], 'stream': False, 'think': False, 'keep_alive': '30m',
                   'truncate': False, 'shift': False,
                   'messages': [{'role': 'system', 'content': prompts['system']},
                                {'role': 'user', 'content': prompts['template'].replace('{text}', text)}],
                   'options': {'num_ctx': 4096, 'num_predict': 128, 'temperature': 0}}
        rendered_response = client.post('/api/chat', json={**payload, '_debug_render_only': True})
        rendered_response.raise_for_status()
        rendered = rendered_response.json()['_debug_info']['rendered_template']
        count = len(tokenizer.encode(rendered, add_special_tokens=False).ids)
        report['rows'].append({'row': number, 'input_tokens': count, 'total_budget': count + 128, 'fits': count + 128 <= 4096})
        if index < 3 and count + 128 <= 4096:
            started = time.monotonic()
            generated = client.post('/api/chat', json=payload)
            generated.raise_for_status()
            raw = generated.json()
            try:
                label = json.loads(raw['message']['content'])['label']
            except (ValueError, KeyError, TypeError):
                label = None
            report['generation'].append({'row': number, 'input_tokens': count, 'prompt_eval_count': raw.get('prompt_eval_count'),
                'prompt_eval_cached_count': raw.get('prompt_eval_cached_count'), 'eval_count': raw.get('eval_count'),
                'done_reason': raw.get('done_reason'), 'label': label, 'seconds': time.monotonic() - started})
report['input_min'] = min(r['input_tokens'] for r in report['rows'])
report['input_max'] = max(r['input_tokens'] for r in report['rows'])
report['fits'] = sum(r['fits'] for r in report['rows'])
path = Path('/tmp/saved-context-check.json')
path.write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps({k: v for k,v in report.items() if k != 'rows'}))
