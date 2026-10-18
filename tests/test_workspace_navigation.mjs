import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const source=readFileSync(new URL('../static/app.js',import.meta.url),'utf8');
const html=readFileSync(new URL('../templates/index.html',import.meta.url),'utf8');
const workspace=source.slice(source.indexOf('const sectionWorkspaceMeta='),source.indexOf('function updateMobileViewTitle('));
const profiles=source.slice(source.indexOf('const pageRefreshProfiles='),source.indexOf('const fixedCadenceViews='));

class Element {
  constructor(view='',classes=[]){
    this.dataset=view?{view}:{};this.children=[];this.textContent=view;this.attributes={};this.clicked=0;
    const values=new Set(classes);
    Object.defineProperty(this,'className',{get:()=>[...values].join(' '),set:value=>{values.clear();for(const key of String(value).split(/\s+/).filter(Boolean))values.add(key)}});
    this.classList={contains:key=>values.has(key),add:key=>values.add(key),remove:key=>values.delete(key),toggle(key,on){if(on===undefined)on=!values.has(key);if(on)values.add(key);else values.delete(key);return on}};
  }
  setAttribute(key,value){this.attributes[key]=value}
  removeAttribute(key){delete this.attributes[key]}
  click(){this.clicked++}
  appendChild(node){this.children=this.children.filter(item=>item!==node);this.children.push(node)}
  querySelectorAll(selector){
    if(selector==='a[data-view]')return this.children.flatMap(node=>node.dataset.view?[node]:node.querySelectorAll('a[data-view]'));
    if(selector==='.nav-section-heading')return this.children.filter(node=>node.classList.contains('nav-section-heading'));
    return [];
  }
  querySelector(selector){
    if(selector==='.daily-nav')return this.children.find(node=>node.classList.contains('daily-nav'))||null;
    if(selector==='[data-subnav="daily-nav"]')return this.children.find(node=>node.dataset.subnav==='daily-nav')||null;
    return null;
  }
}

function harness(){
  const nav=new Element(),chat=new Element('chat',['hidden']),daily=new Element('',['daily-nav','nav-sub','hidden']),rootDaily=new Element('daily-trends');
  rootDaily.dataset.subnav='daily-nav';daily.children=['daily-trends','daily-thoughts','daily-listings'].map(id=>new Element(id));
  nav.children=[chat,new Element('alerts'),new Element('dashboard'),new Element('spot-futures'),rootDaily,daily,new Element('runtime-rules')];
  const nodes=new Map([['workspaceNavigation',nav],...['sectionSearchCount','sectionSearchEmpty','sectionRefreshFeedback','sectionRefreshButton'].map(id=>[id,new Element()])]);
  const heading=new Element(),subtitle=new Element(),created=[];
  const context={location:{hash:''},activeViewId:()=> 'dashboard',byId:id=>nodes.get(id),refreshIntervalLabel:seconds=>`${seconds}秒`,window:{addEventListener(){}},toggleMobileNav(){},document:{title:'',addEventListener(){},querySelectorAll:()=>nav.querySelectorAll('a[data-view]'),querySelector:selector=>selector==='main>header h1'?heading:selector==='main>header .sub'?subtitle:null,createElement(tag){created.push(tag);return new Element()}}};
  vm.createContext(context);vm.runInContext(profiles+workspace,context);
  return {context,nav,nodes,chat,daily,heading,subtitle,created,run:code=>vm.runInContext(code,context)};
}

test('every profile has a dedicated name, purpose and source boundary',()=>{
  const h=harness();const missing=h.run('Object.keys(pageRefreshProfiles).filter(id=>!sectionWorkspaceMeta[id]?.name||!sectionWorkspaceMeta[id]?.description||!sectionWorkspaceMeta[id]?.source)');
  assert.equal(missing.length,0);assert.equal(h.run('Object.keys(sectionWorkspaceMeta).length'),20);
  for(const id of h.run('Object.keys(sectionWorkspaceMeta)')){h.context.updateSectionWorkspace(id);assert.equal(h.heading.textContent,h.run(`sectionWorkspaceMeta['${id}'].name`));assert.ok(h.subtitle.textContent.length>0)}
});

test('grouping moves existing nodes and never creates permission-bearing links',()=>{
  const h=harness(),before=h.nav.querySelectorAll('a[data-view]');h.context.setupSectionWorkspace();
  assert.deepEqual(h.created,['h2','h2','h2','h2']);assert.equal(h.nav.querySelectorAll('a[data-view]').length,before.length);
  assert.ok(before.every(node=>h.nav.querySelectorAll('a[data-view]').includes(node)));
  assert.equal(h.chat.classList.contains('hidden'),true);
  assert.deepEqual(h.nav.querySelectorAll('.nav-section-heading').map(node=>node.dataset.sectionGroup),['实时机会','研究复盘','提醒协作','系统风控']);
});

