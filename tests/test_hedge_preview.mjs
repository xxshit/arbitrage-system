import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const source=readFileSync(new URL('../static/app.js',import.meta.url),'utf8');
const preview=source.slice(source.indexOf('function hedgeQuoteAvailable('),source.indexOf('function spotSortPickerMarkup('));
const action=source.slice(source.indexOf('function dualHedgeAction('),source.indexOf('function dualRowsCompact('));
const loader=source.slice(source.indexOf('async function loadDualFutures('),source.indexOf('function addDualLiquidityHeader('));
const html=readFileSync(new URL('../templates/index.html',import.meta.url),'utf8');

function harness(){
  const elements=new Map(),alerts=[];
  const context={spotOpportunityByKey:new Map(),dualOpportunityByKey:new Map(),selectedHedgeOpportunity:null,
    alert:message=>alerts.push(message),escapeChatHtml:value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),
    hedgeMetric:(value,suffix='')=>`${Number(value).toFixed(2)}${suffix}`,signClass:value=>value<0?'negative':'positive',
    percent:value=>value==null?'—':`${value}%`,byId(id){
      if(!elements.has(id))elements.set(id,{value:'',textContent:'',innerHTML:'',disabled:false,
        classList:{hidden:true,add(name){if(name==='hidden')this.hidden=true},remove(name){if(name==='hidden')this.hidden=false}}});
      return elements.get(id);
    }};
  context.byId('hedgeQuoteAmount').value='100';context.byId('hedgeLeverage').value='5';context.byId('hedgeMaxSlippage').value='.30';
  vm.createContext(context);vm.runInContext(preview+action,context);
  return {context,alerts,element:context.byId,run:code=>vm.runInContext(code,context)};
}
const group={symbol:'SYN/USDT'};
const route=(long='Bybit',short='OKX',extra={})=>({long_exchange:long,short_exchange:short,long_ask:20,short_bid:22,open_spread:10,funding_difference:.025,...extra});

test('dual rows expose each complete long/short route and disable unavailable or invalid books',()=>{
  const h=harness();
  for(const short of ['Binance','OKX']){
    const markup=h.context.dualHedgeAction(group,route('Bybit',short));
    assert.ok(markup.includes(`openHedgePositionBuilder('${encodeURIComponent(`SYN/USDT|Bybit|${short}`)}','futures_futures')`));
    assert.ok(!markup.includes('disabled'));assert.ok(markup.includes('建立对冲仓位'));
  }
  for(const side of ['long_ask','short_bid'])for(const price of [null,undefined,0,-1,NaN,Infinity,true,'invalid']){
    const markup=h.context.dualHedgeAction(group,route('Bybit','OKX',{[side]:price}));
    assert.ok(markup.includes('disabled'));assert.ok(!markup.includes('onclick'));
  }
  assert.ok(h.context.dualHedgeAction(group,route('Bybit','OKX',{_unavailable:true})).includes('disabled'));
});

test('dual preview uses long ask and short bid, equal base quantities and separate margins',()=>{
  const h=harness(),row=route(),amounts=h.context.hedgePreviewAmounts(row,100,5,true);
  assert.deepEqual(JSON.parse(JSON.stringify(amounts)),{quantity:5,longPrice:20,shortPrice:22,longNotional:100,shortNotional:110,longMargin:20,shortMargin:22,totalMargin:42,gap:-10});
  const key='SYN/USDT|Bybit|OKX';h.context.dualOpportunityByKey.set(key,{group,row});
  h.context.openHedgePositionBuilder(encodeURIComponent(key),'futures_futures');
  assert.equal(h.element('hedgePreviewRoute').textContent,'Bybit 买入永续（开多） + OKX 卖出永续（开空）');
  assert.equal(h.element('hedgeSubmitButton').textContent,'需 Bybit + OKX 私有 API');
  assert.equal(h.element('hedgeSubmitButton').disabled,true);
  const markup=h.element('hedgeOrderPreview').innerHTML;
  assert.ok(markup.includes('① 买入永续（开多）'));assert.ok(markup.includes('② 卖出永续（开空）'));
  assert.ok(markup.includes('<span>Bybit</span>'));assert.ok(markup.includes('<span>OKX</span>'));assert.ok(!markup.includes('Binance'));
  assert.equal((markup.match(/<dt>估算数量<\/dt><dd>5.00<\/dd>/g)||[]).length,2);
  assert.ok(markup.includes('<dt>预估保证金</dt><dd>20.00 USDT</dd>'));assert.ok(markup.includes('<dt>预估保证金</dt><dd>22.00 USDT</dd>'));
  assert.ok(markup.includes('预估双腿保证金 42.00 USDT'));assert.ok(markup.includes('两腿采用同一杠杆，仅作保证金估算'));
  assert.ok(markup.includes('下期净资费'));assert.ok(markup.includes('0.025%'));
  h.element('hedgeLeverage').value='1';h.context.renderHedgePositionPreview();
  assert.ok(h.element('hedgeOrderPreview').innerHTML.includes('预估双腿保证金 210.00 USDT'));
});

