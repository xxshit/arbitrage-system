import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const source=readFileSync(new URL('../static/app.js',import.meta.url),'utf8');
const helpers=source.slice(source.indexOf('function transferFlagText('),source.indexOf('function spotRows('));
const css=readFileSync(new URL('../static/workspace-layout.css',import.meta.url),'utf8');
const wallet=(overrides={})=>({exchange:'Gate',symbol:'DEMO/USDT',state:'fresh',chains:[{name:'BSC',deposit_open:true,withdraw_open:true}],captured_at:'10-18 12:00:00 UTC',...overrides});
class Element {
  constructor(tag='div'){this.tagName=tag;this.children=[];this.dataset={};this.attributes={};this.className='';this.style={};this.scrollTop=0;this.writes=0;this.html='';this.classList={contains:key=>this.className.split(' ').includes(key)}}
  appendChild(node){node.remove();this.children.push(node);node.parentElement=this;return node}
  remove(){if(this.parentElement)this.parentElement.children=this.parentElement.children.filter(node=>node!==this);this.parentElement=null}
  matches(selector){return selector.split(',').some(part=>part.trim().startsWith('.')&&this.className.split(' ').includes(part.trim().slice(1)))}
  closest(selector){for(let node=this;node;node=node.parentElement)if(node.matches(selector))return node;return null}
  querySelectorAll(selector){return this.children.flatMap(node=>[...(node.matches(selector)?[node]:[]),...node.querySelectorAll(selector)])}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null}
  get isConnected(){for(let node=this;node;node=node.parentElement)if(node.root)return true;return false}
  getBoundingClientRect(){return this.rect||{left:100,top:200,bottom:220,width:280,height:180}}
  setAttribute(key,value){this.attributes[key]=value}
  removeAttribute(key){delete this.attributes[key]}
  focus(){this.focused=true}
  set innerHTML(value){this.html=value;this.writes++;this.children=[];const scroll=new Element();scroll.className='transfer-popover-chains';this.appendChild(scroll)}
  get innerHTML(){return this.html}
}
function harness(){
  const root=new Element('body');root.root=true;const listeners={},timers=new Map();let id=0;
  const listen=(name,fn)=>(listeners[name]??=[]).push(fn);
  const context={console,setTimeout:fn=>{timers.set(++id,fn);return id},clearTimeout:key=>timers.delete(key),window:{innerWidth:1280,innerHeight:900,addEventListener:(name,fn)=>listen('window:'+name,fn)},document:{body:root,createElement:tag=>new Element(tag),querySelectorAll:selector=>root.querySelectorAll(selector),addEventListener:listen},escapeChatHtml:value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]))};
  vm.createContext(context);vm.runInContext(helpers+'\ninstallTransferPopoverHandlers();',context);
  const view=new Element();view.className='view active';view.id='alerts';root.appendChild(view);
  const trigger=(status=wallet())=>{const node=new Element('button');node.className='transfer-status-control';node.dataset={transferKey:JSON.stringify([status.symbol,status.exchange]),transferStatus:encodeURIComponent(JSON.stringify(status))};view.appendChild(node);return node};
  return {context,root,view,trigger,run:code=>vm.runInContext(code,context),emit:(name,target,extra={})=>(listeners[name]||[]).forEach(fn=>fn({target,preventDefault(){},...extra})),flush:()=>{const jobs=[...timers.values()];timers.clear();jobs.forEach(fn=>fn())}};
}

