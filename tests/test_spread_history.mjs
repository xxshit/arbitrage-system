import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
const source=readFileSync(new URL('../static/spread-history.js',import.meta.url),'utf8');
const app=readFileSync(new URL('../static/app.js',import.meta.url),'utf8');
const template=readFileSync(new URL('../templates/index.html',import.meta.url),'utf8');
function harness(overrides={}){
  const context={console,URLSearchParams,AbortController,setTimeout,clearTimeout,setInterval,clearInterval,document:{getElementById:()=>null},...overrides};
  vm.createContext(context);vm.runInContext(source,context);return context;
}
test('opening buttons encode the exact venue and coin; zero is clickable but missing is not',()=>{
  const ui=harness();
  for(const venue of ['Binance','Gate','Bitget']){
    const markup=ui.spreadHistoryButton('EPIC/USDT',venue,'open',0);
    const key=markup.match(/openSpreadHistory\('([^']+)'/)[1];
    assert.deepEqual(JSON.parse(decodeURIComponent(key)),['EPIC/USDT',venue]);
    assert.ok(markup.includes('0.0000%'));assert.ok(markup.includes("'open'"));
  }
  assert.equal(ui.spreadHistoryButton('A/USDT','Gate','basis',null),'—');
  assert.ok(!ui.spreadHistoryButton("'><img>/USDT",'Gate','open',.1).includes('<img>'));
});
test('only spot open and shared Binance basis are clickable; close and dual fields are unchanged',()=>{
  const spot=app.slice(app.indexOf('function spotRows('),app.indexOf('async function loadSpotFutures('));
  assert.ok(spot.includes("spreadHistoryButton(group.symbol,row.long_exchange,'open',row.open_spread)"));
  assert.ok(spot.includes("spreadHistoryButton(group.symbol,contract.long_exchange,'basis',contract.basis)"));
  assert.ok(spot.includes("row._unavailable?'—':percent(row.close_spread)"));
  assert.equal((app.match(/spreadHistoryButton\(/g)||[]).length,2);
  assert.ok(template.indexOf("filename='spread-history.js'")<template.indexOf("filename='app.js'"));
});
test('curve includes true negative/zero/positive values and a new subpath after a gap',()=>{
  const ui=harness(),data={start:0,end:60,series:{open:{points:[[0,-1,false],[10,0,false],[20,2,true],[30,.1,false]]}}};
  const geometry=ui.spreadChartGeometry(data,['open']);
  assert.ok(geometry.low < -1&&geometry.high > 2);
  const path=ui.spreadChartPath(data.series.open.points,geometry);
  assert.equal((path.match(/M/g)||[]).length,2);
  assert.equal((path.match(/L/g)||[]).length,2);
  assert.equal(ui.spreadChartPercent(0),'0.0000%');
  assert.equal(ui.spreadChartPercent(-.32),'-0.3200%');
});
test('empty and constant data do not produce invalid coordinates',()=>{
  const ui=harness();
  assert.equal(ui.spreadChartGeometry({series:{open:{points:[]}}},['open']),null);
  const data={start:0,end:60,series:{basis:{points:[[0,0,false],[30,0,false]]}}};
  const geometry=ui.spreadChartGeometry(data,['basis']);
  assert.ok(!/NaN|Infinity/.test(ui.spreadChartPath(data.series.basis.points,geometry)));
});
test('short accumulated history expands using real timestamps without fabricating earlier data',()=>{
  const ui=harness(),points=[[86220,.1,false],[86390,.2,false]];
  const data={start:0,end:86400,series:{open:{points}}};
  const geometry=ui.spreadChartGeometry(data,['open']);
  assert.equal(geometry.start,86220);assert.equal(geometry.x(86220),76);
  assert.ok(geometry.x(86390)>800);
  assert.equal(points.length,2);
});
test('late period response cannot overwrite a newer selection and closing aborts requests',async()=>{
  const nodes={spreadHistoryModal:{classList:{contains:()=>false,add:()=>{}},dataset:{symbol:'EPIC/USDT'}},
    spreadHistoryExchange:{value:'Gate'},spreadHistoryPeriod:{value:'24h'},spreadChartStatus:{},spreadChartSummary:{},
    spreadChartPlot:{innerHTML:''}};
  const requests=[];
  const ui=harness({document:{getElementById:id=>nodes[id]},fetch:(url,options)=>new Promise(resolve=>requests.push({url,options,resolve}))});
  const rendered=[];ui.renderSpreadChart=()=>rendered.push(vm.runInContext('spreadChartState.data.period',ui));
  const first=ui.loadSpreadChart(true);
  nodes.spreadHistoryPeriod.value='15d';
  const second=ui.loadSpreadChart(true);
  assert.equal(requests[0].options.signal.aborted,true);
  const data=period=>({period,series:{open:{first_at:null,last_at:null},basis:{first_at:null,last_at:null}}});
  requests[1].resolve({ok:true,json:async()=>data('15d')});await second;
  requests[0].resolve({ok:true,json:async()=>data('24h')});await first;
  assert.deepEqual(rendered,['15d']);
  const third=ui.loadSpreadChart();
  ui.closeSpreadHistory();
  assert.equal(requests[2].options.signal.aborted,true);
  requests[2].resolve({ok:true,json:async()=>data('15d')});await third;
  assert.equal(rendered.length,1);
});
test('refresh failure retains existing plot; interval requests do not overlap',async()=>{
  const nodes={spreadHistoryModal:{classList:{contains:()=>false},dataset:{symbol:'X/USDT'}},
    spreadHistoryExchange:{value:'Gate'},spreadHistoryPeriod:{value:'24h'},spreadChartStatus:{},spreadChartPlot:{innerHTML:'old plot'}};
  let reject,calls=0;
  const ui=harness({document:{getElementById:id=>nodes[id]},fetch:()=>{calls++;return new Promise((_,r)=>reject=r)}});
  vm.runInContext('spreadChartState.data={existing:true}',ui);
  const first=ui.loadSpreadChart();await ui.loadSpreadChart();assert.equal(calls,1);
  reject(new Error('temporary failure'));await first;
  assert.equal(nodes.spreadChartPlot.innerHTML,'old plot');
  assert.equal(nodes.spreadChartStatus.textContent,'temporary failure');
});
test('modal has bounded periods, two independent toggles, focus/Escape and no execution endpoint',()=>{
  const ui=harness();
  const periods=vm.runInContext('spreadChartPeriods.map(p=>p[0]).join(",")',ui);
  assert.equal(periods,'1h,6h,24h,3d,7d,15d');
  for(const text of ['aria-modal="true"',"event.key==='Escape'","event.key==='Tab'",'spreadShow-open','spreadShow-basis','断档不补线']){
    assert.ok(source.includes(text));
  }
  assert.ok(!/hedge|order\/create|lark/i.test(source));
});
