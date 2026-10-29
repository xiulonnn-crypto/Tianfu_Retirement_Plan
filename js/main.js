import {API,esc,formatGap} from './common.js';
import {overview,toggleDrawdown} from './tabs/returns.js';
import {allocation} from './tabs/allocation.js';
import {trades,movePage,setContractFilter,setDisplayMode} from './tabs/history.js';
import {review,setReviewPeriod,toggleReviewDrawdown,moveBacktestTradePage,jumpBacktestTradePage,setBacktestTradeView,setBacktestEventCategory,refreshModelTrades} from './tabs/review.js';

const pages={overview,allocation,trades,review};const titles={overview:'收益概览',allocation:'资产配置',trades:'交易历史',review:'策略复盘'};
let simulationUpdateNotice=null;
if(!location.hash)history.replaceState(null,'',`${location.pathname}${location.search}#overview`);
async function render(){const page=(location.hash.slice(1)||'overview');const selected=pages[page]?page:'overview';document.title=`${titles[selected]} · 天府退休计划`;document.querySelector('#page-title').textContent=titles[selected];document.querySelectorAll('[data-page]').forEach(a=>a.classList.toggle('active',a.dataset.page===selected));const content=document.querySelector('#content');content.innerHTML='<div class="card empty">正在读取账本…</div>';try{const [html,health]=await Promise.all([pages[selected](),API('data-health')]);content.innerHTML=html;document.querySelector('#as-of').textContent=health.as_of?`模拟截至 ${health.as_of}`:'模拟待回填';const banner=document.querySelector('#health-banner');const normalMessage=health.stale&&health.market_as_of?`真实市场日价截至 ${health.market_as_of}；模拟账本截至 ${health.as_of||'待回填'}。缺少 SPY 历史期权报价，当前收益不可用。`:!health.as_of?'历史期权报价待回填；当前不展示模拟收益。':health.gaps.length?`行情数据缺口：${health.gaps[0].date} ${formatGap(health.gaps[0])}`:`已完成行情回放，基准日 ${health.as_of}。`;const notice=simulationUpdateNotice;banner.className=`notice${notice?.level==='success'||(!notice&&!!health.as_of&&!health.stale&&!health.gaps.length)?' good':''}${notice?.level==='warning'?' warning':''}`;banner.textContent=notice?notice.message:normalMessage;}catch(e){content.innerHTML=`<div class="card empty"><strong>读取失败</strong>${esc(e.message)}</div>`}}
window.goPage=n=>{movePage(n);render()};
window.filterTrades=()=>{setContractFilter(document.querySelector('#contract-filter').value);render()};
window.clearTradeFilter=()=>{setContractFilter('');render()};
window.setTradeMode=value=>{setDisplayMode(value);render()};
window.toggleOverviewDrawdown=()=>{toggleDrawdown();render()};
window.setReviewPeriod=years=>{setReviewPeriod(years);render()};
window.toggleReviewDrawdown=()=>{toggleReviewDrawdown();render()};
function refreshBacktestTrades(options){refreshModelTrades(options).then(updated=>{if(!updated)render()}).catch(()=>render())}
async function updateResponse(response){
  const body=await response.text();
  try{return JSON.parse(body)}catch(_error){
    const type=response.headers.get('content-type')||'未知类型';
    throw Error(`更新接口返回非 JSON 响应（HTTP ${response.status}，${type}）。请确认已重启本项目的本地服务。`);
  }
}
window.setBacktestTradePage=step=>{moveBacktestTradePage(step);refreshBacktestTrades({anchor:true})};
window.jumpBacktestTradePage=page=>{jumpBacktestTradePage(page);refreshBacktestTrades({anchor:true})};
window.setBacktestTradeView=view=>{setBacktestTradeView(view);refreshBacktestTrades({preserveScroll:true})};
window.setBacktestEventCategory=category=>{setBacktestEventCategory(category);refreshBacktestTrades({preserveScroll:true})};
window.updateSimulation=async()=>{
  const button=document.querySelector('#simulation-update');
  const status=document.querySelector('#simulation-update-status');
  if(!button||button.disabled)return;
  button.disabled=true;button.setAttribute('aria-busy','true');button.textContent='更新中…';
  status.className='simulation-update-status visible';status.textContent='正在启动并登录 Theta Terminal，然后读取最新行情并重放模拟账本…';
  try{
    const response=await fetch('/api/simulation/update',{method:'POST',headers:{Accept:'application/json'}});
    const payload=await updateResponse(response);
    if(!response.ok)throw Error(payload.detail||payload.error||`更新失败：${response.status}`);
    simulationUpdateNotice={message:payload.message,level:payload.message_level||'info'};
    status.className=`simulation-update-status visible ${payload.message_level||''}`;
    status.textContent=payload.status==='updated'||payload.status==='updated_with_warnings'?'模拟日期已刷新':payload.status==='current'?'模拟已是最新':'模拟尚未推进';
    await render();
  }catch(error){
    simulationUpdateNotice={message:`更新失败：${error.message}`,level:'warning'};
    status.className='simulation-update-status visible error';status.textContent=`更新失败：${error.message}`;
    await render();
  }finally{
    button.disabled=false;button.removeAttribute('aria-busy');button.textContent='更新模拟';
  }
};
window.addEventListener('hashchange',render);render();