test('normal requires fresh, nonempty, explicitly open flags on every chain',()=>{
  const h=harness();assert.equal(h.context.transferSummary(wallet()).key,'normal');
  for(const status of [null,{},wallet({state:'stale'}),wallet({state:'unknown'}),wallet({chains:[]}),wallet({chains:[{deposit_open:true,withdraw_open:null}]}),wallet({chains:[{deposit_open:'true',withdraw_open:true}]})])assert.equal(h.context.transferSummary(status).key,'unknown');
});
test('a single closure or withdrawal delay makes a mixed multi-chain snapshot abnormal',()=>{
  const h=harness();for(const bad of [{deposit_open:false,withdraw_open:true},{deposit_open:true,withdraw_open:false},{deposit_open:true,withdraw_open:true,withdraw_delayed:true}])assert.equal(h.context.transferSummary(wallet({chains:[{deposit_open:true,withdraw_open:true},bad,{deposit_open:null}]})).label,'充提异常');
});
test('semantic tag colors override legacy button hover colors and popup headers have no inherited gap',()=>{
  for(const [state,color] of [['normal','#00866b'],['abnormal','#cf3030'],['unknown','#936b16']])assert.ok(css.includes(`.layout-view .transfer-status-control.transfer-summary-${state} {color:${color};border-color:transparent}`));
  assert.ok(css.includes('flex:0 0 auto;margin:0;padding:0}'));assert.ok(!css.includes('.floating-detail-popup.transfer-popup'));
});
test('hover shows only this exchange and allows moving into its scrollable popup',()=>{
  const h=harness(),first=h.trigger(),other=h.trigger(wallet({exchange:'Bitget',chains:[{name:'ETH',deposit_open:false,withdraw_open:false}]}));
  h.emit('pointerover',first,{pointerType:'mouse'});const popup=h.run('transferPopoverState.popup');assert.ok(popup.innerHTML.includes('BSC'));assert.ok(!popup.innerHTML.includes('Bitget'));assert.equal(first.attributes['aria-expanded'],'true');
  h.emit('pointerout',first);h.emit('pointerover',popup,{pointerType:'mouse'});h.flush();assert.equal(h.run('transferPopoverState.popup'),popup);
  h.emit('pointerover',other,{pointerType:'mouse'});assert.ok(h.run('transferPopoverState.popup').innerHTML.includes('Bitget'));assert.ok(!h.run('transferPopoverState.popup').innerHTML.includes('BSC'));assert.equal(h.root.querySelectorAll('.transfer-popover').length,1);
});
test('leaving closes, while touch taps pin and the second tap closes',()=>{
  const h=harness(),button=h.trigger();h.emit('pointerover',button,{pointerType:'touch'});assert.equal(h.run('transferPopoverState.popup'),null);
  h.emit('click',button);h.emit('pointerout',button);h.flush();assert.ok(h.run('transferPopoverState.popup'));h.emit('click',button);assert.equal(h.run('transferPopoverState.popup'),null);
  h.emit('pointerover',button,{pointerType:'mouse'});h.emit('pointerout',button);h.flush();assert.equal(h.run('transferPopoverState.popup'),null);
});
test('keyboard focus, ArrowDown and Escape work without losing the original trigger',()=>{
  const h=harness(),button=h.trigger();h.emit('focusin',button);assert.ok(h.run('transferPopoverState.popup'));h.emit('keydown',button,{key:'ArrowDown'});assert.ok(h.run('transferPopoverState.popup').querySelector('.transfer-popover-chains').focused);
  h.emit('keydown',button,{key:'Escape'});assert.equal(h.run('transferPopoverState.popup'),null);assert.ok(button.focused);assert.equal(button.attributes['aria-expanded'],'false');
});
test('outside click, navigation, resize and external scrolling dismiss, internal scrolling does not',()=>{
  const h=harness(),button=h.trigger();for(const type of ['click','window:hashchange','window:resize','scroll']){h.context.showTransferPopover(button,true);const popup=h.run('transferPopoverState.popup');h.emit('scroll',popup);assert.equal(h.run('transferPopoverState.popup'),popup);h.emit(type,h.root);assert.equal(h.run('transferPopoverState.popup'),null,type)}
});
test('five-second reconciliation keeps one popup and scroll, and updates changed wallet flags',()=>{
  const h=harness(),first=h.trigger();h.context.showTransferPopover(first,true);const popup=h.run('transferPopoverState.popup');popup.querySelector('.transfer-popover-chains').scrollTop=230;const writes=popup.writes;
  first.remove();const next=h.trigger();h.context.reconcileTransferPopover();assert.equal(h.run('transferPopoverState.popup'),popup);assert.equal(h.run('transferPopoverState.trigger'),next);assert.equal(popup.writes,writes);assert.equal(popup.querySelector('.transfer-popover-chains').scrollTop,230);
  next.dataset.transferStatus=encodeURIComponent(JSON.stringify(wallet({chains:[{name:'BSC',deposit_open:false,withdraw_open:true}]})));h.context.reconcileTransferPopover();assert.ok(popup.innerHTML.includes('充提异常'));assert.equal(popup.querySelector('.transfer-popover-chains').scrollTop,230);
});
test('reused trigger for another coin or removed path closes instead of leaking previous coin chains',()=>{
  const h=harness(),button=h.trigger();h.context.showTransferPopover(button);button.dataset.transferKey=JSON.stringify(['OTHER/USDT','Gate']);h.context.reconcileTransferPopover();assert.equal(h.run('transferPopoverState.popup'),null);
});
test('popup stays inside narrow screens near the bottom and malformed data does not open',()=>{
  const h=harness(),button=h.trigger();h.context.window.innerWidth=320;h.context.window.innerHeight=800;button.rect={left:300,top:730,bottom:750};h.context.showTransferPopover(button);const popup=h.run('transferPopoverState.popup');assert.equal(popup.style.left,'28px');assert.equal(popup.style.top,'544px');
  h.context.closeTransferPopover();for(const payload of ['not-json',encodeURIComponent('null'),encodeURIComponent('[]')]){button.dataset.transferStatus=payload;h.context.showTransferPopover(button);assert.equal(h.run('transferPopoverState.popup'),null)}
});
