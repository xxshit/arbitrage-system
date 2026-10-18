import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const source=readFileSync(new URL('../static/app.js',import.meta.url),'utf8');
const evidence=source.slice(source.indexOf('function normalizedMarketSymbol'),source.indexOf('function exchangeName'));
const listings=source.slice(source.indexOf('function listingEffectiveTime'),source.indexOf('async function loadSimpleThinking'));
const html=readFileSync(new URL('../templates/index.html',import.meta.url),'utf8');
const css=readFileSync(new URL('../static/workspace-layout.css',import.meta.url),'utf8');

class Element {
  constructor(tag='div'){this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.className='';this.attributes={};this.textContent='';this.html='';this.writes=0;this.value='';this.classList={contains:key=>this.className.split(' ').includes(key),toggle:(key,on)=>{const set=new Set(this.className.split(' ').filter(Boolean));on?set.add(key):set.delete(key);this.className=[...set].join(' ')}}}
  appendChild(child){if(child.parentElement)child.remove();this.children.push(child);child.parentElement=this;return child}
  get firstChild(){return this.children[0]||null}
  insertBefore(child,reference){if(!reference)return this.appendChild(child);child.remove();this.children.splice(this.children.indexOf(reference),0,child);child.parentElement=this;return child}
  remove(){if(this.parentElement)this.parentElement.children=this.parentElement.children.filter(child=>child!==this);this.parentElement=null}
  before(child){const parent=this.parentElement;child.remove();parent.children.splice(parent.children.indexOf(this),0,child);child.parentElement=parent}
  replaceWith(child){const parent=this.parentElement,index=parent.children.indexOf(this);child.remove();parent.children.splice(index,1,child);child.parentElement=parent;this.parentElement=null}
  matches(selector='[data-market-symbol],.symbol-detail-trigger'){return selector.split(',').some(part=>{const value=part.trim().replace(':scope > ','');return value==='[data-market-symbol]'?Boolean(this.dataset.marketSymbol):value.startsWith('.')?this.classList.contains(value.slice(1)):false})}
  closest(selector){for(let node=this;node;node=node.parentElement)if(node.matches(selector))return node;return null}
  contains(node){for(let current=node;current;current=current.parentElement)if(current===this)return true;return false}
  get isConnected(){for(let node=this;node;node=node.parentElement)if(node.connectedRoot)return true;return false}
  getBoundingClientRect(){return this.rect||{left:120,right:139,top:200,bottom:219,width:300,height:130}}
  focus(){this.focused=true}
  querySelector(selector){return this.children.find(child=>child.matches(selector))||null}
  querySelectorAll(selector){return this.children.flatMap(child=>[...(child.matches(selector)?[child]:[]),...child.querySelectorAll(selector)])}
  setAttribute(key,value){this.attributes[key]=value}
  removeAttribute(key){delete this.attributes[key]}
  set innerHTML(value){this.html=value;this.writes++}
  get innerHTML(){return this.html}
}
function harness(fetcher=async()=>({events:[],total:0,pages:1,page:1})){
  const nodes=Object.fromEntries(['dailyListings','listingHistoryRange','listingSymbolSearch','listingResultSummary','listingPreviousPage','listingNextPage','listingQueryError'].map(key=>[key,new Element()]));
  const root=new Element('body');root.connectedRoot=true;const listeners={},timers=new Map(),frames=new Map();let id=0;
  const listen=(key,fn)=>(listeners[key]??=[]).push(fn);
  const context={URL,URLSearchParams,Date,console,setTimeout:fn=>{timers.set(++id,fn);return id},clearTimeout:key=>timers.delete(key),requestAnimationFrame:fn=>{frames.set(++id,fn);return id},window:{innerWidth:1280,innerHeight:900,addEventListener:(key,fn)=>listen('window:'+key,fn)},document:{body:root,activeElement:null,createElement:tag=>{const node=new Element(tag);node.style={};return node},addEventListener:listen,querySelectorAll:selector=>root.querySelectorAll(selector),querySelector:()=>null},byId:key=>nodes[key],fetchRefreshJson:fetcher,renderAutomationStatus(){},escapeChatHtml:value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]))};
  vm.createContext(context);
  vm.runInContext("let delistedMarketSymbols=new Set(),delistedMarketBases=new Set(),delistedSymbolsLoadedAt=0,delistingEventsByBase=new Map(),delistingMetadataRequest=null,delistingClockOffset=0,dailyListingState={range:'all',symbol:'',page:1,pageSize:50,total:0,pages:1},dailyListingRequestVersion=0;\n"+source.match(/^const delistingPopoverState=.*;$/m)[0]+'\n'+evidence+'\n'+listings,context);
  const emit=(key,target,extra={})=>(listeners[key]||[]).forEach(fn=>fn({target,preventDefault(){},stopPropagation(){},...extra}));
  const flush=map=>{const jobs=[...map.values()];map.clear();jobs.forEach(fn=>fn())};
  return {context,nodes,root,run:code=>vm.runInContext(code,context),emit,flushTimers:()=>flush(timers),flushFrames:()=>flush(frames)};
}
function event(overrides={}){return {id:47,symbol:'ESPORTS/USDT',matched_symbols:['ESPORTS/USDT'],exchange:'Bitget',market_scope:'合约',type:'下架',source_kind:'official',source_url:'https://www.bitget.com/support/articles/12560603894224',title:'ESPORTS futures delisting',occurred_at:'2026-09-04 04:00 UTC+8',effective_at:'2026-09-08 15:00 UTC+8',effective_at_iso:'2026-09-08T15:00:00+08:00',effective_time_status:'parsed',...overrides}}

