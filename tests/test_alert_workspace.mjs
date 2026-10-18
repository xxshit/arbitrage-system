import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const source=readFileSync(new URL('../static/app.js',import.meta.url),'utf8');
const html=readFileSync(new URL('../templates/index.html',import.meta.url),'utf8');
const css=readFileSync(new URL('../static/workspace-layout.css',import.meta.url),'utf8');
const functions=source.slice(source.indexOf('function isAlertMuted('),source.indexOf('function renderTipsItems('));
const helpers=source.slice(source.indexOf('function alertLabel('),source.indexOf('function toggleTips('));
const transferHelpers=source.slice(source.indexOf('function transferFlagText('),source.indexOf('function spotRows('));
const escape=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const pathKey=(strategy='spot_futures',long='Gate',short='Binance')=>JSON.stringify([strategy,long,short]);
const group=(symbol='DEMO/USDT',count=3,spread=1,basis=2)=>{
  const latest={symbol,created_at:'10-18 10:00:00',type:'rapid_basis',strategy:'spot_futures',path_id:pathKey(),open_spread:0.5,basis:1},value_ranges={open_spread:{max:10,min:-50},basis:{max:3,min:-2}};
  return {symbol,alert_count:count,latest,move_peaks:{open_spread:spread,basis},value_ranges,basis_exchange:'Binance',paths:[{id:pathKey(),strategy:'spot_futures',long_exchange:'Gate',short_exchange:'Binance',long_market:'现货',short_market:'合约',latest,value_ranges,basis_exchange:'Binance'}]};
};
const detail=(symbol='DEMO/USDT',page=1,path_id=pathKey())=>({symbol,strategy:JSON.parse(path_id)[0],path_id,total:43,page,pages:2,page_size:40,items:[{id:page,symbol,strategy:JSON.parse(path_id)[0],path_id,type:'rapid_basis',created_at:'10-18 10:00:00',message:'Synthetic detail',open_spread:0.5,basis:1,binance_basis:1,basis_exchange:'Binance'}]});

