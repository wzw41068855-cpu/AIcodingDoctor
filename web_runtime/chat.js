// 问答复用只读任务和 followup；修复继续使用原有审批、测试及应用接口。
let viewMode='ask', selectedTasks={ask:localStorage.getItem('aicdChat'),repair:localStorage.getItem('aicdRepair')};
let currentTask=null, sending=false, imageLoaded=false, selectionVersion=0;
const repairRefresh=refresh;
const field=name=>document.querySelector('[name="'+name+'"]');
function credentials(){return {api_key:document.querySelector('#key').value,embedding_api_key:field('embedding_api_key').value,rerank_api_key:field('rerank_api_key').value}}
function modelSettings(){return Object.fromEntries(['model','base_url','max_tokens','context_chars'].map(name=>[name,['max_tokens','context_chars'].includes(name)?Number(field(name).value):field(name).value]))}
function setMode(mode){
  viewMode=mode;localStorage.setItem('aicdMode',mode);
  for(const name of ['ask','repair']){document.querySelector('#nav-'+name).setAttribute('aria-pressed',String(name===mode));document.querySelector('#'+name+'-panel').hidden=name!==mode}
  document.querySelector('#repair-detail').hidden=mode!=='repair'||!current;
  document.querySelector('#new-chat').hidden=mode!=='ask';
  document.querySelector('#history-title').textContent=mode==='ask'?'会话历史':'修复历史';
  document.querySelector('#mode-hint').textContent=mode==='ask'?'只读分析 · 支持连续追问':'隔离修改 · 审批后执行 · Docker 测试';
  if(mode==='repair'&&!imageLoaded){imageLoaded=true;loadImages().catch(e=>{imageLoaded=false;showError(e)})}
}
function chatStatus(task){return {queued:'正在排队…',running:'正在阅读代码并分析…',success:'回答完成',failed:'本轮回答失败',blocked:'本轮执行受阻',cancelled:'已停止回答',cancelling:'正在停止…'}[task.status]||statusLabel(task.status)}
function renderChat(task){
  const log=document.querySelector('#messages'),nearBottom=log.scrollHeight-log.scrollTop-log.clientHeight<80;
  log.replaceChildren();
  const messages=task?[...(task.options.conversation||[]),{role:'user',summary:task.options.request}]:[];
  if(task?.answer)messages.push({role:'assistant',summary:task.answer});
  else if(task&&!['running','queued','cancelling'].includes(task.status))messages.push({role:'assistant',summary:task.error||'本轮未能完成回答。可以开始新对话重试。'});
  for(const message of messages){const row=document.createElement('article');row.className='message '+(message.role==='user'?'user':'assistant');const label=document.createElement('strong');label.textContent=message.role==='user'?'你':'代码助手';const content=document.createElement('p');content.textContent=message.summary||'';row.append(label,content);log.append(row)}
  if(!messages.length){const empty=document.createElement('div');empty.className='empty-state';const title=document.createElement('strong');title.textContent='从一个代码问题开始';empty.append(title,document.createTextNode('选择项目，问问它的实现流程、错误原因或调用关系。'));log.append(empty)}
  const busy=task&&['running','queued','cancelling'].includes(task.status);
  document.querySelector('#send').disabled=Boolean(sending||busy||(task&&task.status!=='success'));
  document.querySelector('#chat-cancel').hidden=!task||!['running','queued'].includes(task.status);
  document.querySelector('#chat-status').textContent=task?chatStatus(task)+(task.status==='running'&&task.progress?' · '+task.progress:''):'';
  document.querySelector('#chat-hint').textContent=task&&task.status!=='success'&&!busy?'本轮未完成，点击“新对话”重新提问。':'Enter 发送 · Shift + Enter 换行';
  document.querySelector('#chat-details').textContent=task?JSON.stringify({budget:task.budget,citations:task.result?.citations,report:task.result?.final_report,error:task.error},null,2):'';
  if(nearBottom)log.scrollTop=log.scrollHeight;
}
history=async function(){
  const mode=viewMode,rows=await api('/api/tasks');if(mode!==viewMode)return;
  const el=document.querySelector('#history');el.replaceChildren();
  for(const t of rows.filter(t=>t.options?.mode===mode)){
    button(el,(t.options.request||t.id).slice(0,42)+' · '+(mode==='ask'?chatStatus(t):statusLabel(t.status)),()=>select(t.id));
    if(t.id===current)el.lastChild.className='active';
  }
  if(!el.childNodes.length)el.textContent=mode==='ask'?'还没有会话':'还没有修复任务';
};
refresh=async function(){
  const id=current,version=selectionVersion;if(!id)return;
  const task=await api('/api/tasks/'+id);if(id!==current||version!==selectionVersion)return;
  currentTask=task;
  if(task.options.mode==='ask'){renderChat(task);await history()}
  else await repairRefresh();
};
select=async function(id){
  const version=++selectionVersion;
  if(source){source.close();source=null}
  const task=await api('/api/tasks/'+id);if(version!==selectionVersion)return;
  current=id;currentTask=task;const mode=task.options.mode;
  selectedTasks[mode]=id;localStorage.setItem(mode==='ask'?'aicdChat':'aicdRepair',id);
  const projectChanged=field('repository').value!==task.options.repository;
  setMode(mode);field('repository').value=task.options.repository;
  if(projectChanged){document.querySelector('#chat-trust').checked=false;document.querySelector('[name=trusted_copy]').checked=false;fillOptions('allowed_files',task.options.allowed_files||[],'请重新选择文件夹加载候选文件');fillOptions('tests',task.options.tests||[],'请重新选择文件夹加载测试路径');}
  // 同一项目的连续追问沿用用户确认；切换仓库时由选择器清除授权。
  // 页面重新选中任务时先读持久快照，SSE 只触发刷新，不丢掉已有会话。
  source=new EventSource('/api/tasks/'+id+'/events');source.onmessage=()=>refresh().catch(showError);
  await refresh();
};
async function switchMode(mode){
  ++selectionVersion;if(source){source.close();source=null}current=null;currentTask=null;
  setMode(mode);document.querySelector('#error').textContent='';
  if(selectedTasks[mode])await select(selectedTasks[mode]);else{if(mode==='ask')renderChat(null);await history()}
}
document.querySelector('#nav-ask').onclick=()=>switchMode('ask').catch(showError);
document.querySelector('#nav-repair').onclick=()=>switchMode('repair').catch(showError);
document.querySelector('#new-chat').onclick=()=>{++selectionVersion;if(source){source.close();source=null}current=null;currentTask=null;selectedTasks.ask=null;localStorage.removeItem('aicdChat');document.querySelector('#question').value='';document.querySelector('#error').textContent='';renderChat(null);history().catch(showError)};
document.querySelector('#chat-form').onsubmit=async event=>{
  event.preventDefault();if(sending)return;
  const request=document.querySelector('#question').value.trim(),repository=field('repository').value;
  if(!request||!repository){showError(Error('请选择项目并填写问题'));return}
  if(!document.querySelector('#chat-trust').checked){showError(Error('请确认授权只读分析'));return}
  if(currentTask&&currentTask.status!=='success')return;
  const submittedVersion=selectionVersion;
  sending=true;renderChat(currentTask);document.querySelector('#error').textContent='';
  try{
    const next=currentTask?await api('/api/tasks/'+currentTask.id+'/followup',{approval_id:'followup',approve:true,feedback:request,...credentials()}):await api('/api/tasks',{repository,request,mode:'ask',trusted_copy:true,allowed_files:[],tests:[],image:'',...modelSettings(),...credentials()});
    // 用户等待时可能切换模式或新建会话，迟到响应不得抢回当前页面。
    if(submittedVersion!==selectionVersion){await history();return}
    document.querySelector('#question').value='';await select(next.task_id);
  }catch(e){showError(e)}finally{sending=false;if(viewMode==='ask')renderChat(currentTask)}
};
document.querySelector('#question').onkeydown=event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();if(!document.querySelector('#send').disabled)document.querySelector('#chat-form').requestSubmit()}};
document.querySelector('#chat-cancel').onclick=async()=>{try{if(current){await api('/api/tasks/'+current+'/cancel',{});await refresh()}}catch(e){showError(e)}};
document.querySelector('#form').onsubmit=async event=>{
  event.preventDefault();const submit=event.target.querySelector('[type=submit]');submit.disabled=true;
  try{
    const form=new FormData(event.target),data={...Object.fromEntries(form),repository:field('repository').value,...modelSettings(),...credentials(),mode:'repair',trusted_copy:form.has('trusted_copy')};
    for(const name of ['tests','allowed_files'])data[name]=form.getAll(name);
    if(!data.repository)throw Error('请先选择项目');
    if(data.allowed_files.length>8||data.tests.length>8)throw Error('源码和测试路径分别最多选择 8 项');
    if(!data.image)throw Error('请准备并选择本地 Docker 测试镜像');
    for(const name of ['max_iterations','timeout'])data[name]=Number(data[name]);
    const next=await api('/api/tasks',data);await select(next.task_id);
  }catch(e){showError(e)}finally{submit.disabled=false}
};
document.querySelector('#pick').onclick=async()=>{
  const pick=document.querySelector('#pick');pick.disabled=true;
  try{const data=await api('/api/repositories/select',{});if(data.cancelled)return;
    const changed=field('repository').value!==data.repository;field('repository').value=data.repository;
    fillOptions('allowed_files',data.allowed_files,'没有业务 Python 文件');fillOptions('tests',data.tests,'没有测试路径');
    if(data.tests.length)field('tests').value=data.tests.includes('tests')?'tests':data.tests[0];
    if(changed){document.querySelector('#chat-trust').checked=false;document.querySelector('[name=trusted_copy]').checked=false;++selectionVersion;if(source){source.close();source=null}current=null;currentTask=null;selectedTasks[viewMode]=null;localStorage.removeItem(viewMode==='ask'?'aicdChat':'aicdRepair');document.querySelector('#repair-detail').hidden=true;if(viewMode==='ask')renderChat(null);await history();}
    document.querySelector('#error').textContent='';
  }catch(e){showError(e)}finally{pick.disabled=false}
};
// 延迟至增强脚本完成注册，避免初始化时遗漏刷新扩展。
window.addEventListener('DOMContentLoaded',()=>{loadDefaults().catch(showError);switchMode(localStorage.getItem('aicdMode')==='repair'?'repair':'ask').catch(showError)});
