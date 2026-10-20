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
const filters={grade:'all',status:'all',search:''};
const item={symbol:'DEMO/USDT',long_exchange:'Gate',short_exchange:'Binance',grade:'C',
  grade_label:'低优先观察',status:'watch',open_spread:.5,close_spread:.55,short_interval:4,
  funding_current:.005,funding:{total:.03,complete:true},reasons:['常态溢价，不假定归零'],
  risks:[],scenario:{spread_net:-.86},quoted_at:1000};

test('strategy tabs remain spot/dual; drivers never replace strategies',()=>{
  assert.ok(source.includes('data-thinking-strategy="spot"'));
  assert.ok(source.includes('data-thinking-strategy="dual"'));
  assert.ok(!source.includes('data-thinking-strategy="funding"'));
  assert.ok(!source.includes('data-thinking-strategy="spread"'));
  assert.ok(source.includes('未套用现多期空的分级规则'));
});
test('grades and current/watch status filter independently',()=>{
  const ui=harness().ThinkingPool;
  const items=[item,{...item,grade:'B'},{...item,grade:'A',status:'opportunity'}];
  assert.equal(ui.select(items,{...filters,grade:'C'},1000).length,1);
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
test('live status expires visibly even when automatic refresh is delayed',()=>{
  const node={dataset:{quotedAt:1},textContent:'当前有机会',classList:{remove:s=>node.removed=s}};
  const ui=harness({document:{querySelectorAll:()=>[node]}}).ThinkingPool;
  ui.expire();
  assert.equal(node.textContent,'值得观察');assert.equal(node.removed,'is-current');
});
test('browsing order uses exact coin+venue paths, automatic order resumes when released',()=>{
  const ui=harness().ThinkingPool,a={...item},b={...item,long_exchange:'Bitget'};
  const keys=[a,b].map(i=>JSON.stringify([i.symbol,i.long_exchange,i.short_exchange]));
  assert.equal(ui.ordered([b,a],keys,true)[0].long_exchange,'Gate');
  assert.equal(ui.ordered([b,a],keys,false)[0].long_exchange,'Bitget');
  assert.equal(ui.ordered([b],keys,true).length,1);
});
test('compact spot row includes reasons, grade, status and transfer control without invented return',()=>{
  const ui=harness().ThinkingPool,markup=ui.spotRow(item);
  for(const text of ['C · 低优先观察','值得观察','常态溢价','充提未知','+0.50%','+0.0050%','依据与风险','盘口记录'])assert.ok(markup.includes(text),text);
  assert.ok(!markup.includes('当前有机会</span>'));
  assert.equal(ui.pct(null),'—');assert.equal(ui.pct(0),'0.00%');
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
  const markup=ui.spotRow({...item,symbol:'"><img src=x onerror=x>/USDT',reasons:['<script>bad()</script>']});
  assert.ok(!markup.includes('<script>'));assert.ok(!markup.includes('<img src=x'));
  assert.ok(markup.includes('&lt;script&gt;'));
  assert.ok(!/method:\s*['"]POST|\/order|\/lark|api[_-]key|secret/i.test(source));
  assert.ok(template.indexOf("filename='thinking-pool.js'")<template.indexOf("filename='app.js'"));
  assert.ok(app.includes('rememberServerDelistings(simpleThinkingCache.spot_simple)'));
  assert.equal((app.match(/async function loadSimpleThinking/g)||[]).length,1);
});
