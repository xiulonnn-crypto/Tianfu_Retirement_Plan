"""Archive publicly available daily bars and Theta Terminal option EOD NBBO.

No failed fetch is converted to a made-up quote. Theta Terminal must be installed
and authenticated locally; the free tier's rolling history can expire.
"""
import os
import csv
import io
import time
from datetime import date, datetime, timedelta
import requests
import yfinance as yf
from .calendar import sessions
from .db import upsert_bar, upsert_quote

THETA_BASE=os.environ.get('THETA_URL','http://127.0.0.1:25503').rstrip('/')


def _theta_rows(payload):
    if isinstance(payload,list):return payload
    if isinstance(payload,dict):
        for key in ('response','data','results'):
            x=payload.get(key)
            if isinstance(x,list):return x
        if isinstance(payload.get('response'),dict):return _theta_rows(payload['response'])
    return []


def _theta_quote_rows(payload):
    """Flatten Theta v3's ``{contract, data[]}`` EOD response shape.

    Older Terminal builds returned each quote as a top-level row.  Keep accepting
    that shape so archived fixtures and compatible terminals continue to work.
    """
    flattened=[]
    for row in _theta_rows(payload):
        contract=row.get('contract') if isinstance(row,dict) else None
        reports=row.get('data') if isinstance(row,dict) else None
        if isinstance(contract,dict) and isinstance(reports,list):
            for report in reports:
                if isinstance(report,dict):
                    flattened.append(dict(contract,**report))
        elif isinstance(row,dict):
            flattened.append(row)
    return flattened


def _field(row,*names):
    for name in names:
        if name in row and row[name] is not None:return row[name]
    return None


def _date_string(value):
    if isinstance(value,int) or (isinstance(value,str) and value.isdigit() and len(value)==8):
        return datetime.strptime(str(value),'%Y%m%d').date().isoformat()
    return str(value)[:10]


def sync_bars(db,start,end,symbols=('BOXX','SPY','QQQM','QQQI')):
    failures=[]
    for symbol in symbols:
        try:
            frame=yf.Ticker(symbol).history(start=start.isoformat(),end=(end+timedelta(days=1)).isoformat(),
                                            auto_adjust=False,actions=True)
            if frame.empty:raise ValueError('empty history')
            for stamp,row in frame.iterrows():
                day=stamp.date().isoformat(); close=row.get('Close')
                if close is None or close!=close:continue
                dividend=row.get('Dividends',0.)
                upsert_bar(db,day,symbol,float(close),
                           'Yahoo Finance daily',float(dividend) if dividend==dividend else 0,
                           datetime.now().isoformat())
        except Exception as exc:failures.append(f'{symbol}: {exc}')
    db.commit();return failures


def sync_cboe_vix(db,start,end,session=None):
    session=session or requests.Session()
    url='https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv'
    response=session.get(url,timeout=45)
    response.raise_for_status()
    count=0
    for row in csv.DictReader(io.StringIO(response.text)):
        try:
            raw=row.get('DATE') or row.get('Date')
            if not raw:continue
            cleaned=raw.strip()
            day=(date.fromisoformat(cleaned) if '-' in cleaned else
                 datetime.strptime(cleaned,'%m/%d/%Y').date())
            if not start<=day<=end:continue
            close=float(row.get('CLOSE') or row.get('Close'))
            upsert_bar(db,day.isoformat(),'VIX',close,'Cboe VIX_History.csv')
            count+=1
        except (TypeError,ValueError):continue
    if not count:raise ValueError('Cboe VIX response has no values in range')
    db.commit();return count


def sync_fx_rate(db,start,end):
    failures=[]
    for ticker,table,column,scale in [('CNY=X','fx','usd_cny',1.),('^IRX','rates','annual_rate',.01)]:
        try:
            frame=yf.Ticker(ticker).history(start=start.isoformat(),end=(end+timedelta(days=1)).isoformat())
            if frame.empty:raise ValueError('empty history')
            for stamp,row in frame.iterrows():
                close=float(row['Close'])*scale
                if close<=0:continue
                db.execute(f'''INSERT INTO {table}(date,{column},source) VALUES(?,?,?)
                  ON CONFLICT(date) DO UPDATE SET {column}=excluded.{column},source=excluded.source''',
                  (stamp.date().isoformat(),close,f'Yahoo Finance {ticker} daily'))
        except Exception as exc:failures.append(f'{ticker}: {exc}')
    db.commit();return failures


def sync_theta_day(db,day,session=None):
    session=session or requests.Session()
    params={'symbol':'SPY','expiration':'*','start_date':day.strftime('%Y%m%d'),
            'end_date':day.strftime('%Y%m%d'),'max_dte':210,'format':'json'}
    response=session.get(f'{THETA_BASE}/v3/option/history/eod',params=params,timeout=90)
    response.raise_for_status()
    rows=_theta_quote_rows(response.json())
    if not rows:raise ValueError('Theta EOD returned no option quotes')
    written=0
    for row in rows:
        try:
            expiry=_date_string(_field(row,'expiration','exp'))
            strike=float(_field(row,'strike'))
            right=str(_field(row,'right','option_type','type')).upper()[0]
            bid=float(_field(row,'bid','bid_price'))
            ask=float(_field(row,'ask','ask_price'))
            created=str(row.get('created') or '')
            if created and created[:10]!=day.isoformat():continue
            dte=(date.fromisoformat(expiry)-day).days
            if not (0<=dte<=210):continue
            contract=f'SPY:{expiry}:{right}:{strike:g}'
            if float(row.get('bid_size',1))<=0 or float(row.get('ask_size',1))<=0:continue
            upsert_quote(db,day.isoformat(),contract,expiry,strike,right,bid,ask,
                         'ThetaData EOD NBBO',str(_field(row,'created','timestamp','time') or 'EOD'))
            written+=1
        except (TypeError,ValueError,KeyError):continue
    if not written:raise ValueError('Theta payload had no valid bid/ask rows')
    db.commit();return written


def sync_all(db,start,end,options=True):
    failures=sync_bars(db,start,end)+sync_fx_rate(db,start,end)
    try:sync_cboe_vix(db,start,end)
    except Exception as exc:failures.append(f'Cboe VIX: {exc}')
    if options:
        session=requests.Session()
        for day in sessions(max(start,date(2026,1,1)),end):
            try:
                sync_theta_day(db,day,session)
                time.sleep(3.1)  # stay below free-tier 20 requests/minute
            except Exception as exc:
                failures.append(f'Theta {day}: {exc}')
                # Terminal absence is a global problem; avoid hundreds of futile calls.
                if isinstance(exc,requests.ConnectionError):break
    return failures
