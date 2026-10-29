import {fmtUSD,fmtCNY,pct,esc,API,card,blank,pair} from '../common.js';
import {monthlyIncomeView} from '../components/monthly-income.js';
import {trendChart} from '../components/trend-chart.js';

let showDrawdown=false;

function comparisonPanel(comparison,monthly){
  if(comparison.status!=='ready')return `<div class="card section"><h2>策略与 QQQI 对比</h2><p class="note">QQQI 日价尚未归档；运行 <code>python3 manage.py benchmark --start 2024-01-01</code> 后启用总回报比较。</p></div>${monthlyIncomeView(monthly||[])}`;
  const drawdown=comparison.drawdowns.map(row=>({date:row.date,strategy:row.strategy,benchmark:row.benchmark}));
  const ddPanel=showDrawdown?`<div class="card section"><div class="section-heading"><h2>回撤走势</h2><span>策略与 QQQI 总回报指数的峰值回撤</span></div>${trendChart(drawdown,[{key:'strategy',label:'策略回撤',color:'#D64545'},{key:'benchmark',label:'QQQI 回撤',color:'#8A9199',dash:'5 4',fill:false}],{percentage:true,ariaLabel:'策略与 QQQI 回撤走势'})}</div>`:'';
  return `<section class="section"><div class="section-heading"><h2>策略对比 · QQQI 基准</h2><span>${esc(comparison.start_date)} 至 ${esc(comparison.end_date)} · 同起点、同美元提取路径</span></div><div class="grid kpis">${card('策略总回报',pct(comparison.strategy_return),'期末净值 + 累计已提取')}${card('QQQI 总回报',pct(comparison.benchmark_return),'期末净值 + 同额已提取')}${card('超额收益',pct(comparison.excess_return),'策略回报减 QQQI 回报（百分点）')}${card('最大回撤',pct(comparison.strategy_max_drawdown),`QQQI ${pct(comparison.benchmark_max_drawdown)}`)}</div><div class="card section"><div class="section-heading"><h2>累计总回报走势</h2><button class="button" onclick="toggleOverviewDrawdown()">${showDrawdown?'收起回撤走势':'查看回撤走势'}</button></div>${trendChart(comparison.series,[{key:'strategy',label:'天府退休计划',color:'#4A3D7C'},{key:'benchmark',label:'QQQI 基准',color:'#8A9199',dash:'5 4',fill:false}],{ariaLabel:'天府退休计划与 QQQI 累计总回报走势'})}<p class="note">两条曲线均以区间首日 100 为基准，并统一按“期末净值 + 累计已提取”计算。QQQI 账户按相同美元提取路径模拟，余资按价格与分红再投资滚动。</p></div>${monthlyIncomeView(monthly||[])}${ddPanel}</section>`;
}

function marketPanel(m){
  if(!m.market_as_of)return '';
  const s=m.snapshot||{};
  return `<section class="section"><div class="section-heading"><h2>市场价格</h2><span>截至 ${esc(m.market_as_of)} · 独立于模拟账本</span></div><div class="grid kpis">${card('BOXX',fmtUSD(s.BOXX?.close,2),'Yahoo Finance 收盘价')}${card('SPY',fmtUSD(s.SPY?.close,2),'Yahoo Finance 收盘价')}${card('QQQI',fmtUSD(s.QQQI?.close,2),'总回报比较基准')}${card('VIX',s.VIX?.close?.toFixed(2),'Cboe 历史收盘价')}</div></section>`;
}

function navChart(rows){return trendChart(rows.map(x=>({date:x.date,strategy:x.nav+x.distributions_usd})),[{key:'strategy',label:'账户总财富',color:'#4A3D7C'}],{ariaLabel:'账户总财富走势'})}

export async function overview(){
  const [d,m]=await Promise.all([API('overview'),API('market')]);
  if(!d.latest)return blank();
  const x=d.latest,income=d.option_income||{};
  return `<div class="section-heading section"><h2>模拟账户 · 截至 ${esc(x.date)}</h2><span>25 万美元初始资金 · 日终报价模拟</span></div><div class="grid kpis">${card('最近完整模拟净值',fmtUSD(x.nav),'初始资金 '+fmtUSD(d.initial_capital),true)}${card('剔除提取影响收益率',pct(x.adjusted_return),'截至模拟基准日')}${card('当日回撤',pct(x.drawdown),'按提取调整后的净值')}${card('累计已支付',fmtCNY(x.distributions_cny),'累计欠额 '+fmtCNY(x.arrears_cny))}</div><div class="grid two section"><div class="card"><h2>净值走势</h2>${navChart(d.curve)}<div class="legend">日终净值加累计已提取金额（美元）</div></div><div class="card"><h2>账户现金流</h2><div class="stat-list">${pair('累计已实现净利润',fmtUSD(x.realized_profit))}${pair('累计卖 PUT 收益',fmtUSD(income.sell_put_income_usd),`已结束合约 ${income.sell_put_contracts||0} 笔`)}${pair('累计卖 CALL 收益',fmtUSD(income.sell_call_income_usd),`已结束合约 ${income.sell_call_contracts||0} 笔`)}${pair('累计已提取美元',fmtUSD(x.distributions_usd))}${pair('累计已支付人民币',fmtCNY(x.distributions_cny))}${pair('待支付人民币',fmtCNY(x.arrears_cny))}</div></div></div>${comparisonPanel(d.comparison||{},d.monthly)}${marketPanel(m)}`;
}

export function toggleDrawdown(){showDrawdown=!showDrawdown}
