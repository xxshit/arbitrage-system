import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const source=readFileSync(new URL('../static/app.js',import.meta.url),'utf8');
const css=readFileSync(new URL('../static/workspace-layout.css',import.meta.url),'utf8');
const renderer=source.slice(source.indexOf('function spotRows('),source.indexOf('async function loadSpotFutures('));
const columns=source.slice(source.indexOf('function configureSpotColumns('),source.indexOf('function configureSpotLayoutShell('));
function harness(){
  const context={encodeURIComponent,exchangeName:name=>`EXCHANGE:${name}`,signClass:n=>n<0?'negative':'positive',percent:n=>`${n}%`,
    intervalClass:()=>'',fundingClock:time=>time,coinglassKlineUrl:()=>'/chart',delistingSymbolClass:()=>'',
    spotTransferStatus:(row,symbol)=>`STATUS:${symbol}:${row.long_exchange}`,basisOpeningMarkup:()=>'<span>OPENING</span>',
    spotLiquidity:row=>`SPOT_VOLUME:${row.spot_volume}`,futuresLiquidity:row=>`FUTURE_VOLUME:${row.futures_volume}`,
    fundingStack:row=>`FUNDING:${row.funding_rate}`,priceChangeStack:row=>`CHANGES:${row.change_5m}`};
  vm.createContext(context);vm.runInContext(renderer+columns,context);return context;
}
const row=(exchange,index=0)=>({long_exchange:exchange,open_spread:index+.51,close_spread:-index-.72,basis:.23,funding_rate:.03,
  funding_interval_hours:4,next_funding_time:'12:00',spot_volume:index+100,futures_volume:1000,change_5m:5.7});
const groups=[['ONE',['Gate']],['TWO',['Binance','Bitget']],['THREE',['Binance','Gate','Bitget']]].map(([symbol,venues])=>({symbol:symbol+'/USDT',rows:venues.map(row)}));

test('compact rows retain all twelve columns and align shared cells for one, two and three venues',()=>{
  const markup=harness().spotRows(groups),rows=[...markup.matchAll(/<tr[^>]*>(.*?)<\/tr>/g)].map(match=>match[1]);
  const active=Array(12).fill(null);let route=0;
  for(const group of groups)for(const expected of group.rows){
    const cells=[...rows[route++].matchAll(/<td([^>]*)>(.*?)<\/td>/g)],grid=active.map(item=>item?.value??null);let col=0;
    for(const [,attrs,value] of cells){
      while(grid[col]!==null)col++;
      assert.ok(col<12,'unexpected thirteenth column');grid[col]=value;
      const span=Number(attrs.match(/rowspan="(\d+)"/)?.[1]||1);active[col]={value,left:span};
    }
    assert.ok(grid.every(Boolean),'missing column');
    assert.ok(grid[0].includes(group.symbol));assert.equal(grid[1],`EXCHANGE:${expected.long_exchange}STATUS:${group.symbol}:${expected.long_exchange}`);
    assert.equal(grid[2],`${expected.open_spread}%`);assert.equal(grid[3],`${expected.close_spread}%`);
    assert.equal(grid[4],'0.23%');assert.equal(grid[5],'FUNDING:0.03');
    assert.ok(grid[6].includes('4H'));assert.equal(grid[7],'CHANGES:5.7');
    assert.equal(grid[8],`SPOT_VOLUME:${expected.spot_volume}`);assert.equal(grid[9],'FUTURE_VOLUME:1000');
    assert.equal(grid[10],'EXCHANGE:Binance 合约');assert.ok(grid[11].includes(encodeURIComponent(`${group.symbol}|${expected.long_exchange}`)));
    active.forEach((item,index)=>{if(item&&!--item.left)active[index]=null});
  }
  assert.equal(route,6);assert.ok(active.every(item=>item===null));
});

test('each venue keeps its own edge, wallet control and hedge action while shared metrics render once',()=>{
  const markup=harness().spotRows(groups);
  assert.equal((markup.match(/hedge-build-button/g)||[]).length,6);
  assert.equal((markup.match(/STATUS:/g)||[]).length,6);
  for(const field of ['FUNDING:','CHANGES:','FUTURE_VOLUME:','OPENING'])assert.equal((markup.match(new RegExp(field,'g'))||[]).length,3);
  assert.equal((markup.match(/data-render-key=/g)||[]).length,6);
  assert.ok(!markup.includes('trendLights'));assert.ok(!markup.includes('fetch('));
});

