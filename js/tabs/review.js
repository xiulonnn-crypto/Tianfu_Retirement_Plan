import {fmtCNY,fmtUSD,esc,API,card,blank,pair,pct} from '../common.js';
import {annualIncomeView,monthlyIncomeView} from '../components/monthly-income.js';
import {trendChart} from '../components/trend-chart.js';

let selectedYears=2;
let showDrawdown=false;
let tradePage=0;
let tradeView='orders';
let eventCategory='全部';

function metricCards(m){
  const volatility='年化波动 '+pct(m.annualized_volatility);
  const recovery='回撤恢复 '+(m.recovery_days==null?'未恢复':m.recovery_days+' 个交易日');
  const calmar='卡玛 '+(m.calmar_ratio==null?'—':Number(m.calmar_ratio).toFixed(2));
  const closed='已结算交易 '+m.trade_count;
  return `<div class="grid backtest-metrics">${card('累计收益',pct(m.total_return),'净值加累计提取')}${card('年化收益',pct(m.annualized_return),volatility)}${card('最大回撤',pct(m.max_drawdown),recovery)}${card('夏普比率',Number(m.sharpe_ratio).toFixed(2),calmar)}${card('胜率',pct(m.win_rate),closed)}${card('最终净值',fmtUSD(m.final_equity),'不含累计提取')}</div>`;
}

function eventsTable(items){
  if(!items.length)return '<p class="note">没有模型事件。</p>';
  return `<div class="table-wrap"><table class="table"><thead><tr><th>日期</th><th>分类</th><th>操作</th><th>合约 / 标的</th><th>数量</th><th>价格</th><th>现金变动</th><th>模型说明</th></tr></thead><tbody>${items.map(event=>`<tr><td>${esc(event.date)}</td><td><span class="badge">${esc(event.category)}</span></td><td>${esc(event.operation)}</td><td>${esc(event.contract||event.symbol||'—')}</td><td>${event.quantity==null?'—':Math.round(Number(event.quantity))}</td><td>${event.price==null?'—':fmtUSD(event.price,2)}</td><td>${fmtUSD(event.cash_change,2)}</td><td class="reason">${event.details?.model_quote?`CRR · σ ${(event.details.model_quote.sigma*100).toFixed(1)}% · Δ ${event.details.actual_delta==null?'—':Number(event.details.actual_delta).toFixed(3)}`:esc(event.reason||'—')}</td></tr>`).join('')}</tbody></table></div>`;
}

function orderTable(items){
  if(!items.length)return '<p class="note">没有模型订单。</p>';
  return `<div class="table-wrap"><table class="table contracts-table"><thead><tr><th>类型</th><th>合约 / 标的</th><th>开仓</th><th>平仓 / 状态</th><th>开仓数量</th><th>已平数量</th><th>已实现盈亏</th><th>账本</th></tr></thead><tbody>${items.map(item=>{const closed=item.status==='OPEN'?'持仓中':`${item.close_date} · ${item.close_kind}<br><small>${item.close_price==null?'—':fmtUSD(item.close_price,2)}</small>`;const pnl=item.realized_pnl_usd==null?'—':fmtUSD(item.realized_pnl_usd,2);return `<tr><td>${item.instrument==='OPTION'?'期权':'股票'}</td><td>${esc(item.contract||item.symbol)}</td><td>${esc(item.open_date)} · ${esc(item.open_kind)}<br><small>${item.open_price==null?'—':fmtUSD(item.open_price,2)}</small></td><td>${closed}</td><td>${item.open_quantity??'—'}</td><td>${item.closed_quantity??'—'}</td><td class="${item.realized_pnl_usd>=0?'profit':'loss'}">${pnl}</td><td>${item.event_count} 笔事件</td></tr>`}).join('')}</tbody></table></div>`;
}

function sourceNotice(d){const c=d.config;return `<div class="backtest-meta data-notice"><b>模型回测 · ${esc(c.start_date)} 至 ${esc(c.end_date)}</b><span>${esc(c.option_pricing)}。${esc(c.collateral_proxy)}；${esc(c.benchmark_proxy)}。数据指纹 ${esc(d.fingerprint.slice(0,16))}…</span></div>`;}

function tradeQuery(){
  const category=`&category=${encodeURIComponent(eventCategory)}`;
  return `backtests/${selectedYears}/trades?view=${tradeView}&limit=30&offset=${tradePage*30}${category}`;
}

