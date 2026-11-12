import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source=readFileSync(new URL('../static/binance-hedge.js',import.meta.url),'utf8');
function fixture(fetcher){
  const nodes=new Map();
  function node(id){
    const n={id,value:'',textContent:'',disabled:false,onclick:null,events:{},classList:{toggle(){}},
      addEventListener(k,fn){this.events[k]=fn},before(){},querySelectorAll(){return ['binanceClip','binanceMinEdge','binanceExitSlippage','binanceResidual'].map(id=>nodes.get(id))}};
    Object.defineProperty(n,'id',{get(){return id},set(value){nodes.delete(id);id=value;nodes.set(id,n)}});
    Object.defineProperty(n,'innerHTML',{get(){return this.html||''},set(value){this.html=value;for(const m of value.matchAll(/id="([^"]+)"/g))if(!nodes.has(m[1]))node(m[1]);}});
    nodes.set(id,n);return n;
  }
  for(const id of ['hedgeSubmitButton','hedgeQuoteAmount','hedgeLeverage','hedgeMaxSlippage'])node(id);
  nodes.get('hedgeQuoteAmount').value='100';nodes.get('hedgeLeverage').value='1';nodes.get('hedgeMaxSlippage').value='.1';
  const ctx={window:{},document:{getElementById:id=>nodes.get(id),createElement:()=>node('new')},authState:{is_admin:true,csrf_token:'synthetic-csrf'},fetch:fetcher,AbortController,Date,setTimeout:()=>1,clearTimeout(){},confirm:()=>true};
  vm.runInNewContext(source,ctx);const api=ctx.window.BinanceHedge;
  const opportunity={strategy:'spot_futures',group:{symbol:'BTC/USDT'},row:{long_exchange:'Binance'}};
  api.builder(opportunity);nodes.get('binanceClip').value='20';nodes.get('binanceMinEdge').value='0';nodes.get('binanceExitSlippage').value='20';nodes.get('binanceResidual').value='hold';
  return {nodes,api,opportunity};
}
const response=enabled=>({ok:true,json:async()=>({execution_enabled:enabled,intent:{id:'synthetic',result:{prepared:true,prepared_at:Date.now()/1000},expires_at:Date.now()/1000+15,plan:{quantity:'.001',base:'BTC',buy_cap:'80000',sell_cap:'80100',spot_vwap:'80000',futures_vwap:'80100',worst_net_bps:'1',max_single_leg_quote:'80.1'}}})});

test('account and fill rendering escapes upstream strings',()=>{
  const {api}=fixture(()=>{throw Error('unexpected network')});
  const poison='<img src=x onerror="run()">';
  for(const html of [api.accountMarkup({checked_at_epoch:1,balances:[{asset:poison,free:'1',locked:'0'}]}),api.executionMarkup([{id:poison,state:'UNKNOWN',plan:{display_symbol:poison},result:{reason:poison}}])]){
    assert.ok(!html.includes('<img'));assert.ok(html.includes('&lt;img'));
  }
});
test('disabled execution cannot enable confirmation after a successful preview',async()=>{
  const {nodes}=fixture(async()=>response(false));await nodes.get('binanceDepthCheck').events.click();
  assert.equal(nodes.get('hedgeSubmitButton').disabled,true);
});
test('enabled worker still requires fresh stream preparation for this exact preview',async()=>{
  for(const result of [{},{prepared:false,reason:'订阅失败'},{prepared:true,prepared_at:Date.now()/1000-11}]){
    const {nodes}=fixture(async()=>{const data=await response(true).json();data.intent.result=result;return {ok:true,json:async()=>data}});
    await nodes.get('binanceDepthCheck').events.click();
    assert.equal(nodes.get('hedgeSubmitButton').disabled,true);
  }
});
test('changing inputs invalidates a pending preview response',async()=>{
  let resolve;const pending=new Promise(r=>resolve=r);const {nodes}=fixture(()=>pending);
  const call=nodes.get('binanceDepthCheck').events.click();nodes.get('binanceClip').events.input();resolve(response(true));await call;
  assert.equal(nodes.get('hedgeSubmitButton').disabled,true);assert.equal(nodes.get('hedgeSubmitButton').onclick,null);
});
test('changing selected path invalidates a pending preview response',async()=>{
  let resolve;const pending=new Promise(r=>resolve=r);const {nodes,api,opportunity}=fixture(()=>pending);
  const call=nodes.get('binanceDepthCheck').events.click();api.builder({...opportunity,row:{long_exchange:'Gate'}});resolve(response(true));await call;
  assert.equal(nodes.get('hedgeSubmitButton').disabled,true);
});
test('fresh preview posts exact user caps and CSRF then input changes revoke it',async()=>{
  const calls=[];const {nodes}=fixture(async(url,options)=>{calls.push({url,options});return response(true)});
  await nodes.get('binanceDepthCheck').events.click();assert.equal(nodes.get('hedgeSubmitButton').disabled,false);
  const body=JSON.parse(calls[0].options.body);assert.equal(body.clip_quote,'20');assert.equal(body.slippage_bps,'10');assert.equal(body.exit_slippage_bps,'20');
  assert.equal(calls[0].options.headers['X-CSRF-Token'],'synthetic-csrf');assert.equal(calls[0].options.cache,'no-store');
  nodes.get('binanceResidual').events.input();assert.equal(nodes.get('hedgeSubmitButton').disabled,true);assert.equal(calls.length,1);
});

test('exit budget is disclosed with separate fees and changing it revokes confirmation',async()=>{
  const {nodes}=fixture(async()=>{
    const r=response(true),data=await r.json();
    Object.assign(data.intent.plan,{exit_preview:{spot:{},futures:{}},exit_slippage_bps:'17',exit_fee_bps:{spot:'10',futures:'5'}});
    return {ok:true,json:async()=>data};
  });
  await nodes.get('binanceDepthCheck').events.click();
  assert.match(nodes.get('binanceDepthResult').innerHTML,/退出价差最多 17 bp/);
  assert.match(nodes.get('binanceDepthResult').innerHTML,/手续费另计/);
  assert.match(nodes.get('binanceDepthResult').innerHTML,/零成交或剩余差额/);
  nodes.get('binanceExitSlippage').events.input();
  assert.equal(nodes.get('hedgeSubmitButton').disabled,true);assert.equal(nodes.get('hedgeSubmitButton').onclick,null);
});

test('independent reconciliation shows the failed leg without discarding verified counterpart',()=>{
  const {api}=fixture(()=>{throw Error('unexpected network')});
  const html=api.executionMarkup([{id:'synthetic',state:'UNKNOWN',plan:{display_symbol:'BTC/USDT'},result:{
    reconciliation_errors:{spot:'unconfirmed'},futures:{executed_qty:'0.196',fills_verified:true},
    dispatch_skew_ms:1,request_write_span_ms:81,exchange_fill_skew_ms:0}}]);
  assert.match(html,/独立核验未完成：现货/);assert.match(html,/其余已核实结果保留/);
  assert.match(html,/0.196/);assert.match(html,/两请求写完跨度 81ms/);
  assert.ok(!html.includes('data-binance-ack='));
});
