import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const source=readFileSync(new URL('../static/app.js',import.meta.url),'utf8');
const tracking=source.slice(source.indexOf('const pageRefreshProfiles='),source.indexOf('const delistingWarningObserver='));
const loader=source.slice(source.indexOf('let dashboardLoadingPromise=null;'),source.indexOf('function onchainDecimal'));

function harness(){
  let now=100000,nextId=0;
  const timers=new Map(),elements=new Map();
  const context={Date:class extends Date{static now(){return now}},AbortController,console,
    setTimeout(fn,delay){const id=++nextId;timers.set(id,{fn,at:now+delay});return id},
    clearTimeout(id){timers.delete(id)},setInterval(){return 0},clearInterval(){},
    activeViewId:()=>context.active,active:'dashboard',authState:{is_admin:false},
    document:{querySelectorAll:()=>[]},
    byId(id){if(!elements.has(id))elements.set(id,{textContent:''});return elements.get(id)},
    fetch:async()=>({ok:true,json:async()=>({items:[]})}),
  };
  context.window=context;
  // Every unrelated page callback is inert; real dashboard tracking and scheduling run unchanged.
  for(const match of tracking.matchAll(/names:\[([^\]]*)\]/g)){
    for(const name of match[1].matchAll(/'([^']+)'/g))context[name[1]]=async()=>{};
  }
  context.loadDelistedMarketSymbols=async()=>{};
  vm.createContext(context);vm.runInContext(tracking,context);
  vm.runInContext('renderPageRefreshIndicator=()=>{};',context);
  const flush=async()=>{for(let i=0;i<15;i++)await Promise.resolve()};
  return {context,timers,flush,now:()=>now,run:code=>vm.runInContext(code,context),async advance(ms){
    const target=now+ms;
    for(;;){
      const entry=[...timers].filter(([,timer])=>timer.at<=target).sort((a,b)=>a[1].at-b[1].at)[0];
      if(!entry)break;
      now=entry[1].at;timers.delete(entry[0]);entry[1].fn();await flush();
    }
    now=target;await flush();
  }};
}

test('5-second start-to-start cadence does not add the 2-second response time',async()=>{
  const h=harness(),starts=[];
  h.context.loadDashboard=()=>{starts.push(h.now());return new Promise(resolve=>h.context.setTimeout(resolve,2000))};
  h.run('installPageRefreshTracking()');await h.context.startConfiguredPageTimers();
  h.context.loadDashboard();await h.flush();await h.advance(16000);
  assert.deepEqual(starts,[100000,105000,110000,115000]);
});

test('slow responses never overlap and retry without another full 5-second wait',async()=>{
  const h=harness(),starts=[];let active=0,maxActive=0;
  h.context.loadDashboard=()=>{starts.push(h.now());maxActive=Math.max(maxActive,++active);return new Promise(resolve=>h.context.setTimeout(()=>{active--;resolve()},7000))};
  h.run('installPageRefreshTracking()');await h.context.startConfiguredPageTimers();
  h.context.loadDashboard();await h.flush();await h.advance(16000);
  assert.equal(maxActive,1);assert.deepEqual(starts,[100000,107250,114500]);
});

test('failed request preserves last successful and source timestamps',async()=>{
  const h=harness();h.context.loadDashboard=async()=>{throw new Error('upstream unavailable')};
  h.run('installPageRefreshTracking();pageRefreshState("dashboard").completedAt=90000;pageRefreshState("dashboard").sourceUpdatedAt=85000');
  await assert.rejects(h.context.loadDashboard());
  assert.equal(h.run('pageRefreshState("dashboard").completedAt'),90000);
  assert.equal(h.run('pageRefreshState("dashboard").sourceUpdatedAt'),85000);
  assert.equal(h.run('pageRefreshState("dashboard").failed'),true);
});

test('dashboard loader coalesces simultaneous clicks and clears on error',async()=>{
  const h=harness();h.run(loader);let calls=0,rejectFetch;
  h.context.fetch=()=>{calls++;return new Promise((resolve,reject)=>{rejectFetch=reject})};
  const first=h.context.loadDashboard(),second=h.context.loadDashboard();
  rejectFetch(new Error('network down'));
  const results=await Promise.allSettled([first,second]);
  assert.equal(calls,1);assert.equal(results[0].status,'rejected');assert.equal(results[1].status,'rejected');
  assert.equal(h.run('dashboardLoadingPromise'),null);
  assert.equal(h.timers.size,0);
});

test('stuck dashboard fetch aborts at 10 seconds and allows the next request',async()=>{
  const h=harness();h.run(loader);
  h.context.fetch=(_url,{signal,cache})=>{assert.equal(cache,'no-store');return new Promise((_resolve,reject)=>signal.addEventListener('abort',()=>reject(new Error('aborted'))))};
  const rejected=assert.rejects(h.context.loadDashboard(),/aborted/);
  await h.advance(10000);await rejected;
  assert.equal(h.run('dashboardLoadingPromise'),null);
});

test('inactive dashboard does not accumulate requests or poll at 250ms',async()=>{
  const h=harness();h.run('installPageRefreshTracking();pageRefreshState("dashboard").startedAt=90000');
  h.context.active='spot-futures';await h.context.startConfiguredPageTimers();
  const controller=h.run('pageRefreshControllers.get("dashboard")');
  assert.equal(h.timers.get(controller.timer).at-h.now(),5000);
});

test('indicator displays real source time and ages it even without another response',async()=>{
  const h=harness(),ring={classList:{add(){},toggle(){}},style:{setProperty(){}},setAttribute(){}};
  h.context.document.querySelector=()=>ring;
  h.run(source.slice(source.indexOf('function renderPageRefreshIndicator('),source.indexOf('function beginPageRefresh(')));
  h.run('Object.assign(pageRefreshState("dashboard"),{completedAt:100000,sourceUpdatedAt:90000,sourceStale:false})');
  h.context.renderPageRefreshIndicator();
  const status=h.context.byId('updated');
  assert.ok(status.textContent.includes(new Date(90000).toLocaleTimeString('zh-CN',{hour12:false})));
  assert.ok(!status.textContent.includes('行情延迟'));
  await h.advance(6000);h.context.renderPageRefreshIndicator();
  assert.ok(status.textContent.includes('行情延迟'));
});
