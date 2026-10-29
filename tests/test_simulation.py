from datetime import date,timedelta
import math
import json
import pytest
from strategy.calendar import is_session,sessions,last_session_of_month
from strategy.db import connect,upsert_bar,upsert_quote
from strategy.engine import Simulation,replay,put_target,harvest_count,profit_exit_count,call_target,insurance_trigger
from strategy.providers import sync_theta_day,sync_cboe_vix
from strategy.pricing import american_price_delta,implied_delta


@pytest.fixture
def db(tmp_path):
    connection=connect(tmp_path/'test.sqlite3')
    yield connection
    connection.close()


def seed_day(db,day,spy=500,vix=18,boxx=100,qqqm=200):
    for symbol,close in [('SPY',spy),('VIX',vix),('BOXX',boxx),('QQQM',qqqm)]:
        upsert_bar(db,day.isoformat(),symbol,close,'test')
    db.execute('INSERT OR REPLACE INTO rates VALUES(?,?,?)',(day.isoformat(),.04,'test'))
    db.execute('INSERT OR REPLACE INTO fx VALUES(?,?,?)',(day.isoformat(),7.,'test'))
    db.commit()


def quote(db,day,contract='P1',expiry='2026-02-06',strike=500,right='P',bid=5,ask=5.2,delta=-.16):
    upsert_quote(db,day.isoformat(),contract,expiry,strike,right,bid,ask,'test EOD','16:00',delta,'model-american-crr')
    db.commit()


def test_calendar_and_initial_purchase(db):
    assert not is_session(date(2026,1,1))
    assert is_session(date(2026,1,2))
    seed_day(db,date(2026,1,2))
    result=replay(db,'2026-01-02')
    assert result['complete_through']=='2026-01-02'
    nav=db.execute('SELECT * FROM daily_nav').fetchone()
    assert nav['boxx_shares']==2500 and nav['cash']==0
    assert nav['nav']==250000


def test_low_vix_three_contract_override_and_idempotence(db):
    for d in [date(2026,1,2),date(2026,1,5)]:seed_day(db,d,vix=14)
    quote(db,date(2026,1,5),strike=500)
    first=replay(db,'2026-01-05')
    assert first['complete_through']=='2026-01-05'
    nav=db.execute('SELECT * FROM daily_nav ORDER BY date DESC LIMIT 1').fetchone()
    assert nav['put_collateral']==150000
    assert nav['nav']==pytest.approx(nav['cash']+nav['boxx_value']+nav['spy_value']+
                                      nav['long_put_value']-nav['short_liability'])
    events=[dict(x) for x in db.execute('SELECT * FROM events ORDER BY id')]
    second=replay(db,'2026-01-05')
    assert first==second
    assert events==[dict(x) for x in db.execute('SELECT * FROM events ORDER BY id')]
    assert db.execute('SELECT COUNT(*) FROM open_positions').fetchone()[0]==1


def test_low_vix_weekly_floor_sells_three_fully_secured_contracts(db):
    for d in [date(2026,1,2),date(2026,1,5)]:seed_day(db,d,spy=600,vix=14)
    quote(db,date(2026,1,5),strike=600)
    replay(db,'2026-01-05')
    nav=db.execute('SELECT * FROM daily_nav ORDER BY date DESC LIMIT 1').fetchone()
    assert nav['put_collateral']==180000
    event=db.execute("SELECT * FROM events WHERE kind='SELL_PUT'").fetchone()
    assert event and event['quantity']==3 and 'weekly_floor_override' in event['details']


def test_weekly_put_prefers_target_delta_over_25_percent_collateral_slice(db):
    """The four-week schedule cannot turn a 25% bookkeeping slice into a Delta cap."""
    for d in [date(2026,1,2),date(2026,1,5)]:seed_day(db,d,spy=760,vix=18)
    quote(db,date(2026,1,5),contract='P625',strike=625,bid=1,ask=1.1,delta=-.02)
    quote(db,date(2026,1,5),contract='P700',strike=700,bid=5,ask=5.2,delta=-.16)
    replay(db,'2026-01-05')
    event=db.execute("SELECT * FROM events WHERE kind='SELL_PUT'").fetchone()
    details=json.loads(event['details'])
    assert event['contract']=='P700'
    assert event['quantity']==3
    assert details['actual_delta']==pytest.approx(-.16)
    assert details['collateral']==210000


