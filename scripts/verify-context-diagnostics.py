"""Real HTTPS checks; temporary bearer revoked in finally. No customer input."""
import json,time
from pathlib import Path
import httpx
from app.api_keys import KeyStore
from app.cabinet_store import Store
from app.context_budget import ContextBudget
from app.config import Settings
from app.batch_api import TECHNICAL
settings=Settings()
assert not Store(settings.cabinet_data_dir).active(), 'Active XLSX'
counter=ContextBudget(settings.context_tokenizer_dir)
preset=json.loads(Path('/opt/finkoper-ai/app/presets/sentiment-v1.json').read_text())
params={'num_ctx':settings.api_num_ctx,'num_predict':128,'thinking':False}
keys=KeyStore(settings.api_data_dir)
key=keys.create('context diagnostics acceptance temporary')
report={'checks':[],'context':settings.api_num_ctx}
def payload(items,system=None):
 messages=[] if system is None else [{'role':'system','content':system}]
 messages.append({'role':'user','content':json.dumps(items,ensure_ascii=False)})
 return {'model':'qwen3:8b','messages':messages}
def budget(text,system=None):
 return counter.count((preset['system'] if system is None else system)+'\n\n'+TECHNICAL,preset['user_template'].replace('{text}',text),params)
positive='\u0421\u043f\u0430\u0441\u0438\u0431\u043e \u0431\u043e\u043b\u044c\u0448\u043e\u0435! \u0412\u0441\u0451 \u043e\u0442\u043b\u0438\u0447\u043d\u043e \u0440\u0430\u0431\u043e\u0442\u0430\u0435\u0442.'
neutral='\u041a\u043e\u0433\u0434\u0430 \u0431\u0443\u0434\u0435\u0442 \u0433\u043e\u0442\u043e\u0432 \u043e\u0442\u0447\u0451\u0442?'
negative='\u0423\u0436\u0430\u0441\u043d\u044b\u0439 \u0441\u0435\u0440\u0432\u0438\u0441, \u043f\u0440\u043e\u0431\u043b\u0435\u043c\u0430 \u0434\u043e \u0441\u0438\u0445 \u043f\u043e\u0440 \u043d\u0435 \u0440\u0435\u0448\u0435\u043d\u0430!'
# Decode escapes without altering UTF-8 source files.
texts=[positive,neutral,negative]
try:
 with httpx.Client(base_url='https://ai.example.com',timeout=180,trust_env=False) as client:
  headers={'Authorization':'Bearer '+key['api_key']}
  for name,count,system in [('short_default',1,None),('fifty_default',50,None),('short_pinned_system',1,preset['system']),('fifty_pinned_system',50,preset['system'])]:
   items=[{'item':901-i*3,'text':texts[i%3]} for i in range(count)]
   start=time.monotonic()
   r=client.post('/api/v1/chat/completions',json=payload(items,system),headers=headers)
   assert r.status_code==200, (name,r.status_code,r.json().get('error'))
   raw=r.json()
   assert isinstance(raw['choices'][0]['message']['content'],str)
   result=json.loads(raw['choices'][0]['message']['content'])
   expected=[{'item':x['item'],'tone':[2,1,0][i%3]} for i,x in enumerate(items)]
   assert result==expected,(name,'Incorrect item/order/tone')
   expected_tokens=sum(budget(x['text'],system)['input_tokens'] for x in items)
   assert raw['usage']['prompt_tokens']==expected_tokens,(name,'Token mismatch',expected_tokens,raw['usage'])
   check={'name':name,'http':r.status_code,'items':count,'seconds':round(time.monotonic()-start,3),'usage':raw['usage'],'request_id':r.headers.get('x-request-id'),'exact_token_count':True,'item_order_tones':True}
   report['checks'].append(check)
   print(json.dumps(check),flush=True)
  # Reproduce the observed 4004-token requirement without customer content.
  lo,hi=0,15000
  while lo<hi:
   mid=(lo+hi)//2
   if budget('test '*mid)['input_tokens']>=4004: hi=mid
   else: lo=mid+1
  text='test '*lo
  assert budget(text)['input_tokens']==4004
  start=time.monotonic()
  r=client.post('/api/v1/chat/completions',json=payload([{'item':4004,'text':text}]),headers=headers)
  assert r.status_code==200,(r.status_code,r.json().get('error'))
  output=json.loads(r.json()['choices'][0]['message']['content'])
  assert len(output)==1 and output[0]['item']==4004 and type(output[0]['tone']) is int and output[0]['tone'] in (0,1,2)
  assert r.json()['usage']['prompt_tokens']==4004
  check={'name':'observed_length_4004','http':200,'seconds':round(time.monotonic()-start,3),'usage':r.json()['usage'],'request_id':r.headers.get('x-request-id')}
  report['checks'].append(check); print(json.dumps(check),flush=True)
  # Find the first excessive input along a sequence of synthetic repeated words.
  lo,hi=0,15000
  while lo<hi:
   mid=(lo+hi)//2
   if budget('test '*mid)['remaining_tokens']<0: hi=mid
   else: lo=mid+1
  text='test '*lo
  counted=budget(text)
  r=client.post('/api/v1/chat/completions',json=payload([{'item':17,'text':texts[0]},{'item':73,'text':text}]),headers=headers)
  assert r.status_code==422
  error=r.json()['error']; diagnostic=error['context_budget']
  assert error['code']=='context_budget_exceeded' and error['item']==73 and error['request_id']
  assert str(settings.api_num_ctx) in error['message']
  assert diagnostic=={'input_tokens':counted['input_tokens'],'output_reserve':128,'additional_reserve':0,'num_ctx':settings.api_num_ctx,'method':counted['method']}
  assert diagnostic['input_tokens']+128>settings.api_num_ctx
  check={'name':'boundary_rejected','http':422,'error':error,'previous_step_required_tokens':budget('test '*(lo-1))['required_tokens']}
  report['checks'].append(check); print(json.dumps(check),flush=True)
  r=client.post('/api/v1/chat/completions',json=payload([]))
  assert r.status_code==401 and r.json()['error']['code']=='invalid_api_key'
  report['checks'].append({'name':'no_bearer','http':401})
  body=payload([{'item':1,'text':texts[0]}]); body['max_tokens']=4096
  r=client.post('/api/v1/chat/completions',json=body,headers=headers)
  assert r.status_code==422 and r.json()['error']['code']=='invalid_request'
  report['checks'].append({'name':'max_tokens_unsupported','http':422})
  for path in ['/','/docs','/openapi.json']:
   r=client.get(path); assert r.status_code==401
   report['checks'].append({'name':'basic_auth_preserved','path':path,'http':401})
finally:
 keys.revoke(key['id'])
 report['temporary_key_revoked']=keys.authenticate('Bearer '+key['api_key']) is None
 Path('/var/lib/finkoper-ai/api/context-diagnostics-acceptance.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report),flush=True)
