import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const base=new URL('../',import.meta.url),source=readFileSync(new URL('static/market-flow.js',base),'utf8');
const main=readFileSync(new URL('static/app.js',base),'utf8');
const html=readFileSync(new URL('templates/index.html',base),'utf8');
const evidence={quote_volume:150000,expected_5m_quote:50000,volume_ratio:3,taker_buy_quote:97500,taker_sell_quote:52500,taker_delta_quote:45000,buy_share:.65,positive_delta_minutes:4,change_pct:.5,change_15m_pct:1.5,spread_bps:2,drawdown_pct:.3,window_start:1800000000,window_end:1800000300,baseline_start:1799996400,baseline_end:1800000000,baseline_quote_volume:600000,source:'Binance Spot',thresholds:{volume_ratio:2.5,minimum_5m_quote:100000},interpretation:'不是资金净流入',risks:['不代表主力建仓']};
function harness(intl=Intl){
  const nodes=new Map(),listeners=new Map(),requests=[];
  const node=id=>{if(!nodes.has(id))nodes.set(id,{innerHTML:'',textContent:'',disabled:false});return nodes.get(id)};
  const read={current:()=>true,assertCurrent(){},json:async url=>{requests.push(url);return {items:[],has_more:false}}};
  const c={window:{},document:{getElementById:node,addEventListener:(type,fn)=>listeners.set(type,fn)},Intl:intl,Date,console,
    authState:{is_admin:true,csrf_token:'test'},pageReadContext:()=>read,setPageRefreshSource(){},isPageReadCancelled:()=>false};
  vm.createContext(c);vm.runInContext(source,c);
  return {api:c.window.MarketFlow,node,read,requests,c,listeners};
}
test('evidence renders actual volumes baseline and fact/inference boundary',()=>{
  const h=harness(),out=h.api.evidence(evidence);
  for(const text of ['150,000','50,000','97,500','52,500','45,000','65','600,000','前60分钟','不包含当前5分钟','不是资金净流入'])assert.ok(out.includes(text),text);
});
test('all upstream fields and manual labels are HTML-escaped',()=>{
  const h=harness(),out=h.api.eventCard({symbol:'<script>bad</script>',title:'<img src=x>',evidence:{...evidence,source:'<b>x</b>',risks:['<script>']}});
  assert.ok(!out.includes('<script>'));assert.ok(!out.includes('<img'));assert.ok(out.includes('&lt;script&gt;'));
});
test('empty qualified candidate set does not invent a top three',()=>{
  const h=harness();h.api.render({overview:{},health:{},manual:[],recommended:[],rows:[]});
  assert.match(h.node('marketFlowCandidates').innerHTML,/暂无通过完整证据门槛/);
});
test('recommendations expose expiry score scope and manual pin action',()=>{
  const h=harness();h.api.render({overview:{},health:{},manual:[],recommended:['DEMOUSDT'],valid_until:1800000015,rows:[{contract:'DEMOUSDT',symbol:'DEMO/USDT',status:'live',candidate:true,score:74.2,evidence}]});
  const out=h.node('marketFlowCandidates').innerHTML;
  for(const text of ['74.2/100','有效至','不是收益预测','加入手动标签'])assert.ok(out.includes(text));
});
test('leaving the monitor page prevents fetching its detailed payload',async()=>{
  const h=harness();h.read.current=()=>false;assert.equal(await h.api.load(),false);assert.deepEqual(h.requests,[]);
});
test('global alert update does not fetch hidden history pages',()=>{
  const h=harness();h.api.updateAlerts({active_events:[]},false);assert.deepEqual(h.requests,[]);
});
test('flow tips coexist with original funding and spread alarms and muted symbols',()=>{
  assert.ok(main.includes("item.type==='market_flow'"));assert.ok(main.includes("item.type==='funding_change'?fundingTip(item)"));
  assert.ok(main.includes('...(latestFundingAlerts.active_events||[]),...(data.market_flow?.active_events||[])'));
  assert.ok(main.includes('!isAlertMuted(item.symbol)'));
  assert.ok(main.includes("every('alerts',values.browser_alert_refresh,()=>loadAlerts())"));
  assert.ok(main.includes("every('market-flow',5,()=>loadMarketFlow(),()=>pageIsVisible('market-flow'))"));
});
test('new view is reachable and its script loads before main initialization',()=>{
  for(const id of ['market-flow','marketFlowForm','marketFlowTags','marketFlowCandidates','marketFlowRows','marketFlowHistory'])assert.ok(html.includes(`id="${id}"`));
  assert.ok(html.indexOf("filename='market-flow.js'")<html.indexOf("filename='app.js'"));
  assert.ok(main.includes("if(viewId==='market-flow')loadMarketFlow()"));
  new vm.Script(main);new vm.Script(source);
});

test('candidate cards keep ranking order rather than subscription order',()=>{
  const h=harness();h.api.render({overview:{},health:{},manual:[],recommended:['BUSDT','AUSDT'],rows:['AUSDT','BUSDT'].map(contract=>({contract,symbol:contract,status:'live',candidate:true,score:70,evidence}))});
  const out=h.node('marketFlowCandidates').innerHTML;assert.ok(out.indexOf('1. BUSDT')<out.indexOf('2. AUSDT'));
});

test('repeated evidence formatting creates only one date formatter',()=>{
  let constructors=0;
  function DateTimeFormat(...args){constructors++;return new Intl.DateTimeFormat(...args)}
  const h=harness({DateTimeFormat});
  for(let i=0;i<100;i++)h.api.evidence(evidence);
  assert.equal(constructors,1);
});

test('refresh preserves opened evidence by coin and skips unchanged card HTML',()=>{
  const h=harness(),target=h.node('marketFlowRows');let html='',details=[],writes=0;
  Object.defineProperty(target,'innerHTML',{get:()=>html,set:value=>{html=value;writes++;details=[...value.matchAll(/<details data-flow-evidence="([^"]*)"/g)].map(m=>({dataset:{flowEvidence:m[1]},open:false}))}});
  target.querySelectorAll=selector=>selector.endsWith('[open]')?details.filter(d=>d.open):details;
  const payload={overview:{},health:{},manual:[],recommended:[],rows:[{contract:'DEMOUSDT',symbol:'DEMO/USDT',status:'live',candidate:true,score:70,evidence}]};
  h.api.render(payload);details[0].open=true;
  h.api.render({...payload,rows:[{...payload.rows[0],evidence:{...evidence,stream_at:1800000001}}]});
  assert.equal(details[0].open,true);const count=writes;
  h.api.render({...payload,rows:[{...payload.rows[0],evidence:{...evidence,stream_at:1800000001}}]});
  assert.equal(writes,count);
});