def test_unassigned_week_sells_three_puts_without_collateral_screen(db):
    for d in [date(2026,1,2),date(2026,1,5)]:seed_day(db,d,spy=950,vix=18)
    quote(db,date(2026,1,5),contract='P900',strike=900,bid=5,ask=5.2,delta=-.16)
    replay(db,'2026-01-05')
    event=db.execute("SELECT * FROM events WHERE kind='SELL_PUT'").fetchone()
    assert event['contract']=='P900' and event['quantity']==3


def test_missing_quote_halts_without_publishing_incomplete_day(db):
    for d in [date(2026,1,2),date(2026,1,5)]:seed_day(db,d)
    result=replay(db,'2026-01-05')
    assert result['complete_through']=='2026-01-02'
    assert db.execute('SELECT COUNT(*) FROM daily_nav').fetchone()[0]==1
    assert db.execute('SELECT category FROM data_gaps').fetchone()[0]=='option_selection'


def test_take_profit_rounding_and_assignment(db):
    sim=Simulation(db)
    d=date(2026,1,5);seed_day(db,d,spy=490)
    q={'date':d.isoformat(),'source':'test','contract':'P1','bid':1.9,'ask':2.,'strike':500.,'right':'P','expiration':'2026-02-06','delta':-.16,'delta_method':'model-american-crr'}
    sim.cash=100000
    sim.positions['P1']={'contract':'P1','expiration':'2026-02-06','strike':500.,'right':'P',
        'side':'short','count':3,'entry':5.,'entry_net':5.,'delta':-.16,'half_taken':False,'lot_cost':None}
    assert sim.close_option(d,sim.positions['P1'],q,2,'TAKE_PROFIT',100)
    assert sim.positions['P1']['count']==1
    assert sim.handle_expiry(date(2026,2,6),sim.positions['P1'],490,100)
    assert sim.spy_lots[0]['shares']==100
    assert sim.spy_lots[0]['cost']==495
    assert math.isclose(sim.cash,100000-400-1.3-50000)


def test_assignment_closes_unfunded_contracts_before_delivery(db):
    """Expiry keeps only whole PUTs that can be paid for without selling SPY."""
    sim=Simulation(db);day=date(2026,2,6)
    seed_day(db,day,spy=50)
    quote(db,day,contract='P1',expiry=day.isoformat(),strike=100,right='P',bid=.01,ask=.01,delta=-.99)
    sim.cash=35000;sim.boxx=0
    pos={'contract':'P1','expiration':day.isoformat(),'strike':100.,'right':'P','side':'short',
         'count':6,'entry':2.,'entry_net':2.,'delta':-.16}
    sim.positions['P1']=pos
    assert sim.handle_expiry(day,pos,50,100)
    delever=next(event for event in sim.events if event[2]=='ASSIGNMENT_DELEVER')
    assigned=next(event for event in sim.events if event[2]=='PUT_ASSIGN')
    stock=next(event for event in sim.events if event[2]=='BUY_SPY_ASSIGNMENT')
    assert delever[5]==1  # The engine evaluates affordability after every whole contract.
    assert assigned[5]==3 and stock[5]==300
    assert stock[6]==pytest.approx(98)  # Strike less the opening net premium.
    assert sim.spy_lots[0]['shares']==300
    assert not any(event[2]=='LIQUIDATE_SPY' for event in sim.events)


def test_delta_boundary_and_hedge_budget(db):
    sim=Simulation(db);d=date(2026,1,5)
    seed_day(db,d,spy=500)
    assert sim.annual_realized.get(2026,0)==0
    sim.profit(d,10000)
    assert sim.annual_realized[2026]*.016==160
    sim.annual_insurance_spend[2026]=150
    assert sim.annual_realized[2026]*.016-sim.annual_insurance_spend[2026]==10


def test_exact_signal_boundaries():
    assert put_target(22)==.16 and put_target(22.0001)==.12
    assert harvest_count(40,3)==0
    assert harvest_count(40.001,3)==2
    assert harvest_count(50,3)==2
    assert harvest_count(50.001,3)==3
    assert profit_exit_count(.5499,3)==0
    assert profit_exit_count(.55,3)==2
    assert profit_exit_count(.60,3,True)==3
    assert profit_exit_count(.55,1)==1
    assert call_target(499,500)==.25 and call_target(500,500)==.25
    assert not insurance_trigger(15,100,100)
    assert not insurance_trigger(14.99,98,100)
    assert insurance_trigger(14.99,98.01,100)


