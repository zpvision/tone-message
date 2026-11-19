
"""Live synthetic compatibility probes. Never prints the temporary bearer."""
import argparse,json,sys
from pathlib import Path
import httpx
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.api_keys import KeyStore
a=argparse.ArgumentParser();a.add_argument('--label',required=True);args=a.parse_args()
keys=KeyStore('/var/lib/finkoper-ai/api');key=keys.create('synthetic-contract-'+args.label)
out=[]
def record(name,response,expected):
    body=response.json()
    normalized={'name':name,'status':response.status_code,'top_keys':sorted(body),
                'error_code':body.get('error',{}).get('code'),
                'expected_status':expected,'pass':response.status_code==expected}
    if response.status_code==200:
        result=json.loads(body['choices'][0]['message']['content'])
        normalized.update(result=result,model=body['model'],usage_keys=sorted(body['usage']),
            choice_keys=sorted(body['choices'][0]),message_keys=sorted(body['choices'][0]['message']),
            content_type=type(body['choices'][0]['message']['content']).__name__)
    else:
        normalized['error_keys']=sorted(body.get('error',{}))
    out.append(normalized)
try:
 with httpx.Client(timeout=45,trust_env=False) as c:
    base='https://ai.example.com'
    h={'Authorization':'Bearer '+key['api_key']}
    def payload(items=None):return {'model':'qwen3:8b','messages':[{'role':'user','content':json.dumps(items if items is not None else [])}]}
    for path in ['/api/v1/chat/completions','/v1/chat/completions']:
        record('empty:'+path,c.post(base+path,json=payload(),headers=h),200)
    path='/v1/chat/completions'
    for name,body,status in [
        ('single',payload([{'item':7,'text':'Спасибо большое, вы очень помогли!'}]),200),
        ('order_blank',payload([{'item':91,'text':''},{'item':7,'text':' \n\t'}]),200),
        ('max_items_blank',payload([{'item':i+1,'text':''} for i in range(1000)]),200),
        ('too_many',payload([{'item':i+1,'text':''} for i in range(1001)]),422),
        ('duplicate',payload([{'item':1,'text':'x'},{'item':1,'text':'y'}]),422),
        ('strict_item',payload([{'item':True,'text':'x'}]),422),
        ('stream',{**payload(),'stream':True},422),
        ('wrong_model',{**payload(),'model':'synthetic:missing'},422),
        ('extra',{**payload(),'temperature':0},422)]:
        record(name,c.post(base+path,json=body,headers=h),status)
    record('no_auth',c.post(base+path,json=payload()),401)
    record('bad_auth',c.post(base+path,json=payload(),headers={'Authorization':'Bearer synthetic-invalid'}),401)
    record('get_method',c.get(base+path,headers=h),405)
    record('invalid_json',c.post(base+path,content='{',headers={**h,'Content-Type':'application/json'}),400)
    record('media_type',c.post(base+path,content='[]',headers=h),415)
    record('body_limit',c.post(base+path,content='x'*(5*1024*1024+1),headers={**h,'Content-Type':'application/json'}),413)
    keys.revoke(key['id'])
    record('revoked',c.post(base+path,json=payload(),headers=h),401)
finally:keys.revoke(key['id'])
dest=(Path(__file__).resolve().parents[1]/'verification')/(args.label+'-contract.json')
dest.write_text(json.dumps(out,ensure_ascii=False,indent=2))
print(json.dumps({'checks':len(out),'passed':sum(v['pass'] for v in out),'failures':[v for v in out if not v['pass']]}))
if not all(v['pass'] for v in out):raise SystemExit(1)
