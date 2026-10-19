import {esc} from '../common.js';

function smoothPath(points){
  if(points.length===1)return `M ${points[0][0]} ${points[0][1]}`;
  let path=`M ${points[0][0]} ${points[0][1]}`;
  for(let index=0;index<points.length-1;index++){
    const [x0,y0]=points[index?index-1:index], [x1,y1]=points[index], [x2,y2]=points[index+1], [x3,y3]=points[index+2]||points[index+1];
    path+=` C ${x1+(x2-x0)/6} ${y1+(y2-y0)/6}, ${x2-(x3-x1)/6} ${y2-(y3-y1)/6}, ${x2} ${y2}`;
  }
  return path;
}

function valueLabel(value,percentage){
  if(percentage){const percent=value*100;return `${Number.isInteger(percent)?percent:percent.toFixed(1)}%`;}
  const magnitude=Math.abs(value);
  if(magnitude>=1000)return Math.round(value).toLocaleString('en-US');
  if(magnitude>=100||Number.isInteger(value))return Number(value).toFixed(0);
  return Number(value).toFixed(1);
}

function niceStep(value){
  const exponent=Math.floor(Math.log10(Math.max(value,Number.EPSILON)));
  const fraction=value/10**exponent;
  const unit=fraction<=1?1:fraction<=2?2:fraction<=5?5:10;
  return unit*10**exponent;
}

function niceScale(values){
  const low=Math.min(...values),high=Math.max(...values);
  if(low===high){
    const pad=Math.max(1,Math.abs(low)*.05);
    return {min:low-pad,max:high+pad,step:niceStep(pad/2)};
  }
  // ECharts/Chart.js-like tick selection: first add breathing room, then snap
  // to 1 / 2 / 5 × 10^n.  This avoids raw-data labels such as 248,903.
  const padding=(high-low)*.045;
  const step=niceStep((high-low+padding*2)/5);
  const min=Math.floor((low-padding)/step)*step;
  const max=Math.ceil((high+padding)/step)*step;
  return {min,max,step};
}

/** A light-card, smooth-line SVG matching the reference TWR chart treatment. */
export function trendChart(rows,series,{percentage=false,ariaLabel='趋势图'}={}){
  if(!rows?.length||rows.length<2)return '<p class="note">至少需要两个完整交易日才可绘制曲线。</p>';
  const width=900,height=320,pad={top:22,right:24,bottom:42,left:56};
  const values=series.flatMap(item=>rows.map(row=>Number(row[item.key]))).filter(Number.isFinite);
  const scale=niceScale(values),min=scale.min,max=scale.max;
  const spread=max-min;const x=index=>pad.left+index*(width-pad.left-pad.right)/(rows.length-1);
  const y=value=>pad.top+(max-value)*(height-pad.top-pad.bottom)/spread;
  // Match the reference TWR chart: light horizontal guides only, muted ticks,
  // and enough date labels for a long series without turning into a dense grid.
  const tickValues=[];
  for(let value=min;value<=max+scale.step*.001;value+=scale.step)tickValues.push(value);
  const grid=tickValues.reverse().map(value=>{
    const py=y(value);
    return `<g><line x1="${pad.left}" y1="${py}" x2="${width-pad.right}" y2="${py}"/><text x="${pad.left-9}" y="${py+4}" text-anchor="end">${valueLabel(value,percentage)}</text></g>`;
  }).join('');
  const tickCount=Math.min(8,Math.max(2,Math.floor((width-pad.left-pad.right)/112)+1));
  const tickIndexes=Array.from({length:tickCount},(_,index)=>Math.round(index*(rows.length-1)/(tickCount-1)))
    .filter((value,index,array)=>array.indexOf(value)===index);
  const ticks=tickIndexes.map(index=>`<text x="${x(index)}" y="${height-15}" text-anchor="middle">${esc(rows[index].date)}</text>`).join('');
  const paths=series.map((item,index)=>{
    const points=rows.map((row,rowIndex)=>[x(rowIndex),y(Number(row[item.key]))]);
    const line=smoothPath(points),fill=`${line} L ${points.at(-1)[0]} ${height-pad.bottom} L ${points[0][0]} ${height-pad.bottom} Z`;
    return `${item.fill===false?'':`<path class="trend-area" d="${fill}" fill="url(#trend-fill-${index})"/>`}<path class="trend-line" d="${line}" stroke="${item.color}" stroke-width="${item.width||2}" ${item.dash?`stroke-dasharray="${item.dash}"`:''}/>`;
  }).join('');
  const gradients=series.map((item,index)=>`<linearGradient id="trend-fill-${index}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${item.color}" stop-opacity="${index===0?.12:.045}"/><stop offset="1" stop-color="${item.color}" stop-opacity="0"/></linearGradient>`).join('');
  const legend=series.map(item=>`<span><i style="background:${item.color}"></i>${esc(item.label)}</span>`).join('');
  return `<div class="trend-card" role="img" aria-label="${esc(ariaLabel)}"><div class="trend-legend">${legend}</div><svg class="trend-chart" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none"><defs>${gradients}</defs><g class="trend-grid">${grid}</g><g class="trend-axis-line"><line x1="${pad.left}" y1="${pad.top}" x2="${pad.left}" y2="${height-pad.bottom}"/><line x1="${pad.left}" y1="${height-pad.bottom}" x2="${width-pad.right}" y2="${height-pad.bottom}"/></g><g class="trend-series">${paths}</g><g class="trend-axis">${ticks}</g></svg></div>`;
}
