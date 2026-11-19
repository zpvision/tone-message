"""Contract, cancellation and bounded-load tests. Synthetic inference/tokenizer."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import httpx

import os
os.environ["FINKOPER_MODEL_ID"]="synthetic:1"
os.environ["FINKOPER_INFERENCE_URL"]="http://inference.test"
os.environ["FINKOPER_INFERENCE_LOCK_DIR"]=tempfile.mkdtemp(prefix="finkoper-test-locks-")
from app.context_budget import ContextBudget
def synthetic_count(self,system,user,params):
    n=len((system+user).encode())//3
    reserve=params["num_predict"]
    return {"input_tokens":n,"output_reserve":reserve,"context_tokens":params["num_ctx"],
            "required_tokens":n+reserve,"remaining_tokens":params["num_ctx"]-n-reserve,
            "method":"synthetic_fixture"}
ContextBudget.count=synthetic_count

from app.batch_api import LABEL_SCHEMA
from app.config import Settings
from app.load_control import Busy, LoadControl, QueueTimeout
from app.main import create_app


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.settings=Settings(cabinet_enabled=True,api_enabled=True,
            cabinet_data_dir=self.temp.name+'/cabinet',api_data_dir=self.temp.name+'/api',
            inference_url='http://inference.test',api_inference_timeout=.2)
        self.app=create_app(self.settings)
        self.client=TestClient(self.app,base_url='https://testserver')
        self.client.__enter__()
        self.key=self.app.state.api_keys.create('contract-test')
        self.headers={'Authorization':'Bearer '+self.key['api_key']}
        self.service=self.app.state.batch_service
        self.engine=self.app.state.cabinet_engine
        self.engine.describe_model=AsyncMock(return_value={})
        self.engine.context_budget.verify=AsyncMock()
        self.calls=[]
        self.mode='ok'
        async def upstream(request):
            payload=json.loads(request.content)
            self.calls.append(payload)
            self.assertEqual(payload['format'],LABEL_SCHEMA)
            self.assertFalse(payload['truncate'])
            self.assertFalse(payload['shift'])
            self.assertFalse(payload['think'])
            self.assertEqual(payload['options'],{'temperature':0,'num_ctx':4096,'num_predict':128})
            if self.mode=='timeout':
                await asyncio.sleep(1)
            if self.mode=='503':
                return httpx.Response(503,json={'error':'synthetic'})
            if self.mode=='second_fail' and 'SECOND' in payload['messages'][1]['content']:
                return httpx.Response(502,json={'error':'synthetic'})
            if self.mode=='network_once' and len(self.calls)==1:
                raise httpx.ConnectError('synthetic',request=request)
            label='positive'
            count=self.engine.context_budget.count(payload['messages'][0]['content'],payload['messages'][1]['content'],
                                                   {'thinking':False,'num_predict':128,'num_ctx':4096})['input_tokens']
            content=json.dumps({'label':label})
            if self.mode=='bad_schema':
                content='{"label":"neutral","extra":true}'
            if self.mode=='bad_json':
                content='not JSON'
            return httpx.Response(200,json={'model':'synthetic:wrong' if self.mode=='wrong_model' else payload['model'],'done':True,'done_reason':'length' if self.mode=='length' else 'stop',
                'message':{'content':content},'prompt_eval_count':count+int(self.mode=='mismatch'),'eval_count':6})
        self.service.transport=httpx.MockTransport(upstream)

    def tearDown(self):
        self.client.__exit__(None,None,None)
        self.temp.cleanup()

    def payload(self,items=None,system=None):
        messages=[] if system is None else [{'role':'system','content':system}]
        messages.append({'role':'user','content':json.dumps(items if items is not None else [{'item':7,'text':'Спасибо!'}],ensure_ascii=False)})
        return {'model':'qwen3:8b','messages':messages}

    def post(self,body=None,path='/api/v1/chat/completions',**kwargs):
        return self.client.post(path,json=body if body is not None else self.payload(),headers=self.headers,**kwargs)

    def test_contract_order_usage_alias_and_preset(self):
        for path in ('/api/v1/chat/completions','/v1/chat/completions'):
            response=self.post(self.payload([{'item':91,'text':'А'},{'item':7,'text':'Б'}]),path)
            self.assertEqual(response.status_code,200,response.text)
            body=response.json()
            self.assertIsInstance(body['choices'][0]['message']['content'],str)
            self.assertEqual(json.loads(body['choices'][0]['message']['content']),[{'item':91,'tone':2},{'item':7,'tone':2}])
            self.assertEqual(body['usage']['completion_tokens'],12)
            self.assertEqual(body['usage']['total_tokens'],body['usage']['prompt_tokens']+12)
            self.assertEqual(body['model'],'qwen3:8b')
        self.assertTrue(self.calls[0]['messages'][0]['content'].startswith(self.service.preset['system']))
        self.assertGreater(len(self.service.preset['system']),3000)
        self.assertEqual(self.calls[0]['messages'][1]['content'],self.service.preset['user_template'].replace('{text}','А'))

    def test_exact_model_id_accepted_without_changing_inference(self):
        body=self.payload()
        body["model"]="synthetic:1"
        response=self.post(body)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()["model"],"synthetic:1")
        self.assertEqual(self.calls[-1]["model"],"synthetic:1")

    def test_parallel_results_retain_input_ids(self):
        from app.inference import GpuGate
        self.settings.inference_concurrency=2
        self.settings.sentiment_workers=2
        self.engine.inference.gate=GpuGate(self.settings)
        self.engine.load_control.gpu=asyncio.Semaphore(2)
        original=self.service.transport
        async def delayed(request):
            payload=json.loads(request.content)
            if "FIRST" in payload["messages"][1]["content"]:
                await asyncio.sleep(.02)
            return await original.handle_async_request(request)
        self.service.transport=httpx.MockTransport(delayed)
        response=self.post(self.payload([{"item":91,"text":"FIRST"},{"item":7,"text":"SECOND"}]))
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual([v["item"] for v in json.loads(response.json()["choices"][0]["message"]["content"])],[91,7])

    def test_explicit_system_and_extra_headers(self):
        body=self.payload(system='Выбирай positive во всех случаях.')
        response=self.client.post('/v1/chat/completions',json=body,headers={**self.headers,'X-SDK-Client':'example'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue(self.calls[0]['messages'][0]['content'].startswith(body['messages'][0]['content']))
        self.assertFalse(self.calls[0]['messages'][0]['content'].startswith(self.service.preset['system']))

    def test_empty_no_gpu_and_blank_text(self):
        for data in ([],[{'item':4,'text':''},{'item':19,'text':' \n\t'}]):
            response=self.post(self.payload(data))
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['usage'],{'prompt_tokens':0,'completion_tokens':0,'total_tokens':0})
            self.assertEqual(json.loads(response.json()['choices'][0]['message']['content']),[{'item':v['item'],'tone':1} for v in data])
        self.assertEqual(self.calls,[])

    def test_invalid_auth_revocation_and_metadata(self):
        for headers in ({},{'Authorization':'Bearer invalid'},{'Authorization':'Basic abc'}):
            r=self.client.post('/api/v1/chat/completions',json=self.payload(),headers=headers)
            self.assertEqual(r.status_code,401)
            self.assertIn('request_id',r.json()['error'])
        self.app.state.api_keys.revoke(self.key['id'])
        self.assertEqual(self.post().status_code,401)
        self.assertNotIn('digest',self.app.state.api_keys.list()[0])
        self.assertNotIn('api_key',self.app.state.api_keys.list()[0])
        self.assertNotIn(self.key['api_key'],self.app.state.api_keys.path.read_bytes().decode('latin1'))
        self.assertEqual(self.calls,[])

    def test_validation_all_before_inference(self):
        cases=[([{'item':1,'text':'first'},{'item':1,'text':'duplicate'}],'duplicate_item'),
               ([{'item':True,'text':'x'}],'invalid_item'),([{'item':1.0,'text':'x'}],'invalid_item'),
               ([{'item':0,'text':'x'}],'invalid_item'),([{'item':1,'text':4}],'invalid_item'),
               ([{'item':i+1,'text':''} for i in range(1001)],'batch_too_large'),
               ([{'item':1,'text':'first'},{'item':42,'text':'и\u0306 '*5000}],'context_budget_exceeded')]
        for data,code in cases:
            r=self.post(self.payload(data))
            self.assertEqual(r.status_code,422,r.text)
            self.assertEqual(r.json()['error']['code'],code)
        self.assertEqual(self.calls,[])

    def test_invalid_envelopes_and_json(self):
        cases=[]
        for field,value in (('model','GigaChat'),('stream',True),('stream',0),('max_tokens',512),('temperature',0),('top_p',1),('repetition_penalty',1),('update_interval',0)):
            cases.append({**self.payload(),field:value})
        cases.extend([{'model':'qwen3:8b','messages':[{'role':'assistant','content':'x'}]},
                      {'model':'qwen3:8b','messages':[{'role':'user','content':[]}]},
                      {'model':'qwen3:8b','messages':[{'role':'user','content':'{}'}]}])
        for body in cases:
            self.assertEqual(self.post(body).status_code,422)
        for content in ('{','{"model":"x","model":"y"}',b'\xff'):
            r=self.client.post('/api/v1/chat/completions',content=content,headers={**self.headers,'Content-Type':'application/json'})
            self.assertEqual(r.status_code,400,r.text)
        r=self.client.post('/api/v1/chat/completions',content='x'*(5*1024*1024+1),headers={**self.headers,'Content-Type':'application/json'})
        self.assertEqual(r.status_code,413)
        self.assertEqual(self.calls,[])

    def test_failures_never_partial_or_neutral(self):
        for mode,status in (('503',503),('wrong_model',502),('bad_schema',502),('bad_json',502),('length',502),('timeout',504)):
            self.mode=mode
            r=self.post()
            self.assertEqual(r.status_code,status,r.text)
            self.assertNotIn('choices',r.json())
            self.assertIn('request_id',r.json()['error'])
            self.assertFalse(self.engine.load_control.api_pending)

    def test_counter_warning_is_success(self):
        self.mode='mismatch'
        response=self.post()
        self.assertEqual(response.status_code,200,response.text)

    def test_late_failure_does_not_return_partial_array(self):
        self.mode='second_fail'
        response=self.post(self.payload([{'item':1,'text':'FIRST'},{'item':2,'text':'SECOND'}]))
        self.assertEqual(response.status_code,502)
        self.assertEqual(len(self.calls),2)
        self.assertNotIn('choices',response.json())

    def test_network_error_has_one_retry(self):
        self.mode='network_once'
        self.assertEqual(self.post().status_code,200)
        self.assertEqual(len(self.calls),2)

    def test_incomplete_assembled_result_is_not_success(self):
        self.service.process=AsyncMock(return_value=None)
        response=self.post()
        self.assertEqual(response.status_code,502)
        self.assertEqual(response.json()['error']['code'],'incomplete_batch_result')

    def test_http_cannot_compute_and_csrf_preserved(self):
        r=self.client.post('http://testserver/api/v1/chat/completions',json=self.payload(),headers=self.headers)
        self.assertEqual(r.status_code,426)
        self.assertEqual(self.client.post('/api/prompts',json={}).status_code,403)
        self.assertEqual(self.calls,[])

    def test_deadline_and_busy(self):
        self.mode='timeout'
        self.service.settings.api_deadline=.02
        self.assertEqual(self.post().status_code,504)
        self.assertFalse(self.engine.load_control.api_pending)
        self.service.settings.api_deadline=600
        self.engine.load_control.cabinet_busy=lambda: True
        self.assertEqual(self.post().status_code,429)


class LoadTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_and_cabinet_share_gpu_and_exclude_jobs(self):
        with tempfile.TemporaryDirectory() as root:
            settings=Settings(cabinet_enabled=True,api_enabled=True,cabinet_data_dir=root+'/cab',api_data_dir=root+'/api')
            app=create_app(settings)
            async with app.router.lifespan_context(app):
                engine=app.state.cabinet_engine
                service=app.state.batch_service
                self.assertIs(engine.load_control.gpu,service.control.gpu)
                engine.describe_model=AsyncMock(return_value={})
                async with service.control.slot(1,1):
                    with self.assertRaisesRegex(ValueError,'API'):
                        await engine.start(None,None)
                engine.store.add_job('a'*32,{'selection':{'upload_id':'b'*32}})
                with self.assertRaises(Busy):
                    async with service.control.slot(1,1):
                        pass
                engine.store.set_job('a'*32,status='completed')
                concurrent=0
                maximum=0
                async def inference():
                    nonlocal concurrent,maximum
                    async with engine.load_control.gpu:
                        concurrent+=1
                        maximum=max(maximum,concurrent)
                        await asyncio.sleep(.01)
                        concurrent-=1
                await asyncio.gather(*(inference() for _ in range(4)))
                self.assertEqual(maximum,1)

    async def test_http_queue_overflow_and_wait_timeout(self):
        with tempfile.TemporaryDirectory() as root:
            settings=Settings(cabinet_enabled=True,api_enabled=True,cabinet_data_dir=root+'/cab',api_data_dir=root+'/api',
                              api_max_waiting_batches=1,api_queue_timeout=.1)
            app=create_app(settings)
            async with app.router.lifespan_context(app):
                key=app.state.api_keys.create('queue')
                service=app.state.batch_service
                started=asyncio.Event()
                release=asyncio.Event()
                async def slow_process(system,prepared,results,stats):
                    started.set()
                    await release.wait()
                    results.extend({'item':item.item,'tone':1} for item,_,_ in prepared)
                service.process=slow_process
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='https://testserver') as client:
                    body={'model':'qwen3:8b','messages':[{'role':'user','content':'[{"item":1,"text":"x"}]'}]}
                    headers={'Authorization':'Bearer '+key['api_key']}
                    async def request():
                        return await client.post('/v1/chat/completions',json=body,headers=headers)
                    active=asyncio.create_task(request())
                    await started.wait()
                    waiting=asyncio.create_task(request())
                    for _ in range(100):
                        if service.control.waiting:
                            break
                        await asyncio.sleep(.001)
                    overflow=await request()
                    self.assertEqual(overflow.status_code,429)
                    timed_out=await waiting
                    self.assertEqual(timed_out.status_code,429)
                    self.assertEqual(timed_out.json()['error']['code'],'queue_timeout')
                    self.assertEqual(timed_out.headers['retry-after'],'5')
                    release.set()
                    self.assertEqual((await active).status_code,200)
                    self.assertFalse(service.control.api_pending)

    async def test_fifo_limits_timeout_and_cancellation_cleanup(self):
        gate=LoadControl(max_waiting=1,max_messages=2)
        entered=asyncio.Event()
        release=asyncio.Event()
        async def first():
            async with gate.slot(1,1):
                entered.set()
                await release.wait()
        task=asyncio.create_task(first())
        await entered.wait()
        async def waiting():
            async with gate.slot(2,.02):
                pass
        queued=asyncio.create_task(waiting())
        await asyncio.sleep(0)
        with self.assertRaises(Busy):
            async with gate.slot(1,1):
                pass
        with self.assertRaises(QueueTimeout):
            await queued
        self.assertEqual(len(gate.waiting),0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(gate.api_pending)
        async with gate.slot(1,1):
            self.assertTrue(gate.api_pending)
        self.assertFalse(gate.api_pending)

    async def test_disconnect_cancels_request_and_releases_slot(self):
        with tempfile.TemporaryDirectory() as root:
            settings=Settings(cabinet_enabled=True,api_enabled=True,cabinet_data_dir=root+'/cab',api_data_dir=root+'/api')
            app=create_app(settings)
            async with app.router.lifespan_context(app):
                key=app.state.api_keys.create('disconnect')
                started=asyncio.Event()
                cancelled=asyncio.Event()
                async def process(system,prepared,results,stats):
                    started.set()
                    try:
                        await asyncio.sleep(60)
                    finally:
                        cancelled.set()
                app.state.batch_service.process=process
                body=json.dumps({'model':'qwen3:8b','messages':[{'role':'user','content':'[{"item":1,"text":"x"}]'}]}).encode()
                first=True
                async def receive():
                    nonlocal first
                    if first:
                        first=False
                        return {'type':'http.request','body':body,'more_body':False}
                    await started.wait()
                    return {'type':'http.disconnect'}
                sent=[]
                async def send(message):
                    sent.append(message)
                scope={'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':'POST','scheme':'https',
                       'path':'/api/v1/chat/completions','raw_path':b'/api/v1/chat/completions','query_string':b'',
                       'root_path':'','server':('testserver',443),'client':('127.0.0.1',1000),
                       'headers':[(b'host',b'testserver'),(b'content-type',b'application/json'),(b'authorization',('Bearer '+key['api_key']).encode())]}
                await asyncio.wait_for(app(scope,receive,send),5)
                self.assertTrue(cancelled.is_set())
                self.assertFalse(app.state.cabinet_engine.load_control.api_pending)


if __name__=='__main__':
    unittest.main(verbosity=2)
