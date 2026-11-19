// Existing browser + CDP only. No secret in argv, console, URL or report.
import fs from 'node:fs';
import path from 'node:path';
const root=process.cwd();
const basic=JSON.parse(fs.readFileSync(path.join(root,'.local-secrets/cabinet-credentials.json'),'utf8'));
const api=JSON.parse(fs.readFileSync(path.join(root,'.local-secrets/api-credentials.json'),'utf8'));
const endpoint='/api/v1/chat/completions';
const target=await (await fetch('http://127.0.0.1:9225/json/new?about:blank',{method:'PUT'})).json();
const ws=new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject;});
let seq=0;
const pending=new Map(), requests=new Map(), apiResponses=[], securityErrors=[];
let secretInUrl=false, correctBearerSeen=false;
const origins=new Set();
function send(method,params={}){return new Promise((resolve,reject)=>{
  const id=++seq;pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params}));
  setTimeout(()=>{if(pending.has(id)){pending.delete(id);reject(new Error('CDP timeout: '+method));}},30000).unref();
});}
ws.onmessage=event=>{
  const message=JSON.parse(event.data);
  if(message.id){const item=pending.get(message.id);if(item){pending.delete(message.id);message.error?item.reject(new Error(message.error.message)):item.resolve(message.result);}return;}
  const p=message.params||{};
  if(message.method==='Fetch.authRequired'){
    const allowed=p.authChallenge.scheme.toLowerCase()==='basic'&&new URL(p.request.url).hostname==='ai.example.com';
    void send('Fetch.continueWithAuth',{requestId:p.requestId,authChallengeResponse:allowed?{response:'ProvideCredentials',username:basic.username,password:basic.password}:{response:'CancelAuth'}});
  } else if(message.method==='Fetch.requestPaused'){
    void send('Fetch.continueRequest',{requestId:p.requestId});
  } else if(message.method==='Network.requestWillBeSent'){
    const url=p.request.url;
    secretInUrl ||= url.includes(api.api_key);
    if(url.startsWith('http'))origins.add(new URL(url).origin);
    if(new URL(url).pathname===endpoint){
      const authorization=Object.entries(p.request.headers).find(([k])=>k.toLowerCase()==='authorization')?.[1]||'';
      const bearer=authorization==='Bearer '+api.api_key;
      correctBearerSeen ||= bearer;
      requests.set(p.requestId,{path:endpoint,bearer,authorizationScheme:authorization.split(' ')[0]||'none'});
    }
  } else if(message.method==='Network.responseReceived'&&requests.has(p.requestId)){
    requests.get(p.requestId).status=p.response.status;
  } else if(message.method==='Network.loadingFinished'&&requests.has(p.requestId)){
    const info=requests.get(p.requestId);
    void send('Network.getResponseBody',{requestId:p.requestId}).then(value=>{
      apiResponses.push({...info,body:JSON.parse(value.body)});
    });
  } else if(message.method==='Log.entryAdded'&&p.entry.source==='security'){
    // CSP diagnostics can contain HTML, so only keep the count.
    securityErrors.push(p.entry.level);
  }
};
async function evaluate(expression){const r=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw new Error('Browser evaluation failed');return r.result.value;}
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function until(expression,timeout=15000){const end=Date.now()+timeout;while(Date.now()<end){try{if(await evaluate(`Boolean(${expression})`))return;}catch{}await sleep(100);}throw new Error('Browser condition timeout: '+expression);}
async function waitResponse(status,after=0){const end=Date.now()+60000;while(Date.now()<end){const r=apiResponses.slice(after).find(r=>r.status===status);if(r)return r;await sleep(100);}throw new Error('No API response '+status);}
const report={};
try{
  await send('Network.enable');await send('Page.enable');await send('Runtime.enable');await send('Log.enable');
  await send('Fetch.enable',{handleAuthRequests:true});
  await send('Page.navigate',{url:'https://ai.example.com/docs'});
  await until("window.ui?.specSelectors?.specJson()?.get('paths')?.size === 2 && document.querySelectorAll('.opblock').length === 2");
  report.title=await evaluate("document.querySelector('.info .title').textContent");
  report.schema_paths=await evaluate("window.ui.specSelectors.specJson().get('paths').keySeq().toArray()");
  report.persist_authorization=await evaluate('window.ui.getConfigs().persistAuthorization');
  await evaluate("document.querySelector('.opblock .opblock-summary button').click()");
  await until("document.querySelector('.opblock .try-out__btn')");
  await evaluate("document.querySelector('.opblock .try-out__btn').click()");
  await until("document.querySelector('.opblock textarea') && document.querySelector('.opblock .execute')");
  report.example_content_is_string=await evaluate("typeof JSON.parse(document.querySelector('.opblock textarea').value).messages[0].content === 'string'");
  await evaluate("document.querySelector('.opblock .execute').click()");
  const unauthorized=await waitResponse(401);
  report.without_bearer_status=unauthorized.status;
  await evaluate("document.querySelector('.auth-wrapper .authorize').click()");
  await until("document.querySelector('.dialog-ux input')");
  await evaluate("document.querySelector('.dialog-ux input').focus()");
  await send('Input.insertText',{text:api.api_key});
  await evaluate("document.querySelector('.dialog-ux .auth-btn-wrapper .authorize').click()");
  await until("window.ui.authSelectors.authorized().has('BearerAuth')");
  await evaluate("Array.from(document.querySelectorAll('.dialog-ux button')).find(x=>x.textContent.trim()==='Close').click()");
  const count=apiResponses.length;
  await evaluate("document.querySelector('.opblock .execute').click()");
  const success=await waitResponse(200,count);
  if(!success.bearer)throw new Error('Explicit Bearer header was not observed');
  report.authorized_status=success.status;
  report.response=success.body;
  report.content_is_string=typeof success.body.choices?.[0]?.message?.content==='string';
  report.results=JSON.parse(success.body.choices[0].message.content);
  if(JSON.stringify(report.results)!==JSON.stringify([{item:1,tone:2},{item:2,tone:1},{item:3,tone:0}]))throw new Error('Unexpected real classifications');
  report.secret_in_local_storage=await evaluate("Object.values(localStorage).some(v=>v.includes(window.ui.authSelectors.authorized().getIn(['BearerAuth','value'])))");
  await send('Page.reload');
  await until("window.ui?.specSelectors?.specJson()?.get('paths')?.size === 2");
  report.authorization_cleared_on_reload=await evaluate("window.ui.authSelectors.authorized().size === 0");
  report.correct_bearer_header_seen=correctBearerSeen;
  report.secret_in_url=secretInUrl;
  report.resource_origins=Array.from(origins);
  report.csp_security_error_count=securityErrors.length;
  if(report.persist_authorization!==false||report.secret_in_local_storage||secretInUrl||!report.authorization_cleared_on_reload)throw new Error('Authorization persistence or URL leak');
  fs.writeFileSync(path.join(root,'verification/swagger-browser.json'),JSON.stringify(report,null,2)+'\n');
  console.log(JSON.stringify(report));
}finally{
  await send('Page.close').catch(()=>{});
  ws.close();
}
