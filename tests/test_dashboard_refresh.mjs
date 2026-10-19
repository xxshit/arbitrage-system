import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const source=readFileSync(new URL('../static/app.js',import.meta.url),'utf8');
const tracking=source.slice(source.indexOf('const pageRefreshProfiles='),source.indexOf('const delistingWarningObserver='));
const networkHelper=source.slice(source.indexOf('async function fetchRefreshJson('),source.indexOf('let dashboardLoadingPromise=null;'));
const loader=networkHelper+source.slice(source.indexOf('let dashboardLoadingPromise=null;'),source.indexOf('function onchainDecimal'));

function harness(){
  let now=100000,nextId=0;
  const timers=new Map(),elements=new Map();
  const context={Date:class extends Date{static now(){return now}},AbortController,console,
    setTimeout(fn,delay){const id=++nextId;timers.set(id,{fn,at:now+delay});return id},
    clearTimeout(id){timers.delete(id)},setInterval(){return 0},clearInterval(){},
    activeViewId:()=>context.active,active:'dashboard',authState:{is_admin:false},
    renderAlertStreamStatus(){},alertNoticeMemory:new Map(),alertNotional:1000,alertSummaryVersion:0,latestAlertGroups:[],clearAlertCurrent(){},
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
  vm.createContext(context);vm.runInContext(tracking+networkHelper+source.slice(source.indexOf('function divergenceLabel('),source.indexOf('function spotTransferStatus(')),context);
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
  const rejected=assert.rejects(h.context.loadDashboard(),/超时/);
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

for(const [view,name] of [['spot-futures','loadSpotFutures'],['dual-futures','loadDualFutures'],['my-positions','loadHedgePositions'],['alerts','loadAlerts']]){
  test(`${view}: 5-second start cadence, independent of response duration`,async()=>{
    const h=harness(),starts=[];h.context.active=view;
    h.context[name]=()=>{starts.push(h.now());return new Promise(resolve=>h.context.setTimeout(resolve,2000))};
    h.run('installPageRefreshTracking()');await h.context.startConfiguredPageTimers();h.context[name]();await h.flush();await h.advance(16000);
    assert.deepEqual(starts,[100000,105000,110000,115000]);
  });
  test(`${view}: manual clicks coalesce and false return does not advance success time`,async()=>{
    const h=harness();h.context.active=view;let calls=0,resolve;
    h.context[name]=()=>{calls++;return new Promise(done=>{resolve=done})};
    h.run(`installPageRefreshTracking();pageRefreshState('${view}').completedAt=90000`);
    const first=h.context[name](),second=h.context[name]();assert.equal(calls,1);resolve(false);await Promise.all([first,second]);
    assert.equal(h.run(`pageRefreshState('${view}').completedAt`),90000);
    assert.equal(h.run(`pageRefreshState('${view}').failed`),true);
  });
}

test('all live view reads abort, clear timers and can recover after timeout',async()=>{
  const h=harness();h.context.fetch=(_url,{signal})=>new Promise((_resolve,reject)=>signal.addEventListener('abort',()=>reject(new Error('aborted'))));
  const rejected=assert.rejects(h.context.fetchRefreshJson('/api/alerts'),/超时/);await h.advance(10000);await rejected;
  assert.equal(h.timers.size,0);
  h.context.fetch=async()=>({ok:true,json:async()=>({checked_at_epoch:123})});
  assert.equal((await h.context.fetchRefreshJson('/api/alerts')).checked_at_epoch,123);
});

test('background alert refresh never overwrites another page indicator',()=>{
  const h=harness(),status=h.context.byId('updated');status.textContent='spot status';h.context.active='spot-futures';
  h.context.document.querySelector=()=>{throw new Error('background view must not touch the ring')};
  h.run(source.slice(source.indexOf('function renderPageRefreshIndicator('),source.indexOf('function beginPageRefresh(')));
  h.context.renderPageRefreshIndicator('alerts');assert.equal(status.textContent,'spot status');
});

for(const [name,start,end,lock,hasData,rowId] of [
  ['loadSpotFutures','async function loadSpotFutures(','function hedgeMetric(','spotFuturesLoading','spotFuturesHasData','spotFuturesRows'],
  ['loadDualFutures','async function loadDualFutures(','function addDualLiquidityHeader(','dualFuturesLoading','dualFuturesHasData','dualFuturesRows'],
])test(`${name}: real loader releases its lock after timeout without clearing good rows`,async()=>{
  const h=harness();Object.assign(h.context,{[lock]:false,[hasData]:true,spotFuturesPage:1,spotFuturesSearch:'',spotFundingInterval:'all',spotSort:{key:'open_spread',direction:'desc'},dualFuturesPage:1,dualFuturesSearch:'',dualSort:{key:'open_spread',direction:'desc'},dualIncludeBybitOkx:false});
  h.context.byId(rowId).innerHTML='last good rows';h.run(source.slice(source.indexOf(start),source.indexOf(end)));
  let requests=0;h.context.fetch=(_url,{signal})=>{requests++;return new Promise((_resolve,reject)=>signal.addEventListener('abort',()=>reject(new Error('aborted'))))};
  const pending=h.context[name]();assert.equal(h.context[lock],true);await h.advance(10000);assert.equal(await pending,false);
  assert.equal(h.context[lock],false);assert.equal(h.context.byId(rowId).innerHTML,'last good rows');
  const retry=h.context[name]();assert.equal(requests,2);await h.advance(10000);await retry;assert.equal(h.context[lock],false);
});

test('qualified candidates notify once while low-level retracement observations stay silent',async()=>{
  const h=harness();let sounds=0;
  Object.assign(h.context,{pruneAlertMutes(){},isAlertMuted:()=>false,alertsInitialized:false,alertSignature:'',alertAudibleSignature:'',renderAlertMasterDetail(){},replaceHtmlKeepingExchangeLogos(){},renderAlertMuteList(){},renderTipsItems(){},startAlertCountdown(){},playAlarmSound(){sounds++}});
  h.run(source.slice(source.indexOf('async function loadAlerts('),source.indexOf('function tokenHedgeList(')));
  let active=[];h.context.fetch=async()=>({ok:true,json:async()=>({active_events:active,events:[],basis_tracking:[],checked_at_epoch:100})});
  await h.context.loadAlerts();
  const event={id:10,symbol:'DEMO/USDT',audible:true,type:'rapid_spread',open_spread:1.5,basis:0,pulse:{notice_version:1,current:1.5}};
  active=[event];await h.context.loadAlerts();assert.equal(sounds,1);
  active=[{...event,pulse:{notice_version:1,current:1.2}}];await h.context.loadAlerts();assert.equal(sounds,1);
  active=[{...event,audible:false,pulse:{notice_version:2,current:.1,state:'retraced'}}];await h.context.loadAlerts();assert.equal(sounds,1);
  await h.context.loadAlerts();assert.equal(sounds,1);
  active=[{...event,id:11,open_spread:1.4,pulse:{notice_version:1,current:1.4}}];await h.context.loadAlerts();assert.equal(sounds,2);
});

test('early watch and a fully retraced spike never ring but a later candidate promotion can',async()=>{
  const h=harness();let sounds=0;
  Object.assign(h.context,{pruneAlertMutes(){},isAlertMuted:()=>false,alertsInitialized:false,alertSignature:'',alertAudibleSignature:'',renderAlertMasterDetail(){},replaceHtmlKeepingExchangeLogos(){},renderAlertMuteList(){},renderTipsItems(){},startAlertCountdown(){},playAlarmSound(){sounds++}});
  h.run(source.slice(source.indexOf('async function loadAlerts('),source.indexOf('function tokenHedgeList(')));
  let active=[];h.context.fetch=async()=>({ok:true,json:async()=>({active_events:active,events:[],basis_tracking:[],checked_at_epoch:100})});await h.context.loadAlerts();
  active=[{symbol:'DEMO/USDT',audible:false,notice_key:'round:start:watch'}];await h.context.loadAlerts();assert.equal(sounds,0);
  active=[{symbol:'DEMO/USDT',audible:false,notice_key:'round:retraced:record',pulse:{state:'retraced'}}];await h.context.loadAlerts();assert.equal(sounds,0);
  active=[{symbol:'DEMO/USDT',audible:true,notice_key:'round:restart:start:candidate'}];await h.context.loadAlerts();assert.equal(sounds,1);
});

test('late summary for old amount or an older same-amount request cannot overwrite fresh results',async()=>{
  const h=harness(),pending=[];let rendered='',clears=0;
  Object.assign(h.context,{pruneAlertMutes(){},isAlertMuted:()=>false,alertsInitialized:false,alertSignature:'',alertAudibleSignature:'',renderAlertMasterDetail(groups){rendered=groups[0]?.symbol},replaceHtmlKeepingExchangeLogos(){},renderAlertMuteList(){},renderTipsItems(){},startAlertCountdown(){},playAlarmSound(){},clearAlertCurrent(){clears++}});
  h.run(source.slice(source.indexOf('async function loadAlerts('),source.indexOf('function tokenHedgeList(')));
  h.context.fetch=()=>new Promise(resolve=>pending.push(resolve));
  const old=h.context.loadAlerts();h.context.alertNotional=2000;const fresh=h.context.loadAlerts();
  pending[1]({ok:true,json:async()=>({events:[{symbol:'FRESH'}],active_events:[],basis_tracking:[]})});await fresh;
  pending[0]({ok:false});await old;assert.equal(rendered,'FRESH');assert.equal(clears,0);
});

test('tracking-only alert changes render without causing repeat sound',async()=>{
  const h=harness();let renders=0,sounds=0,requests=0;
  Object.assign(h.context,{pruneAlertMutes(){},latestActiveAlertEvents:[],isAlertMuted:()=>false,alertsInitialized:false,alertSignature:'',alertAudibleSignature:'',renderAlertMasterDetail(){renders++},percent:v=>v.toFixed(1)+'%',strategyLabel:()=>'',replaceHtmlKeepingExchangeLogos:(element,html)=>{element.innerHTML=html},renderAlertMuteList(){},renderTipsItems(){},startAlertCountdown(){},playAlarmSound(){sounds++}});
  h.run(source.slice(source.indexOf('async function loadAlerts('),source.indexOf('function tokenHedgeList(')));
  let tracking={symbol:'AUDIT/USDT',strategy:'spot_futures',max_basis:1,last_level:1,max_at:'00:00:00'};
  h.context.fetch=async()=>{requests++;return {ok:true,json:async()=>({active_events:[],events:[],basis_tracking:[tracking],checked_at_epoch:100})}};
  await h.context.loadAlerts(true);tracking={...tracking,max_basis:1.4,last_level:1.4};await h.context.loadAlerts();
  assert.equal(requests,2);assert.equal(renders,2);assert.equal(sounds,0);assert.ok(h.context.byId('basisTracking').innerHTML.includes('1.4%'));
  h.context.fetch=async()=>({ok:false,json:async()=>({error:'unavailable'})});
  const valid=h.context.byId('basisTracking').innerHTML;assert.equal(await h.context.loadAlerts(),false);assert.equal(h.context.byId('basisTracking').innerHTML,valid);
});

test('one round sounds once per meaningful phase despite path switches and temporary absence',async()=>{
  const h=harness();let sounds=0;
  Object.assign(h.context,{pruneAlertMutes(){},isAlertMuted:()=>false,alertsInitialized:false,alertSignature:'',alertAudibleSignature:'',renderAlertMasterDetail(){},replaceHtmlKeepingExchangeLogos(){},renderAlertMuteList(){},renderTipsItems(){},startAlertCountdown(){},playAlarmSound(){sounds++}});
  h.run(source.slice(source.indexOf('async function loadAlerts('),source.indexOf('function tokenHedgeList(')));
  let active=[];h.context.fetch=async()=>({ok:true,json:async()=>({active_events:active,events:[],basis_tracking:[],checked_at_epoch:100})});
  await h.context.loadAlerts();
  const event={id:1,symbol:'DEMO/USDT',audible:true,notice_key:'pulse:1:start',pulse:{state:'watch',notice_version:1}};
  active=[event];await h.context.loadAlerts();assert.equal(sounds,1);
  active=[{...event,id:2}];await h.context.loadAlerts();assert.equal(sounds,1);
  active=[];await h.context.loadAlerts();active=[event];await h.context.loadAlerts();assert.equal(sounds,1);
  active=[{...event,notice_key:'pulse:1:retraced',pulse:{state:'retraced'}}];await h.context.loadAlerts();assert.equal(sounds,2);
  active=[{...event,id:3,notice_key:'pulse:1:restart:retraced',pulse:{state:'retraced',ordinal:2}}];await h.context.loadAlerts();assert.equal(sounds,3);
  active=[{...active[0],id:4,pulse:{state:'retraced',ordinal:3}}];await h.context.loadAlerts();assert.equal(sounds,3);
  active=[{...event,id:5,notice_key:'pulse:5:start'}];await h.context.loadAlerts();assert.equal(sounds,4);
});

test('data gap, isolated and settled pulses cannot ring while a genuine new round can',async()=>{
  const h=harness();let sounds=0;
  Object.assign(h.context,{pruneAlertMutes(){},isAlertMuted:()=>false,alertsInitialized:false,alertSignature:'',alertAudibleSignature:'',renderAlertMasterDetail(){},replaceHtmlKeepingExchangeLogos(){},renderAlertMuteList(){},renderTipsItems(){},startAlertCountdown(){},playAlarmSound(){sounds++}});
  h.run(source.slice(source.indexOf('async function loadAlerts('),source.indexOf('function tokenHedgeList(')));
  let active=[];h.context.fetch=async()=>({ok:true,json:async()=>({active_events:active,events:[],basis_tracking:[],checked_at_epoch:100})});
  await h.context.loadAlerts();
  for(const state of ['data_gap','isolated','settled']){active=[{id:1,symbol:'DEMO/USDT',notice_key:`pulse:1:${state}`,pulse:{state}}];await h.context.loadAlerts()}
  assert.equal(sounds,0);
  active=[{id:2,symbol:'DEMO/USDT',audible:true,notice_key:'pulse:2:start',pulse:{state:'watch'}}];await h.context.loadAlerts();assert.equal(sounds,1);
});

test('wallet metadata changes refresh the panel without replaying unchanged active alert sounds',async()=>{
  const h=harness();let renders=0,sounds=0,state='unknown';
  Object.assign(h.context,{pruneAlertMutes(){},latestActiveAlertEvents:[],isAlertMuted:()=>false,alertsInitialized:false,alertSignature:'',alertAudibleSignature:'',renderAlertMasterDetail(){renders++},percent:v=>v.toFixed(1)+'%',strategyLabel:()=>'',replaceHtmlKeepingExchangeLogos:(element,html)=>{element.innerHTML=html},renderAlertMuteList(){},renderTipsItems(){},startAlertCountdown(){},playAlarmSound(){sounds++}});
  h.run(source.slice(source.indexOf('async function loadAlerts('),source.indexOf('function tokenHedgeList(')));
  const active={symbol:'AUDIT/USDT',type:'rapid_basis',open_spread:1,basis:1,created_at:'10-18 10:00:00'};
  h.context.fetch=async()=>({ok:true,json:async()=>({active_events:[active],events:[{symbol:active.symbol,transfer_statuses:[{exchange:'Gate',state}]}],basis_tracking:[],checked_at_epoch:100})});
  await h.context.loadAlerts(true);state='fresh';await h.context.loadAlerts();assert.equal(renders,2);assert.equal(sounds,0);
});

test('removing an isolated alert does not replay the surviving alert, while a new alert still sounds',async()=>{
  const h=harness();let sounds=0;
  Object.assign(h.context,{pruneAlertMutes(){},latestActiveAlertEvents:[],isAlertMuted:()=>false,alertsInitialized:false,alertSignature:'',alertAudibleSignature:'',renderAlertMasterDetail(){},replaceHtmlKeepingExchangeLogos(){},renderAlertMuteList(){},renderTipsItems(){},startAlertCountdown(){},playAlarmSound(){sounds++}});
  h.run(source.slice(source.indexOf('async function loadAlerts('),source.indexOf('function tokenHedgeList(')));
  const healthy={symbol:'HEALTHY/USDT',audible:true,type:'rapid_basis',open_spread:1,basis:1,created_at:'10-18 10:00:00'},isolated={...healthy,symbol:'ISOLATED/USDT'};
  let active=[healthy,isolated];h.context.fetch=async()=>({ok:true,json:async()=>({active_events:active,events:[],basis_tracking:[],checked_at_epoch:100})});
  await h.context.loadAlerts(true);active=[healthy];await h.context.loadAlerts();assert.equal(sounds,0);
  active=[{...healthy,basis:2}];await h.context.loadAlerts();assert.equal(sounds,1);
});
