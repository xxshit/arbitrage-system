import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const source=readFileSync(new URL('../static/app.js',import.meta.url),'utf8');
const css=readFileSync(new URL('../static/workspace-layout.css',import.meta.url),'utf8');
const compact=css.slice(css.indexOf('/* Compact paired legs:'),css.indexOf('/* Spot scanner:'));
const renderer=source.slice(source.indexOf('function dualDisplayPercent('),source.indexOf('function positionFloatingPopup('));
const periods=['5m','15m','30m','1h','12h','24h','3d','7d'];
function harness(){
  const context={trendPeriods:periods.map(p=>['change_'+p,p]),percent:n=>`${n>=0?'+':''}${Number(n).toFixed(4)}%`,
    changeClass:n=>n==null?'neutral':n<0?'negative':'positive',signClass:n=>n<0?'negative':'positive',
    displayChange:n=>n==null?'—':`${n}%`,exchangeName:name=>`EXCHANGE:${name}`,intervalClass:()=>'',
    liquidityMetric:(label,value)=>`<span class="liquidity-metric"><b>${label}</b><i>${value??'—'}</i></span>`,
    coinglassKlineUrl:()=>'/chart',delistingSymbolClass:()=>'',basisOpeningMarkup:(_item,key)=>`OPENING:${key}`};
  vm.createContext(context);vm.runInContext(renderer,context);return context;
}
const route=(long,index)=>({long_exchange:long,short_exchange:'Binance',open_spread:index+.51,close_spread:-index-.72,
  long_basis:-.23,short_basis:.45,long_bid:2.1234,long_ask:2.1236,short_bid:2.15,short_ask:2.17,
  long_funding_rate:.01,short_funding_rate:-.02,long_funding_cap:1.5,short_funding_cap:2,
  long_funding_interval_hours:4,short_funding_interval_hours:8,long_next_funding_time:'10-20 12:00',short_next_funding_time:'10-20 16:00',
  long_open_interest:100+index,long_volume:200+index,short_open_interest:300+index,short_volume:400+index,
  funding_difference:.1,funding_difference_previous:.2,funding_difference_24h:.3,funding_difference_3d:.4,funding_difference_7d:.5,funding_difference_30d:.6,
  funding_settlement_label:'双方结算 · 16:00',index_overlap:60,_commonIndexId:`path-${index}-${long}`,
  long_index_components:[{source:'a',weight:.6},{source:'b',weight:.4}],short_index_components:[{source:'a',weight:1}]});
const groups=[['ONE',['Bybit']],['TWO',['Bybit','OKX']],['THREE',['Bybit','OKX','Gate']]].map(([symbol,venues])=>({
  symbol:symbol+'/USDT',...Object.fromEntries(periods.map((p,index)=>['binance_change_'+p,index+.25])),rows:venues.map(route)}));

test('missing pinned dual route retains venue identities without fake zero quotes or funding caps',()=>{
  const html=harness().dualRowsCompact([{symbol:'A/USDT',rows:[{long_exchange:'Bybit',short_exchange:'Binance',_unavailable:true}]}]);
  assert.ok(html.includes('EXCHANGE:Bybit'));assert.ok(html.includes('EXCHANGE:Binance'));assert.ok(html.includes('暂无行情'));
  assert.ok(!html.includes('0.0000%'));assert.ok(!html.includes('2%'));assert.ok(!html.includes('0.000000'));
  assert.equal((html.match(/<td\b/g)||[]).length,5);
});

test('compact dual keeps five aligned columns for one, two and three paths',()=>{
  const markup=harness().dualRowsCompact(groups),rows=[...markup.matchAll(/<tr[^>]*>(.*?)<\/tr>/g)].map(match=>match[1]);
  const active=Array(5).fill(null);let routeIndex=0;
  for(const group of groups)for(const row of group.rows){
    const cells=[...rows[routeIndex++].matchAll(/<td([^>]*)>(.*?)<\/td>/g)],grid=active.map(item=>item?.value??null);let col=0;
    for(const [,attrs,value] of cells){
      while(grid[col]!==null)col++;
      assert.ok(col<5,'unexpected sixth column');grid[col]=value;
      active[col]={value,left:Number(attrs.match(/rowspan="(\d+)"/)?.[1]||1)};
    }
    assert.ok(grid.every(Boolean));assert.ok(grid[0].includes(group.symbol));
    for(let p=0;p<8;p++)assert.ok(grid[1].includes(`${p+.25}%`));
    assert.ok(grid[2].includes(`EXCHANGE:${row.long_exchange}`));assert.ok(grid[3].includes('EXCHANGE:Binance'));
    for(const [column,side] of [[2,'long'],[3,'short']]){
      for(const field of ['basis','funding_rate','funding_cap'])assert.ok(grid[column].includes(harness().dualDisplayPercent(row[`${side}_${field}`])),field);
      for(const field of ['open_interest','volume'])assert.ok(grid[column].includes(`>${row[`${side}_${field}`]}<`),field);
      assert.ok(grid[column].includes(side==='long'?'2.1235':'2.1600'));
      assert.ok(grid[column].includes(side==='long'?'4H':'8H'));assert.ok(grid[column].includes(side==='long'?'12:00':'16:00'));
    }
    for(const field of ['open_spread','close_spread','funding_difference','funding_difference_previous','funding_difference_24h','funding_difference_3d','funding_difference_7d','funding_difference_30d'])assert.ok(grid[4].includes(harness().dualDisplayPercent(row[field])),field);
    assert.ok(!grid[4].includes(row.funding_settlement_label));assert.ok(grid[4].includes('60.00%'));assert.ok(grid[4].includes(row._commonIndexId));
    active.forEach((item,index)=>{if(item&&!--item.left)active[index]=null});
  }
  assert.equal(routeIndex,6);assert.ok(active.every(item=>item===null));
  assert.equal((markup.match(/data-render-key=/g)||[]).length,6);
  for(const group of groups)assert.equal(markup.split(`OPENING:dual:${group.symbol}`).length-1,1);
});

