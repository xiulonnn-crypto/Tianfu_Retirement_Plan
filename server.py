import json
import os
import threading
import sys
from datetime import date,datetime,timedelta
from flask import Flask, jsonify, request, send_from_directory
from strategy import RULE_VERSION
from strategy.db import connect
from strategy.engine import replay
from strategy.calendar import latest_publishable_session
from strategy.providers import sync_all
from strategy.backtest import load_backtest

ROOT=os.path.dirname(os.path.abspath(__file__))
app=Flask(__name__,static_folder='assets')


def dbconn():return connect(app.config.get('DATABASE'))


def asdict(row):return dict(row) if row else None


OPTION_CASH_EVENTS={'SELL_PUT','SELL_CALL','BUY_HEDGE','TAKE_PROFIT','FORCE_CLOSE',
                    'ASSIGNMENT_DELEVER','HEDGE_EXIT','HEDGE_HARVEST'}
OPEN_EVENTS={'SELL_PUT','SELL_CALL','BUY_HEDGE'}
MODEL_OPTION_TERMINALS={'TAKE_PROFIT','FORCE_CLOSE','ASSIGNMENT_DELEVER','OPTION_EXPIRY','PUT_ASSIGN',
                        'CALL_ASSIGN','HEDGE_EXIT','HEDGE_HARVEST'}
MODEL_OPTION_CASH_EVENTS=OPTION_CASH_EVENTS|{'OPTION_EXPIRY'}


def model_order_rows(events):
    """Collapse model events into complete option and stock order lifecycles."""
    ordered=sorted((dict(event) for event in events),key=lambda event:(event['date'],event.get('id',0)))
    option_groups={}
    for event in ordered:
        if event.get('contract'):
            option_groups.setdefault(event['contract'],[]).append(event)
    rows=[]
    for contract,group in option_groups.items():
        opening=next((event for event in group if event['kind'] in OPEN_EVENTS),None)
        if not opening:continue
        closing=[event for event in group if event['kind'] in MODEL_OPTION_TERMINALS]
        latest=closing[-1] if closing else None
        pnl=sum(float(event.get('cash_change') or 0) for event in group if event['kind'] in MODEL_OPTION_CASH_EVENTS)
        rows.append({'instrument':'OPTION','order_id':contract,'symbol':opening.get('symbol') or 'SPY',
                     'contract':contract,'open_date':opening['date'],'open_kind':opening['kind'],
                     'open_quantity':opening.get('quantity'),'open_price':opening.get('price'),
                     'close_date':latest['date'] if latest else None,'close_kind':latest['kind'] if latest else None,
                     'close_price':latest.get('price') if latest else None,
                     'closed_quantity':sum(float(event.get('quantity') or 0) for event in closing),
                     'status':'CLOSED' if latest else 'OPEN','realized_pnl_usd':round(pnl,8),'event_count':len(group)})

    # PUT_ASSIGN/CALL_ASSIGN remain here for old archived runs.  New runs separate
    # the option assignment lifecycle from the corresponding stock transaction.
    stock_buy={'BUY_BOXX','PUT_ASSIGN','BUY_SPY_ASSIGNMENT'}
    stock_sell={'SELL_BOXX','CALL_ASSIGN','SELL_SPY_DELIVERY','LIQUIDATE_SPY'}
    lots_by_symbol={};registry=[]
    for event in ordered:
        kind=event['kind'];symbol=event.get('symbol')
        if kind not in stock_buy|stock_sell or not symbol:continue
        quantity=float(event.get('quantity') or 0)*(100 if kind in {'PUT_ASSIGN','CALL_ASSIGN'} else 1)
        if quantity<=0:continue
        lots=lots_by_symbol.setdefault(symbol,[])
        if kind in stock_buy:
            lot={'instrument':'STOCK','order_id':f"{symbol}:{event['event_key']}",'symbol':symbol,
                 'contract':None,'open_date':event['date'],'open_kind':kind,'open_quantity':quantity,
                 'open_price':event.get('price'),'open_cash_change':float(event.get('cash_change') or 0),
                 'closed_quantity':0.,'proceeds':0.,'close_events':[]}
            lots.append(lot);registry.append(lot);continue
        remaining=quantity
        while remaining>1e-9 and lots:
            lot=lots[0];taken=min(lot['open_quantity']-lot['closed_quantity'],remaining)
            lot['closed_quantity']+=taken;lot['proceeds']+=float(event.get('cash_change') or 0)*taken/quantity
            lot['close_events'].append((event['date'],kind,taken,event.get('price')));remaining-=taken
            if lot['open_quantity']-lot['closed_quantity']<=1e-9:lots.pop(0)
    for lot in registry:
        closes=lot.pop('close_events');closed=lot.pop('closed_quantity');proceeds=lot.pop('proceeds');cost=lot.pop('open_cash_change')
        lot.update({'close_date':closes[-1][0] if closes else None,
                    'close_kind':' / '.join(dict.fromkeys(event[1] for event in closes)) if closes else None,
                    'close_price':round(sum(float(event[3] or 0)*event[2] for event in closes)/closed,8) if closed else None,
                    'closed_quantity':closed,'status':'CLOSED' if closed>=lot['open_quantity']-1e-9 else 'OPEN',
                    'realized_pnl_usd':round(proceeds+cost*closed/lot['open_quantity'],8),'event_count':1+len(closes)})
        rows.append(lot)
    return sorted(rows,key=lambda row:(row['status']=='OPEN',row['open_date'] if row['status']=='OPEN' else row['close_date'],row['order_id']),reverse=True)


