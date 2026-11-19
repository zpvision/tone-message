"""Real HTTPS acceptance using a temporary key (revoked in finally).

No secrets or customer text written to diagnostics. Runs on the GPU server.
"""
import hashlib
import json
from pathlib import Path
import runpy
import socket
import ssl
import time

import httpx

from app.api_keys import KeyStore
from app.cabinet_store import Store
from app.config import Settings

settings=Settings()
keys=KeyStore(settings.api_data_dir)
temporary=keys.create('HTTPS acceptance temporary')
store=Store(settings.cabinet_data_dir)
assert not store.active(), 'Do not compete with an active XLSX job'
fingerprint=runpy.run_path('/opt/finkoper-ai/scripts/recheck-unicode-rows.py')['fingerprint']
before=fingerprint(store)
report={'checks':[]}
headers={'Authorization':'Bearer '+temporary['api_key']}

def payload(items,system=None):
    messages=[] if system is None else [{'role':'system','content':system}]
    messages.append({'role':'user','content':json.dumps(items,ensure_ascii=False)})
    return {'model':'qwen3:8b','stream':False,'messages':messages}

def assert_response(response,status,code=None):
    assert response.status_code==status, {'expected':status,'actual':response.status_code}
    if code:
        assert response.json()['error']['code']==code
        assert response.json()['error']['request_id']
    report['checks'].append({'status':status,'code':code,'request_id':response.headers.get('x-request-id')})

try:
    with httpx.Client(base_url='https://ai.example.com',timeout=120,trust_env=False) as client:
        data=[{'item':1,'text':'Спасибо большое! Всё отлично работает.'},
              {'item':2,'text':'Когда будет готов отчёт?'},
              {'item':3,'text':'Ужасный сервис, проблема до сих пор не решена!'}]
        body=payload(data)
        r=client.post('/api/v1/chat/completions',json=body,headers=headers)
        assert_response(r,200)
        report['real_response']=r.json()
        assert isinstance(r.json()['choices'][0]['message']['content'],str)
        assert json.loads(r.json()['choices'][0]['message']['content'])==[{'item':1,'tone':2},{'item':2,'tone':1},{'item':3,'tone':0}]
        r=client.post('/v1/chat/completions',json=payload([{'item':91,'text':'Спасибо большое! Всё отлично работает.'},{'item':7,'text':'Когда будет готов отчёт?'}]),headers=headers)
        assert_response(r,200)
        assert [x['item'] for x in json.loads(r.json()['choices'][0]['message']['content'])]==[91,7]
        for value in ({},{'Authorization':'Bearer invalid'},{'Authorization':'Basic invalid'}):
            assert_response(client.post('/v1/chat/completions',json=payload([]),headers=value),401,'invalid_api_key')
        for items in ([],[{'item':9,'text':' \t'},{'item':2,'text':''}]):
            r=client.post('/v1/chat/completions',json=payload(items),headers=headers)
            assert_response(r,200)
            assert r.json()['usage']['total_tokens']==0
        for items,code in (([{'item':1,'text':'a'},{'item':1,'text':'b'}],'duplicate_item'),
                           ([{'item':True,'text':'a'}],'invalid_item'),
                           ([{'item':1,'text':0}],'invalid_item'),
                           ([{'item':i+1,'text':''} for i in range(1001)],'batch_too_large'),
                           ([{'item':1,'text':'first'},{'item':19,'text':'и\u0306 '*5000}],'context_budget_exceeded')):
            assert_response(client.post('/v1/chat/completions',json=payload(items),headers=headers),422,code)
        assert_response(client.post('/v1/chat/completions',content=b'{',headers={**headers,'Content-Type':'application/json'}),400,'invalid_json')
        assert_response(client.post('/v1/chat/completions',content=b'x'*(5*1024*1024+1),headers={**headers,'Content-Type':'application/json'}),413,'body_too_large')
        custom='Тестовые правила классификации: фраза «контроль передачи system» относится к positive. Для неё возвращай только {"label":"positive"}.'
        r=client.post('/v1/chat/completions',json=payload([{'item':7,'text':'контроль передачи system'}],custom),headers=headers)
        assert_response(r,200)
        report['explicit_system_result']=json.loads(r.json()['choices'][0]['message']['content'])
        print('explicit_system_result',json.dumps(report['explicit_system_result']))
        assert report['explicit_system_result']==[{'item':7,'tone':2}]
        r=client.post('/v1/chat/completions',json=payload([{'item':7,'text':'контроль передачи system'}]),headers=headers)
        assert_response(r,200)
        report['same_text_default_preset_result']=json.loads(r.json()['choices'][0]['message']['content'])
        assert report['same_text_default_preset_result']==[{'item':7,'tone':1}]
        # Three exact original customer messages remain on the server.
        original=[]
        for number in (1436,3390,4610):
            row=store.one('SELECT text FROM rows WHERE job_id=? AND row_num=?',('2435a5e62d9243b09eec55eb55d6c712',number))
            original.append({'item':number,'text':row['text']})
        r=client.post('/v1/chat/completions',json=payload(original),headers=headers)
        assert_response(r,200)
        report['unicode_rows_response']=r.json()
        assert [x['item'] for x in json.loads(r.json()['choices'][0]['message']['content'])]==[1436,3390,4610]
        report['unicode_request_id']=r.headers['x-request-id']
        for path in ('/','/api/config','/api/jobs','/healthz','/readyz','/static/cabinet.js'):
            assert client.get(path).status_code==401
        assert client.get('/api/jobs/2435a5e62d9243b09eec55eb55d6c712/download/xlsx').status_code==401
        assert_response(httpx.post('http://ai.example.com/v1/chat/completions',json=payload([]),timeout=10),426,'https_required')
        assert httpx.get('http://ai.example.com/',timeout=10,follow_redirects=False).status_code==308
        keys.revoke(temporary['id'])
        assert_response(client.post('/v1/chat/completions',json=payload([]),headers=headers),401,'invalid_api_key')
        keys.enable(temporary['id'])
        assert_response(client.post('/v1/chat/completions',json=payload([]),headers=headers),200)
    context=ssl.create_default_context()
    with socket.create_connection(('ai.example.com',443),timeout=10) as sock:
        with context.wrap_socket(sock,server_hostname='ai.example.com') as tls:
            cert=tls.getpeercert()
            report['tls']={'protocol':tls.version(),'issuer':cert['issuer'],'not_after':cert['notAfter'],'san':cert['subjectAltName']}
    assert fingerprint(store)==before
    report['source_job_unchanged']=True
    Path('/tmp/api-https-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False))
finally:
    keys.revoke(temporary['id'])
