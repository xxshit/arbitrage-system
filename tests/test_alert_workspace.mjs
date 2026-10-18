import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const source=readFileSync(new URL('../static/app.js',import.meta.url),'utf8');
const html=readFileSync(new URL('../templates/index.html',import.meta.url),'utf8');
const css=readFileSync(new URL('../static/workspace-layout.css',import.meta.url),'utf8');
const functions=source.slice(source.indexOf('function isAlertMuted('),source.indexOf('function renderTipsItems('));
const helpers=source.slice(source.indexOf('function alertLabel('),source.indexOf('function toggleTips('));
const escape=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const group=(symbol='DEMO/USDT',count=3,spread=1,basis=2)=>({symbol,alert_count:count,latest:{symbol,created_at:'10-18 10:00:00',type:'rapid_basis',strategy:'spot_futures',open_spread:0.5,basis:1},move_peaks:{open_spread:spread,basis}});
const detail=(symbol='DEMO/USDT',page=1)=>({symbol,total:43,page,pages:2,page_size:40,items:[{id:page,symbol,type:'rapid_basis',created_at:'10-18 10:00:00',message:'Synthetic detail',open_spread:0.5,basis:1}]});

function harness(fetcher=async url=>detail(new URL(url,'https://example.test').searchParams.get('symbol'))){
  const nodes=new Map(),scroll={scrollTop:0};
  const node=id=>{if(!nodes.has(id))nodes.set(id,{id,textContent:'',innerHTML:'',scrollTop:0,querySelector:()=>scroll,scrollIntoView(){}});return nodes.get(id)};
  const context={URLSearchParams,console,window:{innerWidth:1280},byId:node,escapeChatHtml:escape,delistingSymbolClass:()=>'',changeClass:value=>value<0?'negative':'positive',percent:value=>(value>=0?'+':'')+Number(value).toFixed(4)+'%',intervalClass:()=>'',exchangeName:escape,fetchRefreshJson:fetcher,renderTipsItems(){},
    replaceHtmlKeepingExchangeLogos:(target,markup)=>{if(target.innerHTML!==markup)target.innerHTML=markup}};
  vm.createContext(context);
  vm.runInContext("let mutedAlertSymbols=new Set(),selectedAlertSymbol='',latestAlertGroups=[],latestActiveAlertEvents=[],alertListQuery='',alertListSort='latest',alertDetailVersion=0,alertDetailRequest=null,alertDetailState={symbol:'',data:null,loading:false,error:'',expanded:new Set()};\n"+helpers+'\n'+functions,context);
  return {context,node,scroll,run:code=>vm.runInContext(code,context)};
}

test('compact coin rows contain name, count, mute and signed peaks but no timestamps or large metadata cards',()=>{
  const h=harness(),markup=h.context.alertSymbolCard(group());
  for(const value of ['DEMO/USDT','alert-symbol-count','alert-symbol-peak','静音','+1.0000%','+2.0000%'])assert.ok(markup.includes(value),value);
  for(const value of ['10-18','alert-symbol-meta','alert-latest-metrics','最近开差','最近基差'])assert.ok(!markup.includes(value),value);
  assert.ok(markup.includes('aria-pressed="false"'));assert.ok(markup.includes('查看详情'));
});

test('all 168 coins are rendered without an 80-coin list or viewport limit',()=>{
  const h=harness(),groups=Array.from({length:168},(_,i)=>group(`COIN${i}/USDT`));h.context.renderAlertMasterDetail(groups);
  assert.equal((h.node('alertSymbolList').innerHTML.match(/data-alert-symbol=/g)||[]).length,168);
  assert.equal(h.node('alertTotalSymbols').textContent,168);assert.equal(h.node('alertTotalEvents').textContent,504);
});

test('coin search normalizes case and separators and supports existing Chinese coin names',()=>{
  const h=harness();h.context.renderAlertMasterDetail([group(),group('哈基米/USDT')]);
  h.context.filterAlertSymbols('demo-usdt');assert.equal(h.context.visibleAlertGroups().length,1);assert.ok(h.node('alertRosterCount').textContent.includes('1 / 2'));
  h.context.filterAlertSymbols('哈基米');assert.equal(h.context.visibleAlertGroups()[0].symbol,'哈基米/USDT');
  h.context.filterAlertSymbols('none');assert.ok(h.node('alertSymbolList').innerHTML.includes('清空搜索'));
  h.context.filterAlertSymbols('');assert.equal(h.context.visibleAlertGroups().length,2);
});

