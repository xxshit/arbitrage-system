import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const source=readFileSync(new URL('../static/app.js',import.meta.url),'utf8');
const html=readFileSync(new URL('../templates/index.html',import.meta.url),'utf8');
const css=readFileSync(new URL('../static/workspace-layout.css',import.meta.url),'utf8');
const feature=source.slice(source.indexOf('const equityState='),source.indexOf('async function loadDualFutures('));
const escape=value=>String(value).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
function harness(){
  const nodes=new Map(),queued=[],requests=[];
  const context={Date,URL,URLSearchParams,console,
    document:{querySelectorAll:()=>[]},
    byId(id){if(!nodes.has(id))nodes.set(id,{innerHTML:'',textContent:'',disabled:false});return nodes.get(id)},
    setTimeout(fn){queued.push(fn);return queued.length},clearTimeout(){},
    escapeChatHtml:escape,changeClass:value=>value>=0?'positive':'negative',
    alertCompactMetric:value=>Number.isFinite(value)?`${value>=0?'+':''}${value.toFixed(2)}%`:'—',
    exchangeName:value=>`<img class="exchange-logo">${escape(value)}`,delistingSymbolClass:()=>'',
    transferStatusControl:()=>'<span>充提未知</span>',
    replaceHtmlKeepingExchangeLogos:(node,markup)=>node.innerHTML=markup,
    setPageRefreshSource(...args){context.sourceState=args},
    fetchRefreshJson(url){requests.push(url);return Promise.resolve({items:[],page:1,pages:1,total:0,captured_at_epoch:1000})},
  };
  vm.createContext(context);vm.runInContext(feature,context);
  return {context,nodes,queued,requests,run:code=>vm.runInContext(code,context),node:id=>context.byId(id)};
}
function row(extra={}){return {symbol:'TSLA/USDT',strategy:'futures_futures',long_exchange:'Bybit',short_exchange:'Binance',long_ask:123.123456789,short_bid:124.123456789,open_spread:.4567,close_spread:-.1234,long_funding_rate:.02,short_funding_rate:-.01,long_interval:4,short_interval:8,long_basis:null,short_basis:.1234,captured_at_epoch:1000,fresh:false,...extra}}

test('equity navigation is exactly between spot and positions with its own view',()=>{
  const spot=html.indexOf('data-view="spot-futures"'),equity=html.indexOf('data-view="equity-arbitrage"'),positions=html.indexOf('data-view="my-positions"');
  assert.ok(spot<equity&&equity<positions);assert.ok(html.includes('id="equity-arbitrage" class="view"'));
  assert.ok(source.includes("if(viewId==='equity-arbitrage')loadEquityArbitrage();"));
  assert.ok(source.includes("every('equity-arbitrage',pageRefreshProfiles['equity-arbitrage'].seconds,()=>loadEquityArbitrage(),()=>activeViewId()==='equity-arbitrage')"));
  assert.ok(source.includes("'equity-arbitrage':{label:'美股套利',seconds:15,names:['loadEquityArbitrage']}"));
});

test('equity prices remain complete while percentages are two decimals, missing is not zero',()=>{
  const h=harness(),markup=h.context.equityRowsMarkup([row()]);
  for(const value of ['123.123456789','124.123456789','+0.46%','-0.12%','4H','8H','延迟 / 待核验','Bybit','Binance'])assert.ok(markup.includes(value),value);
  assert.match(markup, /多 <b class="[^"]*">—<\/b>/);
  assert.ok(!markup.includes('null'));assert.ok(!markup.includes('NaN'));
  assert.equal((markup.match(/exchange-logo/g)||[]).length,2);
});

test('untrusted symbols and venues are escaped and spot is not assigned funding',()=>{
  const h=harness(),markup=h.context.equityRowsMarkup([row({symbol:'<bad>/USDT',long_exchange:'<venue>',strategy:'spot_futures'})]);
  assert.ok(!markup.includes('<bad>'));assert.ok(!markup.includes('<venue>'));
  assert.ok(markup.includes('现货 · 无资费'));assert.ok(markup.includes('充提未知'));
});

test('rapid filter changes discard late responses and schedule latest filters',async()=>{
  const h=harness();let resolve;
  h.context.fetchRefreshJson=()=>new Promise(done=>resolve=done);
  const pending=h.context.loadEquityArbitrage();h.context.setEquityFilter('strategy','spot_futures');
  resolve({items:[row()],page:1,pages:1,total:1});assert.equal(await pending,false);
  assert.ok(!h.node('equityRows').innerHTML.includes('TSLA/USDT'));
  assert.equal(h.run('equityState.loading'),false);assert.ok(h.queued.length>0);
  h.context.fetchRefreshJson=async url=>{h.requests.push(url);return {items:[],page:1,pages:1,total:0}};
  await h.context.loadEquityArbitrage();assert.ok(h.requests[0].includes('strategy=spot_futures'));
  assert.ok(h.node('equityRows').innerHTML.includes('暂无已核验的现货与合约配对'));
});

