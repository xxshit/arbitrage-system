/* No keys in localStorage, URLs, diagnostics or rendered responses. */
window.BinanceHedge=(()=>{
  let loading=false,lastLoad=0,lastRead=null,context=null,generation=0,preview=null,placing=false;
  const el=id=>document.getElementById(id),esc=value=>String(value??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let executions=[];
  const stateNames={PREVIEW:'待确认',QUEUED:'已排队',CHECKING:'重新核验',DISPATCHING:'双腿发送中',RECONCILING:'核验成交',REDUCING:'限价减少差额',HEDGED:'净数量已配对',ATTENTION:'需要处理',UNKNOWN:'状态待核实，禁止重发',ACKNOWLEDGED:'本人已接管差额',QUERY_QUEUED:'等待只读对账',NO_FILL:'未留配对仓位',ABORTED:'已取消发送'};
  async function api(path,method='GET',body,read=null){
    if(read&&method!=='GET')throw new Error('页面取消范围仅适用于读取');
    read?.assertCurrent();
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),20000);
    read?.controllers.add(controller);
    try{const response=await fetch('/api/binance'+path,{method,credentials:'same-origin',cache:'no-store',signal:controller.signal,
      headers:{'Content-Type':'application/json','X-CSRF-Token':authState.csrf_token||''},...(body?{body:JSON.stringify(body)}:{})});
      read?.assertCurrent();const result=await response.json();read?.assertCurrent();if(!response.ok)throw new Error(result.error||'币安请求未完成');return result;
    }catch(error){read?.assertCurrent();if(error.name==='AbortError')throw new Error('请求超时，状态未确认；请核对原记录，不会自动重发');throw error}finally{clearTimeout(timer);read?.controllers.delete(controller)}
  }
  function ensurePanel(){
    if(el('binanceAccountPanel')||!el('my-positions'))return;
    const panel=document.createElement('section');panel.id='binanceAccountPanel';panel.className='panel binance-account-panel';
    panel.innerHTML=`<div class="panel-title"><div><p class="eyebrow">BINANCE ACCOUNT · TWO-LEG TAKER</p><h3>币安账户与双腿订单</h3></div><button type="button" id="binanceRefresh">刷新币安余额 / 持仓</button></div>
      <p id="binanceAccountState" role="status">正在读取连接状态…</p><details id="binanceConnectDetails"><summary>安全接入 / 更换 API</summary>
      <form id="binanceConnectForm" autocomplete="off"><p>使用专用 HMAC Key，开启读取、限制服务器 IP，关闭提现和转账。需要下单时再在币安开启现货与合约交易权限。密钥保存后不回显。</p>
      <label>API Key<input id="binanceApiKey" type="password" autocomplete="off" spellcheck="false" required minlength="32" maxlength="128"></label>
      <label>API Secret<input id="binanceApiSecret" type="password" autocomplete="off" spellcheck="false" required minlength="32" maxlength="128"></label>
      <button type="submit">验证并加密保存</button><button id="binanceDisconnect" type="button">移除连接</button></form></details>
      <div id="binanceAccountValues"></div><div id="binanceExecutions"></div>`;
    el('my-positions').querySelector('.panel-title').after(panel);
    el('binanceConnectForm').addEventListener('submit',connect);
    el('binanceRefresh').addEventListener('click',refresh);
    el('binanceDisconnect').addEventListener('click',disconnect);
  }
  function accountMarkup(data){
    const rows=[...(data.balances||[]).map(b=>`<tr><td>现货 ${esc(b.asset)}</td><td>${esc(b.free)}</td><td>冻结 ${esc(b.locked)}</td></tr>`),
      ...(data.futures_assets||[]).map(b=>`<tr><td>合约 ${esc(b.asset)}</td><td>可用 ${esc(b.availableBalance)}</td><td>钱包 ${esc(b.walletBalance)}</td></tr>`),
      ...(data.futures_positions||[]).map(p=>`<tr><td>${esc(p.symbol)} ${esc(p.positionSide)}</td><td>${esc(p.positionAmt)}</td><td>未实现盈亏 ${esc(p.unrealizedProfit)}</td></tr>`)];
    return `<p>账户采集：${new Date(data.checked_at_epoch*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hourCycle:'h23'})} 北京时间 · 此处为币安账户实时读取</p><div class="binance-table"><table><thead><tr><th>资产 / 持仓</th><th>可用 / 数量</th><th>详情</th></tr></thead><tbody>${rows.join('')||'<tr><td colspan="3">当前无非零余额或持仓</td></tr>'}</tbody></table></div>`;
  }
  function executionMarkup(items){
    return `<h4>本站双腿执行记录</h4>${items.map(i=>{const r=i.result||{},p=r.plan||i.plan;return `<article class="binance-intent"><strong>${esc(p.display_symbol)} · ${esc(stateNames[i.state]||i.state)}</strong>
      <p>确认单批 ${esc(p.quantity)} ${esc(p.base)} · 现货上限 ${esc(p.buy_cap)} / 开空下限 ${esc(p.sell_cap)}</p>
      ${r.net_spot_qty!==undefined?`<p>实际现货净额 ${esc(r.net_spot_qty)} / 空单 ${esc(r.short_qty)} · 未配对 ${esc(r.residual_qty)} ${esc(p.base)}</p>`:''}
      <p>${esc(r.reason||'每次确认仅执行显示的一批，不自动继续余量。')}</p>
      ${r.dispatch_skew_ms!==undefined?`<small>本机写入开始时差 ${esc(r.dispatch_skew_ms)}ms · 两请求写完跨度 ${esc(r.request_write_span_ms)}ms · 交易所成交时间差 ${esc(r.exchange_fill_skew_ms)}ms</small>`:''}
      ${Object.keys(r.reconciliation_errors||{}).length?`<p>独立核验未完成：${esc(Object.keys(r.reconciliation_errors).map(m=>({spot:'现货',futures:'合约',reduction_fill:'减仓'}[m]||m)).join('、'))}。其余已核实结果保留，禁止按未知数量补单或减仓。</p>`:''}
      ${['UNKNOWN','ATTENTION'].includes(i.state)?`<button type="button" data-binance-reconcile="${esc(i.id)}">按原订单号重新对账</button>`:''}
      ${i.state==='ATTENTION'?`<button type="button" data-binance-ack="${esc(i.id)}">我已核对并接管剩余风险</button>`:''}
      <details><summary>订单与手续费证据</summary><pre>${esc(JSON.stringify({spot:r.spot,futures:r.futures,reduction:r.reduction_fill},null,2))}</pre></details></article>`}).join('')||'<p>尚未提交双腿订单。</p>'}`;
  }
  async function load(force=false){
    const read=globalThis.pageReadContext?.('my-positions');
    if((read&&!read.current())||!authState.is_admin||loading||(!force&&read===lastRead&&Date.now()-lastLoad<5000))return;
    ensurePanel();loading=true;
    try{const data=await api('/account','GET',undefined,read);el('binanceAccountState').textContent=(data.connected?`已连接 · Key 尾号 ${data.key_suffix}。`:'尚未连接。')+data.reason;
      executions=data.executions||[];el('binanceExecutions').innerHTML=executionMarkup(executions);bindActions();el('binanceRefresh').disabled=!data.connected;el('binanceConnectForm').querySelector('button[type=submit]').disabled=!data.configured;
      if(!data.configured)el('binanceAccountState').textContent+=' 服务器密钥保险箱尚未配置。';
      lastLoad=Date.now();lastRead=read;
    }catch(error){if(read&&!read.current())return;if(el('binanceAccountState'))el('binanceAccountState').textContent=error.message}finally{loading=false}
  }
  function bindActions(){
    el('binanceExecutions').querySelectorAll('[data-binance-reconcile]').forEach(b=>b.onclick=async()=>{b.disabled=true;try{await api(`/intents/${b.dataset.binanceReconcile}/reconcile`,'POST',{});await load(true)}catch(e){el('binanceAccountState').textContent=e.message}finally{b.disabled=false}});
    el('binanceExecutions').querySelectorAll('[data-binance-ack]').forEach(b=>b.onclick=async()=>{const i=executions.find(i=>i.id===b.dataset.binanceAck);if(!i||!confirm(`现货净数量 ${i.result.net_spot_qty}，合约空单 ${i.result.short_qty}，未配对 ${i.result.residual_qty}。\n接管后由你自行处理风险；本站不会补单或平仓，也不会将它标记为完全对冲。确认接管？`))return;b.disabled=true;try{await api(`/intents/${i.id}/acknowledge`,'POST',{confirm:'接受已确认差额并自行处理',residual_qty:i.result.residual_qty});await load(true)}catch(e){el('binanceAccountState').textContent=e.message}finally{b.disabled=false}});
  }
  async function connect(event){
    event.preventDefault();const button=event.currentTarget.querySelector('button[type=submit]');button.disabled=true;
    const body={api_key:el('binanceApiKey').value.trim(),api_secret:el('binanceApiSecret').value.trim()};
    el('binanceApiKey').value='';el('binanceApiSecret').value='';
    try{const data=await api('/account','POST',body);el('binanceAccountValues').innerHTML=accountMarkup(data.account);el('binanceConnectDetails').open=false;await load(true)}
    catch(error){el('binanceAccountState').textContent=error.message}finally{body.api_key='';body.api_secret='';button.disabled=false}
  }
  async function refresh(){const button=el('binanceRefresh');button.disabled=true;try{const data=await api('/account/refresh','POST',{});el('binanceAccountValues').innerHTML=accountMarkup(data.account)}catch(error){el('binanceAccountState').textContent=error.message}finally{button.disabled=false}}
  async function disconnect(){if(!confirm('移除本站保存的币安密钥？这不会撤销币安订单或平仓。'))return;try{await api('/account','DELETE');el('binanceAccountValues').innerHTML='';await load(true)}catch(error){el('binanceAccountState').textContent=error.message}}
  function reset(){generation++;preview=null;const submit=el('hedgeSubmitButton');if(submit){submit.disabled=true;submit.onclick=null}if(el('binanceDepthResult'))el('binanceDepthResult').textContent='请核验真实深度。确认仅对显示的单批有效；总预算的剩余部分不自动追单。'}
  function builder(opportunity){
    context=opportunity;reset();
    const enabled=authState.is_admin&&opportunity.strategy==='spot_futures'&&opportunity.row.long_exchange==='Binance';
    if(!el('binanceTakerControls')){
      const controls=document.createElement('div');controls.id='binanceTakerControls';controls.className='binance-taker-controls';
      controls.innerHTML=`<h3>双腿吃单 · 限价 FOK</h3><div class="hedge-preview-fields"><label>单批最大金额（USDT）<input id="binanceClip" type="number" min="5" max="1000" value="20"></label>
        <label>扣费后最低开差（bp）<input id="binanceMinEdge" type="number" min="-100" max="1000" value="0"></label>
        <label>减仓退出价差上限（bp，手续费另计）<input id="binanceExitSlippage" type="number" min="0" max="50" value="20"></label>
        <label>单腿差额处置<select id="binanceResidual"><option value="reduce">限价减掉本次多余敞口</option><option value="hold">暂停并由我处理</option></select></label></div>
        <p>上方为总预算估算，实际只执行下方核验的一批。现货与合约按同一基础币数量并发发送。单腿 FOK 全成或取消，两市场不能原子成交。手续费可能形成差额，未核清时阻止新单。</p>
        <button id="binanceDepthCheck" type="button">核验双腿真实盘口与账户</button><div id="binanceDepthResult" role="status"></div>`;
      el('hedgeSubmitButton').before(controls);controls.querySelectorAll('input,select').forEach(input=>input.addEventListener('input',reset));
      el('binanceDepthCheck').addEventListener('click',checkDepth);
    }
    el('binanceTakerControls').classList.toggle('hidden',!enabled);
    if(enabled){el('hedgeSubmitButton').textContent='先核验实际数量与限价';el('binanceDepthCheck').disabled=placing}
  }
  async function checkDepth(){
    if(!context||placing)return;reset();const serial=generation,button=el('binanceDepthCheck');button.disabled=true;
    const body={symbol:context.group.symbol,quote_amount:el('hedgeQuoteAmount').value,leverage:el('hedgeLeverage').value,
      slippage_bps:String(Number(el('hedgeMaxSlippage').value)*100),clip_quote:el('binanceClip').value,min_net_bps:el('binanceMinEdge').value,residual_policy:el('binanceResidual').value,
      exit_slippage_bps:el('binanceExitSlippage').value};
    el('binanceDepthResult').textContent='正在并行读取两边盘口、精度、余额和现有仓位…';
    try{const data=await api('/preview','POST',body);if(serial!==generation)return;preview=data.intent;const p=preview.plan;
      el('binanceDepthResult').innerHTML=`<dl class="binance-depth"><dt>本次单批基础币数量</dt><dd>${esc(p.quantity)} ${esc(p.base)}</dd><dt>现货买入最高价</dt><dd>${esc(p.buy_cap)}</dd><dt>合约开空最低价</dt><dd>${esc(p.sell_cap)}</dd><dt>深度均价（现货 / 合约）</dt><dd>${esc(p.spot_vwap)} / ${esc(p.futures_vwap)}</dd><dt>最坏边界扣费开差</dt><dd>${esc(p.worst_net_bps)} bp</dd><dt>按确认价格估算单腿名义敞口</dt><dd>${esc(p.max_single_leg_quote)} USDT</dd><dt>限价内深度使用上限</dt><dd>10%</dd><dt>确认时效</dt><dd>15 秒；发单前重新核验</dd></dl><p>不能保证同一毫秒成交；成交不配对或时间超限会暂停新单。${data.execution_enabled?'请确认下面这一批。':'真实执行器尚未启用，本次仅完成实盘口核验。'}</p>`;
      if(p.exit_preview)el('binanceDepthResult').innerHTML+=`<p>仅当一腿已确认多余时，最多尝试一次限价减仓：相对该腿实际成交均价，退出价差最多 ${esc(p.exit_slippage_bps)} bp；减仓手续费另计，现货约 ${esc(p.exit_fee_bps.spot)} bp / 合约约 ${esc(p.exit_fee_bps.futures)} bp。反向盘口变化可能导致零成交或剩余差额，这不是最大亏损保证。</p>`;
      const submit=el('hedgeSubmitButton');submit.disabled=true;submit.textContent=data.execution_enabled?'等待持续盘口与成交订阅就绪':'真实执行尚未开启';
      submit.onclick=confirmOrder;
      if(data.execution_enabled)await checkPrepared(serial, data);
      setTimeout(()=>{if(serial===generation){preview=null;submit.disabled=true;submit.textContent='确认已过期，请重新核验'}},Math.max(0,preview.expires_at*1000-Date.now()));
    }catch(error){if(serial===generation)el('binanceDepthResult').textContent=error.message}finally{button.disabled=false}
  }
  async function checkPrepared(serial, data){
    if(serial!==generation||!preview||placing)return;
    const item=data.intent,submit=el('hedgeSubmitButton');
    if(item.id!==preview.id||item.state&&item.state!=='PREVIEW')return;
    preview=item;
    const until=Math.min(item.expires_at*1000,(item.result?.prepared_at||0)*1000+10000);
    const ready=data.execution_enabled&&item.result?.prepared===true&&Date.now()<until;
    submit.disabled=!ready;
    submit.textContent=ready?'确认这一批双腿 FOK':(item.result?.reason||'等待持续盘口与成交订阅就绪');
    if(ready)setTimeout(()=>{if(serial===generation){submit.disabled=true;submit.textContent='预热已过期，请重新核验'}},Math.max(0,until-Date.now()));
    else if(data.execution_enabled&&Date.now()<item.expires_at*1000&&!item.result?.reason){
      setTimeout(async()=>{if(serial!==generation||!preview||placing)return;try{await checkPrepared(serial,await api(`/intents/${item.id}`))}catch(e){if(serial===generation){submit.disabled=true;submit.textContent='订阅状态未确认，请重新核验'}}},500);
    }
  }
  async function confirmOrder(){
    if(!preview||placing||preview.result?.prepared!==true||Date.now()>=(preview.result.prepared_at+10)*1000||Date.now()>=preview.expires_at*1000)return;
    const item=preview,p=item.plan;
    if(!confirm(`确认真实下单这一批：${p.quantity} ${p.base}\n现货买入上限 ${p.buy_cap}；合约开空下限 ${p.sell_cap}\n一腿失败时按确认价格估算名义敞口约 ${p.max_single_leg_quote} USDT。两市场不能原子成交。\n差额处置：${el('binanceResidual').selectedOptions[0].textContent}${p.exit_preview?`；退出价差最多 ${p.exit_slippage_bps} bp，减仓手续费另计（现货约 ${p.exit_fee_bps.spot} bp / 合约约 ${p.exit_fee_bps.futures} bp），不能保证退出` : ''}\n确认后重复点击不会补发，剩余预算不会自动继续。`))return;
    placing=true;el('hedgeSubmitButton').disabled=true;el('binanceDepthCheck').disabled=true;
    try{const data=await api(`/intents/${item.id}/confirm`,'POST',{confirm:'确认双腿限价FOK和单腿风险',intent_id:item.id});el('binanceDepthResult').textContent=`${stateNames[data.intent.state]||data.intent.state}。请在“我的仓位”查看双腿成交与差额。`;preview=null;await load(true)}
    catch(error){el('binanceDepthResult').textContent=error.message+' 请到“我的仓位”核对原意图，勿重新建单。';preview=null}
    finally{placing=false;el('binanceDepthCheck').disabled=false}
  }
  return {load,builder,accountMarkup,executionMarkup};
})();
