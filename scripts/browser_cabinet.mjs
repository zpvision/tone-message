// QA only: uses existing Node and an isolated local Chrome/Edge instance.
// No dependencies and no credentials in argv or console output.
import fs from 'node:fs';
import path from 'node:path';
const root=process.cwd(), credentials=JSON.parse(fs.readFileSync(path.join(root,'.local-secrets','cabinet-credentials.json'),'utf8'));
let seq=0;
const pending=new Map(), exceptions=[];
const target=await (await fetch('http://127.0.0.1:9223/json/new?about:blank',{method:'PUT'})).json();
const ws=new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject;});
ws.onmessage=event=>{const message=JSON.parse(event.data);if(message.id){const item=pending.get(message.id);if(item){pending.delete(message.id);message.error?item.reject(new Error(message.error.message)):item.resolve(message.result);}}else if(message.method==='Runtime.exceptionThrown'){exceptions.push(message.params.exceptionDetails.text);}};
function send(method,params={}){return new Promise((resolve,reject)=>{const id=++seq;pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params}));setTimeout(()=>{if(pending.has(id)){pending.delete(id);reject(new Error('CDP timeout: '+method));}},20000).unref();});}
async function evaluate(expression){const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw new Error(result.exceptionDetails.text);return result.result.value;}
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function until(expression,timeout=30000){const end=Date.now()+timeout;while(Date.now()<end){try{const value=await evaluate(expression);if(value)return value;}catch{}await sleep(100);}throw new Error('Page condition timed out');}
const report={};
try{
  await send('Network.enable');await send('Page.enable');await send('Runtime.enable');
  await send('Network.setExtraHTTPHeaders',{headers:{Authorization:'Basic '+Buffer.from(credentials.username+':'+credentials.password).toString('base64')}});
  await send('Emulation.setDeviceMetricsOverride',{width:1280,height:1000,deviceScaleFactor:1,mobile:false});
  await send('Page.navigate',{url:credentials.url});
  await until("document.getElementById('system')?.value.length > 50 && document.getElementById('parallel').textContent.includes(': 1')");
  report.page_loaded=true;
  const doc=await send('DOM.getDocument');const input=await send('DOM.querySelector',{nodeId:doc.root.nodeId,selector:'#file'});
  await send('DOM.setFileInputFiles',{nodeId:input.nodeId,files:[path.join(root,'verification','cabinet-synthetic.xlsx')]});
  await until("!document.getElementById('sheet').disabled && !document.getElementById('column').disabled");
  report.sheets=await evaluate("Array.from(document.getElementById('sheet').options).map(x=>x.value)");
  await evaluate("document.getElementById('sheet').value='Справочник'; document.getElementById('sheet').dispatchEvent(new Event('change'))");
  await until("document.getElementById('rowInfo').textContent.includes('Строк данных: 0')");
  await evaluate("document.getElementById('headers').checked=false;document.getElementById('headers').dispatchEvent(new Event('change'))");
  await until("document.getElementById('rowInfo').textContent.includes('Строк данных: 1')");
  await evaluate("document.getElementById('headers').checked=true;document.getElementById('sheet').value='Сообщения';document.getElementById('sheet').dispatchEvent(new Event('change'))");
  await until("document.getElementById('rowInfo').textContent.includes('Строк данных: 8')");
  await evaluate("document.getElementById('column').value='2';document.getElementById('limit').value='4'");
  const prompt=await evaluate("document.getElementById('system').value");
  await evaluate("document.getElementById('savePrompt').click()");
  await until("document.getElementById('notice').textContent.includes('Промпт сохранён')");
  await evaluate("document.getElementById('system').value='Временная правка';document.getElementById('restorePrompt').click()");
  await until("document.getElementById('system').value === "+JSON.stringify(prompt));
  if(await evaluate("document.getElementById('system').value")!==prompt)throw new Error('Prompt restore failed');
  report.prompt_save_restore=true;
  const priorJob=await evaluate("localStorage.getItem('finkoper-job')");
  await evaluate("document.getElementById('start').click()");
  report.job_id=await until("localStorage.getItem('finkoper-job') && localStorage.getItem('finkoper-job') !== "+JSON.stringify(priorJob)+" && localStorage.getItem('finkoper-job')");
  await send('Page.navigate',{url:'about:blank'});await sleep(500);
  await send('Page.navigate',{url:credentials.url});
  await until("document.getElementById('jobState')?.textContent==='Завершено'");
  report.reopened_job=await evaluate("document.getElementById('jobId').textContent");
  report.progress=await evaluate("document.getElementById('progressText').textContent");
  if(!report.progress.startsWith('4 / 4'))throw new Error('Unexpected progress');
  report.download_links=await evaluate("!document.getElementById('downloads').hidden && document.getElementById('xlsx').href.includes('/download/xlsx')");
  await evaluate("window.scrollTo(0,0)");await sleep(150);
  const desktop=await send('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(root,'verification','cabinet-desktop.png'),Buffer.from(desktop.data,'base64'));
  await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});await evaluate("window.scrollTo(0,0)");await sleep(200);
  report.mobile_width=await evaluate("({viewport:innerWidth,document:document.documentElement.scrollWidth})");
  if(report.mobile_width.document>report.mobile_width.viewport)throw new Error('Horizontal page overflow');
  const mobile=await send('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(root,'verification','cabinet-mobile.png'),Buffer.from(mobile.data,'base64'));
  report.javascript_exceptions=exceptions;
  if(exceptions.length)throw new Error('Browser JS exceptions');
  fs.writeFileSync(path.join(root,'verification','cabinet-browser.json'),JSON.stringify(report,null,2)+'\n');
  console.log(JSON.stringify(report,null,2));
}finally{try{await send('Browser.close');}catch{}ws.close();}
