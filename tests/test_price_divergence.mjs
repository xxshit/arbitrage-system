import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const source=readFileSync(new URL('../static/app.js',import.meta.url),'utf8');
const helpers=source.slice(source.indexOf('function divergenceLabel('),source.indexOf('function spotTransferStatus('));
const escape=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const risk={symbol:'GUA/USDT',long_exchange:'Gate',state:'confirmed',long_ask:.12225,short_bid:.02814,index_price:.02804,open_spread:-76.981595,basis:.356633,blocked_chains:['BSC 充关'],observed_at:'10-19 00:00:00 UTC'};
function harness(){
  const nodes=new Map(['alertDivergenceRegister','alertDivergenceList','alertDivergenceCount'].map(id=>[id,{hidden:false,open:true,innerHTML:'',textContent:'',scrollTop:42}]));
  const context={escapeChatHtml:escape,byId:id=>nodes.get(id)};vm.createContext(context);vm.runInContext(helpers,context);return {context,nodes};
}
test('risk evidence shows full prices and two-decimal spreads without claiming missing transfers are normal',()=>{
  const {context}=harness(),text=context.divergenceEvidence(risk);
  for(const value of ['0.12225 USDT','0.02814 USDT','0.02804 USDT','-76.98%','+0.36%','BSC 充关','已暂停该路径报警'])assert.ok(text.includes(value),value);
  assert.ok(context.divergenceEvidence({...risk,blocked_chains:[]}).includes('充提状态未作为受限证据'));
});
test('observing and confirmed risks are distinguished and stale or recovering paths remain isolated',()=>{
  const {context}=harness();assert.equal(context.divergenceLabel({...risk,state:'observing'}),'疑似脱钩');assert.equal(context.divergenceLabel(risk),'价格脱钩');
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
  const {context,nodes}=harness();context.renderDivergenceRegister([{...risk,symbol:'<img src=x>',long_exchange:'"><script>',blocked_chains:['<svg onload=alert(1)>']}]);
  const html=nodes.get('alertDivergenceList').innerHTML;assert.ok(!html.includes('<script>'));assert.ok(!html.includes('<svg'));assert.ok(!html.includes('<img'));assert.ok(html.includes('&lt;svg'));
});
