"""Read-only source-job diagnostics; reports no prompts or customer text."""
import hashlib
import json
import re
import sqlite3
import subprocess
import unicodedata
from pathlib import Path
import httpx
from tokenizers import Tokenizer

JOB = '2435a5e62d9243b09eec55eb55d6c712'
ROWS = (1436, 3390, 4610)
def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()

def main():
    from app.context_budget import ContextBudget
    with sqlite3.connect('/var/lib/finkoper-ai/cabinet/cabinet.sqlite3') as db:
        snap_raw, status = db.execute('SELECT snapshot,status FROM jobs WHERE id=?', (JOB,)).fetchone()
        snap = json.loads(snap_raw)
        rows = db.execute('SELECT row_num,text,status,result FROM rows WHERE job_id=? AND row_num IN (?,?,?) ORDER BY row_num', (JOB,*ROWS)).fetchall()
        all_rows = db.execute('SELECT row_num,text,result FROM rows WHERE job_id=? ORDER BY row_num', (JOB,)).fetchall()
        active = db.execute("SELECT count(*) FROM jobs WHERE status IN ('preparing','warming','running','stopping','exporting')").fetchone()[0]
    assert not active, 'Avoid diagnostic inference during an active job'
    tokenizer_path = '/var/lib/finkoper-ai/tokenizers/qwen3-8b/tokenizer.json'
    hf = Tokenizer.from_file(tokenizer_path)
    hf.no_truncation()
    hf.no_padding()
    raw_hf = Tokenizer.from_file(tokenizer_path)
    raw_hf.normalizer = None
    report = {'source_job': JOB, 'job_status': status, 'parameters': snap['parameters'], 'snapshot_sha256': sha(snap_raw),
              'hf_normalizer': json.loads(Path(tokenizer_path).read_text())['normalizer'], 'rows': []}
    with httpx.Client(base_url='http://127.0.0.1:11434', timeout=180) as client:
        for number,text,row_status,result_raw in rows:
            saved = json.loads(result_raw)
            system = snap['prompts']['system']
            user = snap['prompts']['template'].replace('{text}',text)
            params = snap['parameters']
            payload = {'model': snap['model']['name'], 'stream': False, 'think': params['thinking'], 'keep_alive': snap['keep_alive'],
                       'truncate': False, 'shift': False,
                       'messages': [{'role':'system','content':system},{'role':'user','content':user}],
                       'options': {k: params[k] for k in ('num_ctx','num_predict','temperature')}}
            response = client.post('/api/chat', json={**payload, '_debug_render_only': True})
            response.raise_for_status()
            rendered = response.json()['_debug_info']['rendered_template']
            local = ContextBudget.render(system,user,params['thinking'])
            processes = subprocess.check_output(['ps','-eo','args'], text=True)
            ports = re.findall(r'llama-server .*?--port\s+(\d+)', processes)
            runner = None
            runner_ids_match_raw_hf = None
            if ports:
                r = httpx.post(f'http://127.0.0.1:{ports[0]}/tokenize', json={'content':rendered,'add_special':True,'parse_special':True},timeout=10)
                if r.status_code == 200:
                    runner = len(r.json()['tokens'])
                    runner_ids_match_raw_hf = r.json()['tokens'] == raw_hf.encode(rendered,add_special_tokens=False).ids
            counts = {form: len(hf.encode(unicodedata.normalize(form,rendered),add_special_tokens=False).ids)
                      for form in ('NFC','NFD','NFKC','NFKD')}
            item = {'row':number,'saved_status':row_status,'saved_local_tokens':saved.get('context_budget',{}).get('input_tokens'),
                    'saved_ollama_tokens':saved.get('prompt_tokens'), 'saved_done_reason':saved.get('raw',{}).get('done_reason'),
                    'template_matches':local==rendered,'rendered_sha256':sha(rendered),'runner_tokens':runner,
                    'runner_token_ids_match_unnormalized_hf':runner_ids_match_raw_hf,
                    'http_json_roundtrip_preserves_messages':json.loads(client.build_request('POST','/api/chat',json=payload).content)['messages']==payload['messages'],
                    'hf_tokens':len(hf.encode(rendered,add_special_tokens=False).ids),
                    'hf_without_normalizer':len(raw_hf.encode(rendered,add_special_tokens=False).ids),'normalized_counts':counts,
                    'text_nfc_unchanged':unicodedata.normalize('NFC',text)==text,
                    'text_nfd_unchanged':unicodedata.normalize('NFD',text)==text,
                    'combining_codepoints':sorted({f'U+{ord(c):04X} {unicodedata.name(c,"UNKNOWN")}' for c in text if unicodedata.combining(c)})}
            report['rows'].append(item)
    compared, differences = 0, []
    for number, text, result_raw in all_rows:
        result = json.loads(result_raw)
        actual = result.get('prompt_tokens')
        if not isinstance(actual, int) or actual <= 0:
            continue
        rendered = ContextBudget.render(snap['prompts']['system'], snap['prompts']['template'].replace('{text}',text), snap['parameters']['thinking'])
        count = len(raw_hf.encode(rendered,add_special_tokens=False).ids)
        compared += 1
        if count != actual:
            differences.append({'row':number,'local':count,'ollama':actual})
    report['full_source_comparison'] = {'compared':compared,'differences':differences}
    Path('/tmp/token-mismatch-diagnosis.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))

if __name__ == '__main__':
    main()
