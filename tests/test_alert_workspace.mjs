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
const exchangeHelpers=source.slice(source.indexOf('const exchangeIcon='),source.indexOf('\n',source.indexOf('const exchangeIcon=')))+'\n'+source.slice(source.indexOf('function exchangeName('),source.indexOf('function syncRenderedNode('));
const escape=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const pathKey=(strategy='spot_futures',long='Gate',short='Binance')=>JSON.stringify([strategy,long,short]);
const group=(symbol='DEMO/USDT',count=3,spread=1,basis=2)=>{
  const latest={symbol,created_at:'10-18 10:00:00',type:'rapid_basis',strategy:'spot_futures',path_id:pathKey(),open_spread:0.5,basis:1},value_ranges={open_spread:{max:10,min:-50},basis:{max:3,min:-2}};
  return {symbol,alert_count:count,latest,move_peaks:{open_spread:spread,basis},value_ranges,basis_exchange:'Binance',paths:[{id:pathKey(),strategy:'spot_futures',long_exchange:'Gate',short_exchange:'Binance',long_market:'现货',short_market:'合约',latest,value_ranges,basis_exchange:'Binance'}]};
};
const detail=(symbol='DEMO/USDT',page=1,path_id=pathKey())=>({symbol,strategy:JSON.parse(path_id)[0],path_id,total:43,page,pages:2,page_size:40,items:[{id:page,symbol,strategy:JSON.parse(path_id)[0],path_id,type:'rapid_basis',created_at:'10-18 10:00:00',message:'Synthetic detail',open_spread:0.5,basis:1,binance_basis:1,basis_exchange:'Binance'}]});

function harness(fetcher=async url=>{const params=new URL(url,'https://example.test').searchParams;return detail(params.get('symbol'),Number(params.get('page')),params.get('path'))}){
  const nodes=new Map(),scroll={scrollTop:0},transferScroll={scrollTop:0};
  const node=id=>{if(!nodes.has(id))nodes.set(id,{id,textContent:'',innerHTML:'',scrollTop:0,dataset:{},querySelectorAll:()=>[],querySelector:selector=>selector==='#alertPathSelect'?null:selector==='.alert-transfer-panel'?transferScroll:scroll,scrollIntoView(){}});return nodes.get(id)};
  const context={URLSearchParams,console,clearTimeout(){},window:{innerWidth:1280},byId:node,escapeChatHtml:escape,delistingSymbolClass:()=>'',changeClass:value=>value<0?'negative':'positive',percent:value=>(value>=0?'+':'')+Number(value).toFixed(4)+'%',intervalClass:()=>'',exchangeName:escape,fetchRefreshJson:fetcher,renderTipsItems(){},
    replaceHtmlKeepingExchangeLogos:(target,markup)=>{if(target.innerHTML!==markup)target.innerHTML=markup}};
  vm.createContext(context);
  vm.runInContext("let mutedAlertSymbols=new Set(),selectedAlertSymbol='',latestAlertGroups=[],latestActiveAlertEvents=[],alertListQuery='',alertListSort='latest',alertScope='history',alertNotional=1000,alertDetailVersion=0,alertDetailRequest=null,alertDetailState={symbol:'',data:null,loading:false,error:''};\n"+exchangeHelpers+'\n'+transferHelpers+'\n'+helpers+'\n'+functions,context);
  return {context,node,scroll,transferScroll,run:code=>vm.runInContext(code,context)};
}

test('pulse trace retains peak after retracement and identifies a restart below the old high',()=>{
  const h=harness(),pulse={id:'sample',ordinal:2,metric:'spread',state:'retraced',baseline:.03,baseline_at:0,peak:1.02,peak_at:10,current:.11,current_at:15,retrace_at:15};
  const item={type:'rapid_spread',pulse,created_at:'10-19 00:00:00',open_spread:.3,binance_basis:0,basis_exchange:'Binance'};
  const markup=h.context.alertTimelineNode(item);
  for(const expected of ['已回落','第2次','+0.03%','+1.02%','+0.11%','5.0秒回落'])assert.ok(markup.includes(expected),expected);
  assert.ok(!markup.includes('详细说明'));assert.ok(!markup.includes('主力正在'));
});

