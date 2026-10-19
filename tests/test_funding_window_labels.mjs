import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source=readFileSync(new URL('../static/app.js',import.meta.url),'utf8');
function harness(){
  const context={signClass:n=>n<0?'negative':'positive',changeClass:n=>n<0?'negative':'positive',
    percent:n=>`${n}%`,dualDisplayPercent:n=>n==null?'—':`${n}%`};
  vm.createContext(context);
  vm.runInContext(source.split('\n').filter(line=>/^(function (displayFunding|fundingStack|dualFundingHistory)\(|const dualFundingDifferencePeriods=)/.test(line)).join('\n'),context);
  return context;
}

test('spot 24H keeps settled total separate from current and explains rolling window on hover',()=>{
  const markup=harness().fundingStack({funding_rate:.005,funding_previous:.005,funding_24h:.03,funding_3d:.09,funding_7d:.21,funding_30d:.9});
  assert.ok(markup.includes('title="最近24小时已结算资费累计，不含当前预估"'));
  assert.match(markup,/<b>24H<\/b><span[^>]*>0.03%<\/span>/);
  for(const label of ['当前','上期','24H','3D','7D','30D'])assert.ok(markup.includes(`<b>${label}</b>`));
  for(const [days,hours] of [[3,72],[7,168],[30,720]])assert.ok(markup.includes(`title="最近${days}天（${hours}小时）已结算资费累计，不含当前预估"`));
  assert.ok(!markup.includes('0.035%'));
});

test('dual 24H renders actual signed difference and does not fabricate missing history',()=>{
  const context=harness();
  const markup=context.dualFundingHistory({funding_difference:.005,funding_difference_previous:.005,funding_difference_24h:-.027});
  assert.ok(markup.includes('最近24小时已结算净资费：空端累计减多端累计，不含当前预估'));
  for(const [days,hours] of [[3,72],[7,168],[30,720]])assert.ok(markup.includes(`title="最近${days}天（${hours}小时）已结算净资费：空端累计减多端累计，不含当前预估"`));
  assert.match(markup,/<b>24H<\/b><em class="negative">-0.027%<\/em>/);
  assert.match(context.dualFundingHistory({funding_difference_24h:null}),/<b>24H<\/b><em[^>]*>—<\/em>/);
});