test('header reorder is idempotent and preserves the original header nodes',()=>{
  const context=harness(),original=Array.from({length:12},(_,index)=>({index}));
  const header={children:[...original],dataset:{},appendChild(node){this.children.splice(this.children.indexOf(node),1);this.children.push(node)}};
  context.configureSpotColumns(header);context.configureSpotColumns(header);
  assert.deepEqual(header.children.map(node=>node.index),[0,3,9,10,6,7,8,1,2,5,4,11]);
  assert.equal(new Set(header.children).size,12);assert.ok(header.children.every(node=>original.includes(node)));
  const partial={children:[...original.slice(0,11)],dataset:{},appendChild(){throw new Error('must not alter partial header')}};
  context.configureSpotColumns(partial);context.configureSpotColumns(null);assert.deepEqual(partial.dataset,{});
});

test('compact styling is spot-scoped, keeps all period values and wraps divergence inside its cell',()=>{
  const compact=css.slice(css.indexOf('/* Spot scanner:'),css.indexOf('/* 05 Hedge ledger:'));
  assert.ok(compact.includes('.change-stack {grid-template-columns:repeat(2,max-content)'));
  assert.ok(compact.includes('.funding-stack {grid-template-columns:repeat(2,max-content)'));
  assert.ok(compact.includes('td {padding:5px 8px;font-size:12px'));
  assert.ok(compact.includes('.spot-divergence-warning {display:flex;width:190px;max-width:100%'));
  for(const field of ['.change-item','.funding-item','.spot-divergence-warning','.transfer-status-control']){
    assert.ok(!new RegExp(field.replaceAll('.','\\.')+'[^}]*display:none').test(compact));
  }
  assert.ok(!compact.includes('#dual-futures'));assert.ok(!compact.includes('#equity-arbitrage'));
  assert.ok(source.includes("'spot-futures':{label:'现多期空',seconds:5"));
  assert.ok(source.includes("'equity-arbitrage':{label:'美股套利',seconds:15"));
});

test('spot page allocates remaining viewport height to one table scroll without hiding overflow',()=>{
  const compact=css.slice(css.indexOf('/* Spot scanner:'),css.indexOf('/* 05 Hedge ledger:'));
  assert.ok(compact.includes('body:has(#spot-futures.active) main {display:flex;flex-direction:column;height:100%;min-height:0;'));
  assert.ok(compact.includes('.layout-view#spot-futures.active {display:flex;flex:1 1 0;flex-direction:column;min-height:0}'));
  assert.ok(compact.includes('.layout-view#spot-futures > .panel {display:flex;flex:1 1 0;flex-direction:column;min-height:0;margin:0;'));
  assert.ok(compact.includes('.layout-view#spot-futures.active .table-wrap {flex:1 1 0;min-height:0;max-height:none;overflow:auto;overscroll-behavior:contain}'));
  assert.ok(compact.includes(':is(.panel-title,.pagination) {flex:0 0 auto}'));
  assert.ok(!compact.includes('100dvh - 205px'));assert.ok(!compact.includes('overflow:hidden'));assert.ok(!compact.includes('overflow-y:hidden'));
});

test('narrow pagination keeps all controls reachable and short screens use page scroll instead of clipping rows',()=>{
  const compact=css.slice(css.indexOf('/* Spot scanner:'),css.indexOf('/* 05 Hedge ledger:'));
  assert.ok(compact.includes('grid-template-columns:auto minmax(0,1fr) auto'));
  for(const id of ['previousPage','nextPage','pageInfo'])assert.ok(compact.includes('.pagination #'+id));
  assert.ok(compact.includes('@media(max-height:520px), (max-width:760px) and (max-height:640px)'));
  assert.ok(compact.includes('body:has(#spot-futures.active) main {display:block;height:auto}'));
  assert.ok(!compact.includes('scrollbar-width:none'));assert.ok(!compact.includes('-webkit-scrollbar'));
});
