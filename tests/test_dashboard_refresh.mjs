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
    document:{querySelectorAll:()=>[]},applyGroupFrameClasses(){},
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
  vm.runInContext(source.slice(source.indexOf('class MarketBrowseOrder'),source.indexOf('// End stable browsing helpers.')),context);
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

for(const [view,name] of [['spot-futures','loadSpotFutures'],['dual-futures','loadDualFutures'],['equity-arbitrage','loadEquityArbitrage'],['my-positions','loadHedgePositions'],['alerts','loadAlerts']]){
  const seconds=view==='equity-arbitrage'?15:5;
  test(`${view}: ${seconds}-second start cadence, independent of response duration`,async()=>{
    const h=harness(),starts=[];h.context.active=view;
    h.context[name]=()=>{starts.push(h.now());return new Promise(resolve=>h.context.setTimeout(resolve,2000))};
    h.run('installPageRefreshTracking()');await h.context.startConfiguredPageTimers();h.context[name]();await h.flush();await h.advance(seconds*3000+1000);
    assert.deepEqual(starts,[0,1,2,3].map(i=>100000+i*seconds*1000));
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

test('equity 15-second polling leaves background alerts at 5 seconds',async()=>{
  const h=harness(),equity=[],alerts=[];h.context.active='equity-arbitrage';
  h.context.loadEquityArbitrage=async()=>{equity.push(h.now())};
  h.context.loadAlerts=async()=>{alerts.push(h.now())};
  h.run('installPageRefreshTracking()');await h.context.startConfiguredPageTimers();
  await h.context.loadEquityArbitrage();await h.advance(30000);
  assert.deepEqual(equity,[100000,115000,130000]);
  assert.deepEqual(alerts,[105000,110000,115000,120000,125000,130000]);
});

test('equity ring uses 15 seconds and resets after completion without adding countdown text',async()=>{
  const h=harness(),properties={};h.context.active='equity-arbitrage';
  const ring={classList:{add(){},toggle(){}},style:{setProperty(key,value){properties[key]=value}},setAttribute(){}};
  h.context.document.querySelector=()=>ring;
  h.run(source.slice(source.indexOf('function renderPageRefreshIndicator('),source.indexOf('function beginPageRefresh(')));
  h.run('Object.assign(pageRefreshState("equity-arbitrage"),{startedAt:100000,cycleStartedAt:100000})');
  await h.advance(7500);h.context.renderPageRefreshIndicator();
  assert.equal(properties['--refresh-angle'],'180deg');assert.ok(ring.title.includes('间隔 15秒'));
  assert.ok(!h.context.byId('updated').textContent.includes('秒'));
  h.context.beginPageRefresh('equity-arbitrage');assert.equal(properties['--refresh-angle'],'360deg');
  h.context.finishPageRefresh('equity-arbitrage');assert.equal(properties['--refresh-angle'],'0deg');
});

test('inactive equity stops polling and a manual refresh is immediate, not delayed 15 seconds',async()=>{
  const h=harness(),starts=[];h.context.active='equity-arbitrage';
  h.context.loadEquityArbitrage=async()=>{starts.push(h.now())};
  h.run('installPageRefreshTracking()');await h.context.startConfiguredPageTimers();
  await h.context.loadEquityArbitrage();await h.advance(5000);await h.context.loadEquityArbitrage();
  await h.advance(14999);assert.deepEqual(starts,[100000,105000]);
  await h.advance(1);assert.deepEqual(starts,[100000,105000,120000]);
  h.context.active='dashboard';await h.advance(30000);assert.equal(starts.length,3);
});

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

function spotLoaderHarness(){
  const h=harness(),requests=[],renders=[];
  const getElement=h.context.byId;
  h.context.byId=id=>{
    assert.ok(!['spotFuturesUpdated','spotFuturesCountdown'].includes(id),'removed footer must not be accessed');
    return getElement(id);
  };
  Object.assign(h.context,{active:'spot-futures',spotFuturesLoading:false,spotFuturesHasData:true,
    spotFuturesPage:1,spotFuturesSearch:'',spotFundingInterval:'all',spotSort:{key:'open_spread',direction:'desc'},
    spotSortLabels:{open_spread:'开差',close_spread:'平差'},spotRows:groups=>groups.map(group=>group.symbol).join(','),
    destroyFloatingPopup(){},updateSortHeaders(){},escapeChatHtml:String,
    replaceHtmlKeepingExchangeLogos(target,markup){target.innerHTML=markup;renders.push(markup)}});
  h.context.byId('spotFuturesRows').innerHTML='previous rows';
  h.context.fetch=url=>new Promise((resolve,reject)=>requests.push({url,resolve:data=>resolve({ok:true,json:async()=>data}),reject}));
  h.run(source.slice(source.indexOf('async function loadSpotFutures('),source.indexOf('function hedgeMetric(')));
  h.run('installPageRefreshTracking()');
  const payload=symbol=>({symbols:[{symbol,rows:[{long_exchange:'Gate',open_spread:.5}]}],total_symbols:1,page:1,pages:1,updated_at:'12:00:00',captured_at_epoch:100,errors:{}});
  return {...h,requests,renders,payload};
}

test('spot direction switch discards pending old order and immediately requests the latest direction',async()=>{
  const h=spotLoaderHarness(),first=h.context.loadSpotFutures();
  h.context.spotSort.direction='asc';const coalesced=h.context.loadSpotFutures(true);
  assert.equal(first,coalesced);assert.equal(h.requests.length,1);
  h.requests[0].resolve(h.payload('old-desc'));assert.equal(await first,false);
  assert.deepEqual(h.renders,[]);await h.advance(0);
  assert.equal(h.requests.length,2);assert.ok(h.requests[1].url.endsWith('sort_direction=asc'));
  h.requests[1].resolve(h.payload('new-asc'));await h.flush();
  assert.deepEqual(h.renders,['new-asc']);assert.equal(h.run('pageRefreshState("spot-futures").sourceUpdatedAt'),100000);
  assert.equal(h.run('pageRefreshState("spot-futures").inFlight'),0);
});

test('spot rapid sort and filter changes coalesce to the latest complete request',async()=>{
  const h=spotLoaderHarness(),first=h.context.loadSpotFutures();
  h.context.spotSort={key:'close_spread',direction:'asc'};h.context.loadSpotFutures(true);
  h.context.spotSort.direction='desc';h.context.spotFuturesPage=2;h.context.spotFuturesSearch='DEMO/USDT';
  h.context.spotFundingInterval='4H';h.context.byId('binanceSpotOnly').checked=true;h.context.byId('binanceAlphaOnly').checked=true;
  h.context.loadSpotFutures(true);h.requests[0].resolve(h.payload('old'));await first;await h.advance(0);
  assert.equal(h.requests.length,2);const url=new URL(h.requests[1].url,'http://preview');
  for(const [key,value] of Object.entries({page:'2',symbol:'DEMO/USDT',sort_by:'close_spread',sort_direction:'desc',funding_interval:'4H',binance_spot_only:'1',binance_alpha_only:'1'}))assert.equal(url.searchParams.get(key),value);
  h.requests[1].resolve(h.payload('latest'));await h.flush();assert.deepEqual(h.renders,['latest']);
});

test('spot superseded failure cannot overwrite current status or block a fresh sort request',async()=>{
  const h=spotLoaderHarness(),first=h.context.loadSpotFutures();
  h.context.spotSort.direction='asc';h.run('pageRefreshState("spot-futures").sourceUpdatedAt=85000');
  h.requests[0].reject(new Error('old request failed'));assert.equal(await first,false);
  assert.equal(h.run('pageRefreshState("spot-futures").sourceUpdatedAt'),85000);
  await h.advance(0);assert.equal(h.requests.length,2);
  h.requests[1].resolve(h.payload('recovered'));await h.flush();assert.deepEqual(h.renders,['recovered']);
});

test('spot unchanged request renders server order once without a redundant follow-up',async()=>{
  const h=spotLoaderHarness(),first=h.context.loadSpotFutures();
  const data=h.payload('ranked-first');data.symbols.push({symbol:'ranked-second',rows:[{long_exchange:'Bitget'}]});
  h.requests[0].resolve(data);assert.equal(await first,true);await h.advance(0);
  assert.deepEqual(h.renders,['ranked-first,ranked-second']);assert.equal(h.requests.length,1);
  assert.ok(!source.includes('function groupSortValue('));assert.ok(!source.includes('function sortGroups('));
});

test('spot current request failure retains previous rows without automatic retry burst',async()=>{
  const h=spotLoaderHarness(),first=h.context.loadSpotFutures();
  h.requests[0].reject(new Error('network failed'));assert.equal(await first,false);await h.advance(0);
  assert.equal(h.context.byId('spotFuturesRows').innerHTML,'previous rows');assert.equal(h.requests.length,1);
  assert.equal(h.run('pageRefreshState("spot-futures").failed'),true);
});

function browseHarness(scope){
  const h=spotLoaderHarness(),dual=scope==='dual';
  if(dual){
    Object.assign(h.context,{active:'dual-futures',dualFuturesPage:1,dualFuturesLoading:false,dualFuturesHasData:true,
      dualFuturesSearch:'',dualSort:{key:'open_spread',direction:'desc'},dualIncludeBybitOkx:false,
      dualSortLabels:{open_spread:'开差'},updateDualSortHeaders(){},showDualRefreshCountdown(){},dualRowsCompact:groups=>groups.map(group=>group.symbol).join(',')});
    h.run(source.slice(source.indexOf('async function loadDualFutures('),source.indexOf('function addDualLiquidityHeader(')));
  }
  const name=dual?'loadDualFutures':'loadSpotFutures',state=()=>h.run(`marketBrowseStates.${scope}`);
  const group=(symbol,price,venues=dual?['Bybit','OKX']:['Gate','Bitget'])=>({symbol,rows:venues.map((venue,i)=>({long_exchange:venue,short_exchange:'Binance',open_spread:price+i,close_spread:price-i}))});
  const data=(groups,observed)=>({...h.payload('unused'),symbols:groups,total_symbols:60,pages:2,...(observed?{observed_symbols:observed}:{})});
  const resolve=async(payload)=>{h.requests.at(-1).resolve(payload);await h.flush()};
  h.run('installPageRefreshTracking()');
  return {...h,scope,state,group,data,resolve,load:()=>h.context[name](),lock:()=>h.context.lockMarketBrowse(scope),resort:()=>h.context.resortMarket(scope)};
}

for(const scope of ['spot','dual']){
  test(`${scope}: browsing pins coin membership and paths while off-page quotes and risk metadata update`,async()=>{
    const h=browseHarness(scope),{group,data}=h;
    h.load();await h.resolve(data([group('A',1),group('B',2)]));h.lock();
    const freshA=group('A',91);freshA.rows.reverse();freshA.rows[0].price_divergence={state:'confirmed'};freshA.rows.push({long_exchange:'New venue',short_exchange:'Binance',open_spread:500});
    h.load();assert.deepEqual(JSON.parse(new URL(h.requests.at(-1).url,'http://test').searchParams.get('observed_symbols')),['A','B']);
    await h.resolve(data([group('C',100),group('D',101)],[group('B',92),freshA]));
    assert.equal(h.renders.at(-1),'A,B');assert.equal(h.state().displayed[0].rows[0].open_spread,91);
    assert.equal(h.state().displayed[0].rows[1].price_divergence.state,'confirmed');assert.equal(h.state().displayed[0].rows.length,2);
    assert.equal(h.state().changed,true);assert.equal(h.state().locked,true);
    if(scope==='spot')assert.equal(h.run('spotOpportunityByKey.get("A|Gate").row.open_spread'),91);
  });
  test(`${scope}: starting to browse during an in-flight ranking refresh rejects it and fetches watched coins`,async()=>{
    const h=browseHarness(scope);h.load();await h.resolve(h.data([h.group('A',1)]));
    const pending=h.load();h.lock();await h.resolve(h.data([h.group('B',2)]));assert.equal(await pending,false);
    assert.equal(h.renders.at(-1),'A');await h.advance(0);assert.equal(h.requests.length,3);
    assert.ok(h.requests.at(-1).url.includes('observed_symbols='));await h.resolve(h.data([h.group('B',2)],[h.group('A',3)]));
    assert.equal(h.state().displayed[0].rows[0].open_spread,3);
  });
  test(`${scope}: re-sort discards a pending pinned response, applies latest ranking once and stays fixed`,async()=>{
    const h=browseHarness(scope);h.load();await h.resolve(h.data([h.group('A',1)]));h.lock();
    h.load();h.resort();await h.resolve(h.data([h.group('B',2)],[h.group('A',3)]));await h.advance(0);
    assert.ok(!h.requests.at(-1).url.includes('observed_symbols='));await h.resolve(h.data([h.group('B',4)]));
    assert.equal(h.renders.at(-1),'B');assert.equal(h.state().locked,true);assert.equal(h.state().changed,false);
    await h.advance(0);assert.equal(h.requests.length,3);
  });
  test(`${scope}: missing paths are placeholders, not cached numbers; recovery reuses original slot`,async()=>{
    const h=browseHarness(scope);h.load();await h.resolve(h.data([h.group('A',1),h.group('B',2)]));h.lock();
    const partial=h.group('A',9);partial.rows.pop();h.load();await h.resolve(h.data([partial],[partial]));
    const shown=h.state().displayed;assert.equal(shown[0].rows[1]._unavailable,true);assert.equal(shown[0].rows[1].open_spread,undefined);
    assert.equal(shown[1].rows[0]._unavailable,true);assert.equal(h.state().missing,3);
    if(scope==='spot'){assert.equal(h.run('spotOpportunityByKey.has("A|Bitget")'),false);assert.equal(h.run('spotOpportunityByKey.has("B|Gate")'),false)}
    h.load();await h.resolve(h.data([h.group('B',6)],[h.group('B',6),h.group('A',7)]));
    assert.equal(h.renders.at(-1),'A,B');assert.equal(h.state().displayed[1].rows[0].open_spread,6);assert.equal(h.state().missing,0);
  });
  test(`${scope}: failure or a missing watched payload cannot mark old quotes fresh`,async()=>{
    const h=browseHarness(scope);h.load();await h.resolve(h.data([h.group('A',1)]));h.lock();
    const pending=h.load();await h.resolve(h.data([h.group('B',2)]));assert.equal(await pending,false);
    assert.equal(h.renders.at(-1),'A');assert.equal(h.state().displayed[0].rows[0].open_spread,1);
    await h.advance(0);assert.equal(h.requests.length,2);
  });
  test(`${scope}: explicit context changes clear lock and old ABA responses cannot render`,async()=>{
    const h=browseHarness(scope);h.load();await h.resolve(h.data([h.group('A',1)]));h.lock();
    h.load();h.context.resetMarketBrowse(scope);h.context.resetMarketBrowse(scope);
    await h.resolve(h.data([h.group('B',2)],[h.group('A',3)]));await h.advance(0);
    assert.ok(!h.requests.at(-1).url.includes('observed_symbols='));await h.resolve(h.data([h.group('C',4)]));
    assert.equal(h.renders.at(-1),'C');assert.equal(h.state().locked,false);
    const pageVar=scope==='spot'?'spotFuturesPage':'dualFuturesPage';h.context[pageVar]=2;h.load();
    const page=h.data([h.group('D',5)]);page.page=2;await h.resolve(page);h.lock();assert.equal(h.state().page,2);
  });
  test(`${scope}: locked real loader continues five-second cadence and does not unlock while idle`,async()=>{
    const h=browseHarness(scope);h.load();await h.resolve(h.data([h.group('A',1)]));h.lock();
    // Start only this live view; unrelated profiles remain inert.
    const fetch=h.context.fetch;h.context.fetch=(url,...args)=>url==='/api/runtime-rules'?Promise.resolve({ok:true,json:async()=>({items:[]})}):fetch(url,...args);
    await h.context.startConfiguredPageTimers();
    for(let step=1;step<=3;step++){
      await h.advance(5000);assert.ok(h.requests.at(-1).url.includes('observed_symbols='));
      await h.resolve(h.data([h.group('B',step)],[h.group('A',step+10)]));
      assert.equal(h.state().displayed[0].rows[0].open_spread,step+10);assert.equal(h.state().locked,true);
    }
    assert.equal(h.requests.length,4);
  });
}

test('both scanner states are independent and all explicit filter/page/sort handlers reset the proper lock',()=>{
  const h=browseHarness('dual');h.run('marketBrowseStates.spot.locked=true;marketBrowseStates.dual.locked=true;resetMarketBrowse("spot")');
  assert.equal(h.run('marketBrowseStates.spot.locked'),false);assert.equal(h.state().locked,true);
  for(const [scope,names] of Object.entries({spot:['setSpotSortFromPicker','toggleSpotSortDirection','setSpotSort','changeSpotFuturesPage','selectSpotFuturesPage','setBinanceSpotFilter','setBinanceAlphaFilter','setSpotFundingInterval','setSpotFuturesSearch'],dual:['setDualSortFromPicker','toggleDualSortDirection','setDualSort','setDualIncludeBybitOkx','changeDualFuturesPage','selectDualFuturesPage','setDualFuturesSearch']})){
    for(const name of names){const line=source.split('\n').find(line=>line.startsWith('function '+name+'('));assert.ok(line.includes(`resetMarketBrowse('${scope}')`),name)}
  }
  const hooks=source.slice(source.indexOf('function installMarketBrowseLocks'),source.indexOf('function marketScrollAnchor'));
  for(const event of ['wheel','touchstart','pointerdown','pointerover','focusin','keydown'])assert.ok(hooks.includes(`'${event}'`));
  assert.ok(!hooks.includes("addEventListener('scroll'"));assert.ok(!hooks.includes('setTimeout'));
});

function enableSpotHeader(h){
  const ring={classList:{add(){},toggle(){}},style:{setProperty(){}},setAttribute(){}};
  h.context.document.querySelector=()=>ring;
  h.run(source.slice(source.indexOf('function renderPageRefreshIndicator('),source.indexOf('function beginPageRefresh(')));
  return ring;
}

test('spot header retains real source time, partial-source warnings and the five-second ring without a footer',async()=>{
  const h=spotLoaderHarness(),ring=enableSpotHeader(h);
  h.context.byId('binanceAlphaOnly').checked=true;
  const pending=h.context.loadSpotFutures(true),data=h.payload('fresh');
  data.errors={gate_spot:'timeout'};data.binance_alpha_catalog_stale=true;
  h.requests[0].resolve(data);assert.equal(await pending,true);
  const status=h.context.byId('updated').textContent;
  assert.ok(status.includes('现多期空 · 行情'));assert.ok(status.includes('部分上游延迟'));
  assert.ok(!status.includes('排序后分页'));assert.ok(ring.title.includes('间隔 5秒'));
  assert.ok(h.context.byId('binanceAlphaOnly').title.includes('沿用最近有效缓存'));
  const refresh=h.context.loadSpotFutures();h.requests[1].resolve(h.payload('recovered'));assert.equal(await refresh,true);
  assert.ok(!h.context.byId('updated').textContent.includes('延迟'));assert.ok(h.context.byId('binanceAlphaOnly').title.includes('已同步'));
});

test('spot request failure is visible in the header and retains good rows and source time after footer removal',async()=>{
  const h=spotLoaderHarness();enableSpotHeader(h);
  h.run('pageRefreshState("spot-futures").sourceUpdatedAt=95000');
  const pending=h.context.loadSpotFutures(true);h.requests[0].reject(new Error('network failed'));
  assert.equal(await pending,false);assert.ok(h.context.byId('updated').textContent.includes('请求失败'));
  assert.equal(h.context.byId('spotFuturesRows').innerHTML,'previous rows');assert.equal(h.run('pageRefreshState("spot-futures").sourceUpdatedAt'),95000);
  const refresh=h.context.loadSpotFutures();h.requests[1].resolve(h.payload('recovered'));assert.equal(await refresh,true);
  assert.ok(!h.context.byId('updated').textContent.includes('请求失败'));assert.equal(h.run('pageRefreshState("spot-futures").sourceUpdatedAt'),100000);
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

test('one server admission sounds once across phases and temporary absence, next breakout can ring',async()=>{
  const h=harness();let sounds=0;
  Object.assign(h.context,{pruneAlertMutes(){},isAlertMuted:()=>false,alertsInitialized:false,alertSignature:'',alertAudibleSignature:'',renderAlertMasterDetail(){},replaceHtmlKeepingExchangeLogos(){},renderAlertMuteList(){},renderTipsItems(){},startAlertCountdown(){},playAlarmSound(){sounds++}});
  h.run(source.slice(source.indexOf('async function loadAlerts('),source.indexOf('function tokenHedgeList(')));
  let active=[];h.context.fetch=async()=>({ok:true,json:async()=>({active_events:active,events:[],basis_tracking:[],checked_at_epoch:100})});
  await h.context.loadAlerts();
  const event={id:1,symbol:'DEMO/USDT',audible:true,notice_key:'range:1:1',reminder:{eligible:true},pulse:{state:'watch',notice_version:1}};
  active=[event];await h.context.loadAlerts();assert.equal(sounds,1);
  active=[{...event,id:2}];await h.context.loadAlerts();assert.equal(sounds,1);
  active=[];await h.context.loadAlerts();active=[event];await h.context.loadAlerts();assert.equal(sounds,1);
  active=[{...event,pulse:{state:'retraced'}}];await h.context.loadAlerts();assert.equal(sounds,1);
  active=[{...event,id:3,notice_key:'should-not-ring',reminder:{eligible:false},pulse:{state:'watch',ordinal:2}}];await h.context.loadAlerts();assert.equal(sounds,1);assert.equal(h.context.latestActiveAlertEvents.length,0);
  active=[{...event,notice_key:'range:1:2',pulse:{state:'expanded'}}];await h.context.loadAlerts();assert.equal(sounds,2);
  active=[{...event,id:5,notice_key:'range:5:1'}];await h.context.loadAlerts();assert.equal(sounds,3);
});

test('funding notices have independent identities, respect mute and do not replay on refresh',async()=>{
  const h=harness();let sounds=0,muted=false;
  Object.assign(h.context,{pruneAlertMutes(){},isAlertMuted:()=>muted,alertsInitialized:false,alertSignature:'',alertAudibleSignature:'',renderAlertMasterDetail(){},replaceHtmlKeepingExchangeLogos(){},renderAlertMuteList(){},renderTipsItems(){},startAlertCountdown(){},playAlarmSound(){sounds++}});
  h.run(source.slice(source.indexOf('async function loadAlerts('),source.indexOf('function tokenHedgeList(')));
  let funding=[];h.context.fetch=async()=>({ok:true,json:async()=>({events:[],active_events:[],basis_tracking:[],funding:{groups:[],active_events:funding}})});
  const cap={id:'cap',symbol:'DEMO/USDT',type:'funding_change',notice_key:'funding:cap',audible:true};
  funding=[cap];await h.context.loadAlerts();assert.equal(sounds,0);
  await h.context.loadAlerts();assert.equal(sounds,0);
  funding=[{...cap,id:'1h',notice_key:'funding:1h'}];await h.context.loadAlerts();assert.equal(sounds,1);
  funding=[];await h.context.loadAlerts();funding=[cap];await h.context.loadAlerts();assert.equal(sounds,1);
  muted=true;funding=[{...cap,id:'negative',notice_key:'funding:negative'}];await h.context.loadAlerts();assert.equal(sounds,1);
  assert.equal(h.context.latestActiveAlertEvents.length,1);
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