def model_order_category(order):
    """Classify an order from its opening event for the shared review filter."""
    kind=order['open_kind']
    if order['instrument']=='STOCK' and kind in {'PUT_ASSIGN','BUY_SPY_ASSIGNMENT'}:
        return '接货'
    return model_event_labels({'kind':kind,'contract':order.get('contract')})[0]


def model_backtest_attribution(nav, events):
    """Return realized strategy attribution and month-end cash flow for a model run."""
    categories={'sell_put_income_usd':('SELL_PUT','PUT'),
                'covered_call_income_usd':('SELL_CALL','CALL'),
                'assignment_income_usd':('PUT_ASSIGN','ASSIGNMENT'),
                'hedge_income_usd':('BUY_HEDGE','HEDGE')}
    totals={key:0. for key in categories}
    by_month={}
    previous_wealth=None;previous_distributions=0.;previous_realized=0.
    for row in nav:
        month=row['date'][:7]
        wealth=float(row['nav'])+float(row['distributions_usd'])
        if month not in by_month:
            by_month[month]={'month':month,'sell_put_income_usd':0.,'covered_call_income_usd':0.,
                              'assignment_income_usd':0.,'hedge_income_usd':0.,
                              'realized_strategy_income_usd':0.,'withdrawals_usd':0.,
                              'monthly_return':None,'_base_wealth':previous_wealth}
        item=by_month[month]
        item['withdrawals_usd']+=float(row['distributions_usd'])-previous_distributions
        realized=float(row.get('realized_profit',previous_realized))
        item['realized_strategy_income_usd']+=realized-previous_realized
        item['monthly_return']=None if item['_base_wealth'] is None else wealth/item['_base_wealth']-1
        previous_wealth=wealth;previous_distributions=float(row['distributions_usd']);previous_realized=realized
    for order in model_order_rows(events):
        if order['status']!='CLOSED' or not order['close_date']:
            continue
        if order['instrument']=='OPTION':
            source=order['open_kind']
        elif order['symbol']=='SPY' and order['open_kind'] in {'PUT_ASSIGN','BUY_SPY_ASSIGNMENT'}:
            source='PUT_ASSIGN'
        else:
            continue
        match=next((key for key,(kind,_) in categories.items() if source==kind),None)
        if not match:continue
        pnl=float(order['realized_pnl_usd'])
        totals[match]+=pnl
        month=order['close_date'][:7]
        if month in by_month:by_month[month][match]+=pnl
    monthly=[]
    for month,item in by_month.items():
        item.pop('_base_wealth')
        item['strategy_income_usd']=sum(item[key] for key in categories)
        monthly.append({key:round(value,8) if isinstance(value,float) else value for key,value in item.items()})
    totals={key:round(value,8) for key,value in totals.items()}
    return {'attribution':totals,'monthly':monthly,'annual':annual_income_view(monthly)}


