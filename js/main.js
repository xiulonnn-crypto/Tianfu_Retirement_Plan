import {API,esc,formatGap} from './common.js';
import {overview,toggleDrawdown} from './tabs/returns.js';
import {allocation} from './tabs/allocation.js';
import {trades,movePage,setContractFilter,setDisplayMode} from './tabs/history.js';
import {review,setReviewPeriod,toggleReviewDrawdown,moveBacktestTradePage,jumpBacktestTradePage,setBacktestTradeView,setBacktestEventCategory,refreshModelTrades} from './tabs/review.js';

const pages={overview,allocation,trades,review};const titles={overview:'收益概览',allocation:'资产配置',trades:'交易历史',review:'策略复盘'};
async function render(){const page=(location.hash.slice(1)||'overview');const selected=pages[page]?page:'overview';document.title=`${titles[selected]} · 天府退休计划`;document.querySelector('#page-title').textContent=titles[selected];document.querySelectorAll('[data-page]').forEach(a=>a.classList.toggle('active',a.dataset.page===selected));const content=document.querySelector('#content');content.innerHTML='<div class="card empty">正在读取账本…</div>';try{const [html,health]=await Promise.all([pages[selected](),API('data-health')]);content.innerHTML=html;document.querySelector('#as-of').textContent=health.as_of?`模拟截至 ${health.as_of}`:'模拟待回填';const banner=document.querySelector('#health-banner');banner.classList.toggle('good',!!health.as_of&&!health.stale&&!health.gaps.length);banner.textContent=health.stale&&health.market_as_of?`真实市场日价截至 ${health.market_as_of}；模拟账本截至 ${health.as_of||'待回填'}。缺少 SPY 历史期权报价，当前收益不可用。`:!health.as_of?'历史期权报价待回填；当前不展示模拟收益。':health.gaps.length?`行情数据缺口：${health.gaps[0].date} ${formatGap(health.gaps[0])}`:`已完成行情回放，基准日 ${health.as_of}。`;}catch(e){content.innerHTML=`<div class="card empty"><strong>读取失败</strong>${esc(e.message)}</div>`}}
window.goPage=n=>{movePage(n);render()};
window.filterTrades=()=>{setContractFilter(document.querySelector('#contract-filter').value);render()};
window.clearTradeFilter=()=>{setContractFilter('');render()};
window.setTradeMode=value=>{setDisplayMode(value);render()};
window.toggleOverviewDrawdown=()=>{toggleDrawdown();render()};
window.setReviewPeriod=years=>{setReviewPeriod(years);render()};
window.toggleReviewDrawdown=()=>{toggleReviewDrawdown();render()};
function refreshBacktestTrades(options){refreshModelTrades(options).then(updated=>{if(!updated)render()}).catch(()=>render())}
window.setBacktestTradePage=step=>{moveBacktestTradePage(step);refreshBacktestTrades({anchor:true})};
window.jumpBacktestTradePage=page=>{jumpBacktestTradePage(page);refreshBacktestTrades({anchor:true})};
window.setBacktestTradeView=view=>{setBacktestTradeView(view);refreshBacktestTrades({preserveScroll:true})};
window.setBacktestEventCategory=category=>{setBacktestEventCategory(category);refreshBacktestTrades({preserveScroll:true})};
window.addEventListener('hashchange',render);render();
