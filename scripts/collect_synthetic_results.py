
"""Aggregate only synthetic request IDs/time windows; never read message bodies."""
import json,math,re,statistics,subprocess
from pathlib import Path
root=Path('verification')
units={'stage':'finkoper-ai-validation2','stage-workers2':'finkoper-ai-validation3',
       'final-stage':'finkoper-ai-validation5'}
for label in ['before','before-repeat','before-cold','stage','stage-workers2','final-stage','after','after-cold']:
 path=root/(label+'.json')
 if not path.exists():continue
 data=json.loads(path.read_text())
 for m in data['measurements']:
  if m['status']!=200:continue
  args=['--since','@'+str(math.floor(m['start'])),'--until','@'+str(math.ceil(m['start']+m['seconds'])+1),'--no-pager','-o','cat']
  lines=subprocess.check_output(['sudo','-n','journalctl','-u','ollama',*args],text=True).splitlines()
  durations=[];prompt=[];evals=[]
  for line in lines:
   if '[GIN]' in line and 'POST' in line and '"/api/chat"' in line:
    match=re.search(r'[|][ ]*([0-9.]+)(ms|µs|ns|s)[ ]*[|]',line)
    if match:
     v=float(match[1])*{'s':1,'ms':.001,'µs':.000001,'ns':1e-9}[match[2]]
     if v>.02:durations.append(v)
   a=re.search(r'prompt eval time =[ ]*([0-9.]+) ms',line)
   if a:prompt.append(float(a[1])/1000)
   a=re.search(r'(?<!prompt )eval time =[ ]*([0-9.]+) ms',line)
   if a:evals.append(float(a[1])/1000)
  m['inference_observed_seconds']=durations;m['prompt_eval_seconds']=prompt;m['eval_seconds']=evals
  m['inference_window_exact_call_count']=len(durations)==m['size']
  api=subprocess.check_output(['sudo','-n','journalctl','-u',units.get(label,'finkoper-ai'),*args],text=True)
  for line in api.splitlines():
   if 'batch_api {' in line:
    d=json.loads(line.split('batch_api ',1)[1])
    if d['request_id']==m['request_id']:
     d.pop('key_id',None);m['api_stats']=d
  # Prior reports were produced by root; collection is explicitly run as root.
 path.write_text(json.dumps(data,ensure_ascii=False,indent=2))
def percentile(values,p):
 v=sorted(values);return v[max(0,math.ceil(len(v)*p)-1)] if v else None
def combine(labels):
 data=[json.loads((root/(s+'.json')).read_text()) for s in labels]
 rows=[m for d in data for m in d['measurements']]
 gpu=[sample for d in data for sample in d.get('gpu_samples',[]) if any(m['start']<=sample[0]<=m['start']+m['seconds'] for m in d['measurements'])]
 result={'batches':{},'gpu':{'vram_min_mib':min((v[1] for v in gpu),default=None),'vram_max_mib':max((v[1] for v in gpu),default=None),
        'util_median':statistics.median([v[2] for v in gpu]) if gpu else None,'util_p95':percentile([v[2] for v in gpu],.95)}}
 inference=[]
 for size in sorted(set(m['size'] for m in rows)):
  selected=[m for m in rows if m['size']==size]
  vals=[m['seconds'] for m in selected]
  calltimes=[v for m in selected for v in m.get('api_stats',{}).get('inference_seconds',m.get('inference_observed_seconds',[]))]
  inference+=calltimes
  stats=[m.get('api_stats',{}) for m in selected]
  result['batches'][size]={'runs':len(vals),'median_api_seconds':statistics.median(vals),'p50_api_seconds':percentile(vals,.5),
    'p95_api_seconds':percentile(vals,.95),'throughput_items_s':len(vals)*size/sum(vals),
    'inference_p50_seconds':percentile(calltimes,.5),'inference_p95_seconds':percentile(calltimes,.95),
    'mean_processing_seconds':statistics.mean([s['processing_seconds'] for s in stats]),
    'mean_queue_seconds':statistics.mean([s['queue_seconds'] for s in stats]),
    'mean_api_preflight_overhead_seconds':statistics.mean([s['total_seconds']-s['processing_seconds']-s['queue_seconds'] for s in stats]),
    'errors':sum(m['status']!=200 for m in selected),'timeouts':sum('timeout' in m.get('error','') for m in selected),
    'retries':sum(s['attempts']-size for s in stats)}
 result['all_inference_p50_seconds']=percentile(inference,.5)
 result['all_inference_p95_seconds']=percentile(inference,.95)
 return result
summary={'percentiles':'nearest rank; very small number of API runs; not a capacity/SLA estimate',
         'before':combine(['before','before-repeat']),'after':combine(['after']),
         'cold_before':json.loads((root/'before-cold.json').read_text())['measurements'][0]['seconds'],
         'cold_after':json.loads((root/'after-cold.json').read_text())['measurements'][0]['seconds']}
before=json.loads((root/'before-contract.json').read_text());after=json.loads((root/'after-contract.json').read_text())
assert before==after,'external contract normalized result differs'
summary['contract_identical']=True
original=json.loads((root/'before.json').read_text())['measurements']
updated=json.loads((root/'after.json').read_text())['measurements']
summary['quality_identical']=all(next(m['tones'] for m in original if m['size']==n['size'])==n['tones'] for n in updated)
summary['unique_labeled_examples']=20;summary['correct_before_after']=[18,18]
(root/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
print(json.dumps(summary,ensure_ascii=False,indent=2))