def annual_income_view(monthly):
    """Aggregate a long model run without discarding its annual cash-flow audit."""
    income_keys=('sell_put_income_usd','covered_call_income_usd','assignment_income_usd','hedge_income_usd',
                 'realized_strategy_income_usd','withdrawals_usd')
    grouped={}
    for row in monthly:
        year=row['month'][:4]
        item=grouped.setdefault(year,{'year':year,**{key:0. for key in income_keys},'_growth':1.,'_has_return':False})
        for key in income_keys:item[key]+=float(row.get(key) or 0.)
        if row.get('monthly_return') is not None:
            item['_growth']*=1+float(row['monthly_return']);item['_has_return']=True
    result=[]
    for item in grouped.values():
        item['annual_return']=item['_growth']-1 if item.pop('_has_return') else None
        item.pop('_growth')
        result.append({key:round(value,8) if isinstance(value,float) else value for key,value in item.items()})
    return result


def model_event_labels(event):
    """Human-oriented category and cash-direction labels for model events."""
    kind=event['kind'];contract=(event.get('contract') or '').split(':')
    right=contract[2] if len(contract)>2 else None
    if kind=='SKIP' and '保险' in (event.get('reason') or ''):
        category='保险'
    elif kind in {'BUY_SPY_ASSIGNMENT','SELL_SPY_DELIVERY'}:
        category='接货'
    elif kind in {'BUY_HEDGE','HEDGE_EXIT','HEDGE_HARVEST'}:
        category='保险'
    elif kind in {'BUY_BOXX','SELL_BOXX','WITHDRAW','DEFER','SKIP'}:
        category='现金管理'
    elif kind=='DIVIDEND':
        category='分红'
    elif kind=='SELL_PUT' or (right=='P' and kind in {'TAKE_PROFIT','FORCE_CLOSE','ASSIGNMENT_DELEVER','OPTION_EXPIRY','PUT_ASSIGN'}):
        category='卖PUT'
    elif kind=='SELL_CALL' or (right=='C' and kind in {'TAKE_PROFIT','FORCE_CLOSE','ASSIGNMENT_DELEVER','OPTION_EXPIRY','CALL_ASSIGN'}):
        category='Covered CALL'
    else:
        category='现金管理'
    if kind in {'BUY_BOXX','BUY_HEDGE','BUY_SPY_ASSIGNMENT','TAKE_PROFIT','FORCE_CLOSE','ASSIGNMENT_DELEVER'}:
        operation='买入'
    elif kind in {'SELL_BOXX','SELL_PUT','SELL_CALL','SELL_SPY_DELIVERY','HEDGE_EXIT','HEDGE_HARVEST','LIQUIDATE_SPY'}:
        operation='卖出'
    else:
        operation='无'
    return category,operation


def contract_rows(events, open_contracts):
    """Collapse option ledger events into one auditable record per contract."""
    grouped={}
    for event in events:
        item=dict(event); contract=item.get('contract')
        if not contract:continue
        group=grouped.setdefault(contract,{'contract':contract,'events':[],'realized_pnl_usd':0.0})
        group['events'].append(item)
        if item['kind'] in OPTION_CASH_EVENTS:
            group['realized_pnl_usd']+=float(item['cash_change'] or 0)
    rows=[]
    for contract,group in grouped.items():
        events=group.pop('events'); opening=next((x for x in events if x['kind'] in OPEN_EVENTS),events[0])
        latest=events[-1]
        is_open=contract in open_contracts
        rows.append({**group,'symbol':opening.get('symbol') or 'SPY','right':contract.split(':')[2] if len(contract.split(':'))>2 else None,
                     'open_date':opening['date'],'open_kind':opening['kind'],'open_quantity':opening['quantity'],
                     'close_date':None if is_open else latest['date'],'close_kind':None if is_open else latest['kind'],
                     'status':'OPEN' if is_open else 'CLOSED','event_count':len(events),
                     'realized_pnl_usd':round(group['realized_pnl_usd'],8)})
    return sorted(rows,key=lambda x:(x['close_date'] or x['open_date'],x['contract']),reverse=True)


def indexed_total_return(rows):
    """Price plus cash distributions, rebased to 100 on the first day."""
    index=100.;previous=None;result=[]
    for row in rows:
        close=float(row['close']); dividend=float(row.get('dividend') or 0)
        if previous:index*=(close+dividend)/previous
        result.append({'date':row['date'],'value':round(index,8)})
        previous=close
    return result