test('same symbol and long venue can open different short venues without collision',()=>{
  const h=harness();
  for(const [short,price] of [['Binance',21],['OKX',22]])h.context.dualOpportunityByKey.set(`SYN/USDT|Bybit|${short}`,{group,row:route('Bybit',short,{short_bid:price})});
  for(const [short,expectedMargin] of [['Binance','21.00'],['OKX','22.00']]){
    h.context.openHedgePositionBuilder(encodeURIComponent(`SYN/USDT|Bybit|${short}`),'futures_futures');
    assert.ok(h.element('hedgePreviewRoute').textContent.includes(`${short} 卖出永续`));
    assert.ok(h.element('hedgeOrderPreview').innerHTML.includes(`<dt>预估保证金</dt><dd>${expectedMargin} USDT</dd>`));
  }
});

test('spot preview keeps its cash long leg and Binance short leg unchanged',()=>{
  const h=harness(),row=route('Gate','Binance',{funding_rate:.03}),key='SYN/USDT|Gate';
  h.context.spotOpportunityByKey.set(key,{group,row});h.context.openHedgePositionBuilder(encodeURIComponent(key));
  assert.equal(h.element('hedgePreviewRoute').textContent,'Gate 买入现货 + Binance 卖出永续（开空）');
  const markup=h.element('hedgeOrderPreview').innerHTML;
  assert.ok(markup.includes('① 买入现货'));assert.ok(markup.includes('<dt>投入</dt><dd>100.00 USDT</dd>'));
  assert.ok(markup.includes('<dt>预估保证金</dt><dd>22.00 USDT</dd>'));assert.equal((markup.match(/<dt>预估保证金/g)||[]).length,1);
  assert.ok(markup.includes('当前资费'));assert.ok(markup.includes('0.03%'));assert.ok(!markup.includes('多腿名义金额'));
  assert.equal(h.element('hedgeSubmitButton').disabled,true);
  h.context.closeHedgePositionBuilder();assert.equal(h.context.selectedHedgeOpportunity,null);assert.equal(h.element('hedgePositionModal').classList.hidden,true);
});

test('invalid input or a vanished route cannot produce a false preview or enable submission',()=>{
  const h=harness(),row=route(),key='SYN/USDT|Bybit|OKX';
  h.context.openHedgePositionBuilder(encodeURIComponent(key),'futures_futures');assert.equal(h.alerts.length,1);assert.equal(h.context.selectedHedgeOpportunity,null);
  h.context.dualOpportunityByKey.set(key,{group,row:{...row,_unavailable:true}});
  h.context.openHedgePositionBuilder(encodeURIComponent(key),'futures_futures');assert.equal(h.alerts.length,2);assert.equal(h.context.selectedHedgeOpportunity,null);
  h.context.dualOpportunityByKey.set(key,{group,row});h.context.openHedgePositionBuilder(encodeURIComponent(key),'futures_futures');
  for(const [id,value] of [['hedgeQuoteAmount','0'],['hedgeQuoteAmount','Infinity'],['hedgeLeverage','0'],['hedgeLeverage','11']]){
    h.element('hedgeQuoteAmount').value='100';h.element('hedgeLeverage').value='5';h.element(id).value=value;h.context.renderHedgePositionPreview();
    assert.ok(h.element('hedgeOrderPreview').innerHTML.includes('请输入有效投入金额'));assert.ok(!h.element('hedgeOrderPreview').innerHTML.includes('NaN'));assert.equal(h.element('hedgeSubmitButton').disabled,true);
  }
  assert.ok(!preview.includes('fetch('));assert.ok(!preview.includes('localStorage'));assert.ok(!preview.includes('method:'));
  assert.ok(html.includes('disabled>等待币安私有 API'));
});