test('gap and ended pulse never present their last quote as a live current value',()=>{
  const h=harness(),pulse={metric:'basis',state:'data_gap',baseline:0,baseline_at:0,peak:1,peak_at:1,current:.2,current_at:2};
  assert.ok(h.context.alertPulseTrace({pulse}).includes('<dt>末次</dt><dd>+0.20%'));
  assert.ok(h.context.alertPulseLabel({pulse}).includes('数据中断'));
});

test('noise filtering and empty responses cannot silently unmute a user-selected coin',()=>{
  const h=harness();h.run("mutedAlertSymbols.add('NOISE/USDT');pruneAlertMutes([],[])");
  assert.equal(h.run("mutedAlertSymbols.has('NOISE/USDT')"),true);
});

test('restart and negative pulses use unambiguous labels',()=>{
  const h=harness();
  assert.ok(h.context.alertPulseLabel({pulse:{metric:'spread',state:'watch',ordinal:2}}).includes('再次启动'));
  const pulse={metric:'spread',state:'retraced',direction:-1,baseline:0,baseline_at:0,peak:-1,peak_at:1,current:-.1,retrace_at:2};
  assert.ok(h.context.alertPulseLabel({pulse}).includes('已反弹'));
  assert.ok(h.context.alertPulseTrace({pulse}).includes('<dt>低点</dt><dd>-1.00%'));
  assert.ok(h.context.alertPulseTrace({pulse}).includes('1.0秒反弹'));
});

test('range repeats are marked record-only without implying a new reminder count',()=>{
  const h=harness(),item={pulse:{metric:'spread',state:'watch',ordinal:30},reminder:{record_only:true}};
  const label=h.context.alertPulseLabel(item);
  assert.ok(label.includes('仅记录'));assert.ok(!label.includes('第30次'));
  assert.ok(h.context.alertRosterColumnsMarkup().includes('<span>提醒</span>'));
  assert.ok(html.includes('20261019-alert-range-reminder-v1'));
});

test('price trace converts log prices into relative moves instead of showing a log-price as percent',()=>{
  const h=harness(),pulse={metric:'price',baseline:100*Math.log(100),baseline_at:0,peak:100*Math.log(102),peak_at:1,current:100*Math.log(101)};
  const markup=h.context.alertPulseTrace({pulse});assert.ok(markup.includes('+2.00%'));assert.ok(markup.includes('+1.00%'));
});

test('screenshot case emphasizes a 1.24 percentage-point fall and hides the three-value trace by default',()=>{
  const h=harness(),pulse={metric:'spread',direction:-1,state:'settled',baseline:.04,baseline_at:100,peak:-1.2,peak_at:112.8,current:-1.2,ended_at:114},original=JSON.stringify(pulse);
  const markup=h.context.alertPulseTrace({id:42,pulse}),summary=markup.match(/<summary[^>]*>(.*?)<\/summary>/s)[1].replace(/<[^>]+>/g,'');
  for(const text of ['↓','开差下降','1.24','个百分点','12.8秒内','详情'])assert.ok(summary.includes(text),text);
  for(const text of ['起点','低点','末次','+0.04%','-1.20%','→'])assert.ok(!summary.includes(text),text);
  assert.ok(markup.includes('pulse-fall'));assert.ok(!markup.match(/<details[^>]*\sopen/));
  for(const text of ['<dt>起点</dt><dd>+0.04%','<dt>低点</dt><dd>-1.20%','<dt>末次</dt><dd>-1.20%','非实时报价'])assert.ok(markup.includes(text),text);
  assert.equal(JSON.stringify(pulse),original);
});

test('direction describes the change, not the sign of the final spread or a claim about coin prices',()=>{
  const h=harness(),markup=h.context.alertPulseTrace({pulse:{metric:'spread',baseline:-1.2,peak:-.4,current:-.5,baseline_at:1,peak_at:3}});
  assert.ok(markup.includes('pulse-rise'));assert.ok(markup.includes('开差上升'));assert.ok(markup.includes('0.80'));
  assert.ok(markup.includes('开差变化，不代表币价涨跌'));assert.ok(!markup.includes('开差上涨'));
});