test('count and peak sorting are explicit and use absolute peaks without losing signs',()=>{
  const h=harness();h.context.renderAlertMasterDetail([group('FIRST/USDT',2,1,8),group('SECOND/USDT',10,-12,-3)]);
  h.context.sortAlertSymbols('count');assert.equal(h.context.visibleAlertGroups()[0].symbol,'SECOND/USDT');
  h.context.sortAlertSymbols('open_spread');assert.equal(h.context.visibleAlertGroups()[0].move_peaks.open_spread,-12);
  h.context.sortAlertSymbols('basis');assert.equal(h.context.visibleAlertGroups()[0].symbol,'FIRST/USDT');
  h.context.sortAlertSymbols('unknown');assert.equal(h.run('alertListSort'),'latest');
});

test('selection survives group reorder, while removal resets it without mixing old detail',()=>{
  const h=harness();h.context.renderAlertMasterDetail([group(),group('OTHER/USDT')]);h.run("selectedAlertSymbol='OTHER/USDT'");h.context.renderAlertMasterDetail([group('OTHER/USDT'),group()]);
  assert.equal(h.run('selectedAlertSymbol'),'OTHER/USDT');h.context.renderAlertMasterDetail([group()]);
  assert.equal(h.run('alertDetailState.symbol'),'DEMO/USDT');assert.equal(h.run('alertDetailState.data'),null);
});

test('muting does not select a different coin or create a history request',()=>{
  let reads=0;const h=harness(async()=>{reads++;return detail()});h.context.renderAlertMasterDetail([group(),group('OTHER/USDT')]);let stopped=false;
  h.context.toggleAlertMute('OTHER/USDT',{stopPropagation(){stopped=true}});
  assert.ok(stopped);assert.equal(h.run('selectedAlertSymbol'),'DEMO/USDT');assert.ok(h.context.isAlertMuted('OTHER/USDT'));assert.equal(reads,0);
});

test('detail reads request only the selected coin and page, and preserve both independent scroll positions',async()=>{
  let url;const h=harness(async value=>{url=new URL(value,'https://example.test');return detail('DEMO/USDT',2)});h.context.renderAlertMasterDetail([group()]);h.node('alertSymbolList').scrollTop=390;h.scroll.scrollTop=210;
  await h.context.loadSelectedAlertHistory(2);
  assert.equal(url.searchParams.get('view'),'detail');assert.equal(url.searchParams.get('symbol'),'DEMO/USDT');assert.equal(url.searchParams.get('page'),'2');assert.equal(url.searchParams.get('page_size'),'40');
  assert.equal(h.node('alertSymbolList').scrollTop,390);assert.equal(h.scroll.scrollTop,210);assert.equal(h.run('alertDetailState.data.page'),2);
});

test('rapid coin switching rejects stale detail responses',async()=>{
  const pending=[],h=harness(()=>new Promise(resolve=>pending.push(resolve)));h.context.renderAlertMasterDetail([group(),group('OTHER/USDT')]);
  const first=h.context.loadSelectedAlertHistory();h.context.selectAlertSymbol('OTHER/USDT');
  pending[1](detail('OTHER/USDT'));await Promise.resolve();await Promise.resolve();pending[0](detail('DEMO/USDT'));await first;
  assert.equal(h.run('alertDetailState.data.symbol'),'OTHER/USDT');assert.ok(!h.node('alertDetailPane').innerHTML.includes('DEMO/USDT'));
});

test('duplicate in-flight detail reads coalesce and release their request after completion',async()=>{
  let reads=0,resolve;const h=harness(()=>{reads++;return new Promise(done=>{resolve=done})});h.context.renderAlertMasterDetail([group()]);
  const first=h.context.loadSelectedAlertHistory(),second=h.context.loadSelectedAlertHistory();assert.equal(reads,1);resolve(detail());await Promise.all([first,second]);assert.equal(h.run('alertDetailRequest'),null);
});

test('failed detail reads retain the last valid page and can recover',async()=>{
  let failed=false;const h=harness(async()=>{if(failed)throw new Error('unavailable');return detail()});h.context.renderAlertMasterDetail([group()]);await h.context.loadSelectedAlertHistory();failed=true;
  assert.equal(await h.context.loadSelectedAlertHistory(2),false);assert.equal(h.run('alertDetailState.data.page'),1);assert.ok(h.node('alertDetailPane').innerHTML.includes('已保留上次记录'));
  failed=false;assert.equal(await h.context.loadSelectedAlertHistory(),true);assert.equal(h.run('alertDetailState.error'),'');
});