test('search never reveals a hidden chat or invents an administrator entry',()=>{
  const h=harness();h.context.setupSectionWorkspace();h.context.filterSectionNavigation('协作');
  assert.equal(h.chat.classList.contains('hidden'),true);assert.equal(h.nodes.get('sectionSearchCount').textContent,'0 个匹配栏目');
  h.context.sectionSearchKey({key:'Enter',preventDefault(){}});assert.equal(h.chat.clicked,0);
  h.context.filterSectionNavigation('账号管理');assert.equal(h.nodes.get('sectionSearchCount').textContent,'0 个匹配栏目');
});

test('daily submenu search opens matching children and Enter chooses the actual match',()=>{
  const h=harness();h.context.setupSectionWorkspace();h.context.filterSectionNavigation('思路分析');
  assert.equal(h.daily.classList.contains('section-search-open'),true);
  h.context.sectionSearchKey({key:'Enter',preventDefault(){}});
  assert.equal(h.daily.children.find(node=>node.dataset.view==='daily-thoughts').clicked,1);
  const input={value:'思路分析'};h.context.sectionSearchKey({key:'Escape',target:input,preventDefault(){}});
  assert.equal(input.value,'');assert.equal(h.daily.classList.contains('section-search-open'),false);assert.equal(h.chat.classList.contains('hidden'),true);
});

test('unknown, missing and private deep links cannot activate an unavailable page',()=>{
  const h=harness();for(const hash of ['#not-a-page','#account-management','#chat']){h.context.location.hash=hash;assert.equal(h.context.restoreSectionFromHash(),false)}
  assert.equal(h.chat.clicked,0);
});

test('read-only page has no fake refresh action and failed views retain a retry affordance',()=>{
  const h=harness();h.context.renderWorkspaceRefreshStatus('risk',{seconds:0,inFlight:0});assert.equal(h.nodes.get('sectionRefreshButton').hidden,true);
  h.context.renderWorkspaceRefreshStatus('spot-futures',{seconds:5,inFlight:0,failed:true});
  assert.equal(h.nodes.get('sectionRefreshButton').hidden,false);assert.equal(h.nodes.get('sectionRefreshButton').textContent,'重试');
  assert.ok(h.nodes.get('sectionRefreshFeedback').textContent.includes('保留上次有效数据'));
  h.context.renderWorkspaceRefreshStatus('daily-trends',{seconds:0,inFlight:0,failed:true});
  assert.ok(h.nodes.get('sectionRefreshFeedback').textContent.includes('请点击重试'));
  assert.ok(!h.nodes.get('sectionRefreshFeedback').textContent.includes('将自动重试'));
});

test('unchanged refresh status does not keep rewriting text during ring animation',()=>{
  const h=harness();let writes=0;
  for(const id of ['sectionRefreshButton','sectionRefreshFeedback']){
    const node=h.nodes.get(id);let value=node.textContent;
    Object.defineProperty(node,'textContent',{get:()=>value,set:next=>{writes++;value=next}});
  }
  const state={seconds:5,inFlight:0};h.context.renderWorkspaceRefreshStatus('spot-futures',state);const initial=writes;
  for(let i=0;i<20;i++)h.context.renderWorkspaceRefreshStatus('spot-futures',state);
  assert.equal(writes,initial);
});

test('technical classification, cadence and internal refresh explanation are removed from the actual shared template',()=>{
  for(const id of ['sectionWorkspaceBar','sectionWorkspaceGroup','sectionWorkspaceCadence','sectionWorkspaceSource'])assert.ok(!html.includes(`id="${id}"`),id);
  assert.ok(!html.includes('声音去重与历史'));assert.ok(!html.includes('结构30秒'));
  const header=html.slice(html.indexOf('<header>'),html.indexOf('</header>'));
  for(const id of ['updated','sectionRefreshButton','sectionRefreshFeedback'])assert.ok(header.includes(`id="${id}"`),id);
});

test('compact refresh control stays stable while loading and still clears genuine failure feedback',()=>{
  const h=harness();h.context.renderWorkspaceRefreshStatus('alerts',{seconds:5,inFlight:1});assert.equal(h.nodes.get('sectionRefreshButton').textContent,'刷新');assert.equal(h.nodes.get('sectionRefreshButton').disabled,true);
  h.context.renderWorkspaceRefreshStatus('alerts',{seconds:5,inFlight:0,failed:true});assert.ok(h.nodes.get('sectionRefreshFeedback').textContent.includes('将自动重试'));
  h.context.renderWorkspaceRefreshStatus('alerts',{seconds:5,inFlight:0});assert.equal(h.nodes.get('sectionRefreshFeedback').textContent,'');assert.equal(h.nodes.get('sectionRefreshButton').disabled,false);
});