test('basis and relative price summaries keep distinct names and units',()=>{
  const h=harness(),basis=h.context.alertPulseTrace({pulse:{metric:'basis',baseline:.2,peak:.7,current:.5}});
  assert.ok(basis.includes('币安基差上升'));assert.ok(basis.includes('0.50'));assert.ok(basis.includes('个百分点'));
  for(const ratio of [1.02,.98]){
    const price=h.context.alertPulseTrace({pulse:{metric:'price',baseline:100*Math.log(100),peak:100*Math.log(100*ratio),current:100*Math.log(100)}});
    assert.ok(price.includes(ratio>1?'双端价格上涨':'双端价格下跌'));assert.ok(price.includes('2.00<span class="alert-pulse-unit">%'));assert.ok(!price.includes('个百分点'));
  }
});

test('missing or reversed timestamps and current values are not rendered as fabricated zeroes',()=>{
  const h=harness();
  for(const [baseline_at,peak_at] of [[undefined,12],[2,1],[NaN,1],[0,Infinity]]){
    const markup=h.context.alertPulseTrace({pulse:{metric:'spread',baseline:0,peak:1,current:null,baseline_at,peak_at,retrace_at:0}});
    assert.ok(markup.includes('耗时未知'));assert.ok(markup.includes('<dt>当前</dt><dd>—'));assert.ok(!markup.includes('NaN'));assert.ok(!markup.includes('Infinity'));assert.ok(!markup.includes('0.0秒'));
  }
});

test('invalid metrics and non-finite changes are omitted, identifiers cannot inject attributes',()=>{
  const h=harness();
  for(const pulse of [undefined,...['<img>','constructor','__proto__'].map(metric=>({metric,baseline:0,peak:1})),{metric:'spread',baseline:null,peak:1},{metric:'spread',baseline:0,peak:Infinity},{metric:'price',baseline:-1e8,peak:1e8}])assert.equal(h.context.alertPulseTrace({pulse}),'');
  const markup=h.context.alertPulseTrace({id:'" onclick="bad()',pulse:{metric:'spread',id:'<img>',baseline:0,peak:1,current:1}},true);
  assert.ok(markup.includes(' open>'));assert.ok(!markup.includes(' onclick="bad()'));assert.ok(!markup.includes('<img>'));
});

test('pulse details preserve expansion and keyboard focus only for the matching record',()=>{
  const h=harness(),focus={},key='same-record';let focused=0;
  const row={open:true,dataset:{alertPulse:key},contains:node=>node===focus,querySelector:()=>({focus:options=>{assert.ok(options.preventScroll);focused++}})},root={ownerDocument:{activeElement:focus},querySelectorAll:()=>[row]};
  const state=h.context.alertPulseViewState(root);assert.ok(state.expanded.has(key));assert.equal(state.focused,key);
  h.context.restoreAlertPulseFocus(root,key);assert.equal(focused,1);h.context.restoreAlertPulseFocus(root,'another-record');assert.equal(focused,1);
  const cleared=h.context.alertPulseViewState(root,false);assert.equal(cleared.expanded.size,0);assert.equal(cleared.focused,undefined);
});

test('detail refresh keeps expanded values, while changing coin does not reuse the expansion',async()=>{
  const payload=detail();payload.items[0].pulse={id:'sample',metric:'spread',baseline:.04,peak:-1.2,current:-1.2,baseline_at:0,peak_at:12.8};
  const h=harness(async()=>payload);h.context.renderAlertMasterDetail([group(),group('OTHER/USDT')]);await h.context.loadSelectedAlertHistory();
  const key=h.context.alertPulseKey(payload.items[0]);h.node('alertDetailPane').querySelectorAll=()=>[{open:true,dataset:{alertPulse:key},contains:()=>false}];
  h.context.renderAlertDetailPane();assert.ok(h.node('alertDetailPane').innerHTML.match(/<details[^>]*\sopen/));
  h.run("selectedAlertSymbol='OTHER/USDT'");h.context.renderAlertMasterDetail([group(),group('OTHER/USDT')]);assert.ok(!h.node('alertDetailPane').innerHTML.match(/<details[^>]*\sopen/));
});