test('a response for the wrong coin is never installed as the current history',async()=>{
  const h=harness(async()=>detail('OTHER/USDT'));h.context.renderAlertMasterDetail([group()]);assert.equal(await h.context.loadSelectedAlertHistory(),false);assert.equal(h.run('alertDetailState.data'),null);
});

test('original message stays escaped and expanded rows survive the next render',()=>{
  const h=harness();h.context.renderAlertMasterDetail([group()]);h.context.setAlertMessageExpanded(42,true);
  const markup=h.context.alertTimelineNode({id:42,type:'rapid_basis',created_at:'10-18 10:00',message:'<img onerror=alert(1)>',long_exchange:'Gate',short_exchange:'Binance'});
  assert.ok(markup.includes('data-alert-event="42" open'));assert.ok(markup.includes('&lt;img'));assert.ok(!markup.includes('<img onerror'));h.context.setAlertMessageExpanded(42,false);assert.equal(h.run('alertDetailState.expanded.size'),0);
});

test('untrusted coin strings cannot break markup or inline action arguments',()=>{
  const h=harness(),markup=h.context.alertSymbolCard(group('BAD\"<img>/USDT'));
  assert.ok(!markup.includes('<img>'));assert.ok(markup.includes('&lt;img&gt;'));assert.ok(markup.includes('&quot;'));
});

test('the roster and details have separate keyboard-scrollable containers and compact headers',()=>{
  for(const id of ['alertSymbolList','alertDetailPane','alertSymbolSearch','alertSymbolSort','alertTotalSymbols'])assert.ok(html.includes(`id="${id}"`),id);
  assert.ok(html.includes('24小时报警币种列表，可滚动'));assert.ok(source.includes('报警明细，可滚动'));
  assert.ok(css.includes('.alert-symbol-list {display:block;flex:1 1 auto;min-height:0'));assert.ok(css.includes('.alert-timeline-scroll {min-height:0;flex:1 1 auto;overflow:auto'));
  assert.ok(css.includes('min-height:39px'));assert.equal((source.match(/function renderAlertMasterDetail\(/g)||[]).length,1);
  assert.ok(css.includes('grid-template-columns:minmax(345px,390px) minmax(0,1fr)!important'));
  assert.ok(source.includes("fetchRefreshJson('/api/alerts?view=summary')"));
});

test('sticky roster header shares the data scroll gutter, grid and horizontal padding',()=>{
  const list=html.indexOf('id="alertSymbolList"'),columns=html.indexOf('class="alert-roster-columns"');assert.ok(list<columns);
  assert.ok(source.includes('alertRosterColumnsMarkup()+(groups.length?'));
  assert.ok(css.includes(':is(.alert-roster-columns,.alert-symbol-option) {display:grid;grid-template-columns:'));
  assert.ok(css.includes('align-items:center;padding:5px 6px 5px 7px;border-left:3px solid transparent;box-sizing:border-box'));
  assert.ok(css.includes('.alert-roster-columns {position:sticky;top:0;z-index:3;'));
  assert.ok(css.includes('.alert-symbol-option > .alert-mute-toggle {justify-self:stretch;width:100%'));
});

test('detail omits repeated coin header, mute and normal status while keeping metrics, timeline and pagination',async()=>{
  const h=harness();h.context.renderAlertMasterDetail([group()]);await h.context.loadSelectedAlertHistory();const markup=h.node('alertDetailPane').innerHTML;
  for(const value of ['<header','<h4','alert-latest-label','strategy-badge','alert-mute-toggle','新到旧','共 43 条'])assert.ok(!markup.includes(value),value);
  for(const value of ['最近开差','最近基差','24H开差峰值','24H基差峰值','alert-timeline-scroll','10-18 10:00:00','alert-detail-pagination','aria-label="DEMO/USDT 报警明细"'])assert.ok(markup.includes(value),value);
  assert.ok(markup.includes('<div class="alert-detail-status" role="status"></div>'));assert.ok(css.includes('.alert-detail-status:empty {display:none}'));
  h.run('alertDetailState.loading=true');h.context.renderAlertDetailPane();assert.ok(!h.node('alertDetailPane').innerHTML.includes('正在读取明细…'));
});