test('past, future, exact execution boundary and unknown times are distinct',()=>{
  const h=harness(),item=event(),at=Date.parse(item.effective_at_iso);
  assert.equal(h.context.delistingEventState(item,at-1).key,'scheduled');
  assert.equal(h.context.delistingEventState(item,at).key,'delisted');
  assert.equal(h.context.delistingEventState(event({effective_time_status:'retry'}),at).key,'unconfirmed');
  assert.equal(h.context.delistingEventState(event({effective_at_iso:null}),at).key,'unconfirmed');
  assert.equal(h.context.delistingEventState(event({source_kind:'detected'}),at).key,'detected');
});

test('the same asset shows every exchange and keeps market scopes separate',()=>{
  const h=harness(),items=[event(),event({id:48,exchange:'Gate',source_url:'https://www.gate.com/announcements/preview'}),event({id:49,market_scope:'现货'})];
  h.context.indexDelistingEvidence(items);const actual=h.context.delistingEvidenceFor('ESPORTS');
  assert.equal(actual.length,3);assert.ok(actual.some(item=>item.exchange==='Gate'));assert.ok(actual.some(item=>item.market_scope==='现货'));
});

test('aliases and multiplier codes reach matching evidence but unrelated names do not',()=>{
  const h=harness();h.context.indexDelistingEvidence([event({symbol:'SATS/USDT',matched_symbols:['SATS/USDT','SATSALIAS/USDT']})]);
  assert.equal(h.context.delistingEvidenceFor('1000SATS/USDT').length,1);
  assert.equal(h.context.delistingEvidenceFor('SATSALIAS/USDT').length,1);
  assert.equal(h.context.delistingEvidenceFor('SATSSTOCK/USDT').length,0);
});

test('only newest evidence per exchange/scope is compacted and official evidence beats detection',()=>{
  const h=harness();h.context.indexDelistingEvidence([event(),event({id:48,occurred_at:'2026-10-01 00:00 UTC+8'}),event({id:49,source_kind:'detected',occurred_at:'2026-10-18 00:00 UTC+8'})]);
  assert.equal(h.context.delistingEvidenceFor('ESPORTS')[0].id,48);
});

test('collapsed warning uses the original text-presentation warning triangle, not a circular exclamation',()=>{
  const h=harness(),markup=h.context.delistingEvidenceMarkup('ESPORTS/USDT');
  assert.equal((markup.match(/<button/g)||[]).length,1);assert.ok(markup.includes('<span aria-hidden="true">⚠&#xfe0e;</span>'));assert.ok(markup.includes('aria-expanded="false"'));
  const style=css.match(/\.delisting-evidence \.delisting-warning-trigger \{([^}]+)\}/)[1];
  assert.ok(style.includes('color:#e12636!important'));assert.ok(style.includes('border:0'));assert.ok(!style.includes('border-radius:50%'));
  for(const text of ['Bitget','2026-09-08','<a','title='])assert.ok(!markup.includes(text),text);
});