def test_month_end_arrears_carry_forward(db):
    sim=Simulation(db);sim.cash=10000
    jan=date(2026,1,30)
    seed_day(db,jan)
    assert last_session_of_month(jan)==jan
    assert sim.settle_month(jan,100)
    assert sim.arrears==20000
    feb=date(2026,2,27)
    seed_day(db,feb)
    assert last_session_of_month(feb)==feb
    sim.profit(feb,6000)
    assert sim.settle_month(feb,100)
    assert sim.arrears==0
    assert sim.withdrawn_cny==40000
    assert sim.events[-1][3]=='USD/CNY'


def test_month_end_pays_all_available_profit_then_defers_balance(db):
    sim=Simulation(db);sim.cash=10000
    jan=date(2026,1,30);seed_day(db,jan)
    sim.profit(jan,1000)
    assert sim.settle_month(jan,100)
    assert sim.withdrawn_usd==1000
    assert sim.withdrawn_cny==7000
    assert sim.arrears==13000
    event=sim.events[-1]
    assert event[2]=='WITHDRAW' and '全部提取' in event[9]


def test_month_end_distributes_realised_profit_when_put_notional_exceeds_assets(db):
    """The weekly three-PUT rule must not suppress a realised-profit payout."""
    sim=Simulation(db);sim.cash=0;sim.boxx=2500
    jan=date(2026,1,30);seed_day(db,jan)
    sim.positions['SPY:2026-02-27:P:1000']={
        'side':'short','right':'P','strike':1000,'count':3,
    }
    sim.profit(jan,1000)

    assert sim.settle_month(jan,100)
    assert sim.withdrawn_usd==1000
    assert sim.withdrawn_cny==7000
    assert sim.arrears==13000
    assert sim.events[-1][2]=='WITHDRAW'