def drawdowns(values):
    peak=None;result=[]
    for row in values:
        value=float(row['value']);peak=value if peak is None else max(peak,value)
        result.append({'date':row['date'],'value':round(value/peak-1,8)})
    return result


def model_backtest_payload(years, include_events=False):
    """Read a model run from its isolated database, never from the live ledger."""
    saved=load_backtest(years)
    if not saved:return None
    nav=saved['nav']; benchmark=saved['benchmark']
    if len(nav)<2 or len(benchmark)<2:return None
    bmap={row['date']:float(row['value']) for row in benchmark}
    common=[row for row in nav if row['date'] in bmap]
    if len(common)<2:return None
    first_wealth=float(common[0]['nav'])+float(common[0]['distributions_usd'])
    first_benchmark=bmap[common[0]['date']]
    series=[{'date':row['date'],
             'strategy':round((float(row['nav'])+float(row['distributions_usd']))/first_wealth*100,8),
             'benchmark':round(bmap[row['date']]/first_benchmark*100,8)} for row in common]
    strategy_dd=drawdowns([{'date':row['date'],'value':row['strategy']} for row in series])
    benchmark_dd=drawdowns([{'date':row['date'],'value':row['benchmark']} for row in series])
    counts={}
    for event in saved['events']:counts[event['kind']]=counts.get(event['kind'],0)+1
    attribution=model_backtest_attribution(nav,saved['events'])
    payload={'years':int(years),'mode':'MODEL_BACKTEST','label':'模型回测，不是历史期权成交记录',
             'config':saved['config'],'metrics':saved['metrics'],'fingerprint':saved['fingerprint'],
             'event_counts':counts,'series':series,
             **attribution,
             'drawdowns':[{'date':a['date'],'strategy':a['value'],'benchmark':b['value']}
                          for a,b in zip(strategy_dd,benchmark_dd)]}
    if include_events:payload['events']=saved['events']
    return payload


def strategy_benchmark_comparison(nav_rows, benchmark_rows):
    """Align strategy total wealth with QQQI total return on common dates."""
    if not nav_rows or not benchmark_rows:return {'symbol':'QQQI','status':'missing','series':[],'drawdowns':[]}
    benchmark={row['date']:row for row in indexed_total_return(benchmark_rows)}
    common=[row for row in nav_rows if row['date'] in benchmark]
    if len(common)<2:return {'symbol':'QQQI','status':'missing','series':[],'drawdowns':[]}
    start_wealth=float(common[0]['nav'])+float(common[0]['distributions_usd'])
    start_benchmark=float(benchmark[common[0]['date']]['value'])
    series=[]
    for row in common:
        wealth=float(row['nav'])+float(row['distributions_usd'])
        series.append({'date':row['date'],'strategy':round(wealth/start_wealth*100,8),
                       'benchmark':round(float(benchmark[row['date']]['value'])/start_benchmark*100,8)})
    strategy_dd=drawdowns([{'date':x['date'],'value':x['strategy']} for x in series])
    benchmark_dd=drawdowns([{'date':x['date'],'value':x['benchmark']} for x in series])
    return {'symbol':'QQQI','status':'ready','start_date':series[0]['date'],'end_date':series[-1]['date'],
            'series':series,'drawdowns':[{'date':s['date'],'strategy':s['value'],'benchmark':b['value']}
                                          for s,b in zip(strategy_dd,benchmark_dd)],
            'strategy_return':round(series[-1]['strategy']/100-1,8),
            'benchmark_return':round(series[-1]['benchmark']/100-1,8),
            'excess_return':round(series[-1]['strategy']/series[-1]['benchmark']-1,8),
            'strategy_max_drawdown':round(min(x['value'] for x in strategy_dd),8),
            'benchmark_max_drawdown':round(min(x['value'] for x in benchmark_dd),8)}


