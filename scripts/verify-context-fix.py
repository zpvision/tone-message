"""Small real-GPU regression with saved prompts; never modifies prompt versions.

Run from staged source with --staged before deployment; then without the flag
against the actual backend. Customer content stays on the server.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import time

import httpx
from openpyxl import load_workbook

from app.cabinet_engine import Engine, metrics
from app.cabinet_models import JobRequest
from app.cabinet_store import Store
from app.config import Settings


async def main(staged):
    live = Store('/var/lib/finkoper-ai/cabinet')
    assert not live.active(), 'An existing job is active; do not run GPU diagnostics concurrently'
    before = live.all('SELECT * FROM prompts ORDER BY id')
    current = json.loads(before[-1]['body'])
    source = live.job('0c3fedd881204f98aa2e15f876d7e146')
    selection = source['snapshot']['selection']
    request = JobRequest(**{k: selection[k] for k in ('upload_id', 'sheet', 'column', 'headers')},
                         limit=3, prompts=current, parameters=source['snapshot']['parameters'])
    assert request.parameters.num_ctx == 4096
    report = {'mode': 'staged' if staged else 'deployed', 'context': 4096,
              'saved_prompts_sha256': hashlib.sha256(before[-1]['body'].encode()).hexdigest()}
    with tempfile.TemporaryDirectory(prefix='context-regression-', dir=live.root) as directory:
        if staged:
            store = Store(directory)
            upload = live.one('SELECT * FROM uploads WHERE id=?', (selection['upload_id'],))
            shutil.copyfile(live.root / 'uploads' / (upload['id'] + '.xlsx'),
                            store.root / 'uploads' / (upload['id'] + '.xlsx'))
            store.execute('INSERT INTO uploads(id,name,created,size,meta) VALUES(?,?,?,?,?)',
                          tuple(upload[k] for k in ('id', 'name', 'created', 'size', 'meta')))
            engine = Engine(store, Settings(inference_url='http://127.0.0.1:11434'))
            job_id = await engine.start(request, upload)
            await engine.task
        else:
            store = live
            async with httpx.AsyncClient(base_url='http://127.0.0.1:8000', timeout=10) as client:
                response = await client.post('/api/jobs', json=request.model_dump(), headers={'X-Cabinet-Request': '1'})
                response.raise_for_status()
                job_id = response.json()['id']
                for _ in range(180):
                    if store.job(job_id)['status'] not in ('preparing','warming','running','stopping','exporting'):
                        break
                    await asyncio.sleep(1)
        job = store.job(job_id)
        assert job['status'] == 'completed', {'status': job['status'], 'error': job['error']}
        assert job['snapshot']['prompts'] == current
        rows = store.all('SELECT row_num,status,result FROM rows WHERE job_id=? ORDER BY row_num', (job_id,))
        assert len(rows) == 3
        summaries = []
        for row in rows:
            result = json.loads(row['result'])
            assert row['status'] == 'ok', {'row': row['row_num'], 'status': row['status'], 'error': result.get('error')}
            assert result['context_budget']['input_tokens'] == result['prompt_tokens']
            summaries.append({'row': row['row_num'], 'status': row['status'], 'label': result['label'],
                              'input_tokens': result['prompt_tokens'], 'output_tokens': result['output_tokens'],
                              'required_tokens': result['context_budget']['required_tokens']})
        book = load_workbook(store.root / 'jobs' / job_id / 'result.xlsx')
        sheet = book[selection['sheet']]
        headers = [cell.value for cell in sheet[1]]
        assert [sheet.cell(i, headers.index('ai_status') + 1).value for i in (2,3,4)] == ['ok'] * 3
        book.close()
        report.update(job_id=job_id if not staged else None, rows=summaries, metrics=metrics(store, job_id),
                      gpu=Engine.resources())
    assert live.all('SELECT * FROM prompts ORDER BY id') == before, 'Saved prompt history was modified'
    report['saved_prompts_unchanged'] = True
    Path(f'/tmp/context-fix-{report["mode"]}.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--staged', action='store_true')
    asyncio.run(main(parser.parse_args().staged))