test('tips use the same simplified summary and retain open details across updates',()=>{
  const h=harness(),item={id:42,symbol:'DEMO/USDT',strategy:'spot_futures',type:'rapid_spread',attention:{tier:'candidate'},pulse:{metric:'spread',baseline:0,peak:1,current:1}},key=h.context.alertPulseKey(item);
  h.node('tipsItems').querySelectorAll=()=>[{open:true,dataset:{alertPulse:key},contains:()=>false}];
  vm.runInContext(source.slice(source.indexOf('function renderTipsItems('),source.indexOf('async function loadAlerts(')),h.context);
  h.context.renderTipsItems([item]);assert.ok(h.node('tipsItems').innerHTML.includes('开差上升'));assert.ok(h.node('tipsItems').innerHTML.match(/<details[^>]*\sopen/));
});

test('pulse summaries have readable emphasis, explicit direction and native keyboard disclosure',()=>{
  assert.ok(css.includes('summary:focus-visible'));assert.ok(css.includes('.alert-pulse-move > b {white-space:nowrap;font-size:14px'));
  for(const text of ['.pulse-fall','.pulse-rise','flex-wrap:wrap','grid-template-columns:repeat(3,minmax(0,1fr))'])assert.ok(css.includes(text));
  assert.ok(html.includes('20261019-alert-pulse-clear-v1'));
});

test('pulse labels do not interpolate untrusted backend strings',()=>{
  const h=harness(),markup=h.context.alertPulseLabel({pulse:{metric:'<script>',state:'<img>',ordinal:'<img>'}});
  assert.ok(!markup.includes('<'));assert.ok(markup.includes('已记录'));
});

test('compact coin rows contain name, count, mute and signed peaks but no timestamps or large metadata cards',()=>{
  const h=harness(),markup=h.context.alertSymbolCard(group());
  for(const value of ['DEMO/USDT','alert-symbol-count','alert-symbol-peak','静音','+1.00%','+2.00%'])assert.ok(markup.includes(value),value);
  for(const value of ['10-18','alert-symbol-meta','alert-latest-metrics','最近开差','最近基差'])assert.ok(!markup.includes(value),value);
  assert.ok(markup.includes('aria-pressed="false"'));assert.ok(markup.includes('查看详情'));
});

test('roster peaks round to two decimals, normalize negative zero and preserve missing values',()=>{
  const h=harness();
  for(const [value,expected] of [[-6.9763,'-6.98%'],[0.3789,'+0.38%'],[-76.7095,'-76.71%'],[10,'+10.00%'],[0,'+0.00%'],[-0.0001,'+0.00%'],[null,'—'],[undefined,'—'],[NaN,'—'],[Infinity,'—']])assert.equal(h.context.alertCompactMetric(value),expected);
});

test('workspace rounding does not modify raw peaks, peak sorting or other percentage formatting',()=>{
  const h=harness(),first=group('FIRST/USDT',3,-6.9763,0.3789),second=group('SECOND/USDT',3,-6.9764,0.3791),original=JSON.stringify(first);
  const markup=h.context.alertSymbolCard(first);
  assert.ok(markup.includes('-6.98%'));assert.ok(markup.includes('+0.38%'));assert.equal(JSON.stringify(first),original);
  h.context.renderAlertMasterDetail([first,second]);h.context.sortAlertSymbols('open_spread');assert.equal(h.context.visibleAlertGroups()[0].symbol,'SECOND/USDT');
  assert.equal(h.context.alertMetric(-6.9763),'-6.9763%');assert.ok(h.context.alertMetricRange({max:0.3789,min:-6.9763}).includes('+0.38%'));
});

