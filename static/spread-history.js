/* Public, sampled spot/futures history; independent of the five-second table. */
const spreadChartState={version:0,controller:null,timer:null,data:null,trigger:null};
const spreadChartPeriods=[['1h','1小时'],['6h','6小时'],['24h','24小时'],['3d','3天'],['7d','7天'],['15d','15天']];
const spreadChartNames={open:'开差',close:'平差',basis:'币安基差'};
const spreadChartColors={open:'#087fbd',close:'#cc7a15',basis:'#9860c7'};
function spreadChartEscape(value){return String(value).replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]))}
function spreadChartPercent(value){return Number.isFinite(value)?`${value>0?'+':''}${value.toFixed(4)}%`:'—'}
function spreadHistoryButton(symbol,exchange,metric,value){
  if(!Number.isFinite(value))return '—';
  const key=encodeURIComponent(JSON.stringify([symbol,exchange])).replace(/'/g,'%27');
  return `<button type="button" class="spread-history-trigger" title="查看${spreadChartNames[metric]}历史曲线" aria-label="${spreadChartEscape(symbol)} ${spreadChartEscape(exchange)} ${spreadChartNames[metric]}历史" onclick="openSpreadHistory('${key}','${metric}',this)">${spreadChartPercent(value)}</button>`;
}
function spreadChartTime(epoch,short=false){
  return new Date(epoch*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',...(short?{}:{second:'2-digit'}),hourCycle:'h23'});
}
function spreadChartGeometry(data,metrics,width=970){
  const points=metrics.flatMap(metric=>data.series[metric]?.points||[]);
  if(!points.length)return null;
  let low=Infinity,high=-Infinity,first=Infinity;
  for(const [t,value] of points){low=Math.min(low,value);high=Math.max(high,value);first=Math.min(first,t)}
  const pad=Math.max((high-low)*.12,.005);low-=pad;high+=pad;
  const start=Math.max(data.start,Math.min(first,data.end-60));
  const x=t=>76+(t-start)/(data.end-start)*(width-102);
  const y=value=>24+(high-value)/(high-low)*290;
  return {low,high,x,y,width,start};
}
function spreadChartPath(points,geometry){
  return points.map(([t,value,gap],index)=>`${index===0||gap?'M':'L'}${geometry.x(t).toFixed(2)},${geometry.y(value).toFixed(2)}`).join(' ');
}
function spreadChartMetrics(){return ['open','close','basis'].filter(metric=>document.getElementById(`spreadShow-${metric}`)?.checked)}
function renderSpreadChart(){
  const data=spreadChartState.data,plot=document.getElementById('spreadChartPlot');
  if(!data||!plot)return;
  const metrics=spreadChartMetrics(),geometry=spreadChartGeometry(data,metrics,Math.max(240,plot.clientWidth||970));
  const summary=document.getElementById('spreadChartSummary');
  summary.innerHTML=metrics.map(metric=>{
    const series=data.series[metric]||{points:[],last_at:null,low:null,high:null},latest=series.points.at(-1)?.[1];
    return `<span style="--series-color:${spreadChartColors[metric]}"><b>${spreadChartNames[metric]}</b><strong>${spreadChartPercent(latest)}</strong><small>低 ${spreadChartPercent(series.low)}　高 ${spreadChartPercent(series.high)}</small><small>采样 ${series.last_at===null?'—':spreadChartTime(series.last_at)}</small></span>`;
  }).join('');
  if(!geometry){
    plot.innerHTML=`<div class="spread-chart-empty">${metrics.length?'所选周期暂无有效历史<br><small>从上线后开始积累，不用当前值补画过去</small>':'勾选开差、平差或币安基差查看曲线'}</div>`;
    return;
  }
  const {x,y,low,high,width,start}=geometry,right=width-26,ticks=width<380?1:width<600?2:4;
  let grid='';
  for(let i=0;i<5;i++){const value=low+(high-low)*i/4,py=y(value);grid+=`<line x1="76" x2="${right}" y1="${py}" y2="${py}" class="spread-grid"/><text x="66" y="${py+4}" text-anchor="end">${value.toFixed(2)}%</text>`}
  for(let i=0;i<=ticks;i++){const t=start+(data.end-start)*i/ticks;grid+=`<text x="${x(t)}" y="344" text-anchor="${i===0?'start':i===ticks?'end':'middle'}">${spreadChartEscape(spreadChartTime(t,true))}</text>`}
  if(low<0&&high>0)grid+=`<line x1="76" x2="${right}" y1="${y(0)}" y2="${y(0)}" class="spread-zero"/>`;
  const lines=metrics.map(metric=>{
    const points=data.series[metric]?.points||[];
    return `<path d="${spreadChartPath(points,geometry)}" fill="none" stroke="${spreadChartColors[metric]}" stroke-width="2" vector-effect="non-scaling-stroke"/>`+
      points.filter((p,i)=>i===0||p[2]||i===points.length-1).map(([t,value])=>`<circle cx="${x(t)}" cy="${y(value)}" r="2.5" fill="${spreadChartColors[metric]}"/>`).join('');
  }).join('');
  plot.innerHTML=`<svg viewBox="0 0 ${width} 360" tabindex="0" role="img" aria-label="开差、平差和币安基差历史曲线。方向键查看采样点，空白表示未采集或数据中断。">${grid}${lines}<line id="spreadChartCursor" x1="76" x2="76" y1="24" y2="314" hidden/></svg><output id="spreadChartTooltip" class="spread-chart-tooltip" hidden></output>`;
  const svg=plot.querySelector('svg'),tooltip=document.getElementById('spreadChartTooltip'),cursor=document.getElementById('spreadChartCursor');
  const times=[...new Set(metrics.flatMap(metric=>(data.series[metric]?.points||[]).map(p=>p[0])))].sort((a,b)=>a-b);
  let selected=times.length-1;
  function inspectAt(t){
    cursor.removeAttribute('hidden');cursor.setAttribute('x1',x(t));cursor.setAttribute('x2',x(t));
    tooltip.hidden=false;tooltip.innerHTML=metrics.map(metric=>{
      const nearest=(data.series[metric]?.points||[]).reduce((best,p)=>!best||Math.abs(p[0]-t)<Math.abs(best[0]-t)?p:best,null);
      if(!nearest||Math.abs(nearest[0]-t)>Math.max(60,data.summary_seconds))return `<span>${spreadChartNames[metric]}　—</span>`;
      return `<span><i style="background:${spreadChartColors[metric]}"></i>${spreadChartNames[metric]} <b>${spreadChartPercent(nearest[1])}</b><small>${spreadChartTime(nearest[0])}</small></span>`;
    }).join('');
  }
  svg.addEventListener('pointermove',event=>{
    const box=svg.getBoundingClientRect(),px=(event.clientX-box.left)/box.width*width;
    const t=start+Math.max(0,Math.min(1,(px-76)/(width-102)))*(data.end-start);
    inspectAt(t);
  });
  svg.addEventListener('pointerleave',()=>{cursor.setAttribute('hidden','');tooltip.hidden=true});
  svg.addEventListener('keydown',event=>{
    if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
    event.preventDefault();selected=event.key==='Home'?0:event.key==='End'?times.length-1:Math.max(0,Math.min(times.length-1,selected+(event.key==='ArrowLeft'?-1:1)));
    inspectAt(times[selected]);
  });
}
async function loadSpreadChart(reset=false){
  const modal=document.getElementById('spreadHistoryModal');
  if(!modal||modal.classList.contains('hidden'))return;
  if(!reset&&spreadChartState.controller)return;
  spreadChartState.controller?.abort();
  const version=++spreadChartState.version,controller=new AbortController();
  spreadChartState.controller=controller;
  const status=document.getElementById('spreadChartStatus');
  const symbol=modal.dataset.symbol,exchange=document.getElementById('spreadHistoryExchange').value,period=document.getElementById('spreadHistoryPeriod').value;
  if(reset){spreadChartState.data=null;document.getElementById('spreadChartSummary').innerHTML='';document.getElementById('spreadChartPlot').innerHTML='<div class="spread-chart-empty">正在读取历史…</div>'}
  status.textContent='正在读取…';
  const timeout=setTimeout(()=>controller.abort(),12000);
  try{
    const response=await fetch(`/api/spot-futures/history?${new URLSearchParams({symbol,exchange,period})}`,{signal:controller.signal,cache:'no-store'});
    if(!response.ok)throw new Error(response.status===401?'登录已失效，请重新登录':'历史读取失败，请稍后重试');
    const data=await response.json();
    if(version!==spreadChartState.version)return;
    spreadChartState.data=data;renderSpreadChart();
    const all=Object.values(data.series).filter(s=>s.first_at!==null);
    status.textContent=all.length?`已有历史 ${spreadChartTime(Math.min(...all.map(s=>s.first_at)))} 起 · 最近 ${spreadChartTime(Math.max(...all.map(s=>s.last_at)))}`:'历史从上线后开始积累';
  }catch(error){
    if(version!==spreadChartState.version)return;
    status.textContent=error.name==='AbortError'?'读取超时，可点击重试':error.message;
    if(!spreadChartState.data)document.getElementById('spreadChartPlot').innerHTML='<div class="spread-chart-empty">暂时无法读取历史</div>';
  }finally{
    clearTimeout(timeout);
    if(version===spreadChartState.version)spreadChartState.controller=null;
  }
}
function openSpreadHistory(encoded,metric,trigger){
  const [symbol,exchange]=JSON.parse(decodeURIComponent(encoded));
  closeSpreadHistory(false);spreadChartState.trigger=trigger;
  let modal=document.getElementById('spreadHistoryModal');
  if(!modal){modal=document.createElement('section');modal.id='spreadHistoryModal';modal.className='modal';document.body.appendChild(modal);modal.addEventListener('click',event=>{if(event.target===modal)closeSpreadHistory()});modal.addEventListener('keydown',event=>{
    if(event.key==='Escape'){event.stopPropagation();closeSpreadHistory()}
    if(event.key==='Tab'){
      const targets=[...modal.querySelectorAll('button,select,input,svg[tabindex]')],first=targets[0],last=targets.at(-1);
      if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus()}
      else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus()}
    }
  })}
  modal.dataset.symbol=symbol;modal.classList.remove('hidden');
  modal.innerHTML=`<div class="dialog spread-history-dialog" role="dialog" aria-modal="true" aria-labelledby="spreadHistoryTitle">
    <button type="button" class="close" aria-label="关闭历史曲线" onclick="closeSpreadHistory()">×</button>
    <h2 id="spreadHistoryTitle">${spreadChartEscape(symbol)} <span>开差 / 平差 / 基差历史</span></h2>
    <div class="spread-history-controls"><label>路径 <select id="spreadHistoryExchange" onchange="loadSpreadChart(true)">${['Binance','Gate','Bitget'].map(name=>`<option ${name===exchange?'selected':''} value="${name}">${name}现货 → 币安合约</option>`).join('')}</select></label>
    <label>周期 <select id="spreadHistoryPeriod" onchange="loadSpreadChart(true)">${spreadChartPeriods.map(([value,name])=>`<option value="${value}" ${value==='24h'?'selected':''}>${name}</option>`).join('')}</select></label><button type="button" onclick="loadSpreadChart(true)">刷新</button></div>
    <div class="spread-chart-legend"><label style="--series-color:${spreadChartColors.open}"><input id="spreadShow-open" type="checkbox" ${metric!=='basis'?'checked':''} onchange="renderSpreadChart()">开差</label><label style="--series-color:${spreadChartColors.close}"><input id="spreadShow-close" type="checkbox" ${metric!=='basis'?'checked':''} onchange="renderSpreadChart()">平差</label><label style="--series-color:${spreadChartColors.basis}"><input id="spreadShow-basis" type="checkbox" checked onchange="renderSpreadChart()">币安基差</label><small>基差与现货路径无关 · 北京时间</small></div>
    <div id="spreadChartSummary" class="spread-chart-summary"></div><div id="spreadChartPlot" class="spread-chart-plot"></div>
    <p id="spreadChartStatus" class="spread-chart-status" role="status"></p>
    <p class="spread-chart-note">按已有历史展开 · 约5秒行情采样，每分钟保留首末及高低点 · 长周期合并并保留极值 · 最长15天 · 断档不补线</p>
  </div>`;
  modal.querySelector('.close').focus();loadSpreadChart(true);
  spreadChartState.timer=setInterval(()=>{if(!document.hidden)loadSpreadChart()},30000);
}
function closeSpreadHistory(restore=true){
  ++spreadChartState.version;spreadChartState.controller?.abort();spreadChartState.controller=null;
  clearInterval(spreadChartState.timer);spreadChartState.timer=null;spreadChartState.data=null;
  document.getElementById('spreadHistoryModal')?.classList.add('hidden');
  if(restore&&spreadChartState.trigger?.isConnected)spreadChartState.trigger.focus({preventScroll:true});
}
