/* Path-local research with personal bookmarks; never orders or notifications. */
(() => {
  'use strict';
  const state={strategy:'spot',scope:'all',min_score:'all',status:'all',period:'7d',search:'',page:1,notional:1000,hold_hours:72,
    dual_sort:'open_spread',dual_direction:'desc',
    data:null,error:'',controller:null,version:0,order:[],books:new Map(),bookPending:new Set(),
    watchPaths:[],watchPending:new Set(),auxVersion:0,reviewQuery:{},reviewController:null};
  const $=id=>document.getElementById(id);
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const pct=(value,digits=2)=>Number.isFinite(value)?`${value>0?'+':''}${value.toFixed(digits)}%`:'—';
  const qty=value=>Number.isFinite(value)?value.toLocaleString('en-US',{maximumFractionDigits:8}):'—';
  const money=value=>Number.isFinite(value)?`${value.toLocaleString('en-US',{maximumFractionDigits:0})} USDT`:'—';
  const time=value=>Number.isFinite(value)?new Date(value*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}):'—';
  const key=item=>JSON.stringify([item.symbol,item.long_exchange,item.short_exchange]);
  const attr=item=>esc(encodeURIComponent(key(item)));
  const color=value=>Number.isFinite(value)?value>0?'positive':value<0?'negative':'neutral':'neutral';
  const watchKey=(item,mode)=>JSON.stringify([mode,item.symbol,item.long_exchange,item.short_exchange]);
  function watchButton(item,mode){
    const k=watchKey(item,mode),saved=state.watchPaths.some(path=>JSON.stringify(path)===k);
    return `<button type="button" class="opportunity-watch ${saved?'saved':''}" data-opportunity-key="${attr(item)}" data-watch-strategy="${mode}" onclick="ThinkingPool.watch(this)" aria-pressed="${saved}" title="${saved?'取消':'添加'}本策略、本交易路径的个人关注；不会开启报警或Lark" aria-label="${saved?'取消关注':'关注'} ${esc(item.symbol)} ${esc(item.long_exchange)} → ${esc(item.short_exchange)}" ${state.watchPending.has(k)?'disabled':''}>${saved?'★':'☆'}</button>`;
  }
  function qualityReady(item,now=Date.now()/1000){
    const at=item.funding_quality?.as_of;
    return Number.isFinite(at)&&now>=at&&now-at<=360;
  }
  function qualitySummary(item,now=Date.now()/1000){
    const data=item.funding_quality,ready=qualityReady(item,now);
    const part=key=>{const w=data?.windows?.[key];return w?.classified?`${w.above}/${w.classified}`:'—'};
    return `<button type="button" class="opportunity-quality" data-opportunity-key="${attr(item)}" onclick="ThinkingPool.fundingQuality(this)" title="高于周期基准的已结算期数／可判定期数；点击查看负费率、覆盖和连续性">${ready?`超基准 3D ${part('3d')} · 7D ${part('7d')}`:data?'资费质量待更新':'资费质量采集中'} ›</button>`;
  }
  function qualityMarkup(item,now=Date.now()/1000){
    const data=item.funding_quality;
    if(!data)return '<p>正在整理实际结算记录，不将缺失值当作0或低保。</p>';
    return `<p>统计截至 ${time(data.as_of)} 北京时间${qualityReady(item,now)?'':' · 汇总已过期，以下仅供历史参考'}</p>
      <div class="opportunity-book-scroll"><table class="opportunity-evidence-table"><thead><tr><th>周期</th><th>已采累计</th><th>超基准 / 可判定</th><th>负费期</th><th>最近连正</th><th>覆盖</th></tr></thead><tbody>${[['24h','1天'],['3d','3天'],['7d','7天']].map(([key,label])=>{
        const w=data.windows?.[key];if(!w)return `<tr><th>${label}</th><td colspan="5">未采集</td></tr>`;
        return `<tr><th>${label}</th><td class="${color(w.total)}">${pct(w.total,4)}</td><td>${Number(w.above)||0} / ${Number(w.classified)||0} 期${Number.isFinite(w.above_ratio)?` · ${(w.above_ratio*100).toFixed(0)}%`:''}</td><td>${Number(w.negative)||0} / ${Number(w.count)||0} 期</td><td>${Number(w.recent_positive_streak)||0} 期</td><td>${w.complete?'推断覆盖齐备':'覆盖不足'}${w.unknown_count?` · ${Number(w.unknown_count)}期周期不明`:''}</td></tr>`;
      }).join('')}</tbody></table></div>
      <p>只统计已结算记录，保留负值；超基准比例的分母是周期可判定的期数，不把缺口当低保。基准按每日0.03%折算（4H为0.005%），不是保底。</p>
      <p>历史周期来自相邻结算间隔推断，变长或缺采会保守标为不明；“覆盖齐备”不是交易所全量证明。最近连正遇负值、零值或周期不明即中断。当前评分权重不因这项新展示自动改变。</p>`;
  }
  const reviewLabels={3600:'1小时',86400:'1天',259200:'3天',604800:'7天'};
  function reviewMarkup(data){
    const count=v=>Number.isFinite(v)?String(v):'—';
    const rows=(data.cohorts||[]).map(c=>`<details class="opportunity-review-record"><summary><b>${esc(c.symbol)}</b> · ${esc(c.long_exchange)} → ${esc(c.short_exchange)} · 当时 ${count(c.score)}分 · ${time(c.entered_at)}</summary>
      <p>v${count(c.model)} · ${money(c.entry?.notional)} · 入选时现货卖一 ${qty(c.entry?.spot_ask)} / 合约买一 ${qty(c.entry?.future_bid)}；评分参数持有${count(c.entry?.score_hold_hours/24)}天，冻结不回改。</p>
      <div class="opportunity-book-scroll"><table class="opportunity-evidence-table"><thead><tr><th>之后</th><th>价差净测算</th><th>覆盖</th><th>已见净值范围</th><th>资费率累计（独立）</th></tr></thead><tbody>${Object.entries(reviewLabels).map(([h,label])=>{
        const r=c.outcomes?.[h],pending=(data.as_of<c.entered_at+Number(h))?'待到期':'待采集核验';
        return `<tr><th>${label}</th><td>${r?.status==='observed'?`<b class="${color(r.net)}">${pct(r.net)}</b>`:esc(r?.reason||pending)}</td><td>${r?`${(Number(r.coverage)*100).toFixed(0)}%`:'—'}</td><td>${r?`${pct(r.low)} ～ ${pct(r.high)}`:'—'}</td><td>${r?.funding?`${pct(r.funding.total,4)} · ${Number(r.funding.count)||0}期${r.funding.complete?'':' · 部分记录'}`:'—'}</td></tr>`;
      }).join('')}</tbody></table></div><p>范围仅为有效首档已见值，可能漏见瞬间极值；到期覆盖不足不计为有效样本。${(c.entry?.risks||[]).map(esc).join('；')}</p></details>`).join('');
    return `<div class="opportunity-review-controls"><label>验证周期 <select onchange="ThinkingPool.reviewPeriod(this.value)">${Object.entries(reviewLabels).map(([h,label])=>`<option value="${h}" ${Number(h)===data.horizon?'selected':''}>${label}</option>`).join('')}</select></label><button type="button" onclick="ThinkingPool.reviewPeriod()">刷新记录</button></div>
      <p>近90天 · 冻结评分v${count(data.model)} · 标准${money(data.notional)} / ${count(data.score_hold_hours/24)}天评分参数；与当前个人测算设置独立。</p>
      <p>样本 ${count(data.total)} · 待到期 ${count(data.pending)} · 有效观测 ${count(data.observed)} · 数据不足 ${count(data.insufficient)} · 待采集核验 ${count(data.awaiting_collection)}</p>
      <div class="opportunity-book-scroll"><table class="opportunity-evidence-table"><thead><tr><th>当时评分</th><th>样本</th><th>有效观测</th><th>净价差为正</th><th>净价差中位数</th></tr></thead><tbody>${(data.bands||[]).map(b=>`<tr><th>${esc(b.label)}</th><td>${count(b.total)}</td><td>${count(b.observed)}</td><td>${count(b.positive)} / ${count(b.observed)}</td><td class="${color(b.median_net)}">${pct(b.median_net)}</td></tr>`).join('')}</tbody></table></div>
      <p>这是首档价格与冻结成本下的事后情景，不是成交收益或胜率。等数量双腿按实际后续价格测算；资费率累计独立显示，未按结算标记价换算成真实收入。观察期未结束、覆盖不足和配对变更都不算赢或输。</p>
      <p>样本仅纳入入选时同步首档容量足够的路径，同路径7天一轮，包含低分；每120秒检查新入选，不能覆盖瞬时出现的全部候选。最早1小时才有结果，评分不会自动改写。${data.capacity_limited?'存储/路径上限已到，新样本暂停，旧样本保留。':''}</p>
      ${rows||'<p>尚无已冻结样本，后台将在有效同步报价出现后自动记录，不补造历史评分。</p>'}`;
  }
  function qualified(item,now=Date.now()/1000){
    return item.status==='opportunity'&&Number.isFinite(item.quoted_at)&&now-item.quoted_at<=10&&now>=item.quoted_at-2;
  }
  function displayScore(item,now=Date.now()/1000){
    if(!Number.isFinite(item.score))return null;
    const stale=Number.isFinite(item.quoted_at)&&(now-item.quoted_at>10||now<item.quoted_at-2);
    return Math.max(0,Math.round(item.score-(stale?(item.score_parts?.current||0)+15:0)));
  }
  function scenarioReady(item,now=Date.now()/1000){
    return item.scenario?.calculable===true&&Number.isFinite(item.quoted_at)&&
      now-item.quoted_at<=10&&now>=item.quoted_at-2;
  }
  function scenarioMarkup(item,now=Date.now()/1000){
    const s=item.scenario||{},ready=scenarioReady(item,now);
    const value=v=>ready?pct(v):'—';
    return `<p>${money(s.notional)} 现货参考金额 · 持有 ${Number.isFinite(s.hold_hours)?esc(s.hold_hours/24):'—'} 天 · 报价 ${time(item.quoted_at)} 北京时间</p>
      <div class="opportunity-scenario-totals"><div>净价差情景<b class="${ready?color(s.spread_net):'neutral'}">${value(s.spread_net)}</b><small>不计正资费收入</small></div><div>含资费情景<b class="${ready?color(s.funding_net):'neutral'}">${value(s.funding_net)}</b><small>未来资费不保证延续</small></div></div>
      <dl class="opportunity-calculation">${[
        ['当前开差',pct(item.open_spread)],
        ['退出平差预算',Number.isFinite(s.exit_spread)?pct(Math.max(0,s.exit_spread)*1.1):'—'],
        ['四笔交易费用预算',pct(s.fee_budget)],['滑点预算',pct(s.slippage)],
        ['不利资费预算',pct(s.adverse_funding)],['正资费收入情景',pct(s.funding_income)],
        ['首档参考容量',money(item.capacity_usdt)],
      ].map(([label,v])=>`<div><dt>${label}</dt><dd>${v}</dd></div>`).join('')}</dl>
      ${!ready?'<p class="opportunity-calculation-warning">当前盘口、数量、参考金额或资费／平差数据未满足测算条件，不显示可用净空间。</p>':''}
      ${(item.risks||[]).length?`<p class="opportunity-calculation-warning">${(item.risks||[]).map(esc).join('；')}</p>`:''}
      <p>容量按双腿四个首档最小数量的50%折算，不是全盘口深度，不保证成交。超出此金额不外推首档价格。</p>
      <p>${esc(s.assumptions||'费用为预算，并非账户费率；退出价格和资费为假设，不是收益保证。')}</p>`;
  }
  function select(items,filters,now=Date.now()/1000){
    return (items||[]).filter(item=>
      (filters.min_score==='all'||!filters.min_score||displayScore(item,now)>=Number(filters.min_score))&&
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
      <label class="opportunity-spot-control">评分 <select id="opportunityScore" onchange="ThinkingPool.filter('min_score',this.value)"><option value="all">全部分数</option><option value="80">80分及以上</option><option value="60">60分及以上</option><option value="40">40分及以上</option></select></label>
      <label class="opportunity-spot-control">价差周期 <select id="opportunityPeriod" onchange="ThinkingPool.period(this.value)"><option value="24h">1天</option><option value="3d">3天</option><option value="7d" selected>7天</option><option value="15d">15天</option></select></label>
      <label class="opportunity-dual-control" hidden>排序 <select aria-label="期多期空候选排序" onchange="ThinkingPool.filter('dual_sort',this.value)">${[['open_spread','开差'],['close_spread','平差'],['funding_current','当前资费'],['funding_previous','上期资费'],['funding_24h','24H资费'],['funding_3d','3D资费']].map(([value,label])=>`<option value="${value}">${label}</option>`).join('')}</select><button type="button" id="opportunityDualDirection" onclick="ThinkingPool.direction()">↓</button></label>
      <button type="button" onclick="ThinkingPool.reorder()">重新排序</button></div>
      <div class="opportunity-personal-controls"><label><input id="opportunityWatchOnly" type="checkbox" onchange="ThinkingPool.scope(this.checked)">我的关注</label><button class="opportunity-spot-control" type="button" onclick="ThinkingPool.review()">评分验证</button><span>关注仅保存本账号的策略与路径，不开启提醒</span></div>
      <details class="opportunity-method opportunity-spot-control"><summary>评分与测算设置 <span id="opportunitySettingsLabel"></span></summary>
        <form onsubmit="ThinkingPool.configure(event)"><label>现货参考金额（USDT）<input id="opportunityNotional" type="number" min="100" max="1000000" step="1" value="1000" required></label>
        <label>测算持有期<select id="opportunityHold"><option value="24">1天</option><option value="72" selected>3天</option><option value="168">7天</option></select></label><button type="submit">重新测算</button></form>
        <p>0–100分是人工复核优先级，不是胜率。参考最长15天价差与90天已见打开记录，资费／价差取较强依据、另一项加分，再考虑当前净空间和数据风险。5天前打开不会因超过24小时而被剔除；切换展示周期不改变评分。</p>
        <p>打开次数：有效观测达到正1%／负1%后计一段，收回正0.5%／负0.5%以内再重新计算。同段只算一次；缺口不视为收回，旧分钟极值不补造次数。“已见”是已采记录，不是市场完整次数，也不等于实际盈利次数。</p>
        <p>资金费和价差是入选依据，不是新的策略栏目。4H 的 0.005% 为比较基准，并非交易所保底；其他周期按日基准 0.03% 折算。价格回撤不自动排除。</p>
        <p>净空间：当前开差－历史平差75分位（不计负平差利好，另留10%余量）－开平双腿费用－滑点－不利资费。默认单次现货0.20%、合约0.10%，费用再留10%余量，滑点0.10%；不是账户费率。正资费按当前与已结算24H较低值折半，只是持有情景。</p>
        <p>同币等数量、假定退出时现货价格不变；首档只按50%数量折算容量，不代表未来能成交。不自动下单或新增报警。平差约5秒采样，首档每5分钟留一份同步快照，曲线保留15天，缺失不补造。</p>
      </details>
      <p id="opportunityNotice" class="opportunity-notice" role="status"></p>
      <div id="opportunityScroll" class="opportunity-scroll" tabindex="0" role="region" aria-label="套利思维候选路径，可滚动">
        <div id="opportunityRows"><p class="loading">正在读取候选…</p></div></div>
      <div class="pagination opportunity-pagination"><button id="opportunityPrev" type="button" onclick="ThinkingPool.page(-1)">← 上一页</button><span id="opportunityPage"></span><button id="opportunityNext" type="button" onclick="ThinkingPool.page(1)">下一页 →</button></div>
      <dialog id="opportunityAux" class="opportunity-aux"><button type="button" class="opportunity-aux-close" onclick="ThinkingPool.closeAux()" aria-label="关闭详情">×</button><h3 id="opportunityAuxTitle"></h3><div id="opportunityAuxBody"></div></dialog>
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
  const periodLabels={'24h':'1天','3d':'3天','7d':'7天','15d':'15天'};
  function episodeMarkup(item,period=state.period){
    const activity=item.activity||{},recent=activity.windows?.[period]?.recent||[];
    return `<div class="opportunity-event-list"><b>最近独立打开 · 北京时间</b>${recent.map(e=>`<div><span>${time(e.started)}</span><strong class="${color(e.peak)}">${pct(e.peak)}</strong></div>`).join('')||'<p>尚未采到该周期的独立打开记录。</p>'}
      <p>最多展示最近5段；值为该段已见极值。采集起点：${time(activity.since)}。断档及旧历史不补造次数。</p></div>`;
  }
  function spreadEvidence(item,period=state.period){
    const range=item.history?.windows?.[period],activity=item.activity||{},events=activity.windows?.[period];
    const recent=events?.recent||[];
    const peak=range?(Math.abs(range.high)>=Math.abs(range.low)?range.high:range.low):null;
    const coverage=range?`已采 ${((range.last_at-range.first_at)/86400).toFixed(1)} 天跨度；有效分钟覆盖 ${(range.coverage*100).toFixed(0)}%。范围仅包含实际采样。`:'尚无该周期有效历史';
    const last=recent[0];
    return `<div class="opportunity-spread">
      <span class="opportunity-data-title" title="${esc(coverage)}">${periodLabels[period]}开差${range&&range.coverage<.9?'<small>· 历史未满</small>':''}</span>
      <div class="opportunity-range"><b class="${color(range?.high)}">${pct(range?.high)}</b><span>～</span><b class="${color(range?.low)}">${pct(range?.low)}</b></div>
      <button type="button" class="opportunity-events" data-opportunity-key="${attr(item)}" onclick="ThinkingPool.events(this)" title="独立打开：正≥1%／负≤-1%，回到±0.5%内再计下一次。点击查看已见记录，不代表完整市场历史。">${Number.isFinite(activity.since)?`已见 <span class="positive">正 ${events?.positive||0} 次</span><span class="negative">负 ${events?.negative||0} 次</span>`:'打开次数采集中'} <span>›</span></button>
      <span class="opportunity-latest">${last?`最近打开 <b class="${color(last.peak)}">${pct(last.peak)}</b> <time title="${time(last.peak_at)} 北京时间">${time(last.peak_at).slice(0,-3)}</time>`:
        range?`已采峰值 <b class="${color(peak)}">${pct(peak)}</b> <time title="${time(range.peak_at)} 北京时间">${time(range.peak_at).slice(0,-3)}</time>`:'—'}</span>
    </div>`;
  }
  function spotRow(item){
    const parts=item.score_parts||{};
    const scoreTitle=`复核优先级，不是胜率。价差 ${parts.spread??'—'}；资费 ${parts.funding??'—'}；当前净空间加分 ${parts.current??'—'}；风险扣分 ${parts.deductions??'—'}。${(item.risks||[]).join('；')}`;
    return `<article class="opportunity-row" data-render-key="${attr(item)}">
      <div class="opportunity-main"><button type="button" class="opportunity-score" title="${esc(scoreTitle)} 点击查看该路径评分后续验证" data-opportunity-key="${attr(item)}" onclick="ThinkingPool.review(this)" aria-label="${esc(displayScore(item))}分，查看评分验证"><b data-score-base="${Number.isFinite(item.score)?item.score:''}" data-score-current="${Number(parts.current)||0}" data-quoted-at="${Number.isFinite(item.quoted_at)?item.quoted_at:''}">${displayScore(item)??'—'}</b><span>评分</span></button>
      <div class="opportunity-path"><div class="opportunity-symbol-line">${watchButton(item,'spot')}<button type="button" class="symbol-detail-trigger" data-market-symbol="${esc(item.symbol)}" data-delisting-announced="${item.delisting_announced?'1':'0'}" data-opportunity-key="${attr(item)}" onclick="ThinkingPool.symbol(this)">${esc(item.symbol)}</button></div>
      <span>${exchangeName(item.long_exchange)}现货 → ${exchangeName('Binance')}合约 <b class="opportunity-interval" title="合约资金费结算周期">${Number.isFinite(item.short_interval)?`${esc(item.short_interval)}H`:'—'}</b></span>${transferStatusControl(item.transfer_status||{exchange:item.long_exchange},item.symbol)}${item.watch_state==='unavailable'?'<small>路径暂无有效行情或已隔离</small>':item.watch_state==='outside'?'<small>当前未入选，保留关注</small>':''}</div>
      <div class="opportunity-values"><span>开差 <b class="${color(item.open_spread)}">${pct(item.open_spread)}</b></span><span>平差 <b class="${color(item.close_spread)}">${pct(item.close_spread)}</b></span><button type="button" class="opportunity-net" data-opportunity-key="${attr(item)}" onclick="ThinkingPool.calculation(this)" title="扣除退出平差及成本预算的净价差情景，不计正资费；点击查看容量与明细">净价差 <b class="${scenarioReady(item)?color(item.scenario?.spread_net):'neutral'}" data-scenario-value="${scenarioReady(item)&&Number.isFinite(item.scenario?.spread_net)?item.scenario.spread_net:''}" data-scenario-at="${Number.isFinite(item.quoted_at)?item.quoted_at:''}">${scenarioReady(item)?pct(item.scenario?.spread_net):'—'}</b> ›</button></div>
      <div class="opportunity-funding" role="group" aria-label="币安资费详情">
        <span class="opportunity-funding-title">资费</span>
        <div class="opportunity-funding-grid">${[
          ['当前','funding_current','本期预计资金费率，最终以实际结算为准'],
          ['上期','funding_previous','最近一期已结算资金费率'],
          ['24H','funding_24h','最近24小时已结算滚动累计'],
          ['3D','funding_3d','最近3天已结算滚动累计'],
          ['7D','funding_7d','最近7天已结算滚动累计'],
          ['30D','funding_30d','最近30天已结算滚动累计'],
        ].map(([label,field,description])=>`<span class="opportunity-funding-item" title="${description}" data-funding-field="${field}"><span>${label}</span><b class="${color(item[field])}">${pct(item[field],4)}</b></span>`).join('')}</div>${qualitySummary(item)}
      </div>
      ${spreadEvidence(item)}
      <div class="opportunity-actions"><button type="button" class="opportunity-history" data-opportunity-key="${attr(item)}" onclick="ThinkingPool.history(this)">曲线</button><button type="button" class="opportunity-history" data-opportunity-key="${attr(item)}" onclick="ThinkingPool.books(this)">盘口</button></div></div></article>`;
  }
  function dualRow(item){
    // Keep the pre-existing dual criteria and measurements; no spot grading.
    return `<article class="opportunity-row opportunity-dual" data-render-key="${attr(item)}"><div><div class="opportunity-symbol-line">${watchButton(item,'dual')}<b data-market-symbol="${esc(item.symbol)}">${esc(item.symbol)}</b></div><p>${exchangeName(item.long_exchange)} 合约多 → ${exchangeName(item.short_exchange)} 合约空</p>${item.watch_state==='unavailable'?'<small>路径暂无有效行情或已隔离</small>':item.watch_state==='outside'?'<small>当前未入选，保留关注</small>':''}</div>
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
    const scroll=$('opportunityScroll'),preserve=!force&&(scroll.matches(':hover')||scroll.contains(document.activeElement)||scroll.scrollTop>8||$('opportunityAux')?.open);
    items=ordered(items,state.order,preserve);state.order=items.map(key);
    const pages=Math.max(1,Math.ceil(items.length/30));state.page=Math.min(state.page,pages);
    const rows=items.slice((state.page-1)*30,state.page*30),scrollTop=scroll.scrollTop;
    replaceHtmlKeepingExchangeLogos($('opportunityRows'),rows.map(state.strategy==='spot'?spotRow:dualRow).join('')||'<p class="empty-alert">当前筛选下没有候选路径。</p>');
    scroll.scrollTop=force?0:scrollTop;
    $('opportunityNotice').textContent=state.error?`${state.error}；保留上次记录，当前机会状态已撤下。`:
      state.strategy==='spot'?`${new Set(items.map(i=>i.symbol)).size} 个币种 · ${items.length} 条路径 · 评分从高到低 · ${time(state.data.updated_at_epoch)} 北京时间`:
      '期多期空沿用原有筛选与展示；本轮未套用现多期空的评分规则。';
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
      const response=await fetch(`/api/arbitrage-thinking/simple?${new URLSearchParams({strategy:state.strategy,scope:state.scope,notional:state.notional,hold_hours:state.hold_hours})}`,{signal:controller.signal,cache:'no-store'});
      if(!response.ok)throw new Error(response.status===401?'登录已失效，请重新登录':'候选读取失败');
      const data=await response.json();
      if(version!==state.version)return state.data;
      state.data=data;state.watchPaths=data.watch_paths||[];state.error='';render();return data;
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
    return [...(state.data?.spot_simple||[]),...(state.data?.dual_simple||[])].find(item=>key(item)===k);
  }
  const api={
    load,select,ordered,qualified,displayScore,scenarioReady,scenarioMarkup,qualityReady,qualitySummary,qualityMarkup,reviewMarkup,watchKey,spotRow,spreadEvidence,dualRow,dualSorted,pct,
    scope(checked){resetRequest();state.scope=checked?'watched':'all';state.min_score='all';$('opportunityScore').value='all';render();loadSimpleThinking()},
    async watch(button){
      const item=itemFrom(button),mode=button.dataset.watchStrategy;if(!item||!['spot','dual'].includes(mode))return;
      const k=watchKey(item,mode);if(state.watchPending.has(k))return;
      const watched=!state.watchPaths.some(path=>JSON.stringify(path)===k);
      state.watchPending.add(k);button.disabled=true;
      const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),10000);
      try{
        const token=typeof authState!=='undefined'?authState.csrf_token:'';
        if(!token)throw new Error('登录校验尚未完成，请稍后重试');
        const response=await fetch('/api/arbitrage-thinking/watchlist',{method:'PUT',signal:controller.signal,cache:'no-store',headers:{'Content-Type':'application/json','X-CSRF-Token':token},body:JSON.stringify({strategy:mode,symbol:item.symbol,long_exchange:item.long_exchange,short_exchange:item.short_exchange,watched})});
        const data=await response.json();if(!response.ok)throw new Error(data.error||'关注保存失败');
        ++state.version;state.controller?.abort();state.controller=null;
        state.watchPaths=data.paths||[];
        if(state.data&&state.scope==='watched'&&!watched){const field=mode==='spot'?'spot_simple':'dual_simple';state.data[field]=(state.data[field]||[]).filter(row=>watchKey(row,mode)!==k)}
        render();loadSimpleThinking();
      }catch(error){$('opportunityNotice').textContent=error.name==='AbortError'?'关注保存超时，请重试确认状态':error.message}
      finally{clearTimeout(timeout);state.watchPending.delete(k);button.disabled=false;document.querySelectorAll('[data-watch-strategy]').forEach(node=>{if(node.dataset.opportunityKey===button.dataset.opportunityKey&&node.dataset.watchStrategy===mode)node.disabled=false})}
    },
    strategy(value){if(!['spot','dual'].includes(value)||value===state.strategy)return;resetRequest();state.strategy=value;state.books.clear();$('opportunityRows').innerHTML='<p class="loading">正在读取候选…</p>';render();loadSimpleThinking()},
    filter(name,value){if(!['min_score','status','search','dual_sort'].includes(name))return;state[name]=value;state.page=1;render(true)},
    period(value){if(!Object.hasOwn(periodLabels,value))return;state.period=value;render()},
    direction(){state.dual_direction=state.dual_direction==='desc'?'asc':'desc';state.page=1;render(true)},
    page(delta){state.page=Math.max(1,state.page+delta);render(true)},
    reorder(){render(true)},
    configure(event){event.preventDefault();const amount=Number($('opportunityNotional').value),hold=Number($('opportunityHold').value);if(!Number.isFinite(amount)||amount<100||amount>1000000||![24,72,168].includes(hold))return;resetRequest();state.notional=amount;state.hold_hours=hold;$('opportunityRows').innerHTML='<p class="loading">正在按新金额与持有期测算…</p>';render();loadSimpleThinking()},
    symbol(button){const item=itemFrom(button);if(item)openSymbolDetail(item.symbol)},
    history(button){const item=itemFrom(button);if(item)openSpreadHistory(encodeURIComponent(JSON.stringify([item.symbol,item.long_exchange])),'open',button)},
    calculation(button){
      const item=itemFrom(button);if(!item)return;
      $('opportunityAuxTitle').textContent=`${item.symbol} · ${item.long_exchange} → Binance · 净空间测算`;
      $('opportunityAuxBody').innerHTML=`<div data-calculation-key="${attr(item)}">${scenarioMarkup(item)}</div>`;
      if(!$('opportunityAux').open)$('opportunityAux').showModal();
    },
    fundingQuality(button){const item=itemFrom(button);if(!item)return;$('opportunityAuxTitle').textContent=`${item.symbol} · Binance合约 · 资费质量`;$('opportunityAuxBody').innerHTML=qualityMarkup(item);if(!$('opportunityAux').open)$('opportunityAux').showModal()},
    async review(button){
      const item=button?itemFrom(button):null;if(button&&!item)return;
      state.reviewQuery=item?{symbol:item.symbol,exchange:item.long_exchange,horizon:86400}:{horizon:86400};
      return api.reviewPeriod();
    },
    async reviewPeriod(horizon){
      if(horizon!==undefined){if(!Object.hasOwn(reviewLabels,horizon))return;state.reviewQuery.horizon=Number(horizon)}
      const query={...state.reviewQuery},version=++state.auxVersion;
      state.reviewController?.abort();const controller=new AbortController();state.reviewController=controller;
      $('opportunityAuxTitle').textContent=query.symbol?`${query.symbol} · ${query.exchange} → Binance · 评分验证`:'评分后续验证 · 现多期空';
      $('opportunityAuxBody').innerHTML=`<div data-review-token="${version}">正在读取冻结样本…</div>`;
      if(!$('opportunityAux').open)$('opportunityAux').showModal();
      const timeout=setTimeout(()=>controller.abort(),12000);
      try{
        const response=await fetch(`/api/arbitrage-thinking/validation?${new URLSearchParams(query)}`,{signal:controller.signal,cache:'no-store'});
        const data=await response.json();if(!response.ok)throw new Error(data.error||'验证读取失败');
        if(version!==state.auxVersion)return;
        document.querySelectorAll('[data-review-token]').forEach(node=>{if(node.dataset.reviewToken===String(version))node.innerHTML=reviewMarkup(data)});
      }catch(error){if(version===state.auxVersion)document.querySelectorAll('[data-review-token]').forEach(node=>{if(node.dataset.reviewToken===String(version))node.textContent=error.name==='AbortError'?'读取超时，可关闭后重试':error.message})}
      finally{clearTimeout(timeout);if(version===state.auxVersion)state.reviewController=null}
    },
    closeAux(){++state.auxVersion;state.reviewController?.abort();$('opportunityAux')?.close()},
    events(button){
      const item=itemFrom(button);if(!item)return;
      $('opportunityAuxTitle').textContent=`${item.symbol} · ${item.long_exchange} → Binance · ${periodLabels[state.period]}打开记录`;
      $('opportunityAuxBody').innerHTML=episodeMarkup(item);
      if(!$('opportunityAux').open)$('opportunityAux').showModal();
    },
    async books(button){
      const item=itemFrom(button);if(!item||state.bookPending.has(key(item)))return;
      $('opportunityAuxTitle').textContent=`${item.symbol} · ${item.long_exchange} → Binance · 盘口记录`;
      $('opportunityAuxBody').innerHTML=`<div data-book-key="${attr(item)}">正在读取同步首档…</div>`;
      if(!$('opportunityAux').open)$('opportunityAux').showModal();
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
      document.querySelectorAll('[data-score-base]').forEach(node=>{
        if(node.dataset.scoreBase==='')return;
        const item={score:Number(node.dataset.scoreBase),score_parts:{current:Number(node.dataset.scoreCurrent)},
          quoted_at:node.dataset.quotedAt===''?null:Number(node.dataset.quotedAt)};
        node.textContent=String(displayScore(item));
      });
      document.querySelectorAll('[data-scenario-value]').forEach(node=>{
        if(node.dataset.scenarioValue===undefined)return;
        const age=Date.now()/1000-Number(node.dataset.scenarioAt);
        if(node.dataset.scenarioAt===''||age>10||age< -2){node.textContent='—';node.className='neutral'}
      });
      document.querySelectorAll('[data-calculation-key]').forEach(node=>{
        if(!node.dataset.calculationKey)return;
        const k=decodeURIComponent(node.dataset.calculationKey);
        const item=(state.data?.spot_simple||[]).find(item=>key(item)===k);
        node.innerHTML=item?scenarioMarkup(item):'<p>该路径已不在当前候选中，请重新选择。</p>';
      });
    },
  };
  globalThis.ThinkingPool=api;
  if(typeof document!=='undefined')setInterval(api.expire,2000);
})();