test('detail rows round only their displayed spread and sourced Binance basis without changing raw values',()=>{
  const h=harness(),row={...detail().items[0],open_spread:-76.7095,binance_basis:0.3789},original=JSON.stringify(row),markup=h.context.alertTimelineNode(row);
  assert.ok(markup.includes('-76.71%'));assert.ok(markup.includes('+0.38%'));assert.ok(!markup.includes('-76.7095%'));assert.equal(JSON.stringify(row),original);
  const missing=h.context.alertTimelineNode({...row,open_spread:null,binance_basis:undefined});assert.equal((missing.match(/>—<\/span>/g)||[]).length,2);assert.ok(!missing.includes('+0.00%'));
});

test('range endpoints and accessible labels share two decimals without mutating full precision bounds',()=>{
  const h=harness(),range={max:1.36431,min:-2.62568},original=JSON.stringify(range),markup=h.context.alertMetricRange(range);
  assert.ok(markup.includes('aria-label="最高 +1.36%，最低 -2.63%"'));assert.ok(markup.includes('>+1.36%</span>'));assert.ok(markup.includes('>-2.63%</span>'));assert.equal(JSON.stringify(range),original);
  assert.ok(h.context.alertMetricRange({max:0.0049,min:-0.0049}).includes('最高 +0.00%，最低 +0.00%'));
});

test('all 168 coins are rendered without an 80-coin list or viewport limit',()=>{
  const h=harness(),groups=Array.from({length:168},(_,i)=>group(`COIN${i}/USDT`));h.context.renderAlertMasterDetail(groups);
  assert.equal((h.node('alertSymbolList').innerHTML.match(/data-alert-symbol=/g)||[]).length,168);
  assert.equal(h.node('alertTotalSymbols').textContent,168);assert.equal(h.node('alertTotalEvents').textContent,504);
});

test('default candidate scope is separate from watch and uncapped 24h history',()=>{
  const h=harness(),candidate={...group('GOOD/USDT'),attention:{tier:'candidate',live:true,net_edge:.5}},watch={...group('EARLY/USDT'),attention:{tier:'watch',live:true}},old=group('OLD/USDT');
  h.run("alertScope='candidate'");h.context.renderAlertMasterDetail([old,watch,candidate]);
  assert.deepEqual(Array.from(h.context.visibleAlertGroups(),g=>g.symbol),['GOOD/USDT']);
  assert.equal(h.run('selectedAlertSymbol'),'GOOD/USDT');
  h.context.setAlertScope('watch');assert.equal(h.context.visibleAlertGroups()[0].symbol,'EARLY/USDT');
  h.context.setAlertScope('history');assert.equal(h.context.visibleAlertGroups().length,3);
  assert.ok(source.includes("let alertScope = 'candidate'"));
});

test('no qualifying candidate is honestly empty and does not hide history',()=>{
  const h=harness();h.run("alertScope='candidate'");h.context.renderAlertMasterDetail([group()]);
  assert.equal(h.context.visibleAlertGroups().length,0);assert.equal(h.run('selectedAlertSymbol'),'');
  assert.ok(h.node('alertSymbolList').innerHTML.includes('暂无通过成本与深度筛选'));
  h.context.setAlertScope('history');assert.equal(h.context.visibleAlertGroups().length,1);
});

test('candidate detail starts on qualifying path and does not borrow another path status',()=>{
  const h=harness(),coin=group();coin.attention={tier:'candidate',live:true};
  coin.paths[0].attention={tier:'record',live:false,reason:'仅异动记录'};
  coin.paths.push({...coin.paths[0],id:pathKey('spot_futures','Bitget'),long_exchange:'Bitget',attention:{tier:'candidate',live:true,net_edge:.51,reason:'条件候选'}});
  h.run("alertScope='candidate'");h.context.renderAlertMasterDetail([coin]);
  assert.equal(h.run('alertDetailState.pathId'),pathKey('spot_futures','Bitget'));
  assert.ok(h.node('alertDetailPane').innerHTML.includes('估算净空间 +0.51%'));
  h.context.setAlertPath(pathKey());assert.ok(!h.node('alertDetailPane').innerHTML.includes('估算净空间 +0.51%'));
});