def test_contract_history_rollup_and_backtest_windows():
    from server import annual_income_view,contract_rows,backtest_windows,model_backtest_attribution,model_event_labels,model_order_category,model_order_rows,option_income_summary,strategy_benchmark_comparison
    events=[
        {'contract':'SPY:2026-02-06:C:719','date':'2026-01-05','kind':'SELL_CALL','symbol':'SPY','quantity':1,'cash_change':89.35},
        {'contract':'SPY:2026-02-06:C:719','date':'2026-01-16','kind':'TAKE_PROFIT','symbol':'SPY','quantity':1,'cash_change':-34.65},
    ]
    summary=contract_rows(events,set())
    assert summary[0]['status']=='CLOSED'
    assert summary[0]['open_date']=='2026-01-05'
    assert summary[0]['close_date']=='2026-01-16'
    assert summary[0]['realized_pnl_usd']==pytest.approx(54.70)
    income=option_income_summary(events,set())
    assert income['sell_call_income_usd']==pytest.approx(54.70)
    assert income['sell_call_contracts']==1
    model_events=[
        {'id':1,'event_key':'a','date':'2026-01-05','kind':'SELL_CALL','symbol':'SPY','contract':'SPY:2026-02-06:C:719','quantity':1,'price':.9,'cash_change':89.35},
        {'id':2,'event_key':'b','date':'2026-01-16','kind':'TAKE_PROFIT','symbol':'SPY','contract':'SPY:2026-02-06:C:719','quantity':1,'price':.34,'cash_change':-34.65},
        {'id':3,'event_key':'c','date':'2026-01-05','kind':'BUY_BOXX','symbol':'BOXX','contract':None,'quantity':10,'price':100,'cash_change':-1000},
        {'id':4,'event_key':'d','date':'2026-01-16','kind':'SELL_BOXX','symbol':'BOXX','contract':None,'quantity':10,'price':110,'cash_change':1100},
        {'id':5,'event_key':'e','date':'2026-01-20','kind':'PUT_ASSIGN','symbol':'SPY','contract':'SPY:2026-01-20:P:500','quantity':1,'price':500,'cash_change':0},
        {'id':6,'event_key':'f','date':'2026-01-20','kind':'BUY_SPY_ASSIGNMENT','symbol':'SPY','contract':'SPY:2026-01-20:P:500','quantity':100,'price':498,'cash_change':-50000},
    ]
    model_orders=model_order_rows(model_events)
    option=next(row for row in model_orders if row['instrument']=='OPTION')
    boxx=next(row for row in model_orders if row['symbol']=='BOXX')
    spy=next(row for row in model_orders if row['instrument']=='STOCK' and row['symbol']=='SPY')
    assert option['status']=='CLOSED' and option['realized_pnl_usd']==pytest.approx(54.70)
    assert option['close_price']==pytest.approx(.34)
    assert boxx['status']=='CLOSED' and boxx['realized_pnl_usd']==pytest.approx(100)
    assert spy['status']=='OPEN' and spy['open_quantity']==100
    assert model_order_category(spy)=='接货'
    assert model_order_category(option)=='Covered CALL'
    assert model_orders[0]['status']=='OPEN'
    attribution=model_backtest_attribution(
        [{'date':'2026-01-05','nav':100000,'distributions_usd':0,'realized_profit':0},
         {'date':'2026-01-30','nav':100100,'distributions_usd':50,'realized_profit':70},
         {'date':'2026-02-03','nav':100200,'distributions_usd':50,'realized_profit':80}],
        model_events)
    assert attribution['attribution']['covered_call_income_usd']==pytest.approx(54.70)
    assert attribution['monthly'][0]['withdrawals_usd']==pytest.approx(50)
    assert attribution['monthly'][0]['realized_strategy_income_usd']==pytest.approx(70)
    assert attribution['monthly'][1]['realized_strategy_income_usd']==pytest.approx(10)
    assert attribution['monthly'][1]['monthly_return']==pytest.approx(.0009985)
    annual=annual_income_view(attribution['monthly'])
    assert annual[0]['year']=='2026'
    assert annual[0]['realized_strategy_income_usd']==pytest.approx(80)
    assert annual[0]['annual_return']==pytest.approx(.0009985)
    assert model_event_labels({'kind':'SELL_PUT','contract':'SPY:2026-02-06:P:719'})==('卖PUT','卖出')
    assert model_event_labels({'kind':'PUT_ASSIGN','contract':'SPY:2026-02-06:P:719'})==('卖PUT','无')
    assert model_event_labels({'kind':'BUY_SPY_ASSIGNMENT','contract':'SPY:2026-02-06:P:719'})==('接货','买入')
    assert model_event_labels({'kind':'DIVIDEND'})==('分红','无')
    assert model_event_labels({'kind':'SKIP','reason':'保险预算不足一张或无有效报价'})==('保险','无')
    rows=[{'date':'2024-01-01','nav':100,'distributions_usd':0},
          {'date':'2026-01-03','nav':120,'distributions_usd':10}]
    windows={row['years']:row for row in backtest_windows(rows)}
    assert windows[2]['status']=='complete'
    assert windows[10]['status']=='collecting'
    comparison=strategy_benchmark_comparison(
        [{'date':'2026-01-02','nav':100,'distributions_usd':0},
         {'date':'2026-01-05','nav':110,'distributions_usd':0}],
        [{'date':'2026-01-02','close':10,'dividend':0},
         {'date':'2026-01-05','close':10,'dividend':.1}])
    assert comparison['status']=='ready'
    assert comparison['strategy_return']==pytest.approx(.1)
    assert comparison['benchmark_return']==pytest.approx(.01)
    assert comparison['excess_return']==pytest.approx(.09)
    with_withdrawals=strategy_benchmark_comparison(
        [{'date':'2026-01-02','nav':100,'distributions_usd':0},
         {'date':'2026-01-05','nav':60,'distributions_usd':50},
         {'date':'2026-01-06','nav':66,'distributions_usd':50}],
        [{'date':'2026-01-02','close':10,'dividend':0},
         {'date':'2026-01-05','close':11,'dividend':0},
         {'date':'2026-01-06','close':12.1,'dividend':0}])
    assert with_withdrawals['strategy_return']==pytest.approx(.16)
    assert with_withdrawals['benchmark_return']==pytest.approx(.16)
    assert with_withdrawals['excess_return']==pytest.approx(0)
    assert with_withdrawals['benchmark_final_nav']==pytest.approx(66)
    assert with_withdrawals['benchmark_withdrawals_usd']==pytest.approx(50)


