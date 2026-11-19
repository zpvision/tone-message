"""Recheck only three original Excel rows, without editing the source job.

Private maintenance helper, not a public API. The new job records the explicit
verification subset, preserves Excel row numbers and uses the real worker.
"""
import argparse
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import time
import uuid

import httpx
from openpyxl import load_workbook

from app.cabinet_engine import Engine, metrics
from app.cabinet_store import Store
from app.cabinet_xlsx import export_result
from app.config import Settings

SOURCE = '2435a5e62d9243b09eec55eb55d6c712'
NUMBERS = (1436,3390,4610)

def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()

def fingerprint(store):
    records = {'job':store.job(SOURCE)}
    for table, order in (('rows','row_num'),('attempts','row_num,number'),('samples','elapsed')):
        records[table] = store.all(f'SELECT * FROM {table} WHERE job_id=? ORDER BY {order}', (SOURCE,))
    records['files'] = {p.name:hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in (store.root/'jobs'/SOURCE).iterdir() if p.is_file()}
    return digest(records)

async def check(store, live, source, staged):
    engine = Engine(store, Settings(inference_url='http://127.0.0.1:11434'))
    model = await engine.describe_model()
    assert model == source['snapshot']['model'], 'Model/runtime metadata changed'
    snapshot = copy.deepcopy(source['snapshot'])
    snapshot['verification'] = {'source_job':SOURCE,'excel_rows':list(NUMBERS),'scope':'only_these_rows'}
    snapshot['policy']['context_budget'] = engine.context_budget.metadata(model)
    new_id = uuid.uuid4().hex
    store.add_job(new_id,snapshot)
    for number in NUMBERS:
        row = live.one('SELECT text FROM rows WHERE job_id=? AND row_num=?',(SOURCE,number))
        assert row is not None
        store.execute('INSERT INTO rows(job_id,row_num,text) VALUES(?,?,?)',(new_id,number,row['text']))
    sent = []
    async def on_request(request):
        if request.url.path == '/api/chat':
            payload = json.loads(request.content)
            if not payload.get('_debug_render_only'):
                sent.append({'messages_sha256':digest(payload['messages']), 'options':payload['options'],
                             'think':payload['think'],'keep_alive':payload['keep_alive']})
                assert payload['truncate'] is False and payload['shift'] is False
    try:
        store.set_job(new_id,status='running')
        params = snapshot['parameters']
        async with httpx.AsyncClient(base_url=engine.settings.inference_url,timeout=params['timeout'],trust_env=False,
                                    event_hooks={'request':[on_request]}) as client:
            await engine.context_budget.verify(client, model, params['num_ctx'])
            started = time.monotonic()
            await asyncio.gather(*(engine.worker(new_id,snapshot,client) for _ in range(params['concurrency'])))
            model_seconds = time.monotonic()-started
        rows = store.all('SELECT * FROM rows WHERE job_id=? ORDER BY row_num',(new_id,))
        summaries = []
        for index,row in enumerate(rows):
            result = json.loads(row['result'])
            assert row['status']=='ok', {'row':row['row_num'],'status':row['status'],'error':result.get('error')}
            assert row['attempts']==1 and not result.get('warnings')
            assert result['prompt_tokens']==result['context_budget']['input_tokens']
            messages = [{'role':'system','content':snapshot['prompts']['system']},
                        {'role':'user','content':snapshot['prompts']['template'].replace('{text}',row['text'])}]
            assert sent[index]['messages_sha256']==digest(messages)
            assert sent[index]['options']=={key:params[key] for key in ('temperature','num_ctx','num_predict')}
            assert sent[index]['think']==params['thinking'] and sent[index]['keep_alive']==snapshot['keep_alive']
            summaries.append({'excel_row':row['row_num'],'status':row['status'],'label':result['label'],
                              'local_tokens':result['context_budget']['input_tokens'],'ollama_tokens':result['prompt_tokens'],
                              'required_tokens':result['context_budget']['required_tokens'],'warnings':result.get('warnings',[]),
                              'done_reason':result['raw']['done_reason'],'sent_messages_sha256':sent[index]['messages_sha256']})
        store.set_job(new_id,status='exporting',timing={'model_seconds':model_seconds})
        started = time.monotonic()
        await asyncio.to_thread(export_result,store,new_id,metrics(store,new_id))
        store.set_job(new_id,status='completed',export_ready=1,timing={'model_seconds':model_seconds,'export_seconds':time.monotonic()-started})
        book = load_workbook(store.root/'jobs'/new_id/'result.xlsx')
        sheet = book[snapshot['selection']['sheet']]
        headers = [cell.value for cell in sheet[1]]
        for number in NUMBERS:
            assert sheet.cell(number,headers.index('ai_status')+1).value=='ok'
        book.close()
        return {'mode':'staged' if staged else 'deployed','job_id':None if staged else new_id,
                'source_job':SOURCE,'rows':summaries,'parameters':params,'model_digest':model['digest'],
                'gpu':Engine.resources(),'xlsx_statuses_verified':True}
    except BaseException:
        store.set_job(new_id,status='interrupted',error='verification_did_not_complete')
        raise

async def main(staged):
    live = Store('/var/lib/finkoper-ai/cabinet')
    assert not live.active(), 'Existing job active'
    before = fingerprint(live)
    prompts = digest(live.all('SELECT * FROM prompts ORDER BY id'))
    source = live.job(SOURCE)
    if staged:
        with tempfile.TemporaryDirectory(prefix='unicode-recheck-',dir=live.root) as directory:
            store = Store(directory)
            upload = live.one('SELECT * FROM uploads WHERE id=?',(source['upload_id'],))
            shutil.copyfile(live.root/'uploads'/(upload['id']+'.xlsx'),store.root/'uploads'/(upload['id']+'.xlsx'))
            store.execute('INSERT INTO uploads(id,name,created,size,meta) VALUES(?,?,?,?,?)',tuple(upload[k] for k in ('id','name','created','size','meta')))
            report = await check(store,live,source,True)
    else:
        report = await check(live,live,source,False)
    assert fingerprint(live)==before, 'Original job changed'
    assert digest(live.all('SELECT * FROM prompts ORDER BY id'))==prompts, 'Prompt versions changed'
    report.update(source_job_unchanged=True,saved_prompts_unchanged=True,source_job_fingerprint=before)
    Path(f'/tmp/unicode-recheck-{report["mode"]}.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--staged',action='store_true')
    asyncio.run(main(parser.parse_args().staged))