test('clearing failed live checks preserves history but removes actionable status and tips',()=>{
  const h=harness(),coin={...group(),attention:{tier:'candidate',live:true}};h.context.renderAlertMasterDetail([coin]);
  h.context.clearAlertCurrent('行情读取失败');
  assert.equal(h.run('latestAlertGroups.length'),1);assert.equal(h.run('latestAlertGroups[0].attention.live'),false);
  assert.equal(h.run('latestActiveAlertEvents.length'),0);assert.equal(h.node('tipsCount').textContent,0);
});

test('model assumptions are compact and escaped, reversal never silently changes history path',()=>{
  const h=harness(),markup=h.context.alertAttentionMarkup({tier:'candidate',net_edge:.3333,notional:1000,reason:'<img src=x>',direction:'reverse',long_exchange:'Binance',short_exchange:'Bybit'});
  assert.ok(!markup.includes('<img src=x>'));assert.ok(markup.includes('反向：Binance多 → Bybit空'));
  for(const text of ['+0.33%','50%','非保证回归','四笔','8小时'])assert.ok(markup.includes(text));
});

test('amount changes cannot reuse an old in-flight detail response',async()=>{
  const pending=[],h=harness(()=>new Promise(resolve=>pending.push(resolve)));
  h.context.renderAlertMasterDetail([group()]);const first=h.context.loadSelectedAlertHistory();
  h.run('alertNotional=2000');const second=h.context.loadSelectedAlertHistory();assert.equal(pending.length,2);
  pending[1](detail());await second;pending[0]({...detail(),page:99});await first;
  assert.equal(h.run('alertDetailState.data.page'),1);
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
  for(const value of ['10-18 10:00','基差速变','+0.50%','-1.00%'])assert.ok(markup.includes(value),value);
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
  assert.ok(source.includes('fetchRefreshJson(`/api/alerts?view=summary&notional=${requestedNotional}`)'));
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
  assert.ok(markup.includes('最高 +10.00%，最低 -50.00%'));
  assert.ok(markup.includes('<span class="positive">+10.00%</span><span class="alert-range-separator" aria-hidden="true">～</span><span class="negative">-50.00%</span>'));
  assert.ok(markup.includes('最高 +3.00%，最低 -2.00%'));
  assert.ok(!markup.includes('24H开差峰值'));assert.ok(!markup.includes('24H基差峰值'));
  assert.ok(css.includes('.alert-metric-range {display:flex;align-items:baseline;flex-wrap:wrap;'));
});

test('Binance range is explicitly labelled and never trusts an unspecified or different basis source',()=>{
  const h=harness();h.context.renderAlertMasterDetail([group()]);let markup=h.node('alertDetailPane').innerHTML;
  assert.ok(markup.includes('24H币安基差范围'));assert.ok(markup.includes('币安合约标记价/指数基差'));
  for(const exchange of [undefined,null,'Bybit','OKX']){
    const coin=group();coin.paths[0].basis_exchange=exchange;coin.value_ranges.basis={max:999,min:-999};h.context.renderAlertMasterDetail([coin]);markup=h.node('alertDetailPane').innerHTML;
    assert.ok(markup.includes('24H币安基差范围 <b class="alert-metric-range neutral">—</b>'));
    assert.ok(!markup.includes('999.00%'));assert.ok(markup.includes('最高 +10.00%，最低 -50.00%'));
  }
});

test('ranges retain all-positive, all-negative, zero and equal endpoints without inventing another sign',()=>{
  const h=harness();
  for(const [max,min,label] of [[5,1,'最高 +5.00%，最低 +1.00%'],[-1,-5,'最高 -1.00%，最低 -5.00%'],[0,0,'最高 +0.00%，最低 +0.00%'],[2,2,'最高 +2.00%，最低 +2.00%']])assert.ok(h.context.alertMetricRange({max,min}).includes(label));
});

