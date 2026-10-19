/* Isolated synthetic GET fixtures; never forward requests or execute writes. */
(() => {
  'use strict';
  delete window.PushManager;
  const now = () => Date.now() / 1000;
  const stamp = '2026-10-18 10:30:00';
  const wallet=(exchange,overrides={})=>({exchange,state:'fresh',chains:[{name:'BSC',deposit_open:false,withdraw_open:true}],captured_at:'10-18 10:30:00 UTC',captured_at_epoch:now(),reason:'',source_url:'https://api.gateio.ws/api/v4/spot/currencies',...overrides});
  const symbols = ['DEMO/USDT','SAMPLE/USDT','LONGTOKEN/USDT','SMALL/USDT'];
  const base = {long_exchange:'Gate',short_exchange:'Binance',long_ask:1.23456789,long_bid:1.2344,short_ask:1.246,short_bid:1.245,
    basis:0.83,funding_rate:0.025,funding_previous:0.021,funding_24h:0.14,funding_3d:0.42,funding_7d:0.98,funding_30d:3.8,
    funding_interval_hours:4,next_funding_time:'2026-10-18 12:00:00',spot_volume:18200000,futures_volume:37600000,futures_open_interest:14300000,
    open_spread:0.85,close_spread:-0.93,change_5m:0.42,change_15m:0.7,change_30m:1.2,change_1h:2.1,change_12h:4.6,change_24h:7.9,change_3d:12.6,change_7d:18.4};
  const spot = symbols.map((symbol,index) => ({symbol,rows:[{...base,long_ask:base.long_ask+index/10,transfer_status:wallet('Gate')},{...base,long_exchange:'Bitget',open_spread:0.75,transfer_status:wallet('Bitget',{state:'stale',captured_at:'08-04 02:51:25 UTC',chains:[{name:'BEP20',deposit_open:true,withdraw_open:true}],reason:'快照已过期'})}]}));
  spot[0].rows.unshift({...base,long_exchange:'Binance',open_spread:.65,transfer_status:wallet('Binance',{chains:[{name:'BSC',deposit_open:true,withdraw_open:true}]})});
  spot[1].rows=spot[1].rows.slice(0,1);
  spot[1].rows[0].open_spread=.25;
  spot[2].rows[0].open_spread=-.3;spot[2].rows[1].open_spread=.2;
  spot[3].rows[0].open_spread=-.1;spot[3].rows[1].open_spread=-.5;
  spot.slice(0,3).forEach(group=>{group.basis_opening={open_count:2,opened_basis:-1.1353,opened_at:'09-23 08:40:40',max_basis:-1.7,max_at:'09-23 08:45:00'}});
  for(let i=0;i<7;i++)spot.push({symbol:`DENSITY${i+1}/USDT`,rows:[{...base,open_spread:.1-i*.05,transfer_status:wallet('Gate')}]});
  const isolatedRisk={symbol:'ISOLATED/USDT',long_exchange:'Gate',short_exchange:'Binance',state:'confirmed',long_ask:.12225,short_bid:.02814,index_price:.02804,open_spread:-76.98,basis:.36,blocked_chains:['BSC 充关','ETH 提关'],blocked_transfers:[{exchange:'Gate',chain:'BSC',deposit_open:false,withdraw_open:true},{exchange:'Gate',chain:'ETH',deposit_open:true,withdraw_open:false}],transfer_state:'fresh',stale:false,observed_at:'10-19 00:00:00 UTC'};
  spot.push({symbol:isolatedRisk.symbol,rows:[{...base,...isolatedRisk,transfer_status:wallet('Gate'),price_divergence:isolatedRisk},{...base,long_exchange:'Bitget',transfer_status:wallet('Bitget',{chains:[{name:'BEP20',deposit_open:true,withdraw_open:true}]})}]});
  const dual = symbols.map(symbol => ({symbol,binance_change_5m:0.42,binance_change_15m:0.7,binance_change_30m:1.2,binance_change_1h:2.1,binance_change_12h:-4.6,binance_change_24h:7.9,binance_change_3d:-12.6,binance_change_7d:18.4,rows:[
    {...base,long_exchange:'Bybit',long_basis:-0.2,short_basis:0.83,long_open_interest:12000000,short_open_interest:14300000,long_volume:25000000,short_volume:37600000,
      long_funding_rate:0.01,short_funding_rate:0.025,long_funding_interval_hours:4,short_funding_interval_hours:4,long_next_funding_time:stamp,short_next_funding_time:stamp,
      funding_difference:0.015,funding_difference_previous:0.012,funding_difference_24h:0.09,funding_difference_3d:0.27,funding_difference_7d:0.6,funding_difference_30d:2.4,funding_settlement_label:'双方结算 · 12:00'},
  ]}));
  // Single, double and triple paths plus long identifiers and missing fields.
  // These fixtures never contact an exchange or change the live server.
  dual[0].basis_opening=spot[0].basis_opening;
  dual[0].rows[0]={...dual[0].rows[0],index_overlap:60,long_index_components:[{source:'binance',name:'Binance',weight:.6},{source:'gate',name:'Gate',weight:.4}],short_index_components:[{source:'binance',name:'Binance',weight:.8},{source:'okx',name:'OKX',weight:.2}]};
  dual[1].rows.push({...dual[1].rows[0],long_exchange:'OKX',long_basis:-.1,long_funding_rate:-.02,open_spread:.65,long_funding_interval_hours:8});
  for(let i=0;i<8;i++)dual.push({...dual[2],symbol:`DUALTEST${i+1}/USDT`,rows:[{...dual[2].rows[0],open_spread:.5-i*.1}]});
  dual[9].rows.push({...dual[9].rows[0],long_exchange:'OKX',open_spread:.21},{...dual[9].rows[0],short_exchange:'OKX',open_spread:-.4});
  dual[10]={...dual[10],symbol:'超长合成币种名称测试/USDT',rows:[{...dual[10].rows[0],long_bid:123456.78,long_ask:123456.9,long_funding_cap:12.34,long_open_interest:123456789012,long_volume:987654321098,funding_difference_30d:-123.4567,funding_settlement_label:'双方结算时间不同 · 12:00 / 16:00'}]};
  dual[11].rows[0]={...dual[11].rows[0],long_basis:null,long_funding_rate:null,long_open_interest:null,short_volume:null,funding_difference_24h:null,binance_change_7d:null};
  const funding = symbols.map((symbol,index) => ({symbol,long_exchange:'Gate',score:85-index*7,level:index?'观察机会':'重点机会',funding_interval:4,
    change_24h:7.9,change_3d:12.6,funding_current:0.025,funding_previous:0.021,funding_24h:0.14,funding_3d:0.42,basis:0.83,open_spread:0.85,
    oi_change_12h:9.4,volume_change_12h:26.5,futures_open_interest:14300000,futures_volume:37600000}));
  const simple = symbols.slice(0,3).map(symbol => ({...base,symbol,long_is_spot:true,long_volume:18200000,short_volume:37600000,short_open_interest:14300000,
    long_interval:null,short_interval:4,funding_current:0.025}));
  const event = {id:1,symbol:'DEMO/USDT',strategy:'spot_futures',type:'rapid_basis',message:'合成报警：连续两次确认后的基差变化，仅用于布局和详细说明测试。',basis:1.4,open_spread:0.85,close_spread:-0.93,created_at:stamp,
    long_exchange:'Gate',short_exchange:'Binance',long_market:'现货',short_market:'合约',short_interval:4,funding_interval_hours:4,remaining_seconds:90,expires_at_epoch:now()+90};
  const alertGroups = [...symbols,isolatedRisk.symbol,...Array.from({length:164},(_,i)=>`TESTCOIN${String(i+1).padStart(3,'0')}/USDT`)].map((symbol,index) => {
    const timeline=Array.from({length:index?3:93},(_,i)=>{
      const strategy=symbol===isolatedRisk.symbol||index===1||index===2?'spot_futures':index===3||i%2?'futures_futures':'spot_futures',isSpot=strategy==='spot_futures';
      const long_exchange=isSpot?(symbol===isolatedRisk.symbol||(index===0&&[20,22].includes(i))||(index===2&&i===2)?'Bitget':'Gate'):'Bybit',short_exchange=!isSpot&&(index===3||[3,7].includes(i))?'OKX':'Binance';
      return {...event,id:index*100+i+1,symbol,strategy,long_exchange,short_exchange,long_market:isSpot?'现货':'合约',long_interval:isSpot?null:4,created_at:`10-18 ${String(10-Math.floor(i/12)).padStart(2,'0')}:${String(59-i%12*5).padStart(2,'0')}:00`,open_spread:i%2?-0.8512:0.8534,basis:i%2?-1.4012:1.4034,path_id:JSON.stringify([strategy,long_exchange,short_exchange])};
    });
    if(index===0){Object.assign(timeline[91],{open_spread:10.1234,basis:2.277});Object.assign(timeline[92],{open_spread:-50.5678,basis:-7.8047})}
    if(index===0)timeline.slice(0,8).forEach((item,i)=>{item.type='rapid_spread';item.pulse={id:`synthetic-pulse-${i}`,metric:'spread',ordinal:i<2?2:1,state:i%2?'retraced':'watch',baseline:.03,baseline_at:now()-25,peak:i%2?1.02:.4,peak_at:now()-15,current:i%2?.11:.4,current_at:now(),retrace_at:i%2?now()-10:undefined,notice_version:1}});
    if(index===0){const start=now()-25;Object.assign(timeline[0],{open_spread:-1.2,pulse:{id:'synthetic-pulse-fall',metric:'spread',ordinal:1,direction:-1,state:'settled',baseline:.04,baseline_at:start,peak:-1.2,peak_at:start+12.8,current:-1.2,current_at:start+13,ended_at:start+13}})}
    const value_ranges=Object.fromEntries(['open_spread','basis'].map(field=>{const values=timeline.map(item=>item[field]);return [field,{max:Math.max(...values),min:Math.min(...values)}]}));
    const move_peaks=Object.fromEntries(Object.entries(value_ranges).map(([field,{max,min}])=>[field,Math.abs(max)>=Math.abs(min)?max:min]));
    const transfer_statuses=[wallet('Gate',{chains:[{name:'BSC',deposit_open:index===1,withdraw_open:true},{name:'ETH',deposit_open:true,withdraw_open:false}]}),wallet('Bitget',{state:'stale',captured_at:'08-04 02:51:25 UTC',chains:[{name:'BEP20',deposit_open:true,withdraw_open:true}],reason:'快照已过期'}),wallet('Binance',{state:'unknown',chains:[],captured_at:null,reason:'尚未接入账户只读充提接口'})];
    if(index===1){transfer_statuses[0].chains.forEach(chain=>{chain.deposit_open=true;chain.withdraw_open=true});Object.assign(transfer_statuses[1],{state:'fresh',reason:''})}
    if(symbol===isolatedRisk.symbol)Object.assign(transfer_statuses[1],{state:'fresh',reason:''});
    if(index===2)for(const [offset,count] of [[0,30],[1,21]])Object.assign(transfer_statuses[offset],{state:'fresh',reason:'',chains:Array.from({length:count},(_,i)=>({name:`SYNTHETIC-${offset}-NETWORK-${String(i+1).padStart(2,'0')}`,deposit_open:i%3===0?false:i%3===1?true:null,withdraw_open:i%2===0}))});
    transfer_statuses.push(...['Bybit','OKX'].map(exchange=>wallet(exchange,{state:'unknown',chains:[],reason:'尚未接入账户只读充提接口'})));
    timeline.forEach(item=>{item.binance_basis=item.short_exchange==='Binance'?item.basis:null;item.basis_exchange=item.binance_basis===null?null:'Binance'});
    const buckets=new Map();timeline.forEach(item=>{if(!buckets.has(item.path_id))buckets.set(item.path_id,[]);buckets.get(item.path_id).push(item)});
    const paths=[...buckets].map(([id,items])=>({id,strategy:items[0].strategy,long_exchange:items[0].long_exchange,short_exchange:items[0].short_exchange,long_market:items[0].long_market,short_market:'合约',alert_count:items.length,latest:items[0],basis_exchange:items[0].basis_exchange,value_ranges:Object.fromEntries(['open_spread','basis'].map(field=>{const values=items.map(item=>field==='basis'?item.binance_basis:item[field]).filter(value=>value!==null);return [field,{max:values.length?Math.max(...values):null,min:values.length?Math.min(...values):null}]}))}));
    const attention={tier:index<2?'candidate':index<4?'watch':'record',live:index<4,reason:index<2?'合成条件候选，非真实信号':index<4?'首档深度不足参考金额':'仅异动记录',notional:1000,net_edge:index<2?.48:null};
    paths.forEach(path=>path.attention=attention);timeline[0].attention=attention;timeline[0].audible=false;
    return {symbol,alert_count:timeline.length,latest:timeline[0],timeline,move_peaks,value_ranges,basis_exchange:'Binance',transfer_statuses,paths,attention};
  });
  const plans = symbols.slice(0,2).map((symbol,index) => ({id:index+1,symbol,direction:index?'short':'long',status:'open',horizon:'short',pnl:1.8,
    leverage:1,entry_price:1.23,current_price:1.25,stop_price:1.16,take_profit_1:1.35,take_profit_2:1.42,max_hold_minutes:60,thesis:'合成样本，用于检查长说明、多腿数值和详细记录的布局。'}));
  const user = {id:1,username:'preview_admin',role:'admin',active:true,account_level:'lv3',created_at:stamp,last_login_at:stamp,
    last_login_ip:'192.0.2.10',last_login_location:'示例地址 · 文档保留IP，不是真实用户',devices:{}};
  const profile = {symbol:'DEMO/USDT',name:'合成研究样本',status:'公开资料观察',risk_level:'高',official_facts:['这里只使用合成样本检查版式。','事实区与推演区保持分离。'],
    market_thesis:'此处是合成的推演说明，不是实际币种分析、投资建议或交易信号。',watch_rules:['保留原有数据字段和研究边界。'],sources:[{label:'示例来源',url:'https://example.com/'}]};
  const watch = {symbol:'DEMO/USDT',active:true,note:'合成布局样本',started_at:stamp,duration_seconds:5400,start_price:1.23,current_price:1.25,
    change_pct:1.63,high_price:1.28,high_change_pct:4.07,low_price:1.2,low_change_pct:-2.44,last_push_at:stamp};
  const onchain = symbols.map((symbol,index) => ({token_symbol:symbol.split('/')[0],token_name:'合成链上布局样本',quote_symbol:'USDT',token_address:'0x1234567890abcdef1234567890abcdef12345678',
    network:['bsc','solana','base','eth'][index],chain_label:['BSC','Solana','Base','Ethereum'][index],dex_id:'preview-dex',pair_created_at:stamp,captured_at:stamp,
    price_usd:index===0?'0.0000000001234567890123456789':'1.23456789',opportunity_score:82-index*6,risk_level:index?'注意风险':'极高',score_components:{liquidity:22,activity:20,buy_pressure:12,momentum:28},
    price_change_5m:3.2,price_change_1h:7.8,price_change_6h:13.4,price_change_24h:22.1,liquidity_usd:4500000,volume_24h_usd:2700000,turnover:0.6,buy_ratio:63,source_url:'https://example.com/pool'}));
  const fixtures = {
    '/api/equity-arbitrage':{items:[],page:1,pages:1,total:0,available:{spot_futures:0,futures_futures:0},captured_at_epoch:now(),execution_enabled:false},
    '/api/auth/me':{user_id:1,username:'preview_admin',is_admin:true,account_level:'lv3',csrf_token:'synthetic-only',device_lock_required:false,chat_nav_hidden:false},
    '/api/market-metadata/delisted-symbols':{symbols:[]},
    '/api/spot-futures':{symbols:spot,errors:{},updated_at:'10:30:00',captured_at_epoch:now(),total_symbols:spot.length,page:1,pages:1,next_refresh_in_seconds:5},
    '/api/dual-futures':{symbols:dual,errors:{},updated_at:'10:30:00',captured_at_epoch:now(),total_symbols:dual.length,page:1,pages:1,include_bybit_okx:false,next_refresh_in_seconds:5},
    '/api/arbitrage-thinking/funding-trend':{items:funding,updated_at:stamp},
    '/api/arbitrage-thinking/simple':{spot_simple:simple,dual_simple:simple.map(item=>({...item,long_exchange:'Bybit',long_is_spot:false,long_interval:4,long_open_interest:12000000}))},
    '/api/onchain-opportunities':{items:onchain,updated_at:stamp,stale:false,chains:[{id:'all',label:'全部',count:4},{id:'bsc',label:'BSC',count:1},{id:'solana',label:'Solana',count:1},{id:'base',label:'Base',count:1},{id:'eth',label:'Ethereum',count:1}]},
    '/api/alerts':{checked_at_epoch:now(),realtime:{running:true,paths:1200,fresh_paths:1137},active_events:[alertGroups[0].latest],events:alertGroups,isolated_paths:[isolatedRisk],basis_tracking:[{symbol:'DEMO/USDT',strategy:'spot_futures',max_basis:1.4,last_level:1.4,max_at:stamp}]},
    '/api/hedge-positions':{checked_at_epoch:now(),execution:{enabled:false,reason:'隔离布局样本；真实下单未启用。'},summary:{current_count:1,history_count:1,spot_notional:100,futures_notional:100,hedge_gap_usdt:0,funding_income:0.35,fees:0.12,history_net_result:1.8},
      current:[{id:1,symbol:'DEMO/USDT',spot_exchange:'Gate',futures_exchange:'Binance',status:'open',status_label:'合成持仓',spot_quantity:80,futures_quantity:80,spot_entry_price:1.23,futures_entry_price:1.24,spot_notional:98.4,futures_notional:99.2,hedge_gap_usdt:0.8,opening_spread:0.81,funding_income:0.35,spot_fee:0.05,futures_fee:0.04,spot_order_id:'PREVIEW-SPOT-ORDER-LONG-IDENTIFIER',futures_order_id:'PREVIEW-FUTURES-ORDER-LONG-IDENTIFIER',opened_at:stamp}],
      history:[{id:2,symbol:'SAMPLE/USDT',status:'closed',status_label:'合成已平仓',spot_exchange:'Gate',futures_exchange:'Binance',net_result:1.8,closed_at:stamp}]},
    '/api/trade-validation':{plans,summary:{}},
    '/api/push-signal-validation':{rule_note:'合成统计，不代表真实胜率。',summary:[{category_label:'5MIN急涨',subtype_label:'点火',win_rate:50,decided:2,wins:1,losses:1,neutral:0,pending:1,insufficient_data:0}],items:[],automation:{last_success_at:stamp}},
    '/api/token-hedge/profiles':{profiles:[profile,{...profile,symbol:'SAMPLE/USDT',name:'另一个合成研究样本'}]},
    '/api/strategies':[{id:1,name:'价差监控合成样本',enabled:true,mode:'现多期空',symbol:'DEMO/USDT'},{id:2,name:'跨所监控合成样本',enabled:false,mode:'期多期空',symbol:'SAMPLE/USDT'}],
    '/api/runtime-rules':{is_admin:true,items:[
      {key:'spot_market_refresh',label:'现多期空刷新',category:'行情采集',description:'保持真实运行规则，只在预览检查输入与保存按钮排版。',schedule_type:'interval',value:'5',unit:'秒',editable:true,enabled:true},
      {key:'dual_market_refresh',label:'期多期空刷新',category:'行情采集',description:'行情与持仓采用独立的更新周期。',schedule_type:'interval',value:'5',unit:'秒',editable:true,enabled:true},
      {key:'thought_watch_scan',label:'思路扫描周期',category:'趋势研究',description:'研究任务不能假装是实时盘口。',schedule_type:'interval',value:'300',unit:'秒',editable:true,enabled:true},
      {key:'chat_nav_hidden',label:'协作入口',category:'协作与隐私',description:'账号级显示规则；本预览不接受保存。',schedule_type:'toggle',value:'0',editable:true,enabled:true},
    ]},
    '/api/symbol-aliases':{items:[]},
    '/api/admin/invites':{items:[{prefix:'PREVIEW',created_at:stamp,expires_at:'2026-10-25 10:30:00',used:false,status:'available'}]},
    '/api/admin/users':{items:[user,{...user,id:2,username:'preview_viewer',role:'viewer',account_level:'lv1'}]},
    '/api/admin/security-alerts':{unread:0,items:[{username:'preview_viewer',read:true,device_kind_label:'电脑',created_at:stamp,attempted_device_id:'synthetic-device-fingerprint-long-identifier',client_label:'合成浏览器',source_ip:'192.0.2.20',source_location:'示例地址 · 文档保留IP'}]},
    '/api/chat/users':{unread_total:0,items:[{id:2,username:'preview_peer',display_name:'合成联系人',last_message:'仅用于阅读与键盘布局检查。',online:true,unread:0}]},
    '/api/chat/messages':{items:[{id:1,sender_id:2,recipient_id:1,body:'这是合成消息，不会连接真实聊天或保存数据。',created_at:stamp},{id:2,sender_id:1,recipient_id:2,body:'检查长文本换行、阅读区以及输入区域的布局。',created_at:stamp,read_by_peer:true,read_at:stamp}],has_more_before:false},
    '/api/chat/push/config':{available:false},
    '/api/daily-report/trends':{early_focus:[{symbol:'DEMO/USDT',signal_type:'early_focus',stage_number:0,stage_label:'启动前观察',price_change_5:4.2,oi_change_5:3.1,ratio_change_5:-1.4,cvd_change_5:2.6,volume_ratio:2.1,stage_reason:'合成结构样本，仅用于显示布局。'}],horn_30m:[{symbol:'SAMPLE/USDT',score:75,price_change:8.1,oi_change:5.2,ratio_change:-2.3,cvd_confirmed:true}],horn_4h:[],horn_continue:[],automation_status:[]},
    '/api/daily-report/listings':{events:[{exchange:'Binance',symbol:'DEMO/USDT',type:'上架',occurred_at:stamp,effective_at:'2026-10-20 12:00:00',title:'合成公告标题：核对发布时间、执行时间与完整原文链接的显示',source_url:'https://example.com/announcement'},{exchange:'Bybit',symbol:'SAMPLE/USDT',type:'下架',occurred_at:stamp,effective_time_status:'not_stated',title:'合成下架公告：未注明执行时刻',source_url:'https://example.com/announcement-2'}],automation_status:[]},
    '/api/thought-watchlist':{items:[watch,{...watch,symbol:'SAMPLE/USDT',active:false,stopped_at:stamp,stop_price:1.25}],updated_at:stamp},
    '/api/daily-report/thoughts':{items:[{symbol:'DEMO/USDT',side:'观察',trade_status:'合成研究',last:1.25,entry:1.23,thought_summary:'合成的思路摘要：明确区分当前证据、待验证条件与已经失效的判断。',thesis_win_rate:{rate:50,note:'仅合成样本'},user_mistakes:[],assistant_mistakes:[],take_profit:[],stop_loss:[],review_notes:[],source:'fixture'}]},
    '/api/gainers-losers':{rising:symbols.map((symbol,index)=>({symbol,score:80-index*6,score_label:'合成结构分',price:1.23456789,change:6.2-index,volume_24h:15700000})),falling:symbols.map((symbol,index)=>({symbol,score:40-index*4,price:0.0000123456789,change:-5.2-index,volume_24h:8700000}))},
  };
  fixtures['/api/dashboard']={market_updated_at_epoch:now(),market_stale:false,market_incomplete:false,summary:{best_spread:0.21,markets_scanned:9,active_strategies:1,mode:'合成预览'},
    majors:['BTC/USDT','ETH/USDT','SOL/USDT'].map((symbol,index)=>({symbol,price:[81600,2420,104][index],change_24h:1.5,funding_rate:0.01,basis:0.02,open_interest:14300000,long_short_ratio:1.73,quote_updated_at_epoch:now(),analysis_updated_at_epoch:now(),windows:{'30m':{price_change:0.6},'4h':{price_change:1.2}},forecast:{stance:'观察',forecast:'合成大盘样本，仅用于版式检查。',reason:'检查价格、结构时间与多项指标的层级。'}})),
    momentum_updated_at:stamp,momentum_opportunities:symbols.map(symbol=>({symbol,label:'合成结构',score:76,components:{price:22,open_interest:20,ratio:16,cvd:18},oi_change_4h:3,ratio_change_4h:-2,cvd_ratio_4h:4,reason:'持仓增加 · 人数比下降 · CVD主动买入'})),
    oi_market_cap_updated_at:stamp,oi_market_cap_coverage:4,oi_market_cap_opportunities:symbols.map(symbol=>({symbol,label:'合成持仓比',ratio:0.8,total_open_interest:14000000,market_cap:17500000,market_source:'CoinMarketCap',market_cap_rank:1000,exchange_oi:{Binance:9000000,Bybit:5000000}})),
    opportunities:symbols.slice(0,3).map(symbol=>({symbol,buy_price:1.23,sell_price:1.24,buy_exchange:'Binance',sell_exchange:'Bybit',spread:0.81,estimated_profit:0.69,funding:0.025,updated_at_epoch:now(),exchange_prices:{Binance:1.23,OKX:1.235,Bybit:1.24}}))};
  fixtures['/api/chat/messages/2']=fixtures['/api/chat/messages'];
  // Delisting cases are synthetic: past/future/unknown and more than one history page.
  const listingSample={symbol:'DEMO/USDT',matched_symbols:['DEMO/USDT'],type:'下架',source_kind:'official',announcement:true,market_scope:'合约',occurred_at:'2026-09-04 04:00 UTC+8',title:'合成下架公告，仅供本地版式验证',effective_time_status:'parsed'};
  const listingSamples=[
    {...listingSample,id:1,exchange:'Bitget',source_url:'https://www.bitget.com/support/articles/preview-demo',effective_at:'2026-09-08 15:00 UTC+8',effective_at_iso:'2026-09-08T15:00:00+08:00'},
    {...listingSample,id:2,exchange:'Gate',source_url:'https://www.gate.com/announcements/preview-demo',effective_at:'2027-10-20 15:00 UTC+8',effective_at_iso:'2027-10-20T15:00:00+08:00'},
    {...listingSample,id:3,exchange:'Bybit',source_url:'https://announcements.bybit.com/preview-demo',effective_time_status:'not_stated'},
    {...listingSample,id:4,symbol:'SAMPLE/USDT',matched_symbols:['SAMPLE/USDT'],exchange:'Binance',source_kind:'detected',announcement:false,market_scope:'USDT永续合约',effective_time_status:'pending'},
  ];
  fixtures['/api/market-metadata/delisted-symbols']={symbols:['DEMO/USDT','SAMPLE/USDT'],events:listingSamples,updated_at:new Date().toISOString()};
  fixtures['/api/daily-report/listings']={events:[...listingSamples,...Array.from({length:57},(_,index)=>({...listingSample,id:index+5,symbol:`TEST${index}/USDT`,matched_symbols:[],exchange:'Bitget',source_url:'https://www.bitget.com/support/articles/preview-only',effective_time_status:'not_stated'}))],automation_status:[]};
  window.fetch = async (input, options={}) => {
    const method = String(options.method || 'GET').toUpperCase();
    if (method !== 'GET') return Response.json({error:'隔离预览禁止写入'}, {status:405});
    const url = new URL(String(input), location.origin),path=url.pathname;
    if (!(path in fixtures)) return Response.json({error:`未定义的合成接口 ${path}`}, {status:404});
    const payload = structuredClone(fixtures[path]);
    if(path==='/api/spot-futures'){
      const key=url.searchParams.get('sort_by')||'open_spread',direction=url.searchParams.get('sort_direction')||'desc',sign=direction==='desc'?-1:1;
      const compare=(a,b)=>Number.isFinite(a)?Number.isFinite(b)?sign*(a-b):-1:Number.isFinite(b)?1:0;
      const query=(url.searchParams.get('symbol')||'').toUpperCase(),period=url.searchParams.get('funding_interval')||'all';
      let groups=payload.symbols.filter(group=>(!query||group.symbol.includes(query))&&(url.searchParams.get('binance_spot_only')!=='1'||group.rows.some(row=>row.long_exchange==='Binance'))&&(period==='all'||`${group.rows[0].funding_interval_hours}H`===period));
      if(['open_spread','close_spread'].includes(key))groups.forEach(group=>group.rows.sort((a,b)=>compare(a[key],b[key])));
      groups.sort((a,b)=>compare(a.rows[0][key],b.rows[0][key]));
      const pages=Math.max(1,Math.ceil(groups.length/30)),page=Math.min(pages,Number(url.searchParams.get('page'))||1);
      Object.assign(payload,{symbols:groups.slice((page-1)*30,page*30),total_symbols:groups.length,pages,page,sort_by:key,sort_direction:direction});
    }
    if(path==='/api/alerts'){
      if(url.searchParams.get('view')==='detail'){
        const symbol=url.searchParams.get('symbol'),strategy=url.searchParams.get('strategy')||null,path_id=url.searchParams.get('path')||null,group=alertGroups.find(item=>item.symbol===symbol),items=(group?.timeline||[]).filter(item=>(!strategy||item.strategy===strategy)&&(!path_id||item.path_id===path_id)),size=Math.min(100,Number(url.searchParams.get('page_size'))||40),pages=Math.max(1,Math.ceil(items.length/size)),page=Math.min(pages,Number(url.searchParams.get('page'))||1);
        return Response.json({symbol,strategy,path_id,items:items.slice((page-1)*size,page*size),total:items.length,page,pages,page_size:size,window_hours:24,checked_at_epoch:now()});
      }
      payload.events.forEach(group=>group.timeline=[]);payload.window_hours=24;payload.total_symbols=alertGroups.length;payload.total_alerts=alertGroups.reduce((sum,group)=>sum+group.alert_count,0);
    }
    if(path==='/api/daily-report/listings'){
      const search=(url.searchParams.get('symbol')||'').replace(/[/_-]|USDT$/g,'').toUpperCase(),range=url.searchParams.get('range')||'30d',page=Number(url.searchParams.get('page'))||1,size=Number(url.searchParams.get('page_size'))||50;
      let items=payload.events.filter(item=>!search||item.symbol.includes(search));if(range!=='all')items=items.filter(item=>item.id===4);
      payload.total=items.length;payload.pages=Math.max(1,Math.ceil(items.length/size));payload.page=Math.min(page,payload.pages);payload.events=items.slice((payload.page-1)*size,payload.page*size);payload.range=range;
    }
    if (payload && !Array.isArray(payload)) {
      if ('checked_at_epoch' in payload) payload.checked_at_epoch=now();
      if ('captured_at_epoch' in payload) payload.captured_at_epoch=now();
      if ('market_updated_at_epoch' in payload) payload.market_updated_at_epoch=now();
    }
    return Response.json(payload);
  };
})();