def backtest_windows(nav_rows, benchmark_rows=()):
    if not nav_rows:return []
    latest=nav_rows[-1]; end=date.fromisoformat(latest['date']); first=date.fromisoformat(nav_rows[0]['date'])
    achieved=(end-first).days/365.25
    benchmark_dates=[date.fromisoformat(x['date']) for x in benchmark_rows]
    result=[]
    for years in (2,10):
        cutoff=end-timedelta(days=round(years*365.25))
        complete=first<=cutoff
        window=[row for row in nav_rows if date.fromisoformat(row['date'])>=cutoff] if complete else []
        if complete and window:
            wealth=[float(row['nav'])+float(row['distributions_usd']) for row in window]
            peak=wealth[0]; drawdown=0.
            for value in wealth:
                peak=max(peak,value);drawdown=min(drawdown,value/peak-1)
            item={'years':years,'status':'complete','start_date':window[0]['date'],'end_date':latest['date'],
                  'return':wealth[-1]/wealth[0]-1,'max_drawdown':drawdown,'trading_days':len(window)}
        else:
            item={'years':years,'status':'collecting','start_date':nav_rows[0]['date'],'end_date':latest['date'],
                  'covered_years':achieved,'remaining_years':max(0.,years-achieved),'trading_days':len(nav_rows)}
        benchmark_window=[x for x in benchmark_rows if date.fromisoformat(x['date'])>=cutoff and date.fromisoformat(x['date'])<=end]
        if benchmark_dates and min(benchmark_dates)<=cutoff and benchmark_window:
            indexed=indexed_total_return(benchmark_window);dds=drawdowns(indexed)
            item.update({'benchmark_status':'complete','benchmark_return':indexed[-1]['value']/indexed[0]['value']-1,
                         'benchmark_max_drawdown':min(x['value'] for x in dds),'benchmark_start_date':indexed[0]['date']})
        else:item['benchmark_status']='collecting'
        result.append(item)
    return result


def option_income_summary(events, open_contracts):
    """Net option cash flow for completed short PUT/CALL contracts only."""
    totals={'sell_put_income_usd':0.0,'sell_call_income_usd':0.0,
            'sell_put_contracts':0,'sell_call_contracts':0}
    for row in contract_rows(events,open_contracts):
        if row['status']!='CLOSED':continue
        if row['open_kind']=='SELL_PUT':
            totals['sell_put_income_usd']+=row['realized_pnl_usd'];totals['sell_put_contracts']+=1
        elif row['open_kind']=='SELL_CALL':
            totals['sell_call_income_usd']+=row['realized_pnl_usd'];totals['sell_call_contracts']+=1
    return {key:round(value,8) if isinstance(value,float) else value for key,value in totals.items()}


def envelope(db,content):
    last=db.execute('SELECT date FROM daily_nav ORDER BY date DESC LIMIT 1').fetchone()
    return dict(content,as_of=last[0] if last else None,rule_version=RULE_VERSION,
                simulation='日终报价模拟',status='ready' if last else 'pending_backfill')


@app.get('/')
def index():return send_from_directory(ROOT,'index.html')


@app.get('/js/<path:name>')
def javascript(name):return send_from_directory(os.path.join(ROOT,'js'),name)


@app.get('/api/overview')
def overview():
    with dbconn() as db:
        latest=asdict(db.execute('SELECT * FROM daily_nav ORDER BY date DESC LIMIT 1').fetchone())
        nav=[dict(x) for x in db.execute('SELECT * FROM daily_nav ORDER BY date')]
        curve=[{key:row[key] for key in ('date','nav','adjusted_return','drawdown','distributions_usd','distributions_cny','arrears_cny')} for row in nav]
        all_events=[dict(x) for x in db.execute('SELECT * FROM events ORDER BY date,id')]
        events=[event for event in all_events if event['contract'] is not None]
        open_contracts={x['contract'] for x in db.execute('SELECT contract FROM open_positions')}
        benchmark_rows=[dict(x) for x in db.execute('SELECT date,close,dividend FROM bars WHERE symbol="QQQI" ORDER BY date')]
        return jsonify(envelope(db,{'latest':latest,'curve':curve,'initial_capital':250000,
                                    'option_income':option_income_summary(events,open_contracts),
                                    'monthly':model_backtest_attribution(nav,all_events)['monthly'],
                                    'comparison':strategy_benchmark_comparison(curve,benchmark_rows)}))


@app.get('/api/allocation')
def allocation():
    with dbconn() as db:
        latest=asdict(db.execute('SELECT * FROM daily_nav ORDER BY date DESC LIMIT 1').fetchone())
        positions=[dict(row) for row in db.execute('SELECT * FROM open_positions ORDER BY expiration,contract')]
        return jsonify(envelope(db,{'latest':latest,'open_positions':positions}))