def test_api_empty_state(db,monkeypatch):
    import server
    server.app.config['TESTING']=True
    server.app.config['DATABASE']=str(db.execute('PRAGMA database_list').fetchone()[2])
    client=server.app.test_client()
    assert client.get('/').status_code==200
    assert '模拟复盘' in client.get('/').text
    assert '更新模拟' in client.get('/').text
    assert client.get('/assets/style.css').status_code==200
    assert client.get('/js/app.js').status_code==200
    for module in ['/js/main.js','/js/common.js','/js/tabs/returns.js',
                   '/js/tabs/allocation.js','/js/tabs/history.js','/js/tabs/review.js']:
        assert client.get(module).status_code==200
    for path in ['/api/overview','/api/allocation','/api/trades','/api/review','/api/data-health']:
        response=client.get(path)
        assert response.status_code==200
        assert response.json['as_of'] is None
        assert response.json['rule_version']=='tianfu-v1.5.0'


def test_simulation_update_syncs_then_replays_and_reports_partial_sources(db,monkeypatch):
    import server
    server.app.config['TESTING']=True
    server.app.config['DATABASE']=str(db.execute('PRAGMA database_list').fetchone()[2])
    calls=[]
    monkeypatch.setattr(server,'ensure_theta_terminal',lambda:{'status':'already_running','message':'Theta Terminal 已连接。'})
    monkeypatch.setattr(server,'latest_publishable_session',lambda:date(2026,1,5))
    monkeypatch.setattr(server,'sync_all',lambda connection,start,end: calls.append(('sync',start,end)) or ['Theta unavailable'])
    monkeypatch.setattr(server,'replay',lambda connection,until: calls.append(('replay',until)) or
                        {'complete_through':'2026-01-02','events':4,'gaps':1})

    response=server.app.test_client().post('/api/simulation/update')

    assert response.status_code==200
    assert response.content_type=='application/json'
    assert calls==[('sync',date(2025,1,1),date(2026,1,5)),('replay','2026-01-05')]
    assert response.json['status']=='updated_with_warnings'
    assert response.json['message_level']=='warning'
    assert response.json['as_of_before'] is None
    assert response.json['as_of_after']=='2026-01-02'
    assert response.json['replay']['events']==4
    assert '更新完成' in response.json['message']


def test_simulation_update_reports_when_missing_quotes_prevent_an_advance(db,monkeypatch):
    import server
    server.app.config['TESTING']=True
    server.app.config['DATABASE']=str(db.execute('PRAGMA database_list').fetchone()[2])
    db.execute("INSERT INTO daily_nav VALUES('2026-01-02',0,0,0,0,0,0,0,1,0,0,0,0,0,0,0,'test')")
    upsert_quote(db,'2026-01-02','SPY:2026-02-20:P:100','2026-02-20',100,'P',1,1.1,'test','EOD')
    db.commit()
    monkeypatch.setattr(server,'ensure_theta_terminal',lambda:{'status':'already_running','message':'Theta Terminal 已连接。'})
    monkeypatch.setattr(server,'latest_publishable_session',lambda:date(2026,1,5))
    monkeypatch.setattr(server,'sync_all',lambda *_args:['Theta 2026-01-05: connection refused'])
    monkeypatch.setattr(server,'replay',lambda *_args,**_kwargs:{'complete_through':'2026-01-02','events':4,'gaps':1})

    response=server.app.test_client().post('/api/simulation/update')

    assert response.status_code==200
    assert response.json['status']=='blocked'
    assert response.json['message_level']=='warning'
    assert '模拟未更新：仍截至 2026-01-02，目标为 2026-01-05' in response.json['message']
    assert 'Theta Terminal 未连接' in response.json['message']


def test_update_starts_local_theta_before_syncing(db,monkeypatch):
    import server
    server.app.config['TESTING']=True
    server.app.config['DATABASE']=str(db.execute('PRAGMA database_list').fetchone()[2])
    calls=[];ready=iter([False,True])
    monkeypatch.setattr(server,'theta_terminal_ready',lambda:next(ready))
    monkeypatch.setattr(server.subprocess,'Popen',lambda command,**kwargs:calls.append((command,kwargs)))

    result=server.ensure_theta_terminal(timeout=1)

    assert result['status']=='started'
    assert result['message']=='Theta Terminal 已启动并连接。'
    assert calls[0][0]==[str(server.THETA_START_SCRIPT)]
    assert calls[0][1]['cwd']==server.ROOT


