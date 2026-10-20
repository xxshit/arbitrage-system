import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import {buildExchangeIcons} from '../scripts/build_exchange_icons.mjs';

const source=readFileSync(new URL('../static/app.js',import.meta.url),'utf8');
const bundle=readFileSync(new URL('../static/exchange-icons.js',import.meta.url),'utf8');
const template=readFileSync(new URL('../templates/index.html',import.meta.url),'utf8');
const helpers=source.slice(source.indexOf('const exchangeIcon='),source.indexOf('\n',source.indexOf('const exchangeIcon=')))+'\n'+
  source.slice(source.indexOf('function exchangeName('),source.indexOf('function syncRenderedNode('));
const venues=['Binance','Gate','Bitget','Bybit','OKX'];
const escape=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
function harness(embedded=true){
  const context={escapeChatHtml:escape};
  vm.createContext(context);vm.runInContext((embedded?bundle:'')+'\n'+helpers,context);
  return context;
}
const imageSource=markup=>markup.match(/ src="([^"]+)"/)?.[1];

test('bundle is reproducible and each embedded PNG is byte-identical to the existing logo',()=>{
  assert.equal(bundle,buildExchangeIcons());
  const context=harness();
  for(const venue of venues){
    const uri=imageSource(context.exchangeName(venue));
    assert.ok(uri.startsWith('data:image/png;base64,'));
    assert.deepEqual(Buffer.from(uri.split(',')[1],'base64'),
      readFileSync(new URL(`../static/exchanges/${venue.toLowerCase()}.png`,import.meta.url)));
  }
  assert.ok(Buffer.byteLength(bundle)<25000);
});
test('logos require no per-row image request, regardless of path order or repeated refresh',()=>{
  const context=harness(),initial=Object.fromEntries(venues.map(v=>[v,imageSource(context.exchangeName(v))]));
  for(let refresh=0;refresh<80;refresh++){
    const ordered=refresh%2?venues.toReversed():venues;
    for(const venue of ordered){
      const markup=context.exchangeName(venue,`${venue}现货`);
      assert.equal(imageSource(markup),initial[venue]);
      assert.ok(!markup.includes('/static/exchanges/'));
      assert.ok(markup.endsWith(`${venue}现货`));
    }
  }
});
test('all logos are eager, synchronously decoded, and dimensioned before loading',()=>{
  const context=harness();
  for(const venue of venues){
    const markup=context.exchangeName(venue);
    for(const value of ['class="exchange-logo"','width="21" height="21"','decoding="sync"','loading="eager"','alt="" aria-hidden="true"']){
      assert.ok(markup.includes(value),`${venue}: ${value}`);
    }
    assert.ok(!markup.includes('onerror='));
  }
});
test('a failed bundle still leaves the original local image and readable exchange name',()=>{
  const context=harness(false);
  for(const venue of venues){
    assert.equal(imageSource(context.exchangeName(venue)),`/static/exchanges/${venue.toLowerCase()}.png`);
    assert.ok(context.exchangeName(venue).endsWith(venue));
  }
});
test('unknown or inherited venue names never create arbitrary images or unescaped HTML',()=>{
  const context=harness();
  for(const value of ['<img src=x onerror=1>','constructor','__proto__','New Venue',null]){
    assert.equal(context.exchangeName(value),escape(value));
  }
  assert.ok(context.exchangeName('Bitget','<script>bad</script>').endsWith('&lt;script&gt;bad&lt;/script&gt;'));
});
test('bundle loads exactly once before all shared exchange rendering without async/defer race',()=>{
  assert.equal((template.match(/filename='exchange-icons.js'/g)||[]).length,1);
  assert.ok(template.indexOf("filename='exchange-icons.js'")<template.indexOf("filename='app.js'"));
  const tag=template.match(/<script[^>]*filename='exchange-icons.js'[^>]*>/)?.[0];
  assert.ok(tag&&!/\b(async|defer)\b/.test(tag));
  assert.ok(tag.includes('?v=20261020-exchange-icons-v1'));
  assert.ok(!/fetch\(|XMLHttpRequest|localStorage|sessionStorage|document\./.test(bundle));
});