test('single-coin popup keeps safe original links but suppresses shared titles and old publication metadata',()=>{
  const h=harness();h.context.indexDelistingEvidence([event({title:'Delist 4USDT, GOATUSDT, ESPORTSUSDT futures'})]);const markup=h.context.delistingPopoverMarkup('ESPORTS/USDT');
  for(const text of ['ESPORTS/USDT','Bitget','合约','已下架','https://www.bitget.com/support/articles/12560603894224','noopener noreferrer'])assert.ok(markup.includes(text),text);
  for(const text of ['GOATUSDT','4USDT','2026-09-04','title=','onclick='])assert.ok(!markup.includes(text),text);
});

test('unsafe links and untrusted announcement text cannot become executable markup',()=>{
  const h=harness();
  for(const value of ['javascript:alert(1)','https://bitget.com.evil.test/x','https://www.bitget.com@evil.test/x','http://www.bitget.com/x'])assert.equal(h.context.safeListingSourceUrl(value),'');
  h.context.indexDelistingEvidence([event({exchange:'<img src=x onerror=alert(1)>',title:'" onclick="alert(1)',source_url:'javascript:alert(1)'})]);const markup=h.context.delistingPopoverMarkup('ESPORTS');
  assert.ok(!markup.includes('<img'));assert.ok(!markup.includes('href="javascript:'));assert.ok(markup.includes('&lt;img'));
});

test('warning decoration is idempotent and retains the original symbol button',()=>{
  const h=harness();h.context.indexDelistingEvidence([event()]);h.run("delistedMarketBases.add('ESPORTS')");
  const node=new Element('button');node.dataset.marketSymbol='ESPORTS/USDT';node.className='symbol-detail-trigger';h.root.appendChild(node);
  h.context.applyDelistingWarnings(h.root);const wrapper=h.root.children[0],detail=wrapper.children[0],writes=detail.writes;
  assert.equal(wrapper.children[1],node);assert.ok(detail.classList.contains('delisting-evidence'));assert.equal(wrapper.children.length,2);
  h.context.applyDelistingWarnings(h.root);assert.equal(h.root.children[0],wrapper);assert.equal(wrapper.children.length,2);assert.equal(detail.writes,writes);
  h.run('delistedMarketBases.clear()');h.context.applyDelistingWarnings(h.root);assert.equal(h.root.children[0],node);
});

test('table cells keep valid table structure and evidence inside the cell',()=>{
  const h=harness();h.context.indexDelistingEvidence([event()]);h.run("delistedMarketBases.add('ESPORTS')");
  const row=new Element('tr'),cell=new Element('td');cell.dataset.marketSymbol='ESPORTS/USDT';row.appendChild(cell);h.root.appendChild(row);
  h.context.applyDelistingWarnings(h.root);assert.equal(row.children[0],cell);assert.equal(cell.children.length,1);assert.ok(cell.children[0].classList.contains('delisting-evidence'));
});

test('warning triangle precedes a table cell coin name without moving or duplicating the name',()=>{
  const h=harness();h.run("delistedMarketBases.add('ESPORTS')");
  const cell=new Element('td'),name=new Element('span');cell.dataset.marketSymbol='ESPORTS/USDT';name.textContent='ESPORTS/USDT';cell.appendChild(name);h.root.appendChild(cell);
  h.context.applyDelistingWarnings(h.root);const warning=cell.children[0];
  assert.ok(warning.classList.contains('delisting-evidence'));assert.equal(cell.children[1],name);
  h.context.applyDelistingWarnings(h.root);assert.equal(cell.children[0],warning);assert.equal(cell.children[1],name);assert.equal(cell.children.length,2);
});

test('metadata fetch failures retain last valid exchange evidence and deduplicate concurrent reads',async()=>{
  let calls=0,reject;const h=harness(()=>{calls++;return new Promise((resolve,no)=>{reject=no})});h.context.indexDelistingEvidence([event()]);
  const first=h.context.loadDelistedMarketSymbols(true),second=h.context.loadDelistedMarketSymbols(true);assert.equal(calls,1);reject(new Error('upstream failure'));await Promise.all([first,second]);
  assert.equal(h.context.delistingEvidenceFor('ESPORTS').length,1);
});