def test_simulation_update_stops_before_replay_when_theta_cannot_start(db,monkeypatch):
    import server
    server.app.config['TESTING']=True
    server.app.config['DATABASE']=str(db.execute('PRAGMA database_list').fetchone()[2])
    monkeypatch.setattr(server,'latest_publishable_session',lambda:date(2026,1,5))
    monkeypatch.setattr(server,'ensure_theta_terminal',lambda:{'status':'unavailable','message':'Theta Terminal 未能启动。'})
    monkeypatch.setattr(server,'sync_all',lambda *_args:pytest.fail('should not sync without Theta'))
    monkeypatch.setattr(server,'replay',lambda *_args,**_kwargs:pytest.fail('should not replay without Theta'))

    response=server.app.test_client().post('/api/simulation/update')

    assert response.status_code==200
    assert response.json['status']=='blocked'
    assert 'Theta Terminal 未能启动' in response.json['message']


def test_simulation_update_rejects_a_concurrent_catch_up(db):
    import server
    server.app.config['TESTING']=True
    server.app.config['DATABASE']=str(db.execute('PRAGMA database_list').fetchone()[2])
    assert server.SIMULATION_UPDATE_LOCK.acquire(blocking=False)
    try:
        response=server.app.test_client().post('/api/simulation/update')
    finally:
        server.SIMULATION_UPDATE_LOCK.release()

    assert response.status_code==409
    assert response.content_type=='application/json'
    assert response.json['error']=='simulation update already in progress'


def test_market_api_shows_real_bars_without_simulation(db):
    import server
    server.app.config['TESTING']=True
    server.app.config['DATABASE']=str(db.execute('PRAGMA database_list').fetchone()[2])
    for symbol,close,source in [('BOXX',101,'Yahoo Finance daily'),
                                ('SPY',600,'Yahoo Finance daily'),
                                ('QQQM',230,'Yahoo Finance daily'),
                                ('VIX',16,'Cboe VIX_History.csv')]:
        upsert_bar(db,'2026-01-05',symbol,close,source)
    db.execute("INSERT INTO fx VALUES('2026-01-05',7.2,'Yahoo Finance CNY=X daily')")
    db.commit()
    response=server.app.test_client().get('/api/market')
    assert response.status_code==200
    body=response.json
    assert body['as_of'] is None
    assert body['market_as_of']=='2026-01-05'
    assert body['snapshot']['SPY']['close']==600
    assert body['snapshot']['VIX']['source']=='Cboe VIX_History.csv'
    assert body['fx']['usd_cny']==7.2
    assert body['series']==[{'date':'2026-01-05','BOXX':101,'SPY':600,'QQQM':230}]


def test_model_backtest_quote_is_marked_and_has_valid_spread(db):
    from strategy.backtest import ModelSimulation, MODEL_VERSION
    day=date(2026,1,5); next_day=date(2026,1,6)
    seed_day(db,day,spy=600,vix=18);seed_day(db,next_day,spy=601,vix=18)
    db.execute("INSERT OR REPLACE INTO rates VALUES(?,?,?)",(day.isoformat(),.04,'test'))
    db.execute("INSERT OR REPLACE INTO fx VALUES(?,?,?)",(day.isoformat(),7.2,'test'))
    db.commit()
    sim=ModelSimulation(db,[day,next_day])
    quote=sim._quote(day,day+timedelta(days=32),'P',560)
    assert quote['source'].startswith('Model option quote')
    assert quote['bid']>0 and quote['ask']>=quote['bid']
    assert quote['delta']<0
    assert quote['model']['version']==MODEL_VERSION
    selected,delta=sim.choose(day,'P',.16,28,35,600)
    assert selected['expiration']==(day+timedelta(days=31)).isoformat() or selected['expiration']==(day+timedelta(days=32)).isoformat()
    assert delta<0


