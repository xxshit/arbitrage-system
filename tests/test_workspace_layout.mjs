import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const script=readFileSync(new URL('../static/workspace-layout.js',import.meta.url),'utf8');
const css=readFileSync(new URL('../static/workspace-layout.css',import.meta.url),'utf8');
const html=readFileSync(new URL('../templates/index.html',import.meta.url),'utf8');
const app=readFileSync(new URL('../static/app.js',import.meta.url),'utf8');
const metadata=script.slice(script.indexOf('const layouts ='),script.indexOf('\n\n  function accessibleScroll'));
const helpers=script.slice(script.indexOf('function accessibleScroll'),script.indexOf('function prepareRankingScroll'));

class Element {
  constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.attributes={};this.writes=0}
  appendChild(node){if(node.parentElement)node.parentElement.children=node.parentElement.children.filter(item=>item!==node);this.children.push(node);node.parentElement=this;return node}
  append(...nodes){nodes.forEach(node=>this.appendChild(node))}
  setAttribute(key,value){this.writes++;this.attributes[key]=value}
}
function harness(){const context={document:{createElement:tag=>new Element(tag)}};vm.createContext(context);vm.runInContext(metadata+'\n'+helpers,context);return {context,run:code=>vm.runInContext(code,context)}}

test('all twenty existing page profiles have a dedicated layout and CSS coverage',()=>{
  const h=harness();const ids=h.run('Object.keys(layouts)');assert.equal(ids.length,20);
  const profileText=app.slice(app.indexOf('const pageRefreshProfiles='),app.indexOf('const fixedCadenceViews='));
  for(const id of ids){assert.ok(profileText.includes(id),id);assert.ok(css.includes('#'+id),id);assert.ok(h.run(`layouts['${id}'].kind`),id)}
});

test('funding groups move all twelve original fields, preserving values and node identity',()=>{
  const h=harness(),grid=new Element('div');const values=Array.from({length:12},(_,index)=>{const node=new Element('span');node.textContent=`original-${index}: 0.000123456789`;grid.appendChild(node);return node});
  h.context.groupFundingMetrics(grid);
  assert.equal(grid.children.length,3);
  assert.deepEqual(grid.children.map(node=>node.children[0].textContent),['价格与量仓','资金费率','对冲与流动性']);
  const actual=grid.children.flatMap(node=>node.children[1].children);
  assert.equal(new Set(actual).size,12);assert.ok(values.every(node=>actual.includes(node)));assert.ok(values.every(node=>node.textContent.endsWith('0.000123456789')));
  assert.deepEqual(grid.children.map(node=>node.children[1].children.map(value=>values.indexOf(value))),[[0,1,8,9],[2,3,4,5],[6,7,10,11]]);
});

test('funding grouping is idempotent and partial schemas are left untouched',()=>{
  const h=harness(),grid=new Element('div');for(let i=0;i<12;i++)grid.appendChild(new Element('span'));
  h.context.groupFundingMetrics(grid);const first=[...grid.children];h.context.groupFundingMetrics(grid);assert.deepEqual(grid.children,first);
  const partial=new Element('div'),field=new Element('span');partial.appendChild(field);h.context.groupFundingMetrics(partial);assert.deepEqual(partial.children,[field]);
});

test('scroll regions are keyboard accessible without reapplying attributes each refresh',()=>{
  const h=harness(),node=new Element('div');h.context.accessibleScroll(node,'完整行情表格');assert.equal(node.tabIndex,0);assert.equal(node.attributes.role,'region');assert.equal(node.attributes['aria-label'],'完整行情表格');const writes=node.writes;h.context.accessibleScroll(node,'second label');assert.equal(node.writes,writes);
});

test('layout layer never fetches, replaces business markup, opens hidden views or executes trades',()=>{
  for(const forbidden of [/\bfetch\s*\(/,/\.innerHTML\s*=/,/\.outerHTML\s*=/,/localStorage/,/classList\.remove\(['"]hidden/,/classList\.(add|toggle)\(['"]active/,/openHedgePositionBuilder\(/,/saveRuntimeRule\(/,/setAccountActive\(/])assert.ok(!forbidden.test(script),String(forbidden));
  assert.ok(script.includes('if (!profile) return;'));
  assert.ok(script.includes("record.target.closest?.('.view.active')"));
});

test('market sticky cells target only the symbol class, not rowspan continuation first cells',()=>{
  assert.ok(css.includes('td.symbol {position:sticky;left:0;'));
  assert.ok(!/td:first-child\s*\{[^}]*position:sticky/.test(css));
  assert.ok(css.includes('thead th {position:sticky;top:0;'));
  assert.ok(css.includes('td.price-change-cell {vertical-align:top;'));
  assert.ok(script.includes("hint.className = 'layout-table-hint'"));
});

test('onchain cards assign every block and preserve complete prices on narrow screens',()=>{
  assert.ok(css.includes("grid-template-areas:'rank identity' 'market market' 'depth depth' 'score score' 'risk risk'"));
  for(const area of ['rank','identity','market','depth','score','risk'])assert.ok(css.includes('grid-area:'+area));
  assert.ok(css.includes('.onchain-price b {font-size:23px;line-height:1.4;white-space:normal;overflow-wrap:anywhere}'));
  assert.ok(css.includes('.onchain-score-breakdown > span {display:grid;grid-template-columns:minmax(0,1fr)'));
});

test('movers have six desktop columns and labelled mobile values rather than dropped columns',()=>{
  assert.ok(css.includes('grid-template-columns:28px minmax(110px,1fr) 64px 110px 100px 112px!important'));
  for(const label of ['结构分 ','价格 ','24H成交额 '])assert.ok(css.includes(`content:'${label}'`));
  assert.ok(css.includes('min-width:0!important;gap:8px'));
});

test('layout assets load after the existing business assets, and risk has four independent cards',()=>{
  assert.ok(html.indexOf("filename='workspace-layout.css'")>html.indexOf("filename='style.css'"));
  assert.ok(html.indexOf("filename='workspace-layout.js'")>html.indexOf("filename='app.js'"));
  assert.ok(html.includes("filename='workspace-layout.css') }}?v=20261018-alert-ranges-v1"));
  assert.ok(html.includes("filename='workspace-layout.js') }}?v=20261018-alert-desk-v1"));
  const risk=html.slice(html.indexOf('<section id="risk"'),html.indexOf('<section id="thinking"'));
  assert.equal((risk.match(/<article>/g)||[]).length,4);assert.ok(risk.includes('真实下单尚未开放'));
});

test('navigation restores the shared header but data mutations never reset reading position',()=>{
  assert.ok(script.includes("if (layouts[location.hash.slice(1)]) resetNavigationScroll()"));
  const observer=script.slice(script.indexOf('if (main) new MutationObserver'));
  assert.ok(observer.includes('scheduleActiveLayout()'));assert.ok(!observer.includes('resetNavigationScroll()'));
});

test('security and account identifiers may wrap instead of keeping legacy max-content width',()=>{
  assert.ok(css.includes('.security-alert-item code {width:100%;min-width:0;max-width:100%'));
  assert.ok(css.includes('.account-invite-code code) {width:auto;min-width:0;max-width:100%'));
  assert.ok(css.includes('.security-alert-item > div {min-width:0;grid-template-columns:minmax(0,1fr)}'));
});