@app.get('/api/trades')
def trades():
    limit=min(max(int(request.args.get('limit',100)),1),500)
    offset=max(int(request.args.get('offset',0)),0)
    contract=request.args.get('contract','').strip()
    where='WHERE e.contract=?' if contract else ''
    params=(contract,limit,offset) if contract else (limit,offset)
    with dbconn() as db:
        if request.args.get('view')=='orders':
            events=[dict(x) for x in db.execute('SELECT * FROM events ORDER BY date,id')]
            items=model_order_rows(events)
            if contract:
                items=[item for item in items if contract in {item.get('contract'),item.get('symbol'),item.get('order_id')}]
            total=len(items);items=items[offset:offset+limit]
            return jsonify(envelope(db,{'items':items,'total':total,'limit':limit,'offset':offset,
                                        'contract_filter':contract,'view':'orders'}))
        if request.args.get('view')=='contracts':
            events=db.execute('SELECT * FROM events WHERE contract IS NOT NULL ORDER BY date,id').fetchall()
            open_contracts={x['contract'] for x in db.execute('SELECT contract FROM open_positions')}
            items=contract_rows(events,open_contracts)
            if contract:items=[x for x in items if x['contract']==contract]
            total=len(items);items=items[offset:offset+limit]
            return jsonify(envelope(db,{'items':items,'total':total,'limit':limit,'offset':offset,
                                        'contract_filter':contract,'view':'contracts'}))
        rows=[]
        for x in db.execute(f'''SELECT e.*,q.bid quote_bid,q.ask quote_ask,q.quote_time
            FROM events e LEFT JOIN option_quotes q ON e.quote_date=q.date AND e.contract=q.contract
            {where} ORDER BY e.date DESC,e.id DESC LIMIT ? OFFSET ?''',params):
            item=dict(x)
            item['details']=json.loads(item['details'])
            rows.append(item)
        total=db.execute(f'SELECT COUNT(*) FROM events e {where}',(contract,) if contract else ()).fetchone()[0]
        return jsonify(envelope(db,{'items':rows,'total':total,'limit':limit,'offset':offset,
                                    'contract_filter':contract}))


@app.get('/api/review')
def review():
    with dbconn() as db:
        counts={x['kind']:x['n'] for x in db.execute('SELECT kind,COUNT(*) n FROM events GROUP BY kind')}
        gaps=[dict(x) for x in db.execute('SELECT * FROM data_gaps ORDER BY date DESC LIMIT 100')]
        latest=asdict(db.execute('SELECT * FROM daily_nav ORDER BY date DESC LIMIT 1').fetchone())
        nav_rows=[dict(x) for x in db.execute('SELECT date,nav,distributions_usd FROM daily_nav ORDER BY date')]
        benchmark_rows=[dict(x) for x in db.execute('SELECT date,close,dividend FROM bars WHERE symbol="QQQI" ORDER BY date')]
        model_runs=[]
        for years in (2,10):
            item=model_backtest_payload(years)
            if item:model_runs.append({'years':years,'config':item['config'],'metrics':item['metrics'],
                                       'fingerprint':item['fingerprint'],'label':item['label']})
        return jsonify(envelope(db,{'event_counts':counts,'gaps':gaps,'latest':latest,
                                    'backtest_windows':backtest_windows(nav_rows,benchmark_rows),
                                    'comparison':strategy_benchmark_comparison(nav_rows,benchmark_rows),
                                    'model_backtests':model_runs}))


@app.get('/api/backtests/<int:years>')
def backtest_result(years):
    if years not in (2,10):return jsonify({'error':'only 2 or 10 year model runs are supported'}),404
    payload=model_backtest_payload(years)
    if not payload:return jsonify({'error':'model backtest not yet generated','years':years}),404
    return jsonify(payload)


