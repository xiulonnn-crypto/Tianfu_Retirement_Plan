export const fmtUSD=(n,d=0)=>n==null?'—':new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:d,minimumFractionDigits:d}).format(n);
export const fmtCNY=n=>n==null?'—':new Intl.NumberFormat('zh-CN',{style:'currency',currency:'CNY',maximumFractionDigits:0}).format(n);
export const pct=n=>n==null?'—':`${(100*n).toFixed(2)}%`;
export const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let staticSnapshot;
async function pagesSnapshot(){
  if(!staticSnapshot){
    const url=new URL('../assets/overview-snapshot.json',import.meta.url);
    const response=await fetch(url);
    if(!response.ok)throw Error(`云端概览快照: ${response.status}`);
    staticSnapshot=await response.json();
  }
  return staticSnapshot;
}

export const API=async p=>{
  try{
    const response=await fetch(`/api/${p}`);
    if(!response.ok)throw Error(`${p}: ${response.status}`);
    return response.json();
  }catch(apiError){
    const route=p.split('?')[0];
    const snapshot=await pagesSnapshot();
    if(snapshot[route])return snapshot[route];
    throw apiError;
  }
};
export const card=(label,value,sub='',gold=false)=>`<div class="card"><div class="label">${label}</div><div class="value ${gold?'gold':''}">${value}</div><div class="sub">${sub}</div></div>`;
export const blank=message=>`<div class="card empty"><strong>历史收益待回填</strong>${message||'请先连接 Theta Terminal，并运行行情回填与模拟重放。'}</div>`;
export const pair=(a,b)=>`<div class="stat"><span>${a}</span><b>${b}</b></div>`;
export const formatGap=gap=>{
  const category={option_selection:'期权合约筛选',option_quote:'持仓期权报价',option_mark:'期权日终盯市',option_delta:'期权 Delta 计算',fx:'美元兑人民币汇率',violation:'策略约束'}[gap.category]||gap.category;
  const detail=String(gap.detail||'')
    .replace(/^P\s+(\d+)-(\d+)\s+DTE: no valid priced contract;\s*(\d+)\s+invalid$/,'看跌期权（$1–$2 天到期）：未找到可用于模型 Delta 的有效日终报价；$3 条候选合约模型无效')
    .replace(/^C\s+(\d+)-(\d+)\s+DTE: no valid priced contract;\s*(\d+)\s+invalid$/,'看涨期权（$1–$2 天到期）：未找到可用于模型 Delta 的有效日终报价；$3 条候选合约模型无效')
    .replace('USD/CNY missing or older than 7 days','美元兑人民币汇率缺失或已超过 7 天')
    .replace('insufficient funds to close','平仓资金不足')
    .replace('assignment insufficient funds','接货资金不足');
  return `${category}：${detail}`;
};
