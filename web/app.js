const $ = id => document.getElementById(id);
const names = {sjyx:'中建三局严选',lcgt:'乐从钢铁',bdt:'八达通'};
const platformInfo = {sjyx:['三','www.3jyx.cn'],lcgt:['乐','www.lcgt.cn'],bdt:['八','self.gdbadatong.com']};
const performance={sjyx:{request_interval_ms:2000,concurrency:1},lcgt:{request_interval_ms:1000,concurrency:3},bdt:{request_interval_ms:300,concurrency:1}};
const statuses = {running:'运行中',stopping:'停止中',paused:'已暂停',finishing:'结束汇总中',finished:'已结束并汇总',success:'已完成',failed:'失败',stopped:'已停止',unknown:'已结束 · 状态未知'};
let state={jobs:[]}, selected=null, token='', busy=false;
let cardsKey='', jobsKey='';
let statusFilter='all';
const statusGroups={
  active:{label:'进行中',states:['running','stopping','finishing']},
  paused:{label:'已暂停',states:['paused']},
  ended:{label:'已结束',states:['success','finished','stopped']},
  error:{label:'异常',states:['failed','unknown']}
};
for(const [value,group] of Object.entries({all:{label:'全部'},...statusGroups})){
  const button=el('button','status-nav-item',group.label);button.type='button';button.dataset.status=value;
  button.setAttribute('aria-pressed',String(value==='all'));
  button.onclick=()=>{statusFilter=value;render();};$('status-filter').append(button);
}
function resetFilter(){statusFilter='all';}
const checked=new Set(Object.keys(names));
const active=j=>['running','paused','finishing','stopping'].includes(j.status);
const date=t=>new Date(t*1000).toLocaleString('zh-CN',{hour12:false});
function el(tag,cls,text){const node=document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=text;return node;}
function notify(text){$('notice').textContent=text;$('notice').hidden=!text;}
function performancePayload(targets){
  const result={};
  for(const key of targets){
    const interval=Number(performance[key].request_interval_ms),concurrency=Number(performance[key].concurrency);
    if(!Number.isInteger(interval)||interval<0||interval>600000)throw new Error(names[key]+'的请求间隔必须是 0–600000 毫秒整数');
    if(!Number.isInteger(concurrency)||concurrency<1||concurrency>32)throw new Error(names[key]+'的并发数量必须是 1–32 的整数');
    result[key]={request_interval_ms:interval,concurrency};
  }
  return result;
}
function startTargets(targets,dryRun=false){try{action('/api/start',{targets,dry_run:dryRun,performance:performancePayload(targets)});}catch(error){notify(error.message);}}
async function action(path,body){
  busy=true;render();notify('');
  try{
    const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Control-Token':token},body:JSON.stringify(body)});
    const result=await response.json();if(!response.ok)throw new Error(result.error||'操作失败');
    if(result.ids){resetFilter();selected=result.ids[0];notify('任务已提交，日志即将更新。');}
    await refresh();
  }catch(error){notify(error.message);}finally{busy=false;render();}
}
function render(){
  const processingJob=currentProcessingJob();
  const processingLockedTargets=new Set(processingJob&&['preparing','running','finalizing'].includes(processingJob.status)?processingJob.targets:[]);
  for(const button of $('status-filter').children){
    const current=button.dataset.status===statusFilter;
    button.classList.toggle('selected',current);button.setAttribute('aria-pressed',String(current));
  }
  $('running').textContent=String(state.jobs.filter(active).length).padStart(2,'0');
  $('success').textContent=String(state.jobs.filter(j=>j.status==='success').length).padStart(2,'0');
  $('failed').textContent=String(state.jobs.filter(j=>j.status==='failed').length).padStart(2,'0');
  const cards=[];
  for(const [key,name] of Object.entries(names)){
    const job=state.jobs.find(j=>j.target===key&&active(j));
    const [symbol,domain]=platformInfo[key];
    const card=el('article','card'),top=el('div','card-top'),title=el('div');
    title.append(el('h3','',name),el('div','domain',domain));
    const check=el('input');check.type='checkbox';check.checked=checked.has(key);check.disabled=!!job||busy||processingLockedTargets.has(key);check.setAttribute('aria-label','选择'+name);check.onchange=()=>{check.checked?checked.add(key):checked.delete(key);updateStart();};
    top.append(el('div','icon '+key,symbol),title,check);
    if(job?.performance)performance[key]={...job.performance};
    const tuning=el('div','performance-settings');
    const intervalLabel=el('label','performance-field'),intervalText=el('span','','请求间隔（毫秒）'),intervalInput=el('input');
    intervalInput.type='number';intervalInput.min='0';intervalInput.max='600000';intervalInput.step='1';intervalInput.value=performance[key].request_interval_ms;intervalInput.disabled=!!job||busy||processingLockedTargets.has(key);intervalInput.oninput=()=>{performance[key].request_interval_ms=intervalInput.value;};intervalLabel.append(intervalText,intervalInput);
    const concurrencyLabel=el('label','performance-field'),concurrencyText=el('span','','并发数量'),concurrencyInput=el('input');
    concurrencyInput.type='number';concurrencyInput.min='1';concurrencyInput.max='32';concurrencyInput.step='1';concurrencyInput.value=performance[key].concurrency;concurrencyInput.disabled=!!job||busy||processingLockedTargets.has(key);concurrencyInput.oninput=()=>{performance[key].concurrency=concurrencyInput.value;};concurrencyLabel.append(concurrencyText,concurrencyInput);tuning.append(intervalLabel,concurrencyLabel);
    const bottom=el('div','card-bottom'),buttons=el('div','card-actions');
    const view=el('button','',job?'查看日志':'试运行');view.disabled=busy||(!job&&processingLockedTargets.has(key));view.onclick=()=>{if(job){resetFilter();selected=job.id;render();$('logs').focus();}else startTargets([key],true);};
    const start=el('button','',job?'执行中':'▶ 启动');start.disabled=!!job||busy||processingLockedTargets.has(key);start.onclick=()=>startTargets([key]);
    buttons.append(view);
    if(job){
      const pause=el('button','',job.status==='paused'?'继续':'暂停');
      pause.disabled=busy||!['running','paused'].includes(job.status);
      pause.onclick=()=>action(job.status==='paused'?'/api/resume':'/api/pause',{id:job.id});
      const finish=el('button','',job.status==='finishing'?'汇总中…':'结束并汇总');
      finish.disabled=busy||!['running','paused'].includes(job.status);
      finish.onclick=()=>action('/api/finish',{id:job.id});
      buttons.append(pause,finish);
    }else buttons.append(start);
    bottom.append(el('span','badge '+(job?job.status:''),job?statuses[job.status]:'就绪'),buttons);
    card.append(top,tuning,bottom);cards.push(card);
  }
  const nextCardsKey=JSON.stringify([busy,[...processingLockedTargets].sort(),state.jobs.filter(active).map(j=>[j.id,j.status])]);
  if(cardsKey!==nextCardsKey){$('platforms').replaceChildren(...cards);cardsKey=nextCardsKey;}updateStart();
  const visibleJobs=state.jobs.filter(job=>statusFilter==='all'||statusGroups[statusFilter].states.includes(job.status));
  if(!visibleJobs.some(job=>job.id===selected))selected=visibleJobs[0]?.id??null;
  const jobs=[];
  for(const job of visibleJobs){
    const button=el('button','job'+(job.id===selected?' selected':''));
    button.append(el('span','',names[job.target]),el('span','badge '+job.status,statuses[job.status]),el('small','',date(job.started)+(job.dry_run?' · 试运行':job.owned?'':' · 外部任务')));
    button.onclick=()=>{selected=job.id;render();};jobs.push(button);
  }
  if(!jobs.length)jobs.push(el('p','list-title',state.jobs.length?'当前状态下暂无任务。':'暂无任务，从上方启动采集。'));
  const nextJobsKey=JSON.stringify([statusFilter,selected,visibleJobs.map(j=>[j.id,j.status])]);
  if(jobsKey!==nextJobsKey){$('jobs').replaceChildren(...jobs);jobsKey=nextJobsKey;}$('job-count').textContent=statusFilter==='all'?state.jobs.length:`${visibleJobs.length} / ${state.jobs.length}`;
  const job=state.jobs.find(j=>j.id===selected);
  $('stop').hidden=!job||!active(job);$('stop').disabled=busy||!['running','paused'].includes(job?.status);
  $('stop').textContent=job?.status==='finishing'?'汇总中…':'■ 结束并汇总';
  $('pause').hidden=!job||!active(job);$('pause').disabled=busy||!['running','paused'].includes(job?.status);
  $('pause').textContent=job?.status==='paused'?'▶ 继续':'Ⅱ 暂停';
  $('download').disabled=!job;
  if(job){
    $('log-title').textContent=names[job.target]+' / '+statuses[job.status]+' / PID '+(job.pid??'—');
    const performanceText=job.performance?'\n性能：请求间隔 '+job.performance.request_interval_ms+'ms · 并发 '+job.performance.concurrency:'';
    $('job-meta').textContent='$ '+job.command+performanceText+(job.graceful?'':'\n旧任务：结束仅汇总已落盘数据，尚未保存的内存数据无法恢复。')+(job.output?'\n结果：'+job.output:'')+(job.exported_count!==undefined?'（'+job.exported_count+' 条）':'')+(job.export_note?'\n'+job.export_note:'')+(job.export_error?'\n汇总失败：'+job.export_error:'');
    const logs=$('logs');if(logs.textContent!==job.text){logs.textContent=job.text||'进程已启动，等待日志输出…';if($('autoscroll').checked)logs.scrollTop=logs.scrollHeight;}
  }else{
    $('log-title').textContent='暂无匹配任务';$('job-meta').textContent='';
    $('logs').textContent=state.jobs.length?'当前状态下暂无任务，请切换筛选条件。':'选择一个平台启动采集，或查看已有任务日志。';
  }
}
function available(){const job=currentProcessingJob();const locked=new Set(job&&['preparing','running','finalizing'].includes(job.status)?job.targets:[]);return [...checked].filter(key=>!locked.has(key)&&!state.jobs.some(j=>j.target===key&&active(j)));}
function updateStart(){const n=available().length;$('start-all').disabled=busy||!n||!token;$('start-all').textContent=n?`▶ 启动所选平台 (${n})`:'所有所选平台运行中';}
function currentProcessingJob(){return state.cloud_processing?.jobs?.[0]??null;}
async function refresh(){
  try{
    const response=await fetch('/api/state');if(!response.ok)throw new Error('服务连接失败');
    state=await response.json();token=state.token;
    if(!selected&&state.jobs.length)selected=(state.jobs.find(active)||state.jobs[0]).id;
    $('connection').textContent='● 本地服务已连接 · '+new Date().toLocaleTimeString('zh-CN',{hour12:false});render();
  }catch(error){$('connection').textContent='○ 连接中断 · 正在重试';token='';updateStart();}
}
$('start-all').onclick=()=>startTargets(available());
$('stop').onclick=()=>action('/api/finish',{id:selected});
$('pause').onclick=()=>{const job=state.jobs.find(j=>j.id===selected);if(job)action(job.status==='paused'?'/api/resume':'/api/pause',{id:selected});};
$('download').onclick=()=>{const job=state.jobs.find(j=>j.id===selected);if(!job)return;const url=URL.createObjectURL(new Blob([job.text],{type:'text/plain;charset=utf-8'}));const link=el('a');link.href=url;link.download=job.id+'-recent.log';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
$('date').textContent=new Date().toLocaleDateString('zh-CN',{year:'numeric',month:'long',day:'numeric',weekday:'long'});
async function poll(){await refresh();setTimeout(poll,1000);}poll();