test('missing or non-finite ranges show an unavailable marker instead of zero or the legacy single peak',()=>{
  const h=harness();
  for(const range of [undefined,null,{}, {max:null,min:null},{max:1,min:null},{max:NaN,min:1},{max:Infinity,min:-1},{max:'10',min:-1}])assert.equal(h.context.alertMetricRange(range),'<b class="alert-metric-range neutral">—</b>');
  const legacy=group();delete legacy.paths[0].value_ranges;h.context.renderAlertMasterDetail([legacy]);assert.equal((h.node('alertDetailPane').innerHTML.match(/alert-metric-range neutral/g)||[]).length,2);
});

test('a refreshed summary updates ranges without clearing the selected history page or scroll position',async()=>{
  const h=harness(async()=>detail('DEMO/USDT',2));h.context.renderAlertMasterDetail([group()]);await h.context.loadSelectedAlertHistory(2);h.scroll.scrollTop=210;
  const updated=group();updated.value_ranges.open_spread={max:2,min:-5};h.context.renderAlertMasterDetail([updated]);
  assert.ok(h.node('alertDetailPane').innerHTML.includes('最高 +2.00%，最低 -5.00%'));assert.ok(!h.node('alertDetailPane').innerHTML.includes('最高 +10.00%，最低 -50.00%'));
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
  for(const markup of [h.context.alertTransferPanel(coin),h.context.transferPopoverMarkup(coin.transfer_statuses[0])]){assert.ok(!markup.includes('<img onerror'));assert.ok(!markup.includes('<svg'));assert.ok(!markup.includes('<script>'));assert.ok(markup.includes('&lt;img'))}
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
  assert.ok(markup.includes('最高 +2.00%，最低 -1.00%'));assert.ok(!markup.includes('最高 +10.00%，最低 -50.00%'));
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
  for(const source of [undefined,null,'Bybit']){const markup=h.context.alertTimelineNode({...detail().items[0],basis:0,binance_basis:999,basis_exchange:source});assert.ok(!markup.includes('999.00%'));assert.ok(!markup.includes('+0.00%'))}
  assert.ok(h.context.alertTimelineNode({...detail().items[0],binance_basis:0,basis_exchange:'Binance'}).includes('+0.00%'));
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
  assert.equal(h.run('alertDetailState.pathId'),other.id);assert.equal(h.run('alertDetailState.data.page'),2);assert.equal(h.scroll.scrollTop,217);assert.ok(h.node('alertDetailPane').innerHTML.includes('最高 +4.00%，最低 -3.00%'));
  updated.paths=updated.paths.filter(path=>path.id!==other.id);h.context.renderAlertMasterDetail([updated]);assert.equal(h.run('alertDetailState.pathId'),coin.paths[0].id);assert.equal(h.run('alertDetailState.data'),null);assert.equal(h.scroll.scrollTop,0);
});

test('missing path metadata cannot re-enable globally mixed ranges or fake paths',async()=>{
  const h=harness(),coin=group();delete coin.paths;h.context.renderAlertMasterDetail([coin]);assert.equal(await h.context.loadSelectedAlertHistory(),false);
  const markup=h.node('alertDetailPane').innerHTML;assert.ok(markup.includes('暂无可用路径记录'));assert.ok(!markup.includes('最高 +10.00%'));assert.ok(!markup.includes('id="alertPathSelect"'));
});

test('path menu escapes untrusted labels and identities and missing venues remain explicitly unrecorded',()=>{
  const h=harness(),coin=group(),path=addPath(coin,'futures_futures','<img onerror=1>','OKX');coin.paths=[path];h.context.renderAlertMasterDetail([coin]);const markup=h.node('alertDetailPane').innerHTML;
  assert.ok(!markup.includes('<img onerror'));assert.ok(markup.includes('&lt;img'));assert.ok(markup.includes('&quot;'));
  assert.equal(h.context.alertPathLabel({long_market:'现货',short_exchange:'Binance',short_market:'合约'}),'现货所未记录 → 币安合约');
});

test('mobile path selectors retain a readable non-zooming font and touch height',()=>{
  assert.ok(css.includes('.alert-path-field select {font-size:16px;min-height:40px}'));
  assert.ok(css.includes('.alert-strategy-switch button {min-height:36px}'));
});

