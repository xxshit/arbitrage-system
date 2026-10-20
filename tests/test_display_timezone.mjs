import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import test from 'node:test';

const app=readFileSync(new URL('../static/app.js',import.meta.url),'utf8');
const chart=readFileSync(new URL('../static/spread-history.js',import.meta.url),'utf8');
const funding=app.slice(app.indexOf('function beijingFundingTime('),app.indexOf('function dualFundingCap('))+
  app.split('\n').find(line=>line.startsWith('function fundingTime('));
const chartClock=chart.slice(chart.indexOf('function spreadChartTime('),chart.indexOf('function spreadChartGeometry('));
const zones=['UTC','Asia/Bangkok','Asia/Shanghai','America/New_York'];
const run=(program,TZ)=>{
  const result=spawnSync(process.execPath,['-e',program],{env:{...process.env,TZ},encoding:'utf8'});
  assert.equal(result.status,0,result.stderr);
  return JSON.parse(result.stdout);
};

test('funding history seconds and chart axes/tooltips use Beijing in all browser timezones',()=>{
  for(const zone of zones){
    const result=run(funding+chartClock+`\nprocess.stdout.write(JSON.stringify([
      fundingTime(1792512005),spreadChartTime(1792512005),spreadChartTime(1792512005,true),
      fundingTime(Date.parse('2026-12-31T16:00:05Z')/1000)]));`,zone);
    assert.deepEqual(result,['10-21 00:00:05','10/21 00:00:05','10/21 00:00','01-01 00:00:05']);
  }
});

test('all dashboard, equity and upstream freshness clocks explicitly select Beijing',()=>{
  const clocks=[...app.matchAll(/new Date\(([^;\n]*?)\)\.toLocaleTimeString\('zh-CN',(\{[^}]*\})\)/g)];
  assert.equal(clocks.length,5);
  for(const [, ,options] of clocks){
    assert.ok(options.includes("timeZone:'Asia/Shanghai'"));
    for(const zone of zones)
      assert.equal(run(`process.stdout.write(JSON.stringify(new Date(1792512005000).toLocaleTimeString('zh-CN',${options})));`,zone),'00:00:05');
  }
});

test('alert history and chart label the timezone accurately without local/UTC-only labels',()=>{
  assert.ok(app.includes('时间（北京时间）'));
  assert.ok(app.includes('发现时间（北京时间）'));
  assert.ok(!app.includes('时间（UTC）'));
  assert.ok(!app.includes('${fundingTime(item.settlement_at)} UTC'));
  assert.ok(chart.includes('基差与现货路径无关 · 北京时间'));
  assert.ok(!chart.includes('本机时区'));
  assert.ok(!app.includes('toISOString().slice(5,19)'));
});
