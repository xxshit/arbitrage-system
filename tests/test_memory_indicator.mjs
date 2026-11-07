import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
const app=readFileSync(new URL('../static/app.js',import.meta.url),'utf8');
const source=app.slice(app.indexOf('// Website service memory,'),app.indexOf('// End website memory indicator.'));
function harness(){
  const nodes=new Map(),timers=new Map(),listeners=new Map();let next=0,calls=0;
  class Element{
    constructor(){this.style={setProperty:(k,v)=>this.styles[k]=v};this.styles={};this.dataset={};this.attributes={};this.children=[];this.hidden=false}
    set id(value){this.key=value;nodes.set(value,this)} get id(){return this.key}
    set innerHTML(html){for(const [,id] of html.matchAll(/id="([^"]+)"/g)){const node=new Element();node.id=id}}
    prepend(child){this.children.unshift(child)}
    setAttribute(k,v){this.attributes[k]=v} removeAttribute(k){delete this.attributes[k]}
  }
  const header=new Element();
  const c={AbortController,authState:{is_admin:true},byId:id=>nodes.get(id),
    setTimeout(fn,delay){const id=++next;timers.set(id,{fn,delay});return id},clearTimeout(id){timers.delete(id)},
    document:{hidden:false,createElement:()=>new Element(),querySelector:()=>header,addEventListener:(key,fn)=>listeners.set(key,fn)},
    fetch:async()=>{calls++;return {ok:true,status:200,json:async()=>({cgroup_bytes:512,memory_limit_bytes:1024,memory_high_bytes:900})}},
    addEventListener:(key,fn)=>listeners.set(key,fn)};
  c.window=c;vm.createContext(c);vm.runInContext(source,c);
  return {c,nodes,timers,listeners,header,calls:()=>calls,run:code=>vm.runInContext(code,c),flush:async()=>{for(let i=0;i<12;i++)await Promise.resolve()}};
}
test('remaining is service current versus its configured limit, not worker RSS',()=>{
  const h=harness(),m=h.c.memoryIndicatorModel({cgroup_bytes:768,memory_limit_bytes:1024,rss_bytes:1});
  assert.equal(m.remaining,256);assert.equal(m.percent,25);assert.equal(m.percentLabel,'25%');
  assert.equal(h.c.memoryIndicatorModel({cgroup_bytes:1500,memory_limit_bytes:1024}).percent,0);
  assert.equal(h.c.memoryIndicatorModel({cgroup_bytes:0,memory_limit_bytes:1024}).percent,100);
  assert.equal(h.c.memoryIndicatorModel({cgroup_bytes:999999,memory_limit_bytes:1000000}).percentLabel,'<0.1%');
});
test('missing, unlimited and malformed measurements never display a healthy percentage',()=>{
  const h=harness();
  for(const limit of [undefined,null,'max',0,-1,Infinity,NaN,'1024',Number.MAX_SAFE_INTEGER+1])assert.equal(h.c.memoryIndicatorModel({cgroup_bytes:100,memory_limit_bytes:limit}),null);
  for(const used of [undefined,null,-1,'0',Infinity,NaN])assert.equal(h.c.memoryIndicatorModel({cgroup_bytes:used,memory_limit_bytes:1024}),null);
});
test('high-water pressure is visible even with significant remaining hard-limit capacity',()=>{
  const h=harness();
  assert.equal(h.c.memoryIndicatorModel({cgroup_bytes:1000,memory_limit_bytes:1500,memory_high_bytes:1000}).severity,'critical');
  assert.equal(h.c.memoryIndicatorModel({cgroup_bytes:950,memory_limit_bytes:1500,memory_high_bytes:1000}).severity,'warning');
  assert.equal(h.c.memoryIndicatorModel({cgroup_bytes:700,memory_limit_bytes:1500,memory_high_bytes:1000}).severity,'normal');
});
test('one widget and one independent timer survive repeated auth refreshes',async()=>{
  const h=harness();h.c.syncMemoryIndicator();h.c.syncMemoryIndicator();await h.flush();h.c.syncMemoryIndicator();
  assert.equal(h.header.children.length,1);assert.equal(h.calls(),1);assert.equal(h.timers.size,1);
  assert.equal([...h.timers.values()][0].delay,15000);assert.equal(h.nodes.get('memoryRemainingPercent').textContent,'50%');
  assert.equal(h.nodes.get('memoryRemainingRing').attributes['aria-valuenow'],'50');
});
test('an in-flight request never overlaps; timeout clears stale percentage',async()=>{
  const h=harness();let calls=0;
  h.c.fetch=(_url,{signal,cache,redirect})=>{calls++;assert.equal(cache,'no-store');assert.equal(redirect,'error');return new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(new Error('aborted'))))};
  h.c.syncMemoryIndicator();h.c.pollMemoryIndicator();assert.equal(calls,1);
  h.c.renderMemoryIndicator({cgroup_bytes:500,memory_limit_bytes:1000});
  [...h.timers.values()].find(x=>x.delay===5000).fn();await h.flush();
  assert.equal(h.nodes.get('memoryRemainingPercent').textContent,'—');
  assert.equal(h.nodes.get('memoryRemainingRing').attributes['aria-valuenow'],undefined);
  assert.equal(h.timers.size,1);assert.equal([...h.timers.values()][0].delay,15000);
});
test('hidden tabs stop polling and invalidate the sample, returning tabs request fresh data',async()=>{
  const h=harness();h.c.syncMemoryIndicator();await h.flush();
  h.c.document.hidden=true;h.listeners.get('visibilitychange')();
  assert.equal(h.timers.size,0);assert.equal(h.nodes.get('memoryRemainingPercent').textContent,'—');
  await h.c.pollMemoryIndicator();assert.equal(h.calls(),1);
  h.c.document.hidden=false;h.listeners.get('visibilitychange')();await h.flush();assert.equal(h.calls(),2);
});
test('viewers never create or request the admin-only widget, role revocation stops it',async()=>{
  const h=harness();h.c.authState.is_admin=false;h.c.syncMemoryIndicator();assert.equal(h.calls(),0);assert.equal(h.header.children.length,0);
  h.c.authState.is_admin=true;h.c.syncMemoryIndicator();await h.flush();h.c.authState.is_admin=false;h.c.syncMemoryIndicator();
  assert.equal(h.timers.size,0);assert.equal(h.nodes.get('memoryIndicator').hidden,true);
});
test('HTTP failures clear readings; authentication failures also stop retries',async()=>{
  for(const status of [500,401,403]){
    const h=harness();h.c.fetch=async()=>({ok:false,status});h.c.syncMemoryIndicator();await h.flush();
    assert.equal(h.nodes.get('memoryRemainingPercent').textContent,'—');
    if(status!==500){assert.equal(h.nodes.get('memoryIndicator').hidden,true);assert.equal(h.timers.size,0)}
  }
});
