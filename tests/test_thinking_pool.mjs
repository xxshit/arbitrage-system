import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const source=readFileSync(new URL('../static/thinking-pool.js',import.meta.url),'utf8');
const app=readFileSync(new URL('../static/app.js',import.meta.url),'utf8');
const template=readFileSync(new URL('../templates/index.html',import.meta.url),'utf8');
function harness(overrides={}){
  const context={console,URLSearchParams,AbortController,Date,setTimeout,clearTimeout,setInterval:()=>0,
    document:{getElementById:()=>null,querySelectorAll:()=>[]},
    exchangeName:s=>String(s).replaceAll('<','&lt;'),transferStatusControl:()=>'<span>充提未知</span>',
    liquidityAmount:()=>'$1.00M',...overrides};
  vm.createContext(context);vm.runInContext(source,context);return context;
}
const filters={min_score:'all',status:'all',search:''};
const item={symbol:'DEMO/USDT',long_exchange:'Gate',short_exchange:'Binance',score:18,
  status:'watch',open_spread:.5,close_spread:.55,short_interval:4,
  funding_current:.005,funding_previous:.004,funding_24h:.03,funding_3d:.09,
  funding_7d:.21,funding_30d:1.193,
  funding:{total:.03,complete:true},reasons:['常态溢价，不假定归零'],
  risks:[],scenario:{spread_net:-.86},quoted_at:1000};

test('net space shows scenario costs and known first-level capacity, never a fill promise',()=>{
  const ui=harness().ThinkingPool;
  const sample={...item,capacity_usdt:5000,scenario:{calculable:true,notional:1000,hold_hours:72,
    spread_net:.25,funding_net:.30,exit_spread:.5,fee_budget:.66,slippage:.1,adverse_funding:0,funding_income:.05}};
  assert.ok(ui.scenarioReady(sample,1005));assert.ok(!ui.scenarioReady(sample,1011));
  const html=ui.scenarioMarkup(sample,1005);
  for(const text of ['+0.25%','+0.30%','1,000 USDT','5,000 USDT','四笔交易费用','退出平差预算','不计正资费','不是全盘口深度'])assert.ok(html.includes(text),text);
  assert.ok(!ui.scenarioMarkup(sample,1011).includes('>+0.25%</b>'));
  assert.ok(!ui.scenarioReady({...sample,scenario:{...sample.scenario,calculable:false}},1005));
  assert.ok(ui.scenarioMarkup({...sample,risks:['<script>unsafe</script>']},1005).includes('&lt;script&gt;'));
});