function modelTradesView(trades){
  const switcher=`<div class="view-switch"><button class="button ${tradeView==='events'?'selected':''}" onclick="setBacktestTradeView('events')">按事件展示</button><button class="button ${tradeView==='orders'?'selected':''}" onclick="setBacktestTradeView('orders')">按订单展示</button></div>`;
  const filters=`<div class="event-filters">${['全部','卖PUT','Covered CALL','接货','保险','现金管理','分红'].map(category=>`<button class="text-button ${eventCategory===category?'selected':''}" onclick="setBacktestEventCategory('${category}')">${category}</button>`).join('')}</div>`;
  const title=tradeView==='orders'?'模型订单':'模型交易事件';
  const note=tradeView==='orders'?'每行汇总一笔期权合约或股票持仓批次的开仓、平仓和已实现盈亏。股票按先进先出匹配。':'事件按卖PUT、Covered CALL、接货、保险、现金管理和分红分类，并标记买入、卖出或无操作。';
  const lastPage=Math.max(0,Math.ceil(trades.total/30)-1);
  return `<section id="model-trades" class="card section"><div class="section-heading"><h2>${title}</h2><span>${trades.total} 条 · ${note}</span></div>${switcher}${filters}${tradeView==='orders'?orderTable(trades.items):eventsTable(trades.items)}<div class="pager"><button class="button" ${tradePage===0?'disabled':''} onclick="jumpBacktestTradePage(0)">最前页</button><button class="button" ${tradePage===0?'disabled':''} onclick="setBacktestTradePage(-1)">上一页</button><span>第 ${tradePage+1} / ${lastPage+1} 页</span><button class="button" ${tradePage===lastPage?'disabled':''} onclick="setBacktestTradePage(1)">下一页</button><button class="button" ${tradePage===lastPage?'disabled':''} onclick="jumpBacktestTradePage(${lastPage})">最后页</button></div></section>`;
}

export async function refreshModelTrades({anchor=false,preserveScroll=false}={}){
  const previousTop=window.scrollY;
  const target=document.querySelector('#model-trades');
  if(!target)return false;
  const trades=await API(tradeQuery());
  target.outerHTML=modelTradesView(trades);
  if(anchor)document.querySelector('#model-trades')?.scrollIntoView({block:'start'});
  else if(preserveScroll)window.scrollTo(0,previousTop);
  return true;
}

export async function review(){
  let data;try{data=await API(`backtests/${selectedYears}`)}catch(error){return blank('模型回测尚未生成。运行 python3 manage.py backtest 后查看完整 2 年和 10 年结果。')}
  const trades=await API(tradeQuery());
  const m=data.metrics,counts=data.event_counts,attribution=data.attribution;
  const chart=showDrawdown?trendChart(data.drawdowns,[{key:'strategy',label:'策略回撤',color:'#D64545'},{key:'benchmark',label:'QQQ / QQQI 基准回撤',color:'#8A9199',dash:'5 4',fill:false}],{percentage:true,ariaLabel:`${selectedYears} 年回测回撤走势`}):trendChart(data.series,[{key:'strategy',label:'天府退休计划',color:'#4A3D7C'},{key:'benchmark',label:'QQQ / QQQI 总回报基准',color:'#8A9199',dash:'5 4',fill:false}],{ariaLabel:`${selectedYears} 年回测累计总回报走势`});
  const incomeView=selectedYears===10?annualIncomeView(data.annual||[]):monthlyIncomeView(data.monthly||[]);
  return `<section class="review-hero"><div><p class="eyebrow">HISTORICAL MODEL BACKTEST</p><h2>天府退休计划 v1.4 回测</h2><p>真实历史市场输入驱动的确定性 CRR 模型回测；不等同于历史期权成交。</p></div><span class="pill">${selectedYears} 年完整结果</span></section><div class="period-tabs"><button class="button ${selectedYears===2?'selected':''}" onclick="setReviewPeriod(2)">最近 2 年</button><button class="button ${selectedYears===10?'selected':''}" onclick="setReviewPeriod(10)">最近 10 年</button></div>${sourceNotice(data)}${metricCards(m)}<div class="grid two section"><div class="card"><div class="section-heading"><h2>${showDrawdown?'回撤走势':'累计总回报'}</h2><button class="button" onclick="toggleReviewDrawdown()">${showDrawdown?'查看收益走势':'查看回撤走势'}</button></div>${chart}<p class="note">曲线首日统一设为 100。基准在 2024-01-29 前使用 QQQ 总回报代理，之后使用 QQQI 实际总回报。</p></div><div class="card"><h2>回测现金流与归因</h2><div class="stat-list">${pair('累计卖 PUT 收益',fmtUSD(attribution.sell_put_income_usd),'已结算期权净现金流')}${pair('累计 Covered Call 收益',fmtUSD(attribution.covered_call_income_usd),'已结算期权净现金流')}${pair('累计接货收益',fmtUSD(attribution.assignment_income_usd),'接货 SPY 被 CALL 交割后的股票损益')}${pair('累计保险收益',fmtUSD(attribution.hedge_income_usd),'已结算保险期权净现金流')}${pair('累计提取（美元）',fmtUSD(m.total_distributions_usd))}${pair('累计支付（人民币）',fmtCNY(m.total_distributions_cny))}</div></div></div>${incomeView}${modelTradesView(trades)}`;
}

export function setReviewPeriod(years){selectedYears=years;tradePage=0;showDrawdown=false}
export function toggleReviewDrawdown(){showDrawdown=!showDrawdown}
export function moveBacktestTradePage(step){tradePage=Math.max(0,tradePage+step)}
export function jumpBacktestTradePage(page){tradePage=Math.max(0,page)}
export function setBacktestTradeView(view){tradeView=view==='orders'?'orders':'events';tradePage=0}
export function setBacktestEventCategory(category){eventCategory=category;tradePage=0}