test('compact dual preserves missing states, common-index evidence and original detail actions',()=>{
  const context=harness(),row={...route('Bybit',0),long_basis:null,long_funding_rate:null,long_open_interest:null,funding_difference_24h:null,index_overlap:null};
  const markup=context.dualRowsCompact([{...groups[0],delisting_announced:true,rows:[row]}]);
  assert.ok(markup.includes('data-delisting-announced="1"'));assert.ok(markup.includes('data-market-symbol="ONE/USDT"'));
  assert.ok(markup.includes("openSymbolDetail('ONE/USDT')"));assert.ok(markup.includes('class="kline-link"'));
  assert.ok(markup.includes('class="neutral">—'));assert.ok(markup.includes('成分待同步'));
  assert.ok(!markup.includes('NaN'));assert.ok(!markup.includes('undefined'));
  assert.ok(context.dualIndexOverlap(route('Bybit',0)).includes('onmouseenter="showCommonIndexTip(this)"'));
});

test('dual density only hides duplicated trend lights, empty opening placeholder and repeated liquidity heading',()=>{
  const hidden=[...compact.matchAll(/([^{}]+)\{[^{}]*display:none[^{}]*\}/g)].map(match=>match[1].trim());
  assert.equal(hidden.length,2);
  assert.ok(hidden[0].includes('td.symbol > .trend-lights'));assert.ok(hidden[0].includes('.basis-opening-empty'));
  assert.equal(hidden[1],'.layout-view#dual-futures .dual-liquidity-row > dt');
  assert.ok(compact.includes('.change-stack {grid-template-columns:repeat(2,max-content)'));
  assert.ok(compact.includes('header {min-height:22px;gap:5px;margin:0}'));
  assert.ok(compact.includes('grid-column:2;grid-row:1'));assert.ok(compact.includes('grid-column:1/-1;grid-row:2'));
  assert.ok(compact.includes('.dual-group-divider td {height:3px!important}'));
  assert.ok(compact.includes('overflow:visible;text-overflow:clip'));
  assert.ok(!compact.includes('overflow:hidden'));assert.ok(!compact.includes('text-overflow:ellipsis'));
  assert.ok(!compact.includes('#spot-futures'));assert.ok(!compact.includes('#equity-arbitrage'));
  assert.ok(source.includes("'dual-futures':{label:'期多期空',seconds:5"));
});

test('funding comparison removes the settlement note without dropping any period or mutating data',()=>{
  const context=harness();
  for(const label of ['双方结算 · 16:00','仅空腿结算 · 16:00',undefined]){
    const row={...route('Bybit',0),funding_settlement_label:label},before=JSON.stringify(row),markup=context.dualFundingHistory(row);
    assert.equal(JSON.stringify(row),before);assert.equal((markup.match(/<em /g)||[]).length,6);
    for(const period of ['当前','上期','24H','3D','7D','30D'])assert.ok(markup.includes(`<b>${period}</b>`));
    const visibleText=markup.replace(/<[^>]*>/g,'');
    assert.ok(!markup.includes('<small>'));assert.ok(!visibleText.includes('结算'));assert.ok(!visibleText.includes('待同步'));
    // Each leg's own settlement clock and cycle are still present.
    assert.ok(context.dualExchangeCard(row,'long').includes('12:00'));
    assert.ok(context.dualExchangeCard(row,'short').includes('16:00'));
  }
});

test('dual restores rounded light cards without restoring large padding or tall tiles',()=>{
  assert.ok(compact.includes('.dual-result-panel .common-index {display:inline-block;min-width:0}'));
  assert.ok(compact.includes('td.symbol > div:has(> .kline-link) {display:flex;align-items:center}'));
  for(const selector of ['.dual-exchange-card {','.dual-result-panel {','.dual-funding-history {']){
    const rule=compact.slice(compact.indexOf(selector)).split('}')[0];
    assert.match(rule,/border:1px solid/);assert.ok(rule.includes('border-radius:8px'));assert.ok(rule.includes('background:linear-gradient'));
    assert.ok(!rule.includes('padding:10px'));assert.ok(!rule.includes('padding:12px'));
  }
  for(const selector of ['.dual-result-panel > div {','.dual-funding-history > span {']){
    const rule=compact.slice(compact.indexOf(selector)).split('}')[0];
    assert.ok(rule.includes('min-height:0'));assert.ok(rule.includes('background:rgba(255,255,255,.85)'));
  }
});
