import {fmtUSD,esc,pct} from '../common.js';

export const incomeKeys=['sell_put_income_usd','covered_call_income_usd','assignment_income_usd','hedge_income_usd'];
const incomeLabels=['卖 PUT','Covered Call','接货','保险'];
const incomeColors=['#4a3d7c','#bfa960','#2d8a5e','#bd6a8c'];

function incomeChart(rows,labelKey){
  if(!rows?.length)return '<p class="note">暂无月度收益数据。</p>';
  const totals=rows.map(row=>incomeKeys.reduce((sum,key)=>sum+Number(row[key]||0),0));
  const max=Math.max(1,...totals.filter(value=>value>0));const min=Math.min(-1,...totals.filter(value=>value<0));
  const w=820,h=240,p=30,zero=h-p-(0-min)*(h-2*p)/(max-min),width=(w-2*p)/rows.length*.72;
  const bars=rows.map((row,index)=>{let positive=0,negative=0;const x=p+index*(w-2*p)/rows.length+(w-2*p)/rows.length*.14;return incomeKeys.map((key,keyIndex)=>{const value=Number(row[key]||0);const height=Math.abs(value)*(h-2*p)/(max-min);const y=value>=0?zero-positive-height:zero+negative;value>=0?positive+=height:negative+=height;return height<.2?'':`<rect x="${x}" y="${y}" width="${width}" height="${height}" fill="${incomeColors[keyIndex]}"><title>${esc(row[labelKey])} ${incomeLabels[keyIndex]} ${fmtUSD(value,2)}</title></rect>`}).join('')+((rows.length<=12||index%3===0||index===rows.length-1)?`<text x="${x}" y="${h-7}">${esc(labelKey==='month'?row[labelKey].slice(2):row[labelKey])}</text>`:'')}).join('');
  return `<svg class="chart monthly-income-chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><line x1="${p}" y1="${zero}" x2="${w-p}" y2="${zero}"/>${bars}</svg><div class="market-legend">${incomeLabels.map((label,index)=>`<span><i style="background:${incomeColors[index]}"></i>${label}</span>`).join('')}</div>`;
}

export function monthlyIncomeView(rows){
  return incomeView(rows,{title:'月收益视图',labelKey:'month',label:'月份',returnKey:'monthly_return',note:'柱状图为已结算交易归因；收益与提取均按账本月度增量统计，差额为未分配利润或欠额补发'});
}

export function annualIncomeView(rows){
  return incomeView(rows,{title:'年收益视图',labelKey:'year',label:'年份',returnKey:'annual_return',note:'十年回测按自然年汇总；柱状图为已结算交易归因，收益与提取均为账本年度增量'});
}

function incomeView(rows,{title,labelKey,label,returnKey,note}){
  return `<section class="card section"><div class="section-heading"><h2>${title}</h2><span>${note}</span></div>${incomeChart(rows,labelKey)}<div class="table-wrap"><table class="table monthly-table"><thead><tr><th>${label}</th><th>卖 PUT</th><th>Covered Call</th><th>接货</th><th>保险</th><th>已实现策略收益</th><th>${label}提取额</th><th>总财富${label}收益率</th></tr></thead><tbody>${rows.map(row=>`<tr><td>${esc(row[labelKey])}</td>${incomeKeys.map(key=>`<td class="${row[key]>=0?'profit':'loss'}">${fmtUSD(row[key],2)}</td>`).join('')}<td class="${row.realized_strategy_income_usd>=0?'profit':'loss'}">${fmtUSD(row.realized_strategy_income_usd,2)}</td><td>${fmtUSD(row.withdrawals_usd,2)}</td><td>${row[returnKey]==null?'—':pct(row[returnKey])}</td></tr>`).join('')}</tbody></table></div></section>`;
}
