const $ = id => document.getElementById(id);
const names = {sjyx:'中建三局严选',lcgt:'乐从钢铁',bdt:'八达通'};
const activeCrawlerStatuses = ['running','paused','finishing','stopping'];
const activeProcessingStatuses = ['preparing','running','finalizing'];
const processingStatuses = {preparing:'准备数据',running:'处理中',finalizing:'整理结果',success:'处理完成',failed:'处理失败'};
const modeLabels = {full:'全量处理',incremental:'增量处理'};
let state={jobs:[],cloud_processing:{jobs:[]}}, token='', busy=false;
const checked=new Set(Object.keys(names));

function notify(text){$('notice').textContent=text;$('notice').hidden=!text;}
function currentJob(){return state.cloud_processing?.jobs?.[0]??null;}
function selectedTargets(){return [...checked];}

function render(){
  const job=currentJob();
  const processingActive=job&&activeProcessingStatuses.includes(job.status);
  const targets=selectedTargets();
  const crawlingTargets=new Set(state.jobs.filter(item=>activeCrawlerStatuses.includes(item.status)).map(item=>item.target));
  const conflicts=targets.filter(target=>crawlingTargets.has(target));
  for(const input of document.querySelectorAll('.processing-platform')){
    input.checked=checked.has(input.value);input.disabled=busy||processingActive;
  }
  for(const input of document.querySelectorAll('input[name="processing-mode"]'))input.disabled=busy||processingActive;
  $('processing-select-all').disabled=busy||processingActive;
  $('processing-select-all').textContent=checked.size===Object.keys(names).length?'取消全选':'全选';
  $('processing-start').disabled=busy||processingActive||!token||!targets.length||conflicts.length>0;
  $('processing-start').textContent=processingActive?'处理中…':'开始处理';
  if(conflicts.length&&!processingActive)notify(`${conflicts.map(target=>names[target]).join('、')}正在采集，请等待采集结束后处理。`);
  else if($('notice').dataset.kind==='conflict')notify('');
  $('notice').dataset.kind=conflicts.length&&!processingActive?'conflict':'';
  if(!job){
    $('processing-status').textContent='等待处理';$('processing-percent').textContent='0%';$('processing-progress-bar').style.width='0%';
    $('processing-current').textContent='—';$('processing-count').textContent='0 / 0';$('processing-files').textContent='0';$('metric-files').textContent='00';$('processing-output').textContent='处理结果目录：—';$('processing-error').hidden=true;
    return;
  }
  const percent=Number(job.percent||0), files=Number(job.generated_files||0);
  $('processing-status').textContent=processingStatuses[job.status]||job.status;
  $('processing-percent').textContent=percent.toFixed(percent%1?1:0)+'%';
  $('processing-progress-bar').style.width=Math.min(100,percent)+'%';
  $('processing-current').textContent=job.current_source?names[job.current_source]:'—';
  $('processing-count').textContent=`${job.processed||0} / ${job.total||0}`;
  $('processing-files').textContent=String(files);$('metric-files').textContent=String(files).padStart(2,'0');
  $('processing-output').textContent='处理模式：'+(modeLabels[job.mode]||job.mode||'—')+'\n处理结果目录：'+(job.output_dir||'—')+'\n待导入清单：'+(job.output_dir?job.output_dir+'/待导入文件清单.txt':'—')+'\n处理规则版本：'+(job.rule_version||'旧版本 / 未标记');
  $('processing-error').hidden=!job.error;$('processing-error').textContent=job.error||'';
}

async function startProcessing(){
  const targets=selectedTargets();if(!targets.length)return;
  const mode=document.querySelector('input[name="processing-mode"]:checked')?.value||'full';
  busy=true;notify('');render();
  try{
    const response=await fetch('/api/cloud-processing/start',{method:'POST',headers:{'Content-Type':'application/json','X-Control-Token':token},body:JSON.stringify({targets,mode})});
    const result=await response.json();if(!response.ok)throw new Error(result.error||'数据处理启动失败');
    notify('云商品数据处理任务已启动。');await refresh();
  }catch(error){notify(error.message);}finally{busy=false;render();}
}

async function refresh(){
  try{
    const response=await fetch('/api/state');if(!response.ok)throw new Error('服务连接失败');
    state=await response.json();token=state.token;
    $('connection').textContent='● 本地服务已连接 · '+new Date().toLocaleTimeString('zh-CN',{hour12:false});render();
  }catch(error){$('connection').textContent='○ 连接中断 · 正在重试';token='';render();}
}

$('processing-start').onclick=startProcessing;
for(const input of document.querySelectorAll('.processing-platform'))input.onchange=()=>{input.checked?checked.add(input.value):checked.delete(input.value);render();};
$('processing-select-all').onclick=()=>{if(checked.size===Object.keys(names).length)checked.clear();else Object.keys(names).forEach(name=>checked.add(name));render();};
$('date').textContent=new Date().toLocaleDateString('zh-CN',{year:'numeric',month:'long',day:'numeric',weekday:'long'});
async function poll(){await refresh();setTimeout(poll,1000);}poll();