test('path controls use intrinsic content width without stretching into unused space',()=>{
  const field=css.match(/\.layout-view#alerts \.alert-path-field \{([^}]+)\}/)?.[1],select=css.match(/\.layout-view#alerts \.alert-path-select \{([^}]+)\}/)?.[1];
  assert.ok(field.includes('flex:0 1 auto'));assert.ok(field.includes('min-width:0;max-width:100%'));
  assert.ok(select.includes('display:inline-flex;flex:0 1 auto'));assert.ok(select.includes('min-width:0;max-width:100%'));
  assert.ok(!field.includes('210px'));assert.ok(!select.startsWith('width:100%'));
  assert.ok(css.includes('.alert-detail-controls {display:flex;align-items:center;flex-wrap:wrap;'));
});

test('read errors surface the bounded escaped reason without claiming nonexistent history was retained',async()=>{
  const h=harness(async()=>{throw new Error('<img src=x onerror=1>')});h.context.renderAlertMasterDetail([group()]);assert.equal(await h.context.loadSelectedAlertHistory(),false);
  const markup=h.node('alertDetailPane').innerHTML;assert.ok(markup.includes('明细读取失败：&lt;img'));assert.ok(!markup.includes('<img src=x'));assert.ok(!markup.includes('已保留上次记录'));
});

test('exchange names reuse local icons, retain readable labels and do not duplicate accessible names',()=>{
  const h=harness();
  for(const exchange of ['Binance','Gate','Bitget','Bybit','OKX']){
    const markup=h.context.exchangeName(exchange);
    assert.ok(markup.includes(`src="/static/exchanges/${exchange.toLowerCase()}.png"`));
    assert.ok(markup.endsWith(exchange));assert.ok(markup.includes('alt="" aria-hidden="true"'));
  }
  assert.ok(h.context.exchangeName('Binance','币安合约').endsWith('币安合约'));
});

test('unknown exchange labels are escaped, with no invented icon or inherited object URL',()=>{
  const h=harness();
  for(const exchange of ['<img onerror=1>','constructor','__proto__','New Exchange']){
    const markup=h.context.exchangeName(exchange);assert.equal(markup,escape(exchange));assert.ok(!markup.includes('<img'));
  }
  assert.equal(h.context.exchangeName(null),'');
  assert.ok(h.context.exchangeName('Gate','<script>bad</script>').endsWith('&lt;script&gt;bad&lt;/script&gt;'));
});

test('selected route has two visual icons while its real select keeps escaped text-only native options',()=>{
  const h=harness(),coin=group();h.context.renderAlertMasterDetail([coin]);
  const markup=h.context.alertPathControls(coin,coin.paths[0]);
  assert.equal((markup.match(/class="exchange-logo"/g)||[]).length,2);
  assert.ok(markup.includes('class="alert-path-display" aria-hidden="true"'));
  assert.ok(markup.includes('id="alertPathSelect" aria-label="交易路径"'));
  assert.ok(markup.includes('onchange="setAlertPath(this.value)"'));
  assert.ok(markup.includes('>Gate现货</span>'));assert.ok(markup.includes('>币安合约</span>'));
  assert.ok(!markup.match(/<option[^>]*>[\s\S]*?<img/));
  assert.ok(css.includes('.alert-path-select:focus-within {outline:2px solid'));
});

test('path and wallet icons follow the exact selected route through switching and refresh',async()=>{
  const h=harness(),coin=group(),other=addPath(coin,'futures_futures','Bybit','OKX');h.context.renderAlertMasterDetail([coin]);
  await h.context.setAlertPath(other.id);h.context.renderAlertMasterDetail([coin]);
  const markup=h.node('alertDetailPane').innerHTML;
  for(const exchange of ['bybit','okx'])assert.equal((markup.match(new RegExp(`/static/exchanges/${exchange}\\.png`,'g'))||[]).length,2);
  for(const exchange of ['gate','binance'])assert.ok(!markup.includes(`/static/exchanges/${exchange}.png`));
  assert.ok(markup.includes('充提未知'));assert.ok(markup.includes('data-transfer-status='));
  assert.ok(markup.includes('现货充提 · 非合约权限'));
});
