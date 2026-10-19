import {fmtUSD,esc,API,blank} from '../common.js';

let pageOffset=0;
let contractFilter='';
let displayMode='orders';

function quoteTrace(event){
  if(!event.quote_source)return '';
  const actual=event.details?.actual_delta==null?'—':Number(event.details.actual_delta).toFixed(3);
  const target=event.details?.target_delta==null?'':` / 目标 ${Number(event.details.target_delta).toFixed(3)}`;
  return `<br><small>${esc(event.quote_source)} · ${esc(event.quote_date)} · 模型 Delta ${actual}${target} · bid ${fmtUSD(event.quote_bid,2)} / ask ${fmtUSD(event.quote_ask,2)}</small>`;
}

function row(event){
  return `<tr><td>${esc(event.date)}</td><td><span class="badge ${esc(event.kind)}">${esc(event.kind)}</span></td><td>${esc(event.contract||event.symbol||'—')}</td><td>${event.quantity??'—'}</td><td>${event.price==null?'—':fmtUSD(event.price,2)}</td><td>${fmtUSD(event.cash_change,2)}</td><td class="reason">${esc(event.reason)}${quoteTrace(event)}</td></tr>`;
}

function orderRow(item){
  const pnl=item.realized_pnl_usd==null?'—':fmtUSD(item.realized_pnl_usd,2);
  const close=item.status==='OPEN'?'持仓中':`${item.close_date} · ${item.close_kind}<br><small>${item.close_price==null?'—':fmtUSD(item.close_price,2)}</small>`;
  return `<tr><td>${item.instrument==='OPTION'?'期权':'股票'}</td><td>${esc(item.contract||item.symbol)}</td><td>${esc(item.open_date)} · ${esc(item.open_kind)}<br><small>${item.open_price==null?'—':fmtUSD(item.open_price,2)}</small></td><td>${close}</td><td>${item.open_quantity??'—'}</td><td>${item.closed_quantity??'—'}</td><td class="${item.realized_pnl_usd>=0?'profit':'loss'}">${pnl}</td><td>${item.event_count} 笔事件</td></tr>`;
}

export async function trades(){
  const filter=contractFilter?`&contract=${encodeURIComponent(contractFilter)}`:'';
  const d=await API(`trades?limit=50&offset=${pageOffset}&view=${displayMode}${filter}`);
  if(!d.as_of)return blank();
  const range=d.total?`${pageOffset+1}–${Math.min(pageOffset+50,d.total)} / ${d.total}`:'无匹配记录';
  const modeSwitch=`<div class="view-switch"><button class="button ${displayMode==='orders'?'selected':''}" onclick="setTradeMode('orders')">按订单展示</button><button class="button ${displayMode==='events'?'selected':''}" onclick="setTradeMode('events')">按事件展示</button></div>`;
  const orders=`<table class="table"><thead><tr><th>日期</th><th>事件</th><th>标的 / 合约</th><th>数量</th><th>报价</th><th>现金变动</th><th>原因</th></tr></thead><tbody>${d.items.map(row).join('')}</tbody></table>`;
  const orderSummary=`<table class="table contracts-table"><thead><tr><th>类型</th><th>合约 / 标的</th><th>开仓</th><th>平仓 / 状态</th><th>开仓数量</th><th>已平数量</th><th>已实现盈亏</th><th>账本</th></tr></thead><tbody>${d.items.map(orderRow).join('')}</tbody></table>`;
  return `<div class="card"><h2>成交、指派与规则信号</h2>${modeSwitch}
    <div class="history-filter"><input id="contract-filter" value="${esc(contractFilter)}" placeholder="输入完整合约，例如 SPY:2026-02-06:C:719"><button class="button" onclick="filterTrades()">查看完整开平记录</button>${contractFilter?'<button class="button secondary" onclick="clearTradeFilter()">清除筛选</button>':''}</div>
    <p class="muted">${displayMode==='orders'?'每行汇总一笔期权合约或股票持仓批次的开仓、平仓和已实现盈亏。股票按先进先出匹配。':'事件保留成交、指派、提取、跳过信号及对应原始报价追溯。'}</p>
    <div class="table-wrap">${displayMode==='orders'?orderSummary:orders}</div>
    <div class="pager"><button class="button" onclick="goPage(-50)" ${pageOffset?'':'disabled'}>上一页</button><span>${range}</span><button class="button" onclick="goPage(50)" ${pageOffset+50<d.total?'':'disabled'}>下一页</button></div>
  </div>`;
}

export function movePage(n){pageOffset=Math.max(0,pageOffset+n)}
export function setContractFilter(value){contractFilter=value.trim();pageOffset=0}
export function setDisplayMode(value){displayMode=value==='events'?'events':'orders';pageOffset=0}
