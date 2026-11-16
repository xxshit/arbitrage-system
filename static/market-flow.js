/* Bounded views of public evidence. No exchange secrets or trading actions. */
window.MarketFlow=(()=>{
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const num=(value,digits=2)=>value===null||value===undefined||!Number.isFinite(Number(value))?'—':Number(value).toLocaleString('zh-CN',{maximumFractionDigits:digits});
  const at=value=>Number.isFinite(Number(value))&&value>0?new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(value*1000)):'—';
  const node=id=>document.getElementById(id);
  let historyPage=1,historyHasMore=false,historyAt=0,historyVersion=0,historyBusy=false,lastNotice='',data=null;
  function evidence(e,expanded=false){
    if(!e)return '<p class="flow-muted">正在补齐分钟数据；数据不足时不猜测吸筹。</p>';
    return `<dl class="flow-metrics"><div><dt>5分钟成交额 / 基准</dt><dd>${num(e.quote_volume)} / ${num(e.expected_5m_quote)} USDT</dd></div><div><dt>放量倍数</dt><dd>${num(e.volume_ratio)}×</dd></div><div><dt>主动买入 / 卖出</dt><dd>${num(e.taker_buy_quote)} / ${num(e.taker_sell_quote)} USDT</dd></div><div><dt>主动买卖差额</dt><dd>${num(e.taker_delta_quote)} USDT</dd></div><div><dt>主动买入占比</dt><dd>${num(e.buy_share===null?null:e.buy_share*100)}% · ${num(e.positive_delta_minutes,0)}/5分钟买盘占优</dd></div><div><dt>5分钟 / 15分钟涨跌</dt><dd>${num(e.change_pct)}% / ${num(e.change_15m_pct)}%</dd></div><div><dt>24H成交额 / 5分钟振幅</dt><dd>${num(e.quote_24h)} USDT / ${num(e.range_pct)}%</dd></div><div><dt>买卖价差 / 高点回撤</dt><dd>${num(e.spread_bps)} bps / ${num(e.drawdown_pct)}%</dd></div></dl><details ${expanded?'open':''}><summary>窗口、判定依据与风险</summary><p>成交窗口：${at(e.window_start)} — ${at(e.window_end)}（北京时间，5根已收盘1分钟K线）。基准：${at(e.baseline_start)} — ${at(e.baseline_end)}，前60分钟成交额 ${num(e.baseline_quote_volume)} USDT ÷ 12；不包含当前5分钟。</p><p>放量：≥${num(e.thresholds?.volume_ratio)}倍且5分钟≥${num(e.thresholds?.minimum_5m_quote)} USDT。疑似吸筹还要求主动买入≥60%、至少3分钟买盘占优、5分钟涨跌−0.5%至2.5%、振幅≤5%、回撤≤2%，基准五分钟≥10,000 USDT、24H成交额≥1,000万 USDT、盘口价差≤20 bps且盘口年龄≤20秒。</p>${e.score_parts?`<p>观察分组成：放量 ${num(e.score_parts.volume)}/30，主动买盘 ${num(e.score_parts.aggressive_buy)}/30，持续性 ${num(e.score_parts.persistence)}/20，价格结构 ${num(e.score_parts.structure)}/20。总分用于排序，未校准为收益概率。</p>`:''}<p>来源：${esc(e.source)} · 流更新时间 ${at(e.stream_at)} · 盘口读取 ${at(e.book_at)}</p><p>${esc(e.interpretation)}</p>${(e.risks||[]).map(r=>`<p>${esc(r)}</p>`).join('')}</details>`;
  }
  function eventCard(item){return `<article class="flow-card"><header><b>${esc(item.symbol)} · ${esc(item.title)}</b><time>${at(item.observed_at)}</time></header>${evidence(item.evidence)}</article>`}
  function tip(item){const e=item.evidence||{};return `<article class="tips-item flow-tip"><div class="tips-item-head"><b>${esc(item.symbol)} · ${esc(item.title)}</b>${typeof alertSpeakerButton==='function'?alertSpeakerButton(item.symbol):''}</div><p>5分钟 ${num(e.quote_volume)} USDT · 基准 ${num(e.expected_5m_quote)} · ${num(e.volume_ratio)}×</p><p>主动买入 ${num(e.buy_share*100)}% · 买卖差额 ${num(e.taker_delta_quote)} USDT · 价格 ${num(e.change_pct)}%</p><small>证据截至 ${at(e.window_end)}；结构推断，不代表已识别主力或链上买入。</small></article>`}
  function render(payload){
    data=payload;
    const o=payload.overview||{},h=payload.health||{},recommended=new Set(payload.recommended||[]),manual=new Map((payload.manual||[]).map(t=>[t.contract,t]));
    node('marketFlowOverview').innerHTML=`<div><small>轻量筛查 · 币安USDT现货</small><b>${num(o.screened_count,0)} / ${num(o.catalogue_count,0)} 币</b><span>24H上涨 ${num(o.up_24h,0)} · 下跌 ${num(o.down_24h,0)}</span></div><div><small>重点监控</small><b>${num(o.focus_count,0)} / ${num(o.limits?.focus,0)} 币</b><span>手动 ${manual.size}/20 · 自动候选核验最多12</span></div><div><small>采集状态</small><b>${o.stale||h.status!=='running'||!h.stream_connected?'数据暂不完整':'运行中'}</b><span>${esc(({starting:'正在初始化',running:'采集中',resource_paused:'资源不足，暂缓采集',degraded:'等待数据恢复',storage_unavailable:'存储暂不可用'})[h.status]||'等待采集')} · ${at(o.at)}</span></div>`;
    node('marketFlowHealth').textContent=`${o.scope||''} ${h.stream_connected?'实时成交汇总已连接。':'实时成交汇总尚未连接。'} ${payload.chain_status||''}`;
    const bySymbol=new Map((payload.rows||[]).map(r=>[r.contract,r]));
    const cards=(payload.recommended||[]).map(s=>bySymbol.get(s)).filter(Boolean);
    node('marketFlowCandidates').innerHTML=cards.length?cards.map((r,i)=>`<article class="flow-card"><header><b>${i+1}. ${esc(r.symbol)}</b><span>观察分 ${num(r.score)}/100</span></header><p>入选：成交放大 ${num(r.evidence.volume_ratio)}×，主动买入 ${num(r.evidence.buy_share*100)}%，${r.evidence.positive_delta_minutes}/5分钟买盘占优，盘口价差 ${num(r.evidence.spread_bps)} bps。</p><p class="flow-muted">有效至 ${at(payload.valid_until)}；数据过期或门槛不满足即退出。不是收益预测。</p>${evidence(r.evidence)}${manual.has(r.contract)?'<span class="flow-tag">已手动关注</span>':`<button type="button" class="admin-write-only${typeof authState!=='undefined'&&authState.is_admin?'':' hidden'}" data-flow-pin="${esc(r.contract)}">加入手动标签</button>`}</article>`).join(''):'<p class="flow-empty">暂无通过完整证据门槛的候选。系统最多选3个，不用旧数据或弱信号凑数。</p>';
    node('marketFlowTags').innerHTML=[...manual.values()].map(t=>`<span class="flow-tag"><b>${esc(t.contract)}</b> ${esc(t.label)}<button type="button" data-flow-remove="${esc(t.contract)}" class="admin-write-only${typeof authState!=='undefined'&&authState.is_admin?'':' hidden'}" aria-label="移除${esc(t.contract)}标签">×</button></span>`).join('')||'<span class="flow-muted">尚未添加手动标签，自动筛选独立运行。</span>';
    const rows=payload.rows||[];
    node('marketFlowRows').innerHTML=rows.length?rows.map(r=>`<article class="flow-card"><header><b>${esc(r.symbol)} <small>${manual.has(r.contract)?'手动标签':recommended.has(r.contract)?'系统候选':'待核验池'}</small></b><span class="flow-state ${r.status==='live'?'live':''}">${esc(({live:'数据完整',warming:'预热中',gap:'存在缺口',stale:'数据过期',unavailable:'不支持'})[r.status]||r.status)}</span></header><p>${esc(r.status_reason)} · ${esc(r.kind==='suspected_accumulation'?'疑似吸筹 · 主动买盘占优':r.kind==='volume_expansion'?'现货放量':'未触发提醒')}</p>${evidence(r.evidence)}</article>`).join(''):'<p class="flow-empty">等待大盘筛选或添加标签。其他页面的行情和报警继续运行。</p>';
  }
  async function load(){
    const read=pageReadContext('market-flow');if(!read.current())return false;
    try{const payload=await read.json('/api/market-flow');read.assertCurrent();render(payload);setPageRefreshSource('market-flow',payload.checked_at,'汇总',payload.overview.stale);return true}
    catch(error){if(isPageReadCancelled(error))return false;node('marketFlowHealth').textContent='监控读取失败。下方为上次数据，候选当前不可用，请稍后重试。';node('marketFlowCandidates').innerHTML='<p class="flow-empty">暂不能提供新鲜候选。</p>';return false}
  }
  async function edit(symbol,remove=false,label='手动关注'){
    const message=node('marketFlowMessage');
    try{const response=await fetch('/api/market-flow/tags',{method:remove?'DELETE':'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':authState.csrf_token},body:JSON.stringify({symbol,label})}),payload=await response.json();if(!response.ok)throw new Error(payload.error||'标签保存失败');message.textContent=remove?'已移除手动标签；原报警历史保留。':'已保存；后台将在下一轮更新重点监控。';await load()}
    catch(error){message.textContent=String(error.message||'保存失败')}
  }
  async function loadHistory(force=false){
    const read=pageReadContext('alerts');if(!read.current()||historyBusy||(!force&&Date.now()-historyAt<15000))return;
    const version=++historyVersion;historyBusy=true;
    try{const payload=await read.json(`/api/market-flow/events?page=${historyPage}`);read.assertCurrent();if(version!==historyVersion)return;historyHasMore=payload.has_more;historyAt=Date.now();node('marketFlowHistory').innerHTML=payload.items.map(eventCard).join('')||'<p class="flow-empty">还没有吸筹或放量提醒。这里只保存通过证据门槛的新事件。</p>';node('marketFlowHistoryPage').textContent=`第 ${historyPage} 页`;node('marketFlowHistoryPrev').disabled=historyPage<=1;node('marketFlowHistoryNext').disabled=!historyHasMore}
    catch(error){if(!isPageReadCancelled(error))node('marketFlowAlarmStatus').textContent='吸筹与放量历史读取失败，保留上次记录。'}
    finally{historyBusy=false}
  }
  function updateAlerts(payload,visible){
    if(!visible)return;
    const h=payload?.health||{},events=payload?.active_events||[];
    node('marketFlowAlarmStatus').textContent=`当前 ${events.length} 条提醒 · ${h.running&&h.stream_connected?'采集在线':'等待新鲜成交数据'} · 历史证据独立保存`;
    const signature=events.map(v=>v.id).join('|'),force=signature!==lastNotice;lastNotice=signature;
    loadHistory(force);
  }
  document.addEventListener('submit',event=>{if(event.target.id!=='marketFlowForm')return;event.preventDefault();edit(node('marketFlowSymbol').value,false,node('marketFlowLabel').value||'手动关注')});
  document.addEventListener('click',event=>{const target=event.target.closest?.('[data-flow-pin],[data-flow-remove],[data-flow-page]');if(!target)return;if(target.dataset.flowPin)edit(target.dataset.flowPin);if(target.dataset.flowRemove)edit(target.dataset.flowRemove,true);if(target.dataset.flowPage&&!historyBusy){historyPage=Math.max(1,historyPage+Number(target.dataset.flowPage));loadHistory(true)}});
  return {load,tip,render,evidence,eventCard,updateAlerts,loadHistory};
})();
function loadMarketFlow(){return window.MarketFlow.load()}