test('history queries explicitly include range, symbol and pagination',async()=>{
  let url;const h=harness(async value=>{url=value;return {events:[event()],total:73,pages:2,page:2}});h.run("dailyListingState={...dailyListingState,symbol:'ESPORTS',page:2}");await h.context.loadDailyListings();
  const query=new URL(url,'https://example.test').searchParams;assert.equal(query.get('range'),'all');assert.equal(query.get('symbol'),'ESPORTS');assert.equal(query.get('page'),'2');
  assert.ok(h.nodes.listingResultSummary.textContent.includes('共 73 条'));assert.ok(h.nodes.dailyListings.innerHTML.includes('查看原公告'));
});

test('out-of-order search responses never overwrite a newer result',async()=>{
  const resolvers=[],h=harness(()=>new Promise(resolve=>resolvers.push(resolve)));
  const old=h.context.loadDailyListings();h.run("dailyListingState.symbol='NEW'");const latest=h.context.loadDailyListings();
  resolvers[1]({events:[event({symbol:'NEW/USDT'})],total:1,pages:1,page:1});await latest;
  resolvers[0]({events:[event({symbol:'OLD/USDT'})],total:1,pages:1,page:1});await old;
  assert.ok(h.nodes.dailyListings.innerHTML.includes('NEW/USDT'));assert.ok(!h.nodes.dailyListings.innerHTML.includes('OLD/USDT'));
});

test('failed history requests preserve displayed rows and show a visible error',async()=>{
  const h=harness(async()=>{throw new Error('network unavailable')});h.nodes.dailyListings.innerHTML='previous verified rows';await h.context.loadDailyListings();
  assert.equal(h.nodes.dailyListings.innerHTML,'previous verified rows');assert.ok(h.nodes.listingQueryError.textContent.includes('保留上次结果'));
});

test('search resets to page one and history entry resets date limits',async()=>{
  const urls=[],h=harness(async url=>{urls.push(url);return {events:[],total:0,page:1,pages:1}});h.run('dailyListingState.page=4');h.nodes.listingHistoryRange.value='7d';h.nodes.listingSymbolSearch.value=' esports ';
  await h.context.searchDailyListings();assert.ok(urls[0].includes('symbol=ESPORTS'));assert.ok(urls[0].includes('page=1'));
  h.context.openListingHistory('ESPORTS/USDT');assert.equal(h.run('dailyListingState.range'),'all');assert.equal(h.run('dailyListingState.symbol'),'ESPORTS');
});

test('history UI and mobile warning layout keep the existing market click action',()=>{
  for(const id of ['listingHistoryRange','listingSymbolSearch','listingPreviousPage','listingNextPage','listingQueryError'])assert.ok(html.includes(`id="${id}"`));
  assert.ok(source.includes('onclick="openSymbolDetail('));assert.ok(!evidence.includes('openSymbolDetail('));
  assert.ok(css.includes('.delisting-warning-trigger'));assert.ok(css.includes('overflow-wrap:anywhere'));assert.ok(html.includes('全部已入库记录'));
});

function trigger(h,symbol='ESPORTS/USDT'){const node=new Element('button');node.className='delisting-warning-trigger';node.dataset.delistingSymbol=symbol;h.root.appendChild(node);return node}

test('hover opens only the target asset and leaving closes it',()=>{
  const h=harness();h.context.indexDelistingEvidence([event(),event({id:48,symbol:'OTHER/USDT',matched_symbols:['OTHER/USDT'],exchange:'Gate'})]);const button=trigger(h);
  assert.equal(h.run('delistingPopoverState.popup'),null);h.emit('pointerover',button,{pointerType:'mouse'});
  const popup=h.run('delistingPopoverState.popup');assert.ok(popup.innerHTML.includes('Bitget'));assert.ok(!popup.innerHTML.includes('OTHER'));assert.ok(!popup.innerHTML.includes('Gate'));
  assert.equal(button.attributes['aria-expanded'],'true');h.emit('pointerout',button);h.flushTimers();assert.equal(h.run('delistingPopoverState.popup'),null);
});