function harness(fetcher=async url=>{const params=new URL(url,'https://example.test').searchParams;return detail(params.get('symbol'),Number(params.get('page')),params.get('path'))}){
  const nodes=new Map(),scroll={scrollTop:0},transferScroll={scrollTop:0};
  const node=id=>{if(!nodes.has(id))nodes.set(id,{id,textContent:'',innerHTML:'',scrollTop:0,dataset:{},querySelector:selector=>selector==='#alertPathSelect'?null:selector==='.alert-transfer-panel'?transferScroll:scroll,scrollIntoView(){}});return nodes.get(id)};
  const context={URLSearchParams,console,clearTimeout(){},window:{innerWidth:1280},byId:node,escapeChatHtml:escape,delistingSymbolClass:()=>'',changeClass:value=>value<0?'negative':'positive',percent:value=>(value>=0?'+':'')+Number(value).toFixed(4)+'%',intervalClass:()=>'',exchangeName:escape,fetchRefreshJson:fetcher,renderTipsItems(){},
    replaceHtmlKeepingExchangeLogos:(target,markup)=>{if(target.innerHTML!==markup)target.innerHTML=markup}};
  vm.createContext(context);
  vm.runInContext("let mutedAlertSymbols=new Set(),selectedAlertSymbol='',latestAlertGroups=[],latestActiveAlertEvents=[],alertListQuery='',alertListSort='latest',alertDetailVersion=0,alertDetailRequest=null,alertDetailState={symbol:'',data:null,loading:false,error:''};\n"+transferHelpers+'\n'+helpers+'\n'+functions,context);
  return {context,node,scroll,transferScroll,run:code=>vm.runInContext(code,context)};
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

test('timeline omits the detail toggle and its message, close-spread and funding content entirely',()=>{
  const h=harness(),markup=h.context.alertTimelineNode({id:42,type:'rapid_basis',strategy:'spot_futures',created_at:'10-18 10:00',message:'<img onerror=alert(1)> hidden-message',long_exchange:'Gate',short_exchange:'Binance',open_spread:0.5,basis:-1,binance_basis:-1,basis_exchange:'Binance',close_spread:-0.6943,funding_rate:0.0035});
  for(const value of ['详细说明','<details','<summary','alert-event-message','ontoggle','hidden-message','<img onerror','平差','资费','-0.6943%','+0.0035%'])assert.ok(!markup.includes(value),value);
  for(const value of ['10-18 10:00','基差速变','+0.5000%','-1.0000%'])assert.ok(markup.includes(value),value);
  for(const value of ['现多期空','Gate','Binance','alert-exchange-pair'])assert.ok(!markup.includes(value),value);
  assert.ok(!source.includes('setAlertMessageExpanded'));assert.ok(!source.includes('alertDetailState.expanded'));assert.ok(!css.includes('.alert-event-message'));
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
  for(const value of ['最近开差','最近基差','两种策略都有数据','仅现多期空有数据','仅期多期空有数据'])assert.ok(!markup.includes(value),value);
  for(const value of ['24H开差范围','24H币安基差范围','alert-timeline-scroll','10-18 10:00:00','alert-detail-pagination','aria-label="DEMO/USDT 报警明细"'])assert.ok(markup.includes(value),value);
  assert.ok(markup.includes('<div class="alert-detail-status" role="status"></div>'));assert.ok(css.includes('.alert-detail-status:empty {display:none}'));
  h.run('alertDetailState.loading=true');h.context.renderAlertDetailPane();assert.ok(!h.node('alertDetailPane').innerHTML.includes('正在读取明细…'));
});

test('detail ranges show the full-window maximum then minimum with independently signed colors',()=>{
  const h=harness();h.context.renderAlertMasterDetail([group()]);const markup=h.node('alertDetailPane').innerHTML;
  assert.ok(markup.includes('最高 +10.0000%，最低 -50.0000%'));
  assert.ok(markup.includes('<span class="positive">+10.0000%</span><span class="alert-range-separator" aria-hidden="true">～</span><span class="negative">-50.0000%</span>'));
  assert.ok(markup.includes('最高 +3.0000%，最低 -2.0000%'));
  assert.ok(!markup.includes('24H开差峰值'));assert.ok(!markup.includes('24H基差峰值'));
  assert.ok(css.includes('.alert-metric-range {display:flex;align-items:baseline;flex-wrap:wrap;'));
});

test('Binance range is explicitly labelled and never trusts an unspecified or different basis source',()=>{
  const h=harness();h.context.renderAlertMasterDetail([group()]);let markup=h.node('alertDetailPane').innerHTML;
  assert.ok(markup.includes('24H币安基差范围'));assert.ok(markup.includes('币安合约标记价/指数基差'));
  for(const exchange of [undefined,null,'Bybit','OKX']){
    const coin=group();coin.paths[0].basis_exchange=exchange;coin.value_ranges.basis={max:999,min:-999};h.context.renderAlertMasterDetail([coin]);markup=h.node('alertDetailPane').innerHTML;
    assert.ok(markup.includes('24H币安基差范围 <b class="alert-metric-range neutral">—</b>'));
    assert.ok(!markup.includes('999.0000%'));assert.ok(markup.includes('最高 +10.0000%，最低 -50.0000%'));
  }
});

test('ranges retain all-positive, all-negative, zero and equal endpoints without inventing another sign',()=>{
  const h=harness();
  for(const [max,min,label] of [[5,1,'最高 +5.0000%，最低 +1.0000%'],[-1,-5,'最高 -1.0000%，最低 -5.0000%'],[0,0,'最高 +0.0000%，最低 +0.0000%'],[2,2,'最高 +2.0000%，最低 +2.0000%']])assert.ok(h.context.alertMetricRange({max,min}).includes(label));
});

test('missing or non-finite ranges show an unavailable marker instead of zero or the legacy single peak',()=>{
  const h=harness();
  for(const range of [undefined,null,{}, {max:null,min:null},{max:1,min:null},{max:NaN,min:1},{max:Infinity,min:-1},{max:'10',min:-1}])assert.equal(h.context.alertMetricRange(range),'<b class="alert-metric-range neutral">—</b>');
  const legacy=group();delete legacy.paths[0].value_ranges;h.context.renderAlertMasterDetail([legacy]);assert.equal((h.node('alertDetailPane').innerHTML.match(/alert-metric-range neutral/g)||[]).length,2);
});

test('a refreshed summary updates ranges without clearing the selected history page or scroll position',async()=>{
  const h=harness(async()=>detail('DEMO/USDT',2));h.context.renderAlertMasterDetail([group()]);await h.context.loadSelectedAlertHistory(2);h.scroll.scrollTop=210;
  const updated=group();updated.value_ranges.open_spread={max:2,min:-5};h.context.renderAlertMasterDetail([updated]);
  assert.ok(h.node('alertDetailPane').innerHTML.includes('最高 +2.0000%，最低 -5.0000%'));assert.ok(!h.node('alertDetailPane').innerHTML.includes('最高 +10.0000%，最低 -50.0000%'));
  assert.equal(h.run('selectedAlertSymbol'),'DEMO/USDT');assert.equal(h.run('alertDetailState.data.page'),2);assert.equal(h.scroll.scrollTop,210);
});

const wallet=(exchange='Gate',overrides={})=>({exchange,state:'fresh',chains:[{name:'BSC',deposit_open:false,withdraw_open:true}],captured_at:'10-18 11:30:00 UTC',reason:'',...overrides});

test('wallet panel groups exchanges once per coin rather than repeating statuses on every historical event',async()=>{
  const h=harness(),coin=group();coin.transfer_statuses=[wallet(),wallet('Binance',{state:'unknown',chains:[],reason:'尚未接入该交易所的账户只读充提接口'})];h.context.renderAlertMasterDetail([coin]);await h.context.loadSelectedAlertHistory();
  const markup=h.node('alertDetailPane').innerHTML;assert.equal((markup.match(/class="alert-transfer-panel/g)||[]).length,1);
  for(const text of ['Gate','Binance','充提异常','充提未知','非历史报警时状态','data-transfer-status='])assert.ok(markup.includes(text),text);
  assert.ok(!markup.includes('transfer-network-chip'));assert.equal((markup.match(/class="transfer-status-control/g)||[]).length,2);
  assert.ok(!markup.includes('详细说明'));assert.ok(!h.context.alertTimelineNode(detail().items[0]).includes('transfer-network-chip'));
});

test('wallet strips omit the heading and visible capture times without discarding freshness provenance',()=>{
  const h=harness();
  for(const state of ['fresh','stale','unknown']){
    const coin=group();coin.transfer_statuses=[wallet('Gate',{state,source_url:'https://api.gateio.ws/api/v4/spot/currencies'})];
    const markup=h.context.alertTransferPanel(coin),visible=markup.replace(/<[^>]*>/g,'');
    for(const unwanted of ['当前充提','UTC','11:30','快照采集'])assert.ok(!visible.includes(unwanted),`${state}: ${unwanted}`);
    assert.ok(!markup.includes('<time'));assert.ok(!markup.includes('alert-transfer-heading'));assert.ok(!markup.includes('transfer-updated'));
    const popover=h.context.transferPopoverMarkup(coin.transfer_statuses[0]);
    assert.ok(popover.includes('快照采集：10-18 11:30:00 UTC'));assert.ok(popover.includes('来源：https://api.gateio.ws/api/v4/spot/currencies'));
    assert.ok(visible.includes(state==='fresh'?'充提异常':'充提未知'));
  }
  assert.ok(!css.includes('.transfer-updated'));assert.ok(!css.includes('.alert-transfer-heading'));
});

test('all chains move into their exchange popup, without a first-chain, open-only or short-list cap',()=>{
  const h=harness(),coin=group(),chains=(prefix,count)=>Array.from({length:count},(_,i)=>({name:`${prefix}-NETWORK-${i}`,deposit_open:i%3===0?false:i%3===1?true:null,withdraw_open:i%2===0}));
  coin.transfer_statuses=[wallet('Gate',{chains:chains('GATE',30)}),wallet('Bitget',{chains:chains('BITGET',21)}),wallet('Binance',{state:'unknown',chains:[],reason:'尚未接入账户只读充提接口'})];
  const markup=h.context.alertTransferPanel(coin);
  assert.equal((markup.match(/class="transfer-network-chip"/g)||[]).length,0);
  const popovers=coin.transfer_statuses.map(status=>h.context.transferPopoverMarkup(status));
  assert.equal((popovers.join('').match(/class="transfer-network-chip"/g)||[]).length,51);
  for(const [index,prefix,count] of [[0,'GATE',30],[1,'BITGET',21]])for(let i=0;i<count;i++)assert.ok(popovers[index].includes(`<b>${prefix}-NETWORK-${i}</b>`));
  assert.ok(popovers[0].includes('充关'));assert.ok(popovers[0].includes('充开'));assert.ok(popovers[0].includes('充未知'));assert.ok(popovers[0].includes('提关'));
  const missing=h.context.transferStatusMarkup(wallet('Binance',{state:'unknown',chains:[]}));assert.ok(!missing.includes('transfer-network-chip'));
});

test('multi-chain reading position survives summary and detail rerenders but resets for another coin',async()=>{
  const h=harness();h.context.renderAlertMasterDetail([group(),group('OTHER/USDT')]);h.transferScroll.scrollTop=240;h.scroll.scrollTop=180;
  await h.context.loadSelectedAlertHistory();assert.equal(h.transferScroll.scrollTop,240);assert.equal(h.scroll.scrollTop,180);
  h.context.renderAlertMasterDetail([group(),group('OTHER/USDT')]);assert.equal(h.transferScroll.scrollTop,240);
  h.context.selectAlertSymbol('OTHER/USDT');assert.equal(h.transferScroll.scrollTop,0);
});

test('per-chain labels never collapse partial closures into a claim that every chain is closed',()=>{
  const h=harness(),markup=h.context.transferStatusMarkup(wallet('Gate',{chains:[{name:'ETH',deposit_open:true,withdraw_open:false},{name:'BSC',deposit_open:false,withdraw_open:true}]}));
  assert.ok(markup.includes('ETH'));assert.ok(markup.includes('BSC'));assert.equal((markup.match(/transfer-open/g)||[]).length,2);assert.equal((markup.match(/transfer-closed/g)||[]).length,2);
  assert.ok(markup.includes('充开'));assert.ok(markup.includes('提关'));assert.ok(markup.includes('充关'));assert.ok(markup.includes('提开'));
});

test('stale snapshots show current unknowns and explicitly label last capture, never current open/closed colors',()=>{
  const h=harness(),status=wallet('Bitget',{state:'stale',captured_at:'08-04 02:51:25 UTC'}),markup=h.context.transferStatusMarkup(status);
  assert.ok(markup.includes('快照过期'));assert.ok(markup.includes('08-04 02:51:25 UTC'));assert.ok(markup.includes('充未知'));assert.ok(markup.includes('提未知'));
  assert.ok(!markup.includes('class="transfer-open"'));assert.ok(!markup.includes('class="transfer-closed"'));assert.equal(h.context.transferHasRestriction(status),false);
});

test('missing, null or string-valued flags are unknown rather than false closures or green open states',()=>{
  const h=harness(),markup=h.context.transferStatusMarkup(wallet('Gate',{chains:[{name:'ETH',deposit_open:null,withdraw_open:'false'}]}));
  assert.equal((markup.match(/transfer-unknown/g)||[]).length,2);assert.equal(h.context.transferHasRestriction(wallet('Gate',{chains:[{name:'ETH'}]})),false);
  assert.ok(h.context.transferStatusMarkup(null).includes('充提未知'));assert.ok(h.context.spotTransferStatus({transfer_networks:[{deposit_open:true,withdraw_open:true}]}).includes('充提未知'));
});

test('withdrawal delay remains visible even if the explicit withdrawal switch is open',()=>{
  const h=harness(),status=wallet('Gate',{chains:[{name:'ETH',deposit_open:true,withdraw_open:true,withdraw_delayed:true}]}),markup=h.context.transferStatusMarkup(status);
  assert.ok(markup.includes('transfer-delayed'));assert.ok(markup.includes('提延迟'));assert.equal(h.context.transferHasRestriction(status),true);
});

test('untrusted exchange, chain, reason and timestamp text cannot inject HTML or attributes',()=>{
  const h=harness(),coin=group();coin.transfer_statuses=[wallet('<img onerror=1>',{state:'unknown',chains:[{name:'"><svg onload=1>',deposit_open:null}],reason:'"><img onerror=1>',captured_at:'"><script>alert(1)</script>'})];
  coin.paths[0].long_exchange='<img onerror=1>';
  for(const markup of [h.context.alertTransferPanel(coin),h.context.transferPopoverMarkup(coin.transfer_statuses[0])]){assert.ok(!markup.includes('<img'));assert.ok(!markup.includes('<svg'));assert.ok(!markup.includes('<script>'));assert.ok(markup.includes('&lt;img'))}
  assert.ok(h.context.transferPopoverMarkup(coin.transfer_statuses[0]).includes('&lt;svg'));
});

test('spot transfer summary has accessible per-chain details and marks stale data as unknown',()=>{
  const h=harness(),markup=h.context.spotTransferStatus({transfer_status:wallet()});
  for(const value of ['transfer-summary-abnormal','充提异常','type="button"','aria-haspopup="dialog"','aria-expanded="false"','data-transfer-status='])assert.ok(markup.includes(value),value);
  const stale=h.context.spotTransferStatus({transfer_status:wallet('Bitget',{state:'stale'})});assert.ok(stale.includes('transfer-summary-unknown'));assert.ok(stale.includes('充提未知'));assert.ok(!stale.includes('充提正常'));
});

test('wallet-only updates replace the current status without clearing history selection or scroll',async()=>{
  const h=harness(async()=>detail('DEMO/USDT',2)),coin=group();coin.transfer_statuses=[wallet()];h.context.renderAlertMasterDetail([coin]);await h.context.loadSelectedAlertHistory(2);h.scroll.scrollTop=210;
  const updated=group();updated.transfer_statuses=[wallet('Gate',{chains:[{name:'BSC',deposit_open:true,withdraw_open:true}]})];h.context.renderAlertMasterDetail([updated]);
  assert.ok(!h.node('alertDetailPane').innerHTML.includes('受限，价差需复核'));assert.equal(h.run('selectedAlertSymbol'),'DEMO/USDT');assert.equal(h.run('alertDetailState.data.page'),2);assert.equal(h.scroll.scrollTop,210);
});

function addPath(coin,strategy='spot_futures',long='Bitget',short='Binance',range={max:2,min:-1}){
  const path={...coin.paths[0],id:pathKey(strategy,long,short),strategy,long_exchange:long,short_exchange:short,long_market:strategy==='spot_futures'?'现货':'合约',short_market:'合约',value_ranges:{open_spread:range,basis:{max:1,min:-1}},basis_exchange:short==='Binance'||long==='Binance'?'Binance':null};
  path.latest={...coin.latest,strategy,path_id:path.id};coin.paths.push(path);return path;
}

test('only present strategies appear, with no mockup scenario captions or ghost tabs',()=>{
  const h=harness(),coin=group();h.context.renderAlertMasterDetail([coin]);let markup=h.node('alertDetailPane').innerHTML;
  assert.equal((markup.match(/onclick="setAlertStrategy/g)||[]).length,1);assert.ok(!markup.includes('>期多期空</button>'));
  const dual=addPath(coin,'futures_futures','Bybit','OKX');coin.paths=[dual];h.context.renderAlertMasterDetail([coin]);markup=h.node('alertDetailPane').innerHTML;
  assert.equal((markup.match(/onclick="setAlertStrategy/g)||[]).length,1);assert.ok(!markup.includes('>现多期空</button>'));assert.ok(markup.includes('>期多期空</button>'));
  for(const text of ['两种都有数据','两种策略都有数据','仅现多期空有数据','仅期多期空有数据'])assert.ok(!markup.includes(text));
});

test('two strategies have separate path menus and switching resets page and timeline scroll',async()=>{
  const h=harness(),coin=group(),spot=addPath(coin),dual=addPath(coin,'futures_futures','Bybit','OKX');h.context.renderAlertMasterDetail([coin]);await h.context.loadSelectedAlertHistory(2);h.scroll.scrollTop=230;
  assert.equal((h.node('alertDetailPane').innerHTML.match(/onclick="setAlertStrategy/g)||[]).length,2);
  await h.context.setAlertStrategy('futures_futures');const markup=h.node('alertDetailPane').innerHTML;
  assert.equal(h.run('alertDetailState.pathId'),dual.id);assert.equal(h.run('alertDetailState.data.page'),1);assert.equal(h.scroll.scrollTop,0);
  assert.ok(markup.includes('Bybit合约 → OKX合约'));assert.ok(!markup.includes('Bitget现货'));assert.ok(markup.includes('现货充提 · 非合约权限'));
  await h.context.setAlertStrategy('spot_futures');await h.context.setAlertPath(spot.id);assert.ok(h.node('alertDetailPane').innerHTML.includes('Bitget现货 → 币安合约'));
});

test('path-specific ranges and wallet statuses never borrow a different venue or global aggregate',async()=>{
  const h=harness(),coin=group(),bitget=addPath(coin);coin.transfer_statuses=[wallet(),wallet('Bitget',{chains:[{name:'BEP20',deposit_open:true,withdraw_open:true}]}),wallet('Binance',{state:'unknown',chains:[]})];
  h.context.renderAlertMasterDetail([coin]);await h.context.setAlertPath(bitget.id);const markup=h.node('alertDetailPane').innerHTML;
  assert.ok(markup.includes('最高 +2.0000%，最低 -1.0000%'));assert.ok(!markup.includes('最高 +10.0000%，最低 -50.0000%'));
  assert.equal((markup.match(/class="transfer-status-control/g)||[]).length,2);assert.ok(markup.includes('充提正常'));assert.ok(!markup.includes('充提异常'));assert.ok(!markup.includes('<b>Gate</b>'));
});

test('every filtered request carries strategy and exact directional path, not just a coin name',async()=>{
  let url;const h=harness(async value=>{url=new URL(value,'https://example.test');return detail('DEMO/USDT',1,url.searchParams.get('path'))}),coin=group(),path=addPath(coin,'futures_futures','OKX','Binance');
  h.context.renderAlertMasterDetail([coin]);await h.context.setAlertPath(path.id);
  assert.equal(url.searchParams.get('strategy'),'futures_futures');assert.equal(url.searchParams.get('path'),path.id);assert.equal(url.searchParams.get('page'),'1');
});

test('a non-Binance path and its legacy zero or foreign basis samples display unavailable',async()=>{
  const h=harness(),coin=group(),path=addPath(coin,'futures_futures','Bybit','OKX');path.value_ranges.basis={max:999,min:0};coin.paths=[path];h.context.renderAlertMasterDetail([coin]);await h.context.loadSelectedAlertHistory();
  assert.ok(h.node('alertDetailPane').innerHTML.includes('24H币安基差范围 <b class="alert-metric-range neutral">—</b>'));
  for(const source of [undefined,null,'Bybit']){const markup=h.context.alertTimelineNode({...detail().items[0],basis:0,binance_basis:999,basis_exchange:source});assert.ok(!markup.includes('999.0000%'));assert.ok(!markup.includes('+0.0000%'))}
  assert.ok(h.context.alertTimelineNode({...detail().items[0],binance_basis:0,basis_exchange:'Binance'}).includes('+0.0000%'));
});

test('rapid same-coin path changes reject old responses even when switching back to the first path',async()=>{
  const pending=[],h=harness(url=>new Promise(resolve=>pending.push({url:new URL(url,'https://example.test'),resolve}))),coin=group(),other=addPath(coin);h.context.renderAlertMasterDetail([coin]);
  const first=h.context.loadSelectedAlertHistory(),second=h.context.setAlertPath(other.id),third=h.context.setAlertPath(coin.paths[0].id);
  pending[2].resolve(detail('DEMO/USDT',2,pending[2].url.searchParams.get('path')));await third;
  pending[1].resolve(detail('DEMO/USDT',1,pending[1].url.searchParams.get('path')));await second;pending[0].resolve(detail());await first;
  assert.equal(h.run('alertDetailState.pathId'),coin.paths[0].id);assert.equal(h.run('alertDetailState.data.page'),2);assert.equal(h.run('alertDetailState.data.path_id'),coin.paths[0].id);
});

test('wrong-path responses or individual rows are rejected even if they name the right coin',async()=>{
  for(const rowOnly of [false,true]){
    const h=harness(async()=>{const response=detail();if(rowOnly)response.items[0].path_id=pathKey('spot_futures','Bitget');else response.path_id=pathKey('spot_futures','Bitget');return response});
    h.context.renderAlertMasterDetail([group()]);assert.equal(await h.context.loadSelectedAlertHistory(),false);assert.equal(h.run('alertDetailState.data'),null);
  }
});

test('refreshes preserve the selected path and page, then remove expired choices without stale data',async()=>{
  const h=harness(),coin=group(),other=addPath(coin);h.context.renderAlertMasterDetail([coin]);await h.context.setAlertPath(other.id);await h.context.loadSelectedAlertHistory(2);h.scroll.scrollTop=217;
  const updated=structuredClone(coin);updated.paths.reverse();updated.paths[0].value_ranges.open_spread={max:4,min:-3};h.context.renderAlertMasterDetail([updated]);
  assert.equal(h.run('alertDetailState.pathId'),other.id);assert.equal(h.run('alertDetailState.data.page'),2);assert.equal(h.scroll.scrollTop,217);assert.ok(h.node('alertDetailPane').innerHTML.includes('最高 +4.0000%，最低 -3.0000%'));
  updated.paths=updated.paths.filter(path=>path.id!==other.id);h.context.renderAlertMasterDetail([updated]);assert.equal(h.run('alertDetailState.pathId'),coin.paths[0].id);assert.equal(h.run('alertDetailState.data'),null);assert.equal(h.scroll.scrollTop,0);
});

test('missing path metadata cannot re-enable globally mixed ranges or fake paths',async()=>{
  const h=harness(),coin=group();delete coin.paths;h.context.renderAlertMasterDetail([coin]);assert.equal(await h.context.loadSelectedAlertHistory(),false);
  const markup=h.node('alertDetailPane').innerHTML;assert.ok(markup.includes('暂无可用路径记录'));assert.ok(!markup.includes('最高 +10.0000%'));assert.ok(!markup.includes('id="alertPathSelect"'));
});

test('path menu escapes untrusted labels and identities and missing venues remain explicitly unrecorded',()=>{
  const h=harness(),coin=group(),path=addPath(coin,'futures_futures','<img onerror=1>','OKX');coin.paths=[path];h.context.renderAlertMasterDetail([coin]);const markup=h.node('alertDetailPane').innerHTML;
  assert.ok(!markup.includes('<img'));assert.ok(markup.includes('&lt;img'));assert.ok(markup.includes('&quot;'));
  assert.equal(h.context.alertPathLabel({long_market:'现货',short_exchange:'Binance',short_market:'合约'}),'现货所未记录 → 币安合约');
});

test('mobile path selectors retain a readable non-zooming font and touch height',()=>{
  assert.ok(css.includes('.alert-path-field select {font-size:16px;min-height:40px}'));
  assert.ok(css.includes('.alert-strategy-switch button {min-height:36px}'));
});

test('read errors surface the bounded escaped reason without claiming nonexistent history was retained',async()=>{
  const h=harness(async()=>{throw new Error('<img src=x onerror=1>')});h.context.renderAlertMasterDetail([group()]);assert.equal(await h.context.loadSelectedAlertHistory(),false);
  const markup=h.node('alertDetailPane').innerHTML;assert.ok(markup.includes('明细读取失败：&lt;img'));assert.ok(!markup.includes('<img'));assert.ok(!markup.includes('已保留上次记录'));
});