test('finite positive inputs that overflow quantities, notionals or combined margins are rejected',()=>{
  const h=harness(),key='SYN/USDT|Bybit|OKX';
  for(const [long_ask,short_bid,quote] of [[Number.MIN_VALUE,22,100],[1,8,Number.MAX_VALUE/4],[1,1,Number.MAX_VALUE/1.5]]){
    const row=route('Bybit','OKX',{long_ask,short_bid});
    for(const dual of [false,true])assert.equal(h.context.hedgePreviewAmounts(row,quote,1,dual),null);
    h.context.dualOpportunityByKey.set(key,{group,row});h.element('hedgeQuoteAmount').value=String(quote);h.element('hedgeLeverage').value='1';
    h.context.openHedgePositionBuilder(encodeURIComponent(key),'futures_futures');
    assert.ok(h.element('hedgeOrderPreview').innerHTML.includes('请输入有效投入金额'));
    assert.ok(!/Infinity|NaN/.test(h.element('hedgeOrderPreview').innerHTML));assert.equal(h.element('hedgeSubmitButton').disabled,true);
  }
});

test('same-venue preview labels the actual exchange and leaves submission disabled',()=>{
  const h=harness();
  for(const [strategy,exchange] of [['spot_futures','Binance'],['futures_futures','Bybit']]){
    const dual=strategy==='futures_futures',key=dual?`SYN/USDT|${exchange}|${exchange}`:`SYN/USDT|${exchange}`;
    h.context[dual?'dualOpportunityByKey':'spotOpportunityByKey'].set(key,{group,row:route(exchange,exchange)});
    h.context.openHedgePositionBuilder(encodeURIComponent(key),strategy);
    assert.equal(h.element('hedgeSubmitButton').textContent,`等待 ${exchange} 私有 API`);
    assert.equal(h.element('hedgeSubmitButton').disabled,true);
  }
});

test('real dual loader replaces the route map, preserving all three identities and removing unavailable routes',async()=>{
  const h=harness(),state={version:1,locked:false,missing:0,prepare(){},requestUrl:base=>base,accept:data=>data.symbols};
  let data={symbols:[{...group,rows:[route('Bybit','Binance'),route('Bybit','OKX'),route('OKX','Binance')]}],page:1,pages:1,total_symbols:1,errors:{},updated_at:'12:00',captured_at_epoch:1};
  Object.assign(h.context,{marketBrowseStates:{dual:state},marketBaseUrl:()=>'/api/dual-futures',dualFuturesLoading:false,dualFuturesHasData:false,fetchRefreshJson:async()=>data,
    destroyFloatingPopup(){},renderMarketGroups(){},updateDualSortHeaders(){},showDualRefreshCountdown(){},setPageRefreshSource(){},dualSort:{key:'open_spread',direction:'desc'},dualSortLabels:{open_spread:'开差'},setTimeout(){throw new Error('unexpected reschedule')}});
  h.run(loader);assert.equal(await h.context.loadDualFutures(),true);
  assert.deepEqual([...h.context.dualOpportunityByKey.keys()],['SYN/USDT|Bybit|Binance','SYN/USDT|Bybit|OKX','SYN/USDT|OKX|Binance']);
  data={...data,symbols:[{...group,rows:[route('Bybit','OKX',{short_bid:24}),route('OKX','Binance',{_unavailable:true})]}]};
  assert.equal(await h.context.loadDualFutures(),true);assert.deepEqual([...h.context.dualOpportunityByKey.keys()],['SYN/USDT|Bybit|OKX']);
  h.context.openHedgePositionBuilder(encodeURIComponent('SYN/USDT|Bybit|OKX'),'futures_futures');
  assert.ok(h.element('hedgeOrderPreview').innerHTML.includes('<dt>预估保证金</dt><dd>24.00 USDT</dd>'));
});
