
"""Synthetic only. Temporary key remains in memory and is revoked in finally."""
import argparse, asyncio, json, math, statistics, subprocess, sys, time, threading
from pathlib import Path
import httpx
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.api_keys import KeyStore
DATA=[
("Спасибо большое, вы очень помогли! Я очень рад!",2,"positive"),
("Ужас, я в ярости из-за этой задержки!",0,"negative"),
("Отправьте, пожалуйста, акт за октябрь.",1,"neutral"),
("Ну замечательно, опять всё сломалось! Как же это бесит.",0,"sarcasm"),
("Спасибо за помощь, я рад результату, хотя вчера немного волновался.",2,"mixed"),
("Понял.",1,"short"),
('Коллега написал: «Я в ярости». Пересылаю его сообщение для сведения.',1,"quote"),
("Очень переживаю, что не успеем. Мне страшно.",0,"negative"),
("Ура, получилось! Очень счастлив!",2,"positive"),
("Оплата поступила вчера в 14:00.",1,"neutral"),
("Хорошо, сделаю.",1,"short"),
("Я расстроен задержкой, но благодарен вам за помощь.",0,"mixed"),
("Вот это сервис: три дня молчания. Просто кошмар!",0,"sarcasm"),
('В письме клиента фраза «кошмар, всё пропало». Это цитата из обращения.',1,"quote"),
("Благодарю за отличную работу, вы меня выручили!",2,"positive"),
("Срок сдачи отчёта — 25 число.",1,"neutral"),
("Мне обидно и очень неприятно такое отношение.",0,"negative"),
("Да.",1,"short"),
("Документы подписаны. Можно отправлять.",1,"neutral"),
("Наконец-то всё решено! Какое облегчение!",2,"positive")]
async def main():
 a=argparse.ArgumentParser();a.add_argument('--label',required=True);a.add_argument('--sizes',default='1,20,80');a.add_argument('--url',default='https://ai.example.com');a.add_argument('--data-dir',default='/var/lib/finkoper-ai/api'); args=a.parse_args()
 keys=KeyStore(args.data_dir); key=keys.create('synthetic-model-neutral-'+args.label)
 samples=[]; stop=threading.Event()
 def monitor():
  while not stop.wait(1):
   try:
    out=subprocess.check_output(['nvidia-smi','--query-gpu=memory.used,utilization.gpu','--format=csv,noheader,nounits'],text=True,timeout=3)
    v,g=map(float,out.strip().split(','));samples.append([time.time(),v,g])
   except Exception: pass
 t=threading.Thread(target=monitor,daemon=True);t.start()
 report={'label':args.label,'synthetic_only':True,'dataset':DATA,'measurements':[],'cold_load':'not forced: production requests each minute','cache':'unique case number per item; repeated static system prefix benefits KV cache; no response cache'}
 try:
  async with httpx.AsyncClient(timeout=150,trust_env=False) as client:
   for size in map(int,args.sizes.split(',')):
    # Start large batch just after observed regular client window (~second 34).
    if size>=80:
     delay=(36-time.time()%60)%60
     await asyncio.sleep(delay)
    items=[{'item':1000+i*7,'text':DATA[i%len(DATA)][0]+f' [Синтетический пример {i+1:03d}]'} for i in range(size)]
    body={'model':'qwen3:8b','messages':[{'role':'user','content':json.dumps(items,ensure_ascii=False)}]}
    start=time.time()
    r=await client.post(args.url+'/v1/chat/completions',json=body,headers={'Authorization':'Bearer '+key['api_key'],'X-Forwarded-Proto':'https'})
    elapsed=time.time()-start
    row={'size':size,'start':start,'seconds':elapsed,'status':r.status_code,'request_id':r.headers.get('x-request-id')}
    data=r.json()
    if r.status_code==200:
     result=json.loads(data['choices'][0]['message']['content'])
     row.update(ordered=[v['item'] for v in result]==[v['item'] for v in items],tones=[v['tone'] for v in result],usage=data['usage'],model=data['model'],items_per_second=size/elapsed,
       quality_correct=sum(v['tone']==DATA[i%len(DATA)][1] for i,v in enumerate(result)))
    else: row['error']=data.get('error',{}).get('code')
    report['measurements'].append(row)
    print(json.dumps(row),flush=True)
    await asyncio.sleep(2)
 finally:
  keys.revoke(key['id']);stop.set();t.join(4);report['gpu_samples']=samples
  dest=(Path(__file__).resolve().parents[1]/'verification');dest.mkdir(exist_ok=True)
  (dest/(args.label+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2))
 if any(m['status']!=200 for m in report['measurements']):raise SystemExit(1)
asyncio.run(main())
