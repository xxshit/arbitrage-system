import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const source=readFileSync(new URL('../static/app.js',import.meta.url),'utf8');
const periods=source.match(/const trendPeriods=([^\n]+);/)[1];
const context={displayChange:n=>n==null?'—':`${n}%`,changeClass:n=>n==null?'neutral':n<0?'negative':'positive'};
vm.createContext(context);
vm.runInContext(`const trendPeriods=${periods};`+
  source.match(/function priceChangeStack\(row\)\{[^\n]+/)[0]+
  source.match(/function dualPriceChanges\(group\)\{[^\n]+/)[0],context);

test('both pages show ten chronological periods in the same compact grid',()=>{
  for(const html of [context.priceChangeStack({change_2d:10.2,change_30d:-28.7}),
                     context.dualPriceChanges({binance_change_2d:10.2,binance_change_30d:-28.7})]){
    assert.deepEqual([...html.matchAll(/<b>([^<]+)<\/b>/g)].map(m=>m[1]),
      ['5MIN','15MIN','30MIN','1H','12H','24H','2D','3D','7D','30D']);
    assert.ok(html.includes('positive">10.2%'));
    assert.ok(html.includes('negative">-28.7%'));
    assert.equal((html.match(/class="change-item"/g)||[]).length,10);
  }
});
test('missing 30D remains a dash, not zero',()=>{
  const html=context.priceChangeStack({change_2d:0});
  assert.ok(html.includes('<b>2D</b><span class="positive">0%'));
  assert.ok(html.includes('<b>30D</b><span class="neutral">—'));
});
test('2D and 30D sorting choices exist for both strategies',()=>{
  for(const key of ['change_2d','change_30d','binance_change_2d','binance_change_30d'])
    assert.ok(source.includes(`['${key}','涨跌幅 · `));
});