test('strategy tabs remain spot/dual; drivers never replace strategies',()=>{
  assert.ok(source.includes('data-thinking-strategy="spot"'));
  assert.ok(source.includes('data-thinking-strategy="dual"'));
  assert.ok(!source.includes('data-thinking-strategy="funding"'));
  assert.ok(!source.includes('data-thinking-strategy="spread"'));
  assert.ok(source.includes('未套用现多期空的评分规则'));
});
test('numeric threshold and internal qualification filter independently',()=>{
  const ui=harness().ThinkingPool;
  const items=[item,{...item,score:55},{...item,score:80,status:'opportunity'}];
  assert.equal(ui.select(items,{...filters,min_score:'60'},1000).length,1);
  assert.equal(ui.select(items,{...filters,status:'watch'},1000).length,2);
  assert.equal(ui.select(items,{...filters,status:'opportunity'},1000).length,1);
  assert.equal(ui.select(items,{...filters,search:'dem'},1000).length,3);
});
test('aged quotes never remain labelled as a current opportunity',()=>{
  const ui=harness().ThinkingPool;
  const current={...item,status:'opportunity'};
  assert.equal(ui.qualified(current,1009),true);
  assert.equal(ui.qualified(current,1011),false);
  assert.equal(ui.qualified({...current,quoted_at:1003},1000),false);
  assert.equal(ui.qualified({...current,quoted_at:null},1000),false);
  assert.equal(ui.select([current],{...filters,status:'opportunity'},1011).length,0);
});
test('stale quotes remove current-space score bonus without restoring status badges',()=>{
  const node={dataset:{scoreBase:'80',scoreCurrent:'20',quotedAt:'1'},textContent:'80'};
  const ui=harness({document:{querySelectorAll:()=>[node]}}).ThinkingPool;
  ui.expire();
  assert.equal(node.textContent,'45');
  assert.equal(ui.displayScore({score:80,score_parts:{current:20},quoted_at:1000},1005),80);
});
test('browsing order uses exact coin+venue paths, automatic order resumes when released',()=>{
  const ui=harness().ThinkingPool,a={...item},b={...item,long_exchange:'Bitget'};
  const keys=[a,b].map(i=>JSON.stringify([i.symbol,i.long_exchange,i.short_exchange]));
  assert.equal(ui.ordered([b,a],keys,true)[0].long_exchange,'Gate');
  assert.equal(ui.ordered([b,a],keys,false)[0].long_exchange,'Bitget');
  assert.equal(ui.ordered([b],keys,true).length,1);
});
test('compact spot row shows score and evidence without grade or status labels',()=>{
  const ui=harness().ThinkingPool,markup=ui.spotRow({...item,quoted_at:Date.now()/1000});
  const main=markup.split('<details')[0];
  for(const text of ['>18</b>','评分','充提未知','+0.50%','+0.0050%','币安资费详情','当前','上期','24H','3D','7D','30D'])assert.ok(main.includes(text),text);
  for(const text of ['值得观察','依据与风险','B ·','C ·','当前有机会</span>'])assert.ok(!markup.includes(text),text);
  assert.ok(markup.includes('盘口'));
  assert.ok(!main.includes('常态溢价'));
  assert.ok(!main.includes('opportunity-reasons'));
  assert.ok(!main.includes('<span>本期预计'));
  assert.equal((main.match(/data-funding-field=/g)||[]).length,6);
  for(const value of ['+0.0040%','+0.0300%','+0.0900%','+0.2100%','+1.1930%'])assert.ok(main.includes(value),value);
  assert.ok(!markup.includes('当前有机会</span>'));
  assert.equal(ui.pct(null),'—');assert.equal(ui.pct(0),'0.00%');
  assert.match(markup,/合约 <b class="opportunity-interval"[^>]*>4H<\/b>/);
  assert.ok(!markup.includes('资费 <span>4H'));
});
test('funding display uses settled totals, distinguishes missing from zero and retains negative values',()=>{
  const ui=harness().ThinkingPool;
  const main=ui.spotRow({...item,funding:{total:999},funding_24h:null,
    funding_3d:0,funding_7d:-.218,funding_30d:undefined,
    trend:'价格走强',risks:[]}).split('<details')[0];
  assert.match(main,/data-funding-field="funding_24h"[^]*?<b class="neutral">—<\/b>/);
  assert.match(main,/data-funding-field="funding_3d"[^]*?<b class="neutral">0\.0000%<\/b>/);
  assert.match(main,/data-funding-field="funding_7d"[^]*?<b class="negative">-0\.2180%<\/b>/);
  assert.ok(!main.includes('999'));assert.ok(!main.includes('价格走强'));
});
test('dual cards have no grades or new spot rules and retain original fields',()=>{
  const ui=harness().ThinkingPool,markup=ui.dualRow({...item,long_exchange:'Bybit',funding_previous:.01,funding_24h:.03,funding_3d:.09});
  for(const text of ['开差','平差','当前资费差','上期','24H','3D','OI','Bybit'])assert.ok(markup.includes(text));
  assert.ok(!markup.includes('opportunity-grade'));
  assert.ok(!markup.includes('opportunity-state'));
  const list=ui.dualSorted([{open_spread:null},{open_spread:.3},{open_spread:-.5}],'open_spread','asc');
  assert.equal(list[0].open_spread,-.5);assert.equal(list.at(-1).open_spread,null);
  assert.ok(source.includes('期多期空候选排序'));
});
test('API text, symbols and reasons are escaped; no order, notification or secret endpoint',()=>{
  const ui=harness().ThinkingPool;
  const markup=ui.spotRow({...item,symbol:'"><img src=x onerror=x>/USDT',risks:['<script>bad()</script>']});
  assert.ok(!markup.includes('<script>'));assert.ok(!markup.includes('<img src=x'));
  assert.ok(markup.includes('&lt;script&gt;'));
  assert.ok(!/method:\s*['"]POST|\/order|\/lark|api[_-]key|secret/i.test(source));
  assert.ok(template.indexOf("filename='thinking-pool.js'")<template.indexOf("filename='app.js'"));
  assert.ok(app.includes('rememberServerDelistings(simpleThinkingCache.spot_simple)'));
  assert.equal((app.match(/async function loadSimpleThinking/g)||[]).length,1);
});
test('period changes show different real ranges and observed counts without changing score',()=>{
  const ui=harness().ThinkingPool;
  const data={...item,score:77,history:{windows:{'24h':{high:.5,low:.4,peak_at:1000,first_at:1,last_at:1000,coverage:.5},
    '7d':{high:1.3,low:-1.2,peak_at:1000,first_at:1,last_at:1000,coverage:.5}}},
    activity:{since:1,windows:{'24h':{positive:0,negative:0,recent:[]},
      '7d':{positive:3,negative:1,recent:[{started:900,peak:1.3,peak_at:1000}]}}}};
  const day=ui.spreadEvidence(data,'24h'),week=ui.spreadEvidence(data,'7d');
  assert.ok(day.includes('+0.50%'));assert.ok(!day.includes('+1.30%'));
  assert.ok(week.includes('+1.30%'));assert.ok(week.includes('-1.20%'));assert.ok(week.includes('正 3 次'));
  assert.ok(week.includes('历史未满'));assert.ok(week.includes('已见'));assert.equal(data.score,77);
  assert.ok(!ui.spreadEvidence(item).includes('正 0 次'));
});

test('multi-day funding quality distinguishes classified periods, gaps and stale summaries',()=>{
  const ui=harness().ThinkingPool;
  const w={count:8,classified:6,above:3,above_ratio:.5,negative:2,total:-.03,
    recent_positive_streak:1,unknown_count:2,complete:false};
  const sample={...item,funding_quality:{as_of:1000,windows:{'24h':w,'3d':w,'7d':w}}};
  assert.match(ui.qualitySummary(sample,1001),/3D 3\/6 · 7D 3\/6/);
  const html=ui.qualityMarkup(sample,1001);
  for(const s of ['-0.0300%','3 / 6 期','50%','2 / 8 期','2期周期不明','覆盖不足','不是保底'])assert.ok(html.includes(s),s);
  assert.match(ui.qualitySummary(sample,1361),/待更新/);
  assert.match(ui.qualityMarkup(sample,1361),/仅供历史参考/);
  assert.match(ui.qualitySummary(item,1000),/采集中/);
  assert.ok(!ui.qualityMarkup(item,1000).includes('0.0000%'));
});

test('validation presentation never invents wins or conflates settled funding with price returns',()=>{
  const ui=harness().ThinkingPool;
  const data={model:2,horizon:3600,notional:1000,score_hold_hours:72,as_of:5000,
    total:1,pending:0,observed:0,insufficient:1,awaiting_collection:0,
    cohorts:[{symbol:'<bad>/USDT',long_exchange:'Gate',short_exchange:'Binance',score:19,entered_at:1,
      model:2,entry:{notional:1000,score_hold_hours:72,risks:['<script>x</script>']},
      outcomes:{3600:{status:'insufficient',reason:'覆盖不足',coverage:.1,net:null,low:-.5,high:1,
        funding:{total:-.02,count:1,complete:false}}}}],bands:[{label:'0–39',total:1,observed:0,positive:0,median_net:null}]};
  const html=ui.reviewMarkup(data);
  for(const s of ['覆盖不足','-0.0200%','部分记录','同路径7天一轮','不是成交收益或胜率','未按结算标记价换算','&lt;bad&gt;','&lt;script&gt;'])assert.ok(html.includes(s),s);
  assert.ok(!html.includes('<script>'));
  assert.ok(!html.includes('100%胜率'));
  assert.match(html,/待到期/);
  assert.match(ui.reviewMarkup({...data,cohorts:[]}),/不补造历史评分/);
});

test('personal bookmarks separate strategy and direction and keep missing paths removable',()=>{
  const ui=harness().ThinkingPool;
  assert.notEqual(ui.watchKey(item,'spot'),ui.watchKey(item,'dual'));
  assert.notEqual(ui.watchKey(item,'dual'),ui.watchKey({...item,long_exchange:'Binance',short_exchange:'Gate'},'dual'));
  const row=ui.spotRow({...item,score:null,open_spread:null,close_spread:null,scenario:{},watch_state:'unavailable'});
  assert.match(row,/路径暂无有效行情或已隔离/);
  assert.match(row,/data-watch-strategy="spot"/);
  assert.ok(!row.includes('>+0.50%</b>'));
  assert.match(source,/method:'PUT'/);
  assert.match(source,/'X-CSRF-Token':token/);
});

test('thin-book price trajectories are labelled observation and never displayed as capacity-qualified gains',()=>{
  const ui=harness().ThinkingPool;
  const html=ui.reviewMarkup({model:2,horizon:3600,notional:1000,score_hold_hours:72,as_of:4000,
    total:1,price_observed:1,observed:0,pending:0,insufficient:1,awaiting_collection:0,
    bands:[{label:'60–79',total:1,price_observed:1,observed:0,positive:0,median_price_net:.4}],
    cohorts:[{symbol:'DEMO/USDT',long_exchange:'Gate',short_exchange:'Binance',score:72,entered_at:1,model:2,
      entry:{notional:1000,score_hold_hours:72,capacity:50,entry_size_ok:false},
      outcomes:{3600:{status:'insufficient',price_net:.4,net:null,coverage:1,reason:'入选首档容量不足或未知，仅观察行情'}}}]});
  for(const s of ['行情 +0.40%','50 USDT','仅观察行情','首档达标 0','0 / 0','行情净价差中位数'])assert.ok(html.includes(s),s);
  assert.ok(!html.includes('<b class="positive">+0.40%</b>'));
});
