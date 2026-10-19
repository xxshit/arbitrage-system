import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

const css=readFileSync(new URL('../static/workspace-layout.css',import.meta.url),'utf8');
const app=readFileSync(new URL('../static/app.js',import.meta.url),'utf8');
const dual=css.slice(css.indexOf('/* Dual scanner:'),css.indexOf('/* Spot scanner:'));

test('dual allocates remaining viewport height to the table without hiding overflow',()=>{
  assert.ok(dual.includes('body:has(#dual-futures.active) {height:var(--mobile-viewport-height,100dvh);min-height:0}'));
  assert.ok(dual.includes('body:has(#dual-futures.active) main {display:flex;flex-direction:column;height:100%;min-height:0;'));
  assert.ok(dual.includes('.layout-view#dual-futures.active {display:flex;flex:1 1 0;flex-direction:column;min-height:0}'));
  assert.ok(dual.includes('.layout-view#dual-futures > .panel {display:flex;flex:1 1 0;flex-direction:column;min-height:0;margin:0;'));
  assert.ok(dual.includes('.layout-view#dual-futures.active .table-wrap {flex:1 1 0;min-height:0;max-height:none;overflow:auto;overscroll-behavior:contain}'));
  assert.ok(!/overflow(?:-y)?:hidden|scrollbar-width:none|-webkit-scrollbar/.test(dual));
  assert.ok(!dual.includes('#spot-futures'));assert.ok(!dual.includes('#equity-arbitrage'));
});

test('dual fixes controls and status outside the scroll region and retains all filters',()=>{
  assert.ok(dual.includes(':is(.panel-title,.pagination,.data-status) {flex:0 0 auto}'));
  const shell=app.slice(app.indexOf('function renderDualFuturesShell('),app.indexOf('const equityState='));
  for(const id of ['dualFuturesRows','dualFuturesSearch','dualIncludeBybitOkx','dualPreviousPage','dualNextPage','dualFuturesPageSelect','dualPageInfo','dualFuturesUpdated'])assert.ok(shell.includes(`id="${id}"`),id);
  for(const name of ['dual-sort-picker','search-suggest-wrap','filter-check','pagination','data-status'])assert.ok(!new RegExp('\\.'+name+'[^}]*display:none').test(dual),name);
  assert.ok(app.includes("'dual-futures':{label:'期多期空',seconds:5"));
});

test('dual narrow pagination and short-screen fallback keep every control reachable',()=>{
  assert.ok(dual.includes('grid-template-columns:auto minmax(0,1fr) auto'));
  for(const id of ['dualPreviousPage','dualNextPage','dualPageInfo'])assert.ok(dual.includes('.pagination #'+id));
  assert.ok(dual.includes('@media(max-height:520px), (max-width:760px) and (max-height:640px)'));
  assert.ok(dual.includes('body:has(#dual-futures.active) main {display:block;height:auto}'));
  assert.ok(dual.includes('.layout-view#dual-futures > .panel {display:block}'));
});
