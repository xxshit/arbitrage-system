import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import vm from 'node:vm';
import test from 'node:test';

const source=readFileSync(new URL('../static/app.js',import.meta.url),'utf8');
const helpers=source.slice(source.indexOf('function beijingFundingTime('),source.indexOf('function dualFundingCap('));
const context=vm.createContext({});
vm.runInContext(helpers,context);
const now=Date.parse('2026-10-20T13:00:00Z'); // Beijing 21:00, not the browser's timezone.

test('exchange epoch renders next-day midnight in Beijing with no 24:00 ambiguity',()=>{
  assert.equal(context.beijingFundingTime(1792512000),'10-21 00:00');
  assert.equal(context.fundingClock('10-21 00:00',now),'明日 00:00');
});
test('current-day clock is compact and preformatted Beijing API text is never shifted twice',()=>{
  assert.equal(context.fundingClock('10-20 22:00',now),'22:00');
  assert.equal(context.fundingClock('10-20 23:30',now),'23:30');
});
test('midnight rollover removes tomorrow and preserves the authoritative settlement hour',()=>{
  assert.equal(context.fundingClock('10-21 01:00',Date.parse('2026-10-20T16:00:00Z')),'01:00');
  assert.equal(context.fundingClock('10-21 00:30',now),'明日 00:30');
});
test('month and year crossover are based on Beijing calendar',()=>{
  assert.equal(context.fundingClock('11-01 00:00',Date.parse('2026-10-31T13:00:00Z')),'明日 00:00');
  assert.equal(context.fundingClock('01-01 00:00',Date.parse('2026-12-31T13:00:00Z')),'明日 00:00');
  assert.equal(context.fundingClock('02-29 00:00',Date.parse('2028-02-28T13:00:00Z')),'明日 00:00');
});
test('older or more distant dates retain their date rather than becoming a fake next interval',()=>{
  assert.equal(context.fundingClock('10-19 16:00',now),'10-19 16:00');
  assert.equal(context.fundingClock('10-22 00:00',now),'10-22 00:00');
  assert.equal(context.fundingClock('10-20 08:00',now),'08:00');
});
test('missing, invalid and unsafe text remains a dash, not zero or guessed time',()=>{
  for(const value of [null,undefined,'',0,'—','12:00','<img onerror=alert(1)>','10-21 24:00','13-01 00:00'])
    assert.equal(context.fundingClock(value,now),'—');
  for(const value of [null,undefined,NaN,Infinity,0,-1,1e30])
    assert.equal(context.beijingFundingTime(value),'—');
});
test('browser timezone changes cannot change funding clocks or the meaning of tomorrow',()=>{
  for(const TZ of ['UTC','Asia/Bangkok','Asia/Shanghai','America/New_York']){
    const program=helpers+`\nprocess.stdout.write(JSON.stringify([beijingFundingTime(1792512000),fundingClock('10-21 00:00',${now})]));`;
    const result=spawnSync(process.execPath,['-e',program],{env:{...process.env,TZ},encoding:'utf8'});
    assert.equal(result.status,0,result.stderr);
    assert.deepEqual(JSON.parse(result.stdout),['10-21 00:00','明日 00:00']);
  }
});
test('both strategy clocks label Beijing time and dual does not call an absolute time a countdown',()=>{
  const spot=source.slice(source.indexOf('function spotRows('),source.indexOf('function changeSpotFuturesPage('));
  const dual=source.slice(source.indexOf('function dualFundingLine('),source.indexOf('function dualCardLiquidity('));
  for(const text of [spot,dual])assert.ok(text.includes('下次结算 · 北京时间（UTC+8）'));
  assert.ok(dual.includes('资费 / 上限 / 结算'));
  assert.ok(!dual.includes('倒计时'));
});
