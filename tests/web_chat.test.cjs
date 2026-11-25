// 无第三方依赖：通过最小 DOM 替身验证页面交互与实际请求边界。
const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');

function harness(){
  class Element{
    constructor(){this.children=[];this.value='';this.checked=false;this.disabled=false;this.hidden=false;this.textContent='';this.scrollHeight=10;this.scrollTop=0;this.clientHeight=10}
    append(...nodes){this.children.push(...nodes)}
    replaceChildren(...nodes){this.children=nodes}
    setAttribute(name,value){this[name]=value}
    get childNodes(){return this.children}
    get lastChild(){return this.children.at(-1)}
  }
  const nodes=new Map(),get=key=>{if(!nodes.has(key))nodes.set(key,new Element());return nodes.get(key)};
  const tasks={},calls=[];let count=0,images=0;
  const context=vm.createContext({console,current:null,source:null,localStorage:{getItem:()=>null,setItem(){},removeItem(){}},
    window:{addEventListener(){}},document:{querySelector:get,createElement:()=>new Element(),createTextNode:text=>({textContent:text})},
    EventSource:class{close(){}},refresh:async()=>{},loadDefaults:async()=>{},loadImages:async()=>{images++},fillOptions(){},
    statusLabel:status=>status,showError:error=>{throw error},button:(el,label,fn)=>{const b=new Element();b.textContent=label;b.onclick=fn;el.append(b)},
    api:async(url,body)=>{
      calls.push({url,body});
      if(url==='/api/tasks'&&body){const id=String(++count);tasks[id]={id,status:'success',options:body,answer:'module.py:1 回答'};return {task_id:id}}
      if(url==='/api/tasks')return Object.values(tasks).reverse();
      if(url.endsWith('/followup')){const parent=tasks[url.split('/')[3]],id=String(++count);tasks[id]={id,status:'success',answer:'module.py:2 追问回答',options:{...parent.options,request:body.feedback,conversation:[...(parent.options.conversation||[]),{role:'user',summary:parent.options.request},{role:'assistant',summary:parent.answer}]}};return {task_id:id}}
      return tasks[url.split('/')[3]];
    }});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../web_runtime/chat.js'),'utf8'),context);
  get('[name="repository"]').value='repo';get('[name="max_tokens"]').value='4096';get('[name="context_chars"]').value='24000';get('#chat-trust').checked=true;
  return {context,get,calls,tasks,images:()=>images,run:code=>vm.runInContext(code,context)};
}

test('聊天首问与两次追问保持只读、双向记录及安全文本显示',async()=>{
  const h=harness();
  for(const question of ['解释代码','谁调用它？','<script>alert(1)</script>']){
    h.get('#question').value=question;await h.get('#chat-form').onsubmit({preventDefault(){}});
  }
  const create=h.calls.find(c=>c.url==='/api/tasks'&&c.body).body;
  assert.equal(create.mode,'ask');assert.equal(create.image,'');assert.equal(create.tests.length,0);assert.equal(create.allowed_files.length,0);
  assert.equal(h.calls.filter(c=>c.url.endsWith('/followup')).length,2);
  assert.equal(h.get('#messages').children.length,6);
  assert.equal(h.get('#messages').children[4].children[1].textContent,'<script>alert(1)</script>');
  assert.equal(h.get('#question').value,'');assert.equal(h.images(),0);
  assert.equal(h.get('#repair-panel').hidden,true);assert.equal(h.get('#repair-detail').hidden,true);
});

test('模式独立恢复，修复时才查询镜像，重复发送及失败状态受控',async()=>{
  const h=harness();
  await h.run("switchMode('ask')");assert.equal(h.images(),0);
  await h.run("switchMode('repair')");assert.equal(h.images(),1);assert.equal(h.get('#ask-panel').hidden,true);
  await h.run("switchMode('ask')");
  h.run('sending=true');h.get('#question').value='重复';await h.get('#chat-form').onsubmit({preventDefault(){}});
  assert.equal(h.calls.filter(c=>c.body).length,0);
  h.run("sending=false;renderChat({status:'blocked',options:{request:'问题'},error:'执行失败'})");
  assert.equal(h.get('#send').disabled,true);assert.equal(h.get('#chat-cancel').hidden,true);
  h.get('#new-chat').onclick();assert.equal(h.get('#send').disabled,false);
});

test('发送期间开启新对话，迟到响应不能覆盖新会话',async()=>{
  const h=harness();let finish;
  const original=h.context.api;
  h.context.api=async(url,body)=>url==='/api/tasks'&&body?new Promise(resolve=>{finish=resolve}):original(url,body);
  h.get('#question').value='旧问题';
  const pending=h.get('#chat-form').onsubmit({preventDefault(){}});
  h.get('#new-chat').onclick();h.get('#question').value='新问题';
  finish({task_id:'late'});await pending;
  assert.equal(h.run('current'),null);assert.equal(h.get('#question').value,'新问题');
});