test('refresh failure keeps previous same-filter rows and does not advance source time',async()=>{
  const h=harness();h.context.fetchRefreshJson=async()=>({items:[row()],page:1,pages:2,total:40,captured_at_epoch:1000});
  assert.equal(await h.context.loadEquityArbitrage(),true);const before=h.node('equityRows').innerHTML;
  const stamp=h.context.sourceState;
  h.context.fetchRefreshJson=async()=>{throw new Error('timeout')};
  assert.equal(await h.context.loadEquityArbitrage(),false);
  assert.equal(h.node('equityRows').innerHTML,before);assert.equal(h.context.sourceState,stamp);
  assert.ok(h.node('equityStatus').textContent.includes('已保留上次数据'));assert.equal(h.run('equityState.loading'),false);
});

test('empty modes and out-of-range pages clamp to the server page without invented rows',async()=>{
  const h=harness();h.context.changeEquityPage(100);await Promise.resolve();await Promise.resolve();
  await h.context.loadEquityArbitrage();assert.equal(h.run('equityState.page'),1);
  assert.ok(h.node('equityRows').innerHTML.includes('没有已核验'));assert.equal(h.node('equityNext').disabled,true);
  assert.equal(h.node('equityPrevious').disabled,true);
});

test('market panel scrolls inside its own container and does not introduce trading or sounds',()=>{
  assert.ok(css.includes('#equity-arbitrage .table-wrap'));assert.ok(css.includes('min-width:940px'));assert.ok(css.includes('overflow:auto'));
  assert.ok(html.includes('aria-label="美股套利路径"'));assert.ok(html.includes('aria-label="搜索美股标的"'));
  for(const forbidden of ['playAlarmSound','openHedgePositionBuilder','method:',"fetch('/api/alerts",'loadAlerts('])assert.ok(!feature.includes(forbidden),forbidden);
});

test('spot row shows raw asset identity, normalized price, and real wallet symbol',()=>{
  const h=harness();let walletSymbol;
  h.context.transferStatusControl=(_,symbol)=>{walletSymbol=symbol;return '充提正常'};
  const item=row({strategy:'spot_futures',spot_symbol:'AAPLX/USDT',product:'xStocks',units_per_token:1.003269,raw_long_ask:123.987654321,unit_basis:'Bybit实时乘数折股'});
  const markup=h.context.equityRowsMarkup([item]);
  for(const value of ['AAPLX/USDT','折合 USDT / 股','123.987654321','1.003269','配对依据','xStocks'])assert.ok(markup.includes(value),value);
  assert.equal(walletSymbol,'AAPLX/USDT');assert.ok(!markup.includes('data-equity-pair=""'));
});

test('pairing proof links are official https only and free text is escaped',()=>{
  const h=harness(),markup=h.context.equityPairingMarkup(row({product_name:'<script>x</script>',pairing_sources:['javascript:alert(1)','https://evil.test/','https://api.bybit.com.evil.test/','https://user@www.bybit.com/','https://api.bybit.com/v5/market/instruments-info?category=spot','https://www.gate.com/help/'],raw_long_ask:null}));
  assert.ok(!markup.includes('<script>'));assert.ok(!markup.includes('href="javascript'));
  assert.ok(!markup.includes('evil.test'));assert.ok(!markup.includes('user@'));
  assert.equal((markup.match(/rel="noopener noreferrer"/g)||[]).length,2);
  assert.ok(markup.includes('原始卖一 —'));
});

test('refresh preserves expanded pairing evidence by route, coverage is compact and mode scoped',async()=>{
  const h=harness(),item=row({strategy:'spot_futures',spot_symbol:'AAPLX/USDT'}),key=h.context.equityPairKey(item);
  h.context.document.querySelectorAll=selector=>selector.includes('[open]')?[{dataset:{equityPair:key}}]:[];
  h.context.fetchRefreshJson=async()=>({items:[item],page:1,pages:1,total:1,spot_coverage:{Gate:{status:'ok',quoted_pairs:12},Bybit:{status:'stale'}}});
  await h.context.loadEquityArbitrage();
  assert.ok(h.node('equityRows').innerHTML.includes(' open><summary>配对依据'));
  assert.equal(h.node('equityCoverage').textContent,'Gate 12 条 · Bybit 待更新');
  h.context.setEquityFilter('strategy','futures_futures');await h.context.loadEquityArbitrage();
  assert.equal(h.node('equityCoverage').textContent,'');
});