def test_theta_contract_identity_and_cboe_archive(db):
    class Response:
        def __init__(self,data=None,text=''):self.data=data;self.text=text
        def raise_for_status(self):pass
        def json(self):return self.data
    class Session:
        def __init__(self,response):self.response=response
        def get(self,*args,**kwargs):return self.response
    rows=[{'symbol':'SPY','expiration':'2026-02-06','strike':500,'right':'put',
           'bid':5.0,'ask':5.2,'bid_size':10,'ask_size':15,'created':'2026-01-05T17:15:00'},
          {'symbol':'SPY','expiration':'2026-02-06','strike':505,'right':'put',
           'bid':7.0,'ask':7.2,'bid_size':10,'ask_size':15,'created':'2026-01-05T17:15:00'}]
    count=sync_theta_day(db,date(2026,1,5),Session(Response(rows)))
    assert count==2
    assert db.execute('SELECT COUNT(DISTINCT contract) FROM option_quotes').fetchone()[0]==2
    assert db.execute('SELECT quote_time FROM option_quotes LIMIT 1').fetchone()[0].endswith('17:15:00')
    csv_text='DATE,OPEN,HIGH,LOW,CLOSE\n01/05/2026,14,15,13,14.5\n'
    assert sync_cboe_vix(db,date(2026,1,5),date(2026,1,5),Session(Response(text=csv_text)))==1
    row=db.execute("SELECT close,source FROM bars WHERE symbol='VIX'").fetchone()
    assert row['close']==14.5 and row['source'].startswith('Cboe')


def test_theta_v3_nested_eod_response_is_flattened(db):
    class Response:
        def raise_for_status(self):pass
        def json(self):
            return {'response':[{'contract':{'symbol':'SPY','expiration':'2026-02-06',
                                              'strike':650,'right':'PUT'},
                                 'data':[{'bid':2.18,'ask':2.20,'bid_size':314,'ask_size':155,
                                          'created':'2026-01-05T17:17:18.459'}]}]}
    class Session:
        def get(self,*args,**kwargs):return Response()
    assert sync_theta_day(db,date(2026,1,5),Session())==1
    quote=db.execute('SELECT * FROM option_quotes').fetchone()
    assert quote['contract']=='SPY:2026-02-06:P:650'
    assert quote['bid']==pytest.approx(2.18)
    assert quote['quote_time'].startswith('2026-01-05T17:17:18')


def test_american_delta_uses_observed_bid_ask():
    years=32/365
    mid,reference_delta=american_price_delta(500,475,years,.04,.012,.20,'P')
    estimate,iv=implied_delta(500,475,years,.04,.012,mid-.05,mid+.05,'P')
    assert iv==pytest.approx(.20,abs=.0001)
    assert estimate==pytest.approx(reference_delta,abs=.0001)


def test_landlord_call_and_physical_delivery(db):
    prior=date(2026,1,2);day=date(2026,1,5)
    seed_day(db,prior,spy=490);seed_day(db,day,spy=490)
    quote(db,day,contract='P1',expiry='2026-02-06',strike=480,right='P',
          bid=3,ask=3.2,delta=-.16)
    quote(db,day,contract='C1',expiry='2026-02-06',strike=500,right='C',
          bid=3,ask=3.2,delta=.25)
    sim=Simulation(db);sim.cash=1000;sim.boxx=1000;sim.completed=prior
    sim.spy_lots=[{'id':'lot1','shares':100,'cost':495,'tax_basis':500}]
    sim.previous={x:sim.bar(prior,x) for x in ('BOXX','SPY','QQQM','VIX')}
    sim.previous_day=prior
    assert sim.day(day)
    call=sim.positions['C1']
    assert call['lot_id']=='lot1'
    assert call['delta']==.25
    assert sim.handle_expiry(date(2026,2,6),call,510,100)
    assert not sim.spy_lots
    assert sim.cash>=50000
    assert any(e[3]=='SPY' and e[2]=='CALL_ASSIGN' for e in sim.events)


def test_regular_weekly_covered_call_uses_delta_25_above_cost(db):
    prior=date(2026,1,2);day=date(2026,1,5)
    seed_day(db,prior,spy=510);seed_day(db,day,spy=510)
    quote(db,day,contract='P1',expiry='2026-02-06',strike=480,right='P',
          bid=3,ask=3.2,delta=-.16)
    quote(db,day,contract='C25',expiry='2026-02-06',strike=525,right='C',
          bid=2,ask=2.2,delta=.25)
    sim=Simulation(db);sim.cash=1000;sim.boxx=1000;sim.completed=prior
    sim.spy_lots=[{'id':'lot1','shares':300,'cost':495,'tax_basis':500}]
    sim.previous={x:sim.bar(prior,x) for x in ('BOXX','SPY','QQQM','VIX')}
    sim.previous_day=prior
    assert sim.day(day)
    call=sim.positions['C25']
    assert call['count']==3
    assert call['delta']==pytest.approx(.25)
    assert call['slot'] is None
    assert sim.positions['P1']['count']==1


