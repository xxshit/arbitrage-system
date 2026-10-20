/* Read-only, path-local research. Strategy tabs remain spot/dual, not drivers. */
(() => {
  'use strict';
  const state={strategy:'spot',grade:'all',status:'all',search:'',page:1,notional:1000,hold_hours:72,
    dual_sort:'open_spread',dual_direction:'desc',
    data:null,error:'',controller:null,version:0,order:[],opened:new Set(),books:new Map(),bookPending:new Set()};
  const $=id=>document.getElementById(id);
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const pct=(value,digits=2)=>Number.isFinite(value)?`${value>0?'+':''}${value.toFixed(digits)}%`:'—';
  const qty=value=>Number.isFinite(value)?value.toLocaleString('en-US',{maximumFractionDigits:8}):'—';
  const money=value=>Number.isFinite(value)?`${value.toLocaleString('en-US',{maximumFractionDigits:0})} USDT`:'—';
  const time=value=>Number.isFinite(value)?new Date(value*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}):'—';
  const key=item=>JSON.stringify([item.symbol,item.long_exchange,item.short_exchange]);
  const attr=item=>esc(encodeURIComponent(key(item)));
  const color=value=>Number.isFinite(value)?value>0?'positive':value<0?'negative':'neutral':'neutral';
  function qualified(item,now=Date.now()/1000){
    return item.status==='opportunity'&&Number.isFinite(item.quoted_at)&&now-item.quoted_at<=10&&now>=item.quoted_at-2;
  }
  function select(items,filters,now=Date.now()/1000){
    return (items||[]).filter(item=>
      (filters.grade==='all'||item.grade===filters.grade)&&
      (filters.status==='all'||(filters.status==='opportunity'?qualified(item,now):!qualified(item,now)))&&
      (!filters.search||String(item.symbol).toUpperCase().includes(filters.search.toUpperCase())));
  }
  function ordered(items,previous,preserve){
    if(!preserve)return items;
    const map=new Map(items.map(item=>[key(item),item]));
    return [...previous.filter(k=>map.has(k)).map(k=>{const item=map.get(k);map.delete(k);return item}),...map.values()];
  }
  function dualSorted(items,field,direction){
    return [...items].sort((a,b)=>{
      const av=a[field],bv=b[field];
      if(!Number.isFinite(av))return Number.isFinite(bv)?1:0;
      if(!Number.isFinite(bv))return -1;
      return (av-bv)*(direction==='asc'?1:-1);
    });
  }
  function init(){
    const host=$('thinking');if(!host)return false;
    if($('simpleThinkingList'))return true;
    host.insertAdjacentHTML('beforeend',`<div class="panel opportunity-workspace" id="simpleThinkingList">
      <div class="opportunity-toolbar"><div class="opportunity-tabs" role="group" aria-label="套利策略">
        <button type="button" data-thinking-strategy="spot" onclick="ThinkingPool.strategy('spot')">现多期空</button>
        <button type="button" data-thinking-strategy="dual" onclick="ThinkingPool.strategy('dual')">期多期空</button>
      </div><label><input id="opportunitySearch" type="search" placeholder="搜索币种" aria-label="搜索机会币种" oninput="ThinkingPool.filter('search',this.value)"></label>
      <label class="opportunity-spot-control">等级 <select id="opportunityGrade" onchange="ThinkingPool.filter('grade',this.value)"><option value="all">全部等级</option><option value="A">A · 优先</option><option value="B">B · 关注</option><option value="C">C · 低优先观察</option></select></label>
      <label class="opportunity-spot-control">状态 <select id="opportunityStatus" onchange="ThinkingPool.filter('status',this.value)"><option value="all">全部状态</option><option value="opportunity">当前有机会</option><option value="watch">值得观察</option></select></label>
      <label class="opportunity-dual-control" hidden>排序 <select aria-label="期多期空候选排序" onchange="ThinkingPool.filter('dual_sort',this.value)">${[['open_spread','开差'],['close_spread','平差'],['funding_current','当前资费'],['funding_previous','上期资费'],['funding_24h','24H资费'],['funding_3d','3D资费']].map(([value,label])=>`<option value="${value}">${label}</option>`).join('')}</select><button type="button" id="opportunityDualDirection" onclick="ThinkingPool.direction()">↓</button></label>
      <button type="button" onclick="ThinkingPool.reorder()">重新排序</button></div>
      <details class="opportunity-method opportunity-spot-control"><summary>分级与测算设置 <span id="opportunitySettingsLabel"></span></summary>
        <form onsubmit="ThinkingPool.configure(event)"><label>现货参考金额（USDT）<input id="opportunityNotional" type="number" min="100" max="1000000" step="1" value="1000" required></label>
        <label>测算持有期<select id="opportunityHold"><option value="24">1天</option><option value="72" selected>3天</option><option value="168">7天</option></select></label><button type="submit">重新测算</button></form>
        <p>A 优先：当前净空间情景 ≥0.30%；B 关注：资费改善、价差活跃或当前净空间 ≥0.10%；C 低优先观察：常态溢价、资费弱，或盘口／通道待核验。</p>
        <p>资金费和价差是入选依据，不是新的策略栏目。4H 的 0.005% 为比较基准，并非交易所保底；其他周期按日基准 0.03% 折算。价格回撤不自动排除。</p>
        <p>净空间：当前开差－历史平差75分位（不计负平差利好，另留10%余量）－开平双腿费用－滑点－不利资费。默认单次现货0.20%、合约0.10%，费用再留10%余量，滑点0.10%；不是账户费率。正资费按当前与已结算24H较低值折半，只是持有情景。</p>
        <p>同币等数量、假定退出时现货价格不变；首档只按50%数量折算容量，不代表未来能成交。等级是复核优先级，不是收益承诺，不自动下单或新增报警。平差约5秒采样，首档每5分钟留一份同步快照，保留15天，缺失不补造。</p>
      </details>
      <p id="opportunityNotice" class="opportunity-notice" role="status"></p>
      <div id="opportunityScroll" class="opportunity-scroll" tabindex="0" role="region" aria-label="套利思维候选路径，可滚动">
        <div id="opportunityRows"><p class="loading">正在读取候选…</p></div></div>
      <div class="pagination opportunity-pagination"><button id="opportunityPrev" type="button" onclick="ThinkingPool.page(-1)">← 上一页</button><span id="opportunityPage"></span><button id="opportunityNext" type="button" onclick="ThinkingPool.page(1)">下一页 →</button></div>
    </div>`);
    return true;
  }
  function bookMarkup(item){
    const data=state.books.get(key(item));
    if(!data)return '<p>按“读取盘口记录”查看近期同步首档报价和数量。</p>';
    if(data.error)return `<p>${esc(data.error)}</p>`;
    const samples=(data.samples||[]).slice(-12).reverse();
    return `<p>每5分钟一个同步首档快照，最近12份 · 数量为同一标的单位，非全深度／实际成交</p>
      <div class="opportunity-book-scroll"><table><thead><tr><th>北京时间</th><th>现货买一 / 数量</th><th>现货卖一 / 数量</th><th>合约买一 / 数量</th><th>合约卖一 / 数量</th></tr></thead><tbody>
      ${samples.map(s=>`<tr><td>${time(s[0])}</td>${[1,2,3,4].map(i=>`<td>${qty(s[i])} / ${qty(s[i+4])}</td>`).join('')}</tr>`).join('')||'<tr><td colspan="5">尚无有效同步快照，部署后开始积累。</td></tr>'}</tbody></table></div>`;
  }
  function details(item){
    const scenario=item.scenario||{},history=item.history||{},funding=typeof item.funding==='object'&&item.funding||{};
    return `<details class="opportunity-details" data-opportunity-key="${attr(item)}" ${state.opened.has(key(item))?'open':''} ontoggle="ThinkingPool.toggle(this)">
      <summary>依据与风险</summary><div>
      <ul>${[...(item.reasons||[]),...(item.risks||[]),...(item.notes||[])].map(reason=>`<li>${esc(reason)}</li>`).join('')}</ul>
      <div class="opportunity-evidence"><span>首档保守容量 <b>${money(item.capacity_usdt)}</b></span><span>报价 <b>${time(item.quoted_at)} 北京时间</b></span>
      <span>已采开差范围 <b>${pct(history.low)} ～ ${pct(history.high)}</b></span><span>采样起点 <b>${time(history.first_at)}</b></span>
      <span>平差退出参考 <b>${pct(scenario.exit_spread)}</b></span><span>费用 / 滑点预算 <b>${pct(scenario.fee_budget)} / ${pct(scenario.slippage)}</b></span>
      <span>不计正资费净空间 <b>${pct(scenario.spread_net)}</b></span><span>含假设资费净空间 <b>${pct(scenario.funding_net)}</b></span>
      <span>预计资费收入情景 <b>${pct(scenario.funding_income,4)}</b></span><span>已结算覆盖 <b>${funding.complete?'24H覆盖通过':'待积累或存在缺口'}</b></span></div>
      <p>${esc(scenario.assumptions||'历史尚在积累，不能把当前开差直接当作收益。')} 以上是现货名义金额口径，不是账户收益率。</p>
      <button type="button" data-opportunity-key="${attr(item)}" onclick="ThinkingPool.books(this)">读取盘口记录</button>
      <div class="opportunity-books" data-book-key="${attr(item)}">${bookMarkup(item)}</div></div></details>`;
  }
  function spotRow(item){
    const current=!state.error&&qualified(item),grade=item.grade||'C';
    const funding=typeof item.funding==='object'&&item.funding||{};
    const firstReason=(item.reasons||[]).slice(0,2);
    return `<article class="opportunity-row" data-render-key="${attr(item)}">
      <div class="opportunity-main"><div class="opportunity-priority"><b class="opportunity-grade grade-${esc(grade)}">${esc(grade)} · ${esc(item.grade_label||'待评估')}</b>
      <span class="opportunity-state ${current?'is-current':''}" data-live-status="${item.status==='opportunity'?'1':'0'}" data-quoted-at="${Number(item.quoted_at)||0}">${current?'当前有机会':'值得观察'}</span></div>
      <div class="opportunity-path"><button type="button" class="symbol-detail-trigger" data-market-symbol="${esc(item.symbol)}" data-delisting-announced="${item.delisting_announced?'1':'0'}" data-opportunity-key="${attr(item)}" onclick="ThinkingPool.symbol(this)">${esc(item.symbol)}</button>
      <span>${exchangeName(item.long_exchange)}现货 → ${exchangeName('Binance')}合约</span>${transferStatusControl(item.transfer_status||{exchange:item.long_exchange},item.symbol)}</div>
      <div class="opportunity-values"><span>开差 <b class="${color(item.open_spread)}">${pct(item.open_spread)}</b></span><span>平差 <b class="${color(item.close_spread)}">${pct(item.close_spread)}</b></span></div>
      <div class="opportunity-values"><span>本期预计 <b class="${color(item.funding_current)}">${pct(item.funding_current,4)}</b> / ${esc(item.short_interval||'—')}H</span>
      <span>已结算24H <b class="${color(funding.total)}">${pct(funding.total,4)}</b></span></div>
      <div class="opportunity-reasons">${firstReason.map(reason=>`<span>${esc(reason)}</span>`).join('')||'<span>筛选依据待同步</span>'}
      <small>${esc(item.trend||'')} ${item.risks?.length?`· ${esc(item.risks[0])}`:''}</small></div>
      <button type="button" class="opportunity-history" data-opportunity-key="${attr(item)}" onclick="ThinkingPool.history(this)">历史</button></div>${details(item)}</article>`;
  }
  function dualRow(item){
    // Keep the pre-existing dual criteria and measurements; no spot grading.
    return `<article class="opportunity-row opportunity-dual" data-render-key="${attr(item)}"><div><b data-market-symbol="${esc(item.symbol)}">${esc(item.symbol)}</b><p>${exchangeName(item.long_exchange)} 合约多 → ${exchangeName(item.short_exchange)} 合约空</p></div>
      <div class="opportunity-values"><span>开差 <b class="${color(item.open_spread)}">${pct(item.open_spread)}</b></span><span>平差 <b class="${color(item.close_spread)}">${pct(item.close_spread)}</b></span></div>
      <div class="opportunity-values">${[['当前资费差',item.funding_current],['上期',item.funding_previous],['24H',item.funding_24h],['3D',item.funding_3d]].map(([label,value])=>`<span>${label} <b class="${color(value)}">${pct(value,4)}</b></span>`).join('')}</div>
      <div class="opportunity-values"><span>多：${esc(item.long_interval||'—')}H · 量 ${liquidityAmount(item.long_volume)} · OI ${liquidityAmount(item.long_open_interest)}</span><span>空：${esc(item.short_interval||'—')}H · 量 ${liquidityAmount(item.short_volume)} · OI ${liquidityAmount(item.short_open_interest)}</span></div></article>`;
  }
  function render(force=false){
    if(!init())return;
    document.querySelectorAll('[data-thinking-strategy]').forEach(button=>{const active=button.dataset.thinkingStrategy===state.strategy;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active))});
    document.querySelectorAll('.opportunity-spot-control').forEach(node=>node.hidden=state.strategy!=='spot');
    document.querySelectorAll('.opportunity-dual-control').forEach(node=>node.hidden=state.strategy!=='dual');
    $('opportunityDualDirection').textContent=state.dual_direction==='desc'?'从高到低 ↓':'从低到高 ↑';
    $('opportunitySettingsLabel').textContent=`· ${money(state.notional)} / ${state.hold_hours/24}天`;
    if(!state.data){$('opportunityNotice').textContent=state.error||'正在读取候选…';return}
    let items=state.strategy==='spot'?select(state.data.spot_simple,state,state.error?Infinity:Date.now()/1000):
      dualSorted((state.data.dual_simple||[]).filter(item=>!state.search||String(item.symbol).toUpperCase().includes(state.search.toUpperCase())),state.dual_sort,state.dual_direction);
    const scroll=$('opportunityScroll'),preserve=!force&&(scroll.matches(':hover')||scroll.contains(document.activeElement)||scroll.scrollTop>8||state.opened.size>0);
    items=ordered(items,state.order,preserve);state.order=items.map(key);
    const pages=Math.max(1,Math.ceil(items.length/30));state.page=Math.min(state.page,pages);
    const rows=items.slice((state.page-1)*30,state.page*30),scrollTop=scroll.scrollTop;
    replaceHtmlKeepingExchangeLogos($('opportunityRows'),rows.map(state.strategy==='spot'?spotRow:dualRow).join('')||'<p class="empty-alert">当前筛选下没有候选路径。</p>');
    scroll.scrollTop=force?0:scrollTop;
    $('opportunityNotice').textContent=state.error?`${state.error}；保留上次记录，当前机会状态已撤下。`:
      state.strategy==='spot'?`${new Set(items.map(i=>i.symbol)).size} 个币种 · ${items.length} 条路径 · 按等级优先 · 行情自动更新，浏览时顺序不跳动 · ${time(state.data.updated_at_epoch)} 北京时间`:
      '期多期空沿用原有筛选与展示；本轮未套用现多期空的分级规则。';
    $('opportunityPage').textContent=`第 ${state.page} / ${pages} 页`;
    $('opportunityPrev').disabled=state.page<=1;$('opportunityNext').disabled=state.page>=pages;
  }
  async function load(refresh=true){
    if(!init())return null;
    if(!refresh&&state.data){render();return state.data}
    if(state.controller)return state.data;
    const controller=new AbortController(),version=++state.version;state.controller=controller;
    const timeout=setTimeout(()=>controller.abort(),12000);
    try{
      const response=await fetch(`/api/arbitrage-thinking/simple?${new URLSearchParams({strategy:state.strategy,notional:state.notional,hold_hours:state.hold_hours})}`,{signal:controller.signal,cache:'no-store'});
      if(!response.ok)throw new Error(response.status===401?'登录已失效，请重新登录':'候选读取失败');
      const data=await response.json();
      if(version!==state.version)return state.data;
      state.data=data;state.error='';render();return data;
    }catch(error){
      if(version!==state.version)return state.data;
      state.error=error.name==='AbortError'?'读取超时':error.message;render();return state.data;
    }finally{clearTimeout(timeout);if(version===state.version)state.controller=null}
  }
  function resetRequest(){
    ++state.version;state.controller?.abort();state.controller=null;state.error='';state.data=null;state.order=[];state.page=1;
  }
  function itemFrom(button){
    const k=decodeURIComponent(button.dataset.opportunityKey);
    return (state.data?.spot_simple||[]).find(item=>key(item)===k);
  }
  const api={
    load,select,ordered,qualified,spotRow,dualRow,dualSorted,pct,
    strategy(value){if(!['spot','dual'].includes(value)||value===state.strategy)return;resetRequest();state.strategy=value;state.opened.clear();state.books.clear();$('opportunityRows').innerHTML='<p class="loading">正在读取候选…</p>';render();loadSimpleThinking()},
    filter(name,value){if(!['grade','status','search','dual_sort'].includes(name))return;state[name]=value;state.page=1;render(true)},
    direction(){state.dual_direction=state.dual_direction==='desc'?'asc':'desc';state.page=1;render(true)},
    page(delta){state.page=Math.max(1,state.page+delta);render(true)},
    reorder(){render(true)},
    toggle(node){const k=decodeURIComponent(node.dataset.opportunityKey);if(node.open)state.opened.add(k);else state.opened.delete(k)},
    configure(event){event.preventDefault();const amount=Number($('opportunityNotional').value),hold=Number($('opportunityHold').value);if(!Number.isFinite(amount)||amount<100||amount>1000000||![24,72,168].includes(hold))return;resetRequest();state.notional=amount;state.hold_hours=hold;$('opportunityRows').innerHTML='<p class="loading">正在按新金额与持有期测算…</p>';render();loadSimpleThinking()},
    symbol(button){const item=itemFrom(button);if(item)openSymbolDetail(item.symbol)},
    history(button){const item=itemFrom(button);if(item)openSpreadHistory(encodeURIComponent(JSON.stringify([item.symbol,item.long_exchange])),'open',button)},
    async books(button){
      const item=itemFrom(button);if(!item||state.bookPending.has(key(item)))return;
      const k=key(item),controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),12000);
      state.bookPending.add(k);button.disabled=true;
      try{
        const response=await fetch(`/api/arbitrage-thinking/book-history?${new URLSearchParams({symbol:item.symbol,exchange:item.long_exchange,period:'24h'})}`,{signal:controller.signal,cache:'no-store'});
        if(!response.ok)throw new Error('盘口记录读取失败，可重试');
        state.books.set(k,await response.json());
      }catch(error){state.books.set(k,{error:error.name==='AbortError'?'读取超时，可重试':error.message})}
      finally{
        clearTimeout(timeout);state.bookPending.delete(k);button.disabled=false;
        if(state.books.size>30)state.books.delete(state.books.keys().next().value);
        document.querySelectorAll('[data-book-key]').forEach(node=>{if(decodeURIComponent(node.dataset.bookKey)===k)node.innerHTML=bookMarkup(item)});
      }
    },
    expire(){
      document.querySelectorAll('[data-live-status="1"]').forEach(node=>{
        if(state.error||!qualified({status:'opportunity',quoted_at:Number(node.dataset.quotedAt)})){node.textContent='值得观察';node.classList.remove('is-current')}
      });
    },
  };
  globalThis.ThinkingPool=api;
  if(typeof document!=='undefined')setInterval(api.expire,2000);
})();