test('upcoming popup includes exact date while a past row stays compact',()=>{
  const h=harness();h.context.indexDelistingEvidence([event(),event({id:48,exchange:'Gate',effective_at:'2099-10-20 15:00 UTC+8',effective_at_iso:'2099-10-20T15:00:00+08:00'})]);const markup=h.context.delistingPopoverMarkup('ESPORTS');
  assert.ok(markup.includes('2099-10-20 15:00 UTC+8 即将下架'));assert.ok(!markup.includes('2026-09-08 15:00'));
});

test('moving from the exclamation into its popup keeps announcement links usable',()=>{
  const h=harness();h.context.indexDelistingEvidence([event()]);const button=trigger(h);h.emit('pointerover',button,{pointerType:'mouse'});const popup=h.run('delistingPopoverState.popup');
  h.emit('pointerout',button);h.emit('pointerover',popup,{pointerType:'mouse'});h.flushTimers();assert.equal(h.run('delistingPopoverState.popup'),popup);
});

test('hovering another coin replaces the popup rather than mixing their exchanges',()=>{
  const h=harness();h.context.indexDelistingEvidence([event(),event({id:48,symbol:'OTHER/USDT',matched_symbols:['OTHER/USDT'],exchange:'Gate'})]);const first=trigger(h),second=trigger(h,'OTHER/USDT');
  h.emit('pointerover',first,{pointerType:'mouse'});h.emit('pointerover',second,{pointerType:'mouse'});const popup=h.run('delistingPopoverState.popup');
  assert.ok(popup.innerHTML.includes('OTHER/USDT'));assert.ok(popup.innerHTML.includes('Gate'));assert.ok(!popup.innerHTML.includes('ESPORTS'));assert.ok(!popup.innerHTML.includes('Bitget'));
  assert.equal(h.root.querySelectorAll('.delisting-popover').length,1);
});

test('phone tap pins the popup, second tap closes it, and hover is ignored for touch',()=>{
  const h=harness();h.context.indexDelistingEvidence([event()]);const button=trigger(h);
  h.emit('pointerover',button,{pointerType:'touch'});assert.equal(h.run('delistingPopoverState.popup'),null);
  h.emit('click',button);assert.ok(h.run('delistingPopoverState.popup'));h.emit('pointerout',button);h.flushTimers();assert.ok(h.run('delistingPopoverState.popup'));
  h.emit('click',button);assert.equal(h.run('delistingPopoverState.popup'),null);
});

test('keyboard focus opens and Escape dismisses without discarding the symbol action',()=>{
  const h=harness();h.context.indexDelistingEvidence([event()]);const button=trigger(h);h.emit('focusin',button);assert.ok(h.run('delistingPopoverState.popup'));
  h.emit('keydown',button,{key:'Escape'});assert.equal(h.run('delistingPopoverState.popup'),null);assert.ok(button.focused);
});

test('outside click, page navigation and document scrolling dismiss stale popups',()=>{
  const h=harness();h.context.indexDelistingEvidence([event()]);const button=trigger(h);
  for(const type of ['click','scroll','window:hashchange','window:resize']){h.context.showDelistingPopover(button,true);h.emit(type,h.root);assert.equal(h.run('delistingPopoverState.popup'),null,type)}
});

test('a 5-second rerender retargets an open popup without flicker or duplicate content',()=>{
  const h=harness();h.context.indexDelistingEvidence([event()]);const first=trigger(h);h.context.showDelistingPopover(first);const popup=h.run('delistingPopoverState.popup'),writes=popup.writes;first.remove();const second=trigger(h);
  h.context.scheduleDelistingPopoverReconcile();h.flushFrames();assert.equal(h.run('delistingPopoverState.popup'),popup);assert.equal(h.run('delistingPopoverState.trigger'),second);assert.equal(second.attributes['aria-expanded'],'true');assert.equal(popup.writes,writes);
  second.remove();h.context.reconcileDelistingPopover();assert.equal(h.run('delistingPopoverState.popup'),null);
});

test('popup placement stays inside narrow viewports and flips above near the bottom',()=>{
  const h=harness(),position=h.context.delistingPopoverPosition({left:310,top:730,bottom:749},{width:296,height:180},320,800);
  assert.equal(position.left,12);assert.equal(position.top,543);
  const top=h.context.delistingPopoverPosition({left:2,top:5,bottom:24},{width:296,height:180},320,800);assert.equal(top.left,12);assert.equal(top.top,31);
});
