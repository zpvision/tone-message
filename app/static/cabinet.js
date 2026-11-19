'use strict';
const $ = id => document.getElementById(id);
const activeStates = ['preparing','warming','running','stopping','exporting'];
const states = {preparing:'Подготовка',warming:'Прогрев модели',running:'Обработка',stopping:'Остановка: ждём начатые запросы',exporting:'Запись XLSX',completed:'Завершено',stopped:'Остановлено',interrupted:'Прервано: исполнитель перезапущен',failed:'Ошибка задания'};
let upload = null, selectedJob = localStorage.getItem('finkoper-job'), savedPrompts = null, versions = [], busy = false;
function notify(text) { $('notice').textContent = text; $('notice').hidden = !text; }
async function api(path, options = {}) {
  const headers = {'X-Cabinet-Request':'1', ...options.headers};
  if (options.body && !(options.body instanceof FormData)) {headers['Content-Type']='application/json'; options.body=JSON.stringify(options.body);}
  const response = await fetch(path, {...options,headers,credentials:'same-origin'});
  if (!response.ok) {let detail; try {detail=(await response.json()).detail;} catch {detail='';} if (Array.isArray(detail)) detail=detail.map(v=>`${v.loc.slice(1).join('.')}: ${v.msg}`).join('; '); throw new Error(detail || `HTTP ${response.status}. Проверьте доступ и параметры.`);}
  return response.json();
}
const safe = fn => async (...args) => {try {await fn(...args);} catch (error) {notify(error.message);}};
function option(select,value,text) {const item=document.createElement('option');item.value=value;item.textContent=text;select.append(item);}
function cell(row,text) {const item=document.createElement('td');item.textContent=text;row.append(item);return item;}
function duration(value) {if(value==null) return '—'; if(value<10)return `${Math.max(0,value).toFixed(2)} с`; const s=Math.max(0,Math.round(value));return s<60?`${s} с`:`${Math.floor(s/60)} мин ${s%60} с`;}
function promptFields(value) {$('system').value=value.system; $('template').value=value.template;}
async function prompts() {versions=await api('/api/prompts');$('promptVersions').replaceChildren();option($('promptVersions'),'','Выберите сохранённую версию');for(const p of versions) option($('promptVersions'),p.id,`#${p.id} · ${new Date(p.created).toLocaleString('ru')}`);}
async function refreshPreview() {
  if (!upload) return;
  const data=await api(`/api/uploads/${upload.id}/preview?sheet=${encodeURIComponent($('sheet').value)}&headers=${$('headers').checked}`);
  $('column').replaceChildren();for(const c of data.columns) option($('column'),c.index,c.name);$('column').disabled=false;
  $('rowInfo').textContent=`Строк данных: ${data.total}. Пустые строки сохраняются со статусом «пропущено».`;
  const head=$('preview').querySelector('thead'),body=$('preview').querySelector('tbody');head.replaceChildren();body.replaceChildren();
  const tr=document.createElement('tr');for(const c of data.columns){const th=document.createElement('th');th.textContent=c.name;tr.append(th);}head.append(tr);
  for(const values of data.rows){const row=document.createElement('tr');for(const value of values) cell(row,value);body.append(row);} $('start').disabled=false;
}
$('file').onchange=safe(async()=>{
  const file=$('file').files[0];if(!file)return;if(file.size>30*1024*1024)throw new Error('Файл превышает 30 МБ');
  $('start').disabled=true;$('fileInfo').textContent='Загрузка и проверка книги…';const form=new FormData();form.append('file',file);
  upload=await api('/api/uploads',{method:'POST',body:form});$('fileInfo').textContent=upload.name;$('sheet').replaceChildren();
  for(const s of upload.sheets)option($('sheet'),s.name,`${s.name} · ${s.rows} строк`);$('sheet').disabled=false;await refreshPreview();notify('');
});
$('sheet').onchange=safe(refreshPreview);$('headers').onchange=safe(refreshPreview);
$('scope').onchange=()=>{$('limit').disabled=$('scope').value==='all';};
$('savePrompt').onclick=safe(async()=>{const body={system:$('system').value,template:$('template').value};const result=await api('/api/prompts',{method:'POST',body});savedPrompts=body;await prompts();$('promptVersions').value=result.id;notify('Промпт сохранён на сервере.');});
$('restorePrompt').onclick=safe(async()=>{const config=await api('/api/config');savedPrompts=config.prompts;promptFields(savedPrompts);});
$('promptVersions').onchange=()=>{const p=versions.find(v=>String(v.id)===$('promptVersions').value);if(p){promptFields(p);savedPrompts={system:p.system,template:p.template};}};
$('start').onclick=safe(async()=>{
  if(!upload||busy)return;busy=true;$('start').disabled=true;
  try {
    const data=await api('/api/jobs',{method:'POST',body:{upload_id:upload.id,sheet:$('sheet').value,column:Number($('column').value),headers:$('headers').checked,
      limit:$('scope').value==='all'?null:Number($('limit').value),prompts:{system:$('system').value,template:$('template').value},
      parameters:{temperature:Number($('temperature').value),num_ctx:Number($('numCtx').value),num_predict:Number($('numPredict').value),thinking:$('thinking').checked,concurrency:Number($('concurrency').value),timeout:Number($('timeout').value)}}});
    selectedJob=data.id;localStorage.setItem('finkoper-job',selectedJob);notify('Задание запущено. Можно закрыть страницу.');await refreshAll();
  } finally {busy=false;$('start').disabled=false;}
});
function stat(name,value) {const line=document.createElement('div');const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=name;dd.textContent=value;line.append(dt,dd);$('jobStats').append(line);}
async function renderJob() {
  if(!selectedJob)return;
  let job;try {job=await api(`/api/jobs/${selectedJob}`);}catch(error){if(error.message.includes('не найдено')){selectedJob=null;localStorage.removeItem('finkoper-job');$('jobPanel').hidden=true;$('emptyJob').hidden=false;return;}throw error;}
  $('emptyJob').hidden=true;$('jobPanel').hidden=false;const m=job.metrics;
  $('jobState').textContent=states[job.status]||job.status;$('jobId').textContent=job.id.slice(0,8);$('jobFile').textContent=`${job.snapshot.selection.filename} · ${job.snapshot.selection.sheet}`;
  $('progress').max=m.total||1;$('progress').value=m.processed;$('progressText').textContent=`${m.processed} / ${m.total} · успешно ${m.success} · ошибок ${m.errors} · пропущено ${m.skipped}`;
  $('rate').textContent=m.messages_per_minute.toFixed(1);$('elapsed').textContent=duration(m.total_seconds);$('jobStats').replaceChildren();
  stat('Осталось, приблизительно',activeStates.includes(job.status)?duration(m.eta_seconds):'—');stat('Прогрев · отдельно',duration(m.warmup_seconds));stat('Обработка · wall-clock',duration(m.model_seconds));stat('Подготовка книги',duration(m.prepare_seconds));stat('Запись XLSX',duration(m.export_seconds));stat('Средний запрос / p50 / p95',`${Math.round(m.mean_request_ms)} / ${Math.round(m.p50_request_ms)} / ${Math.round(m.p95_request_ms)} мс`);stat('Попыток / ошибок',`${m.attempts} / ${m.errors}`);stat('Входных / выходных токенов',`${m.prompt_tokens} / ${m.output_tokens}`);stat('HTTP / Ollama параллелизм',`${job.snapshot.parameters.concurrency} / ${job.snapshot.ollama_num_parallel}`);
  if(m.incomplete_attempts)stat('Попыток с неполным временем',m.incomplete_attempts);
  const r=job.resources;if(r)$('resources').textContent=`GPU ${r.gpu_utilization??'—'}% · VRAM ${Math.round(r.vram_mib??0)} / ${Math.round(r.vram_total_mib??0)} MiB · RAM занято ${Math.round((r.MemTotal_mib??0)-(r.MemAvailable_mib??0))} / ${Math.round(r.MemTotal_mib??0)} MiB`;
  $('jobError').textContent=job.error?`Причина: ${job.error}`:'';$('stop').hidden=!activeStates.includes(job.status)||['exporting','stopping'].includes(job.status);$('resume').hidden=!['stopped','interrupted','failed'].includes(job.status);
  $('downloads').hidden=!job.export_ready;for(const kind of ['xlsx','jsonl','metrics'])$(kind).href=`/api/jobs/${job.id}/download/${kind}`;
  $('partialExport').hidden=job.export_ready||!['interrupted','stopped','failed'].includes(job.status);
  $('snapshot').textContent=JSON.stringify(job.snapshot,null,2);const tbody=$('recent').querySelector('tbody');tbody.replaceChildren();for(const item of job.recent){const tr=document.createElement('tr');cell(tr,item.row_num);cell(tr,item.status);cell(tr,`${item.result.label||''} ${item.result.response||item.result.error||''}`);tbody.append(tr);}
}
$('stop').onclick=safe(async()=>{await api(`/api/jobs/${selectedJob}/stop`,{method:'POST'});await renderJob();});
$('resume').onclick=safe(async()=>{await api(`/api/jobs/${selectedJob}/resume`,{method:'POST'});await renderJob();});
$('partialExport').onclick=safe(async()=>{await api(`/api/jobs/${selectedJob}/export`,{method:'POST'});await renderJob();});
async function history() {
  const items=await api('/api/jobs');const body=$('history').querySelector('tbody');body.replaceChildren();
  for(const item of items){const tr=document.createElement('tr');cell(tr,new Date(item.created).toLocaleString('ru'));cell(tr,item.filename);cell(tr,states[item.status]||item.status);const actions=cell(tr,'');
    const open=document.createElement('button');open.className='quiet';open.textContent='Открыть';open.onclick=safe(async()=>{selectedJob=item.id;localStorage.setItem('finkoper-job',item.id);await renderJob();$('jobPanel').scrollIntoView({behavior:'smooth',block:'start'});});actions.append(open);
    const del=document.createElement('button');del.className='quiet';del.textContent='Удалить';del.disabled=activeStates.includes(item.status);del.onclick=safe(async()=>{if(!confirm('Удалить задание и его файлы? Это действие необратимо.'))return;await api(`/api/jobs/${item.id}?confirm=true`,{method:'DELETE'});await refreshAll();});actions.append(del);body.append(tr);}
}
async function refreshAll(){await Promise.all([renderJob(),history()]);}
$('refresh').onclick=safe(refreshAll);
async function boot(){const config=await api('/api/config');savedPrompts=config.prompts;promptFields(config.prompts);$('parallel').textContent=`Ollama NUM_PARALLEL: ${config.ollama_num_parallel}`;await prompts();await refreshAll();try{const ready=await api('/readyz');$('ready').textContent=ready.model.loaded?'Модель в GPU-памяти':'Модель установлена';}catch{$('ready').textContent='Inference недоступен';}}
safe(boot)();let polling=false;setInterval(async()=>{if(polling)return;polling=true;try{await refreshAll();}catch(error){notify(error.message);}finally{polling=false;}},2500);