@app.get('/api/backtests/<int:years>/trades')
def backtest_trades(years):
    if years not in (2,10):return jsonify({'error':'only 2 or 10 year model runs are supported'}),404
    payload=model_backtest_payload(years,include_events=True)
    if not payload:return jsonify({'error':'model backtest not yet generated','years':years}),404
    limit=min(max(int(request.args.get('limit',100)),1),500)
    offset=max(int(request.args.get('offset',0)),0)
    view=request.args.get('view','events')
    events=[]
    for event in reversed(payload['events']):
        event=dict(event)
        event['details']=json.loads(event.get('details') or '{}')
        event['category'],event['operation']=model_event_labels(event)
        events.append(event)
    category=request.args.get('category','全部').strip()
    if view=='orders':
        orders=model_order_rows(events)
        for order in orders:
            order['category']=model_order_category(order)
        if category and category!='全部':
            orders=[order for order in orders if order['category']==category]
        return jsonify({'years':years,'mode':'MODEL_BACKTEST','view':'orders','label':payload['label'],
                        'total':len(orders),'category':category,'limit':limit,'offset':offset,'items':orders[offset:offset+limit]})
    if category and category!='全部':
        events=[event for event in events if event['category']==category]
    return jsonify({'years':years,'mode':'MODEL_BACKTEST','label':payload['label'],'total':len(events),
                    'view':'events','category':category,'limit':limit,'offset':offset,'items':events[offset:offset+limit]})


@app.get('/api/data-health')
def health():
    with dbconn() as db:
        coverage={x['symbol']:x['last_date'] for x in db.execute('SELECT symbol,MAX(date) last_date FROM bars GROUP BY symbol')}
        common=db.execute('''SELECT date FROM bars WHERE symbol IN ('BOXX','SPY','QQQM','VIX')
            GROUP BY date HAVING COUNT(DISTINCT symbol)=4 ORDER BY date DESC LIMIT 1''').fetchone()
        quote=db.execute('SELECT MAX(date) FROM option_quotes').fetchone()[0]
        gaps=[dict(x) for x in db.execute('SELECT * FROM data_gaps ORDER BY date DESC LIMIT 30')]
        response=envelope(db,{'bar_coverage':coverage,'market_as_of':common['date'] if common else None,
                              'option_coverage':quote,'gaps':gaps})
        response['stale']=not response['as_of'] or response['as_of']<latest_publishable_session().isoformat()
        return jsonify(response)


@app.get('/api/market')
def market():
    """Archived market observations; never mixed with simulated account returns."""
    with dbconn() as db:
        row=db.execute('''SELECT date FROM bars WHERE symbol IN ('BOXX','SPY','QQQM','VIX')
            GROUP BY date HAVING COUNT(DISTINCT symbol)=4 ORDER BY date DESC LIMIT 1''').fetchone()
        day=row['date'] if row else None
        snapshot={x['symbol']:dict(x) for x in db.execute(
            'SELECT * FROM bars WHERE date=? ORDER BY symbol',(day,))} if day else {}
        fx=asdict(db.execute('SELECT * FROM fx WHERE date<=? ORDER BY date DESC LIMIT 1',
                             (day,)).fetchone()) if day else None
        rows=db.execute('''SELECT date,
              MAX(CASE WHEN symbol='BOXX' THEN close END) BOXX,
              MAX(CASE WHEN symbol='SPY' THEN close END) SPY,
              MAX(CASE WHEN symbol='QQQM' THEN close END) QQQM
            FROM bars WHERE symbol IN ('BOXX','SPY','QQQM')
              AND date BETWEEN '2026-01-01' AND ?
            GROUP BY date HAVING COUNT(DISTINCT symbol)=3
            ORDER BY date DESC LIMIT 260''',(day,)).fetchall() if day else []
        return jsonify(envelope(db,{'market_as_of':day,'snapshot':snapshot,'fx':fx,
                                    'series':[dict(x) for x in reversed(rows)]}))


def catch_up():
    with dbconn() as db:
        last=db.execute('SELECT MAX(date) FROM daily_nav').fetchone()[0]
        start=date.fromisoformat(last) if last else date(2025,1,1)
        end=latest_publishable_session()
        if start<=end:
            for error in sync_all(db,start,end):print('DATA ERROR:',error,file=sys.stderr)
        replay(db,until=end.isoformat())


if __name__=='__main__':
    threading.Thread(target=catch_up,name='tianfu-catch-up',daemon=True).start()
    app.run(host='127.0.0.1',port=int(os.environ.get('PORT',10020)),debug=False)