def test_covered_call_holds_to_expiry_and_puts_resume_after_assignment(db):
    prior=date(2026,1,2);opened=date(2026,1,5);near_expiry=date(2026,1,30);expiry=date(2026,2,6);resume=date(2026,2,9)
    for day in (prior,opened,near_expiry,resume):seed_day(db,day,spy=510)
    quote(db,opened,contract='C1',expiry=expiry.isoformat(),strike=500,right='C',bid=3,ask=3.2,delta=.25)
    quote(db,opened,contract='P0',expiry='2026-02-05',strike=480,right='P',bid=3,ask=3.2,delta=-.16)
    quote(db,near_expiry,contract='C1',expiry=expiry.isoformat(),strike=500,right='C',bid=8,ask=8.2,delta=.60)
    quote(db,resume,contract='P1',expiry='2026-03-12',strike=480,right='P',bid=3,ask=3.2,delta=-.16)
    sim=Simulation(db);sim.cash=1000;sim.boxx=0;sim.completed=prior
    sim.spy_lots=[{'id':'lot1','shares':100,'cost':495,'tax_basis':500}]
    sim.previous={x:sim.bar(prior,x) for x in ('BOXX','SPY','QQQM','VIX')};sim.previous_day=prior
    assert sim.day(opened) and sim.positions['C1']['count']==1
    assert sim.day(near_expiry)
    assert 'C1' in sim.positions
    assert not any(event[2] in {'TAKE_PROFIT','FORCE_CLOSE'} and event[4]=='C1' for event in sim.events)
    assert sim.handle_expiry(expiry,sim.positions['C1'],510,100)
    assert not sim.spy_lots
    assert sim.day(resume)
    assert sim.positions['P1']['count']==3


def test_unassigned_weekly_unit_does_not_sell_call(db):
    for d in [date(2026,1,2),date(2026,1,5)]:seed_day(db,d,spy=760,vix=18)
    quote(db,date(2026,1,5),contract='P700',strike=700,bid=5,ask=5.2,delta=-.16)
    quote(db,date(2026,1,5),contract='C820',expiry='2026-02-06',strike=820,right='C',
          bid=2,ask=2.2,delta=.09)
    replay(db,'2026-01-05')
    assert db.execute("SELECT * FROM events WHERE kind='SELL_CALL'").fetchone() is None


def test_insurance_previous_day_signal_and_budget(db):
    prior=date(2026,1,5);day=date(2026,1,6)
    for i in range(20):
        d=prior-timedelta(days=i)
        upsert_bar(db,d.isoformat(),'QQQM',200,'test')
    seed_day(db,prior,vix=14,qqqm=200)
    seed_day(db,day,vix=14,qqqm=200)
    quote(db,day,contract='H1',expiry='2026-07-10',strike=400,right='P',
          bid=.9,ask=1.,delta=-.025)
    sim=Simulation(db);sim.cash=250000;sim.completed=prior
    sim.previous={x:sim.bar(prior,x) for x in ('BOXX','SPY','QQQM','VIX')}
    sim.previous_day=prior
    sim.annual_realized[2026]=10000
    assert sim.day(day)
    assert sim.positions['H1']['side']=='long'
    assert sim.annual_insurance_spend[2026]==pytest.approx(100.65)


def test_insurance_with_no_budget_does_not_block_the_day(db):
    prior=date(2026,1,5);day=date(2026,1,6)
    for i in range(20):
        d=prior-timedelta(days=i)
        upsert_bar(db,d.isoformat(),'QQQM',200,'test')
    seed_day(db,prior,vix=14,qqqm=200)
    seed_day(db,day,vix=14,qqqm=200)
    sim=Simulation(db);sim.cash=250000;sim.completed=prior
    sim.previous={x:sim.bar(prior,x) for x in ('BOXX','SPY','QQQM','VIX')}
    sim.previous_day=prior
    assert sim.day(day)
    assert sim.completed==day
    assert any(e[2]=='SKIP' and '保险预算不足' in e[9] for e in sim.events)
