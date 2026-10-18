import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const source=readFileSync(new URL('../static/app.js',import.meta.url),'utf8');
const helpers=source.slice(source.indexOf('function divergenceLabel('),source.indexOf('function transferPopoverMarkup('));
const escape=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const risk={symbol:'GUA/USDT',long_exchange:'Gate',short_exchange:'Binance',state:'confirmed',long_ask:.12225,short_bid:.02814,index_price:.02804,open_spread:-76.981595,basis:.356633,blocked_chains:['BSC 充关'],blocked_transfers:[{exchange:'Gate',chain:'BSC',deposit_open:false,withdraw_open:true}],transfer_state:'fresh',observed_at:'10-19 00:00:00 UTC'};
function harness(){
  const nodes=new Map(['alertDivergenceRegister','alertDivergenceList','alertDivergenceCount'].map(id=>[id,{hidden:false,open:true,innerHTML:'',textContent:'',scrollTop:42}]));
  const context={escapeChatHtml:escape,byId:id=>nodes.get(id),transferStatusControl:()=>'<button>充提状态</button>'};vm.createContext(context);vm.runInContext(helpers,context);return {context,nodes};
}
test('risk evidence shows full prices and two-decimal spreads without claiming missing transfers are normal',()=>{
  const {context}=harness(),text=context.divergenceEvidence(risk);
  for(const value of ['0.12225 USDT','0.02814 USDT','0.02804 USDT','-76.98%','+0.36%','Gate · BSC：充值关闭','仅暂停该路径报警'])assert.ok(text.includes(value),value);
  assert.ok(context.divergenceEvidence({...risk,blocked_chains:[],blocked_transfers:[]}).includes('未发现明确关充/关提证据'));
});
test('observing and confirmed risks are distinguished and stale or recovering paths remain isolated',()=>{
  const {context}=harness();assert.equal(context.divergenceLabel({...risk,state:'observing'}),'Gate 现货疑似脱钩');assert.equal(context.divergenceLabel(risk),'Gate 现货脱钩');
  assert.ok(context.divergenceEvidence({...risk,stale:true}).includes('报价待更新，继续隔离'));
  assert.ok(context.divergenceEvidence({...risk,recovering:true}).includes('价格已收敛，等待稳定确认'));
});
test('compact register preserves its open state and scroll without adding risks to alarm totals',()=>{
  const {context,nodes}=harness();context.renderDivergenceRegister([risk]);
  assert.equal(nodes.get('alertDivergenceCount').textContent,1);assert.equal(nodes.get('alertDivergenceRegister').open,true);assert.equal(nodes.get('alertDivergenceList').scrollTop,42);
  context.renderDivergenceRegister([{...risk,short_bid:.02815}]);assert.equal(nodes.get('alertDivergenceRegister').open,true);
  context.renderDivergenceRegister([]);assert.equal(nodes.get('alertDivergenceRegister').hidden,true);
});
test('untrusted chain, symbol and exchange text is escaped in labels and evidence',()=>{
  const {context,nodes}=harness();context.renderDivergenceRegister([{...risk,symbol:'<img src=x>',long_exchange:'"><script>',blocked_transfers:[{exchange:'<svg onload=alert(1)>',chain:'<img src=x>',withdraw_open:false}]}]);
  const html=nodes.get('alertDivergenceList').innerHTML;assert.ok(!html.includes('<script>'));assert.ok(!html.includes('<svg'));assert.ok(!html.includes('<img'));assert.ok(html.includes('&lt;svg'));
});

test('same-coin isolated routes remain separate and every closed chain includes its exchange and operation',()=>{
  const {context,nodes}=harness();
  const second={...risk,long_exchange:'Bitget',blocked_transfers:[{exchange:'Bitget',chain:'ETH',deposit_open:true,withdraw_open:false},{exchange:'Bitget',chain:'BEP20',deposit_open:false,withdraw_open:false}]};
  context.renderDivergenceRegister([risk,second]);
  const html=nodes.get('alertDivergenceList').innerHTML;
  assert.equal((html.match(/data-divergence-path=/g)||[]).length,2);
  for(const text of ['Gate现货 → 币安合约','Bitget现货 → 币安合约','Gate 现货脱钩','Bitget 现货脱钩','Gate · BSC：充值关闭','Bitget · ETH：提现关闭','Bitget · BEP20：充值关闭、提现关闭'])assert.ok(html.includes(text),text);
});

test('only the affected spot row displays the depeg badge and explicit closed-chain line',()=>{
  const {context}=harness();
  const bad=context.spotTransferStatus({long_exchange:'Gate',price_divergence:risk},risk.symbol);
  const healthy=context.spotTransferStatus({long_exchange:'Bitget',price_divergence:null},risk.symbol);
  assert.ok(bad.includes('Gate 现货脱钩'));assert.ok(bad.includes('Gate · BSC：充值关闭'));
  assert.ok(!healthy.includes('脱钩'));assert.ok(!healthy.includes('充值关闭'));
});

test('unknown or stale wallet states do not present old closures as current, while legacy evidence names its venue',()=>{
  const {context}=harness();
  for(const state of ['unknown','stale']){
    const text=context.divergenceTransferText({...risk,transfer_state:state});
    assert.ok(text.startsWith('Gate · '));assert.ok(!text.includes('充值关闭'));
  }
  assert.ok(context.divergenceTransferText({...risk,stale:true}).startsWith('上次快照：Gate · BSC'));
  assert.equal(context.divergenceTransferText({...risk,blocked_transfers:undefined}),'Gate · BSC 充关');
});
