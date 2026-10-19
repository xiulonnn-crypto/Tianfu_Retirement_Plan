"""Deterministic, market-driven historical model backtests.

These runs are deliberately stored outside the live ThetaData ledger.  Historical
SPY prices, VIX, rates and FX are observed data; option NBBO quotes are generated
from a documented CRR model and are never presented as historical executions.
"""
import hashlib
import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path

import yfinance as yf

from .db import connect, get_meta, set_meta, upsert_bar
from .engine import INITIAL_CASH, Simulation
from .pricing import american_price_delta, european_price_delta
from .providers import sync_cboe_vix

MODEL_VERSION = 'crr-v1-vix-2pct-spread'
PROXY_SWITCH = date(2022, 12, 28)
BENCHMARK_SWITCH = date(2024, 1, 29)
ROOT = Path(__file__).resolve().parent.parent


def database_path(years):
    return ROOT / 'data' / f'backtest-{int(years)}y.sqlite3'


def _round(value):
    return round(float(value), 8)


def _write_yahoo(db, ticker, symbol, start, end):
    frame = yf.Ticker(ticker).history(
        start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
        auto_adjust=False, actions=True,
    )
    if frame.empty:
        raise ValueError(f'{ticker} returned no history')
    count = 0
    for stamp, row in frame.iterrows():
        close = row.get('Close')
        if close is None or close != close or float(close) <= 0:
            continue
        dividend = row.get('Dividends', 0.0)
        upsert_bar(db, stamp.date().isoformat(), symbol, float(close),
                   f'Yahoo Finance {ticker} daily',
                   float(dividend) if dividend == dividend else 0.0,
                   datetime.now().isoformat())
        count += 1
    db.commit()
    return count


def _write_series(db, ticker, table, column, scale, start, end):
    frame = yf.Ticker(ticker).history(
        start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
        auto_adjust=False, actions=False,
    )
    if frame.empty:
        raise ValueError(f'{ticker} returned no history')
    count = 0
    for stamp, row in frame.iterrows():
        value = row.get('Close')
        if value is None or value != value or float(value) <= 0:
            continue
        db.execute(f'''INSERT INTO {table}(date,{column},source) VALUES(?,?,?)
          ON CONFLICT(date) DO UPDATE SET {column}=excluded.{column},source=excluded.source''',
                   (stamp.date().isoformat(), float(value) * scale,
                    f'Yahoo Finance {ticker} daily'))
        count += 1
    db.commit()
    return count


def _rows(db, symbol):
    return {row['date']: row for row in db.execute(
        'SELECT * FROM bars WHERE symbol=? ORDER BY date', (symbol,))}


def _continuous_collateral(db):
    """Write a continuous BIL/BOXX total-return proxy into the BOXX slot."""
    bil, boxx = _rows(db, 'BIL_RAW'), _rows(db, 'BOXX_RAW')
    dates = sorted(set(bil) | set(boxx))
    if not dates:
        raise ValueError('BIL/BOXX proxy source is empty')
    value = 100.0
    prior = None
    switched = False
    for day in dates:
        day_date = date.fromisoformat(day)
        source_row = boxx.get(day) if day_date >= PROXY_SWITCH and boxx.get(day) else bil.get(day)
        if source_row is None:
            continue
        raw = float(source_row['close'])
        if prior is not None:
            value *= (raw + float(source_row['dividend'])) / prior
        source = ('BOXX actual total-return leg' if day_date >= PROXY_SWITCH and boxx.get(day)
                  else 'BIL proxy total-return leg')
        upsert_bar(db, day, 'BOXX', value, source, 0.0, source_row['source_time'])
        prior = raw
        switched = switched or day_date >= PROXY_SWITCH
    db.commit()


def _copy_qqq_signal(db):
    for row in db.execute('SELECT * FROM bars WHERE symbol="QQQ_RAW" ORDER BY date'):
        upsert_bar(db, row['date'], 'QQQM', row['close'],
                   'QQQ long-history proxy for QQQM signal', row['dividend'], row['source_time'])
    db.commit()


def sync_market(db, start, requested_end):
    """Archive all observed inputs needed by an isolated model-backtest DB."""
    for ticker, symbol in (
        ('SPY', 'SPY'), ('BIL', 'BIL_RAW'), ('BOXX', 'BOXX_RAW'),
        ('QQQ', 'QQQ_RAW'), ('QQQI', 'QQQI_RAW'),
    ):
        _write_yahoo(db, ticker, symbol, start, requested_end)
    _write_series(db, 'CNY=X', 'fx', 'usd_cny', 1.0, start, requested_end)
    _write_series(db, '^IRX', 'rates', 'annual_rate', .01, start, requested_end)
    sync_cboe_vix(db, start, requested_end)
    _continuous_collateral(db)
    _copy_qqq_signal(db)
    # Only use dates where every strategy input is observed.  This avoids a
    # fabricated holiday calendar and makes all cutoffs auditable.
    row = db.execute('''SELECT MIN(last_date) FROM (
      SELECT MAX(date) last_date FROM bars WHERE symbol='SPY'
      UNION ALL SELECT MAX(date) FROM bars WHERE symbol='BOXX'
      UNION ALL SELECT MAX(date) FROM bars WHERE symbol='QQQM'
      UNION ALL SELECT MAX(date) FROM bars WHERE symbol='VIX')''').fetchone()
    if not row or not row[0]:
        raise ValueError('no common historical market end date')
    return date.fromisoformat(row[0])


class ModelSimulation(Simulation):
    """Existing rule engine driven by on-demand CRR option quotes."""
    def __init__(self, db, trading_days):
        super().__init__(db)
        self.trading_days = tuple(trading_days)
        self.day_set = {x.isoformat() for x in self.trading_days}
        self.week_starts = {}
        self.month_ends = set()
        for day in self.trading_days:
            self.week_starts.setdefault(day.isocalendar()[:2], day)
        for day in self.trading_days:
            next_day = next((x for x in self.trading_days if x > day), None)
            if next_day is None or next_day.month != day.month:
                self.month_ends.add(day)
        self.quote_cache = {}

    def week_start(self, day):
        return self.week_starts.get(day.isocalendar()[:2])

    def month_end(self, day):
        return day if day in self.month_ends else date.min

    def _context(self, day):
        spot = float(self.bar(day, 'SPY')['close'])
        vix = float(self.bar(day, 'VIX')['close'])
        rate_row = self.db.execute('SELECT annual_rate FROM rates WHERE date<=? ORDER BY date DESC LIMIT 1',
                                   (day.isoformat(),)).fetchone()
        rate = float(rate_row['annual_rate']) if rate_row else .01
        dividends = self.db.execute('SELECT SUM(dividend) FROM bars WHERE symbol="SPY" AND date>? AND date<=?',
                                    ((day - timedelta(days=365)).isoformat(), day.isoformat())).fetchone()[0] or 0.0
        return spot, max(.05, min(1.5, vix / 100.0)), rate, max(0.0, float(dividends) / spot)

    @staticmethod
    def _contract(expiry, right, strike):
        return f'SPY:{expiry.isoformat()}:{right}:{strike:g}'

    def _quote(self, day, expiry, right, strike):
        contract = self._contract(expiry, right, strike)
        key = (day.isoformat(), contract)
        if key in self.quote_cache:
            return self.quote_cache[key]
        spot, sigma, rate, yield_rate = self._context(day)
        years = max(1, (expiry - day).days) / 365.0
        fair, delta = american_price_delta(spot, strike, years, rate, yield_rate, sigma, right, steps=48)
        half = max(.01, fair * .02)
        bid = max(.01, math.floor(max(.01, fair - half) * 100) / 100)
        ask = max(bid, math.ceil((fair + half) * 100) / 100)
        quote = {'date': day.isoformat(), 'contract': contract, 'expiration': expiry.isoformat(),
                 'strike': float(strike), 'right': right, 'bid': bid, 'ask': ask,
                 'quote_time': 'MODEL-EOD', 'source': f'Model option quote {MODEL_VERSION}',
                 'delta': float(delta), 'delta_method': 'model-american-crr',
                 'model': {'version': MODEL_VERSION, 'sigma': _round(sigma), 'rate': _round(rate),
                           'dividend_yield': _round(yield_rate), 'theoretical_mid': _round(fair),
                           'half_spread_pct': .02}}
        self.quote_cache[key] = quote
        return quote

    def quote(self, day, contract):
        try:
            _, expiry, right, strike = contract.split(':')
            expiry = date.fromisoformat(expiry)
            if day > expiry:
                return None
            return self._quote(day, expiry, right, float(strike))
        except (ValueError, IndexError):
            return None

    def _candidate_quotes(self, day, right, target, min_dte, max_dte):
        spot, sigma, rate, yield_rate = self._context(day)
        # Select a narrow candidate grid using the inexpensive European formula,
        # then price only the returned contracts with the CRR American tree.
        expiry = day + timedelta(days=(min_dte + max_dte) // 2)
        years = (expiry - day).days / 365.0
        if right == 'P':
            lo, hi = int(spot * .45), int(spot * 1.05)
        else:
            lo, hi = int(spot * .95), int(spot * 1.65)
        best = min(range(max(1, lo), max(lo + 1, hi) + 1, 2),
                   key=lambda strike: abs(abs(european_price_delta(
                       spot, strike, years, rate, yield_rate, sigma, right)[1]) - target))
        return [self._quote(day, expiry, right, strike) for strike in range(max(1, best - 3), best + 4)]

    def quotes(self, day, right):
        return self._candidate_quotes(day, right, .16 if right == 'P' else .09, 28, 35)

    def choose(self, day, right, target, min_dte, max_dte, spot, min_strike=0,
               max_strike=None, record_gap=True):
        candidates=[]
        for quote in self._candidate_quotes(day, right, target, min_dte, max_dte):
            if quote['contract'] in self.positions or quote['strike'] < min_strike:
                continue
            if max_strike is not None and quote['strike'] > max_strike:
                continue
            candidates.append((abs(abs(quote['delta']) - target), quote['contract'], quote))
        if candidates:
            _, _, quote=min(candidates)
            return quote, quote['delta']
        if record_gap:
            name='看跌期权' if right == 'P' else '看涨期权'
            self.gap(day, 'option_selection', f'{name}（{min_dte}–{max_dte} 天到期）：模型候选合约不可用')
        return None, None

    def event(self, *args, **kwargs):
        quote = kwargs.get('quote') if 'quote' in kwargs else (args[10] if len(args) > 10 else None)
        details = dict(kwargs.get('details') if 'details' in kwargs else (args[9] if len(args) > 9 else {}) or {})
        if isinstance(quote, dict) and quote.get('model'):
            details['model_quote'] = quote['model']
        if len(args) > 9:
            args = (*args[:9], details, *args[10:])
        else:
            kwargs['details'] = details
        super().event(*args, **kwargs)


def _trading_days(db, start, end):
    rows = db.execute('''SELECT date FROM bars WHERE symbol IN ('BOXX','SPY','QQQM','VIX')
      AND date BETWEEN ? AND ? GROUP BY date HAVING COUNT(DISTINCT symbol)=4 ORDER BY date''',
                      (start.isoformat(), end.isoformat())).fetchall()
    return [date.fromisoformat(row['date']) for row in rows]


def _drawdowns(values):
    peak = -float('inf')
    output = []
    for value in values:
        peak = max(peak, value)
        output.append(value / peak - 1 if peak else 0.0)
    return output


def _benchmark_rows(db, days):
    qqq = _rows(db, 'QQQ_RAW')
    qqqi = _rows(db, 'QQQI_RAW')
    value = 100.0
    prior = None
    rows = []
    switches = []
    for day in days:
        key = day.isoformat()
        use_actual = day >= BENCHMARK_SWITCH and key in qqqi
        source = qqqi[key] if use_actual else qqq.get(key)
        if source is None:
            continue
        raw = float(source['close'])
        if prior is not None:
            value *= (raw + float(source['dividend'])) / prior
        if use_actual and not switches:
            switches.append({'date': key, 'from': 'QQQ 总回报代理', 'to': 'QQQI 实际总回报'})
        rows.append({'date': key, 'value': _round(value), 'source': 'QQQI' if use_actual else 'QQQ proxy'})
        prior = raw
    return rows, switches


def _metrics(sim, benchmark):
    wealth = [float(row[8]) + float(row[10]) for row in sim.nav_rows]
    dates = [row[0] for row in sim.nav_rows]
    if len(wealth) < 2:
        raise ValueError('backtest did not produce enough daily NAV rows')
    returns = [wealth[i] / wealth[i - 1] - 1 for i in range(1, len(wealth))]
    mean = sum(returns) / len(returns)
    variance = sum((x - mean) ** 2 for x in returns) / max(1, len(returns) - 1)
    volatility = math.sqrt(variance) * math.sqrt(252)
    rate_rows = [sim.db.execute('SELECT annual_rate FROM rates WHERE date<=? ORDER BY date DESC LIMIT 1',
                                (day,)).fetchone() for day in dates[1:]]
    daily_rf = sum(float(row['annual_rate']) / 252 for row in rate_rows if row) / max(1, len(rate_rows))
    sharpe = ((mean - daily_rf) / math.sqrt(variance) * math.sqrt(252)) if variance else 0.0
    dds = _drawdowns(wealth)
    trough = dds.index(min(dds))
    recovery = next((index - trough for index in range(trough + 1, len(wealth))
                     if wealth[index] >= max(wealth[:trough + 1])), None)
    # Determine option win/loss from each contract's option cash flows only.
    # Assignment cash purchases/sales belong to the SPY lot, not the option
    # premium result, and must not turn every assignment into a forced loss.
    grouped = {}
    terminal = {'TAKE_PROFIT', 'FORCE_CLOSE', 'ASSIGNMENT_DELEVER', 'OPTION_EXPIRY', 'PUT_ASSIGN', 'CALL_ASSIGN', 'HEDGE_EXIT', 'HEDGE_HARVEST'}
    option_cash = {'SELL_PUT', 'SELL_CALL', 'BUY_HEDGE', 'TAKE_PROFIT', 'FORCE_CLOSE',
                   'OPTION_EXPIRY', 'HEDGE_EXIT', 'HEDGE_HARVEST'}
    for event in sim.events:
        contract = event[4]
        if not contract:
            continue
        bucket = grouped.setdefault(contract, {'cash': 0.0, 'terminal': False})
        if event[2] in option_cash:
            bucket['cash'] += float(event[7])
        if event[2] in terminal:
            bucket['terminal'] = True
    closed = [value for value in grouped.values() if value['terminal']]
    wins = sum(1 for value in closed if value['cash'] > 0)
    start = date.fromisoformat(dates[0]); end = date.fromisoformat(dates[-1])
    years = max((end - start).days / 365.25, 1 / 365.25)
    b_start, b_end = benchmark[0]['value'], benchmark[-1]['value']
    return {'total_return': _round(wealth[-1] / INITIAL_CASH - 1),
            'annualized_return': _round((wealth[-1] / INITIAL_CASH) ** (1 / years) - 1),
            'max_drawdown': _round(min(dds)), 'annualized_volatility': _round(volatility),
            'sharpe_ratio': _round(sharpe),
            'calmar_ratio': _round(((wealth[-1] / INITIAL_CASH) ** (1 / years) - 1) / abs(min(dds))) if min(dds) else None,
            'recovery_days': recovery, 'win_rate': _round(wins / len(closed)) if closed else 0.0,
            'trade_count': len(closed), 'final_equity': _round(float(sim.nav_rows[-1][8])),
            'total_distributions_usd': _round(float(sim.nav_rows[-1][10])),
            'total_distributions_cny': _round(float(sim.nav_rows[-1][11])),
            'benchmark_return': _round(b_end / b_start - 1),
            'benchmark_max_drawdown': _round(min(_drawdowns([x['value'] for x in benchmark]))),
            'trading_days': len(wealth)}


def _fingerprint(db):
    digest = hashlib.sha256()
    digest.update(MODEL_VERSION.encode())
    for table, fields in (('bars', 'date,symbol,close,dividend,source'), ('fx', 'date,usd_cny,source'), ('rates', 'date,annual_rate,source')):
        for row in db.execute(f'SELECT {fields} FROM {table} ORDER BY date,symbol' if table == 'bars' else f'SELECT {fields} FROM {table} ORDER BY date'):
            digest.update(json.dumps(tuple(row), separators=(',', ':'), ensure_ascii=False).encode())
    return digest.hexdigest()


def run_backtest(years, end=None):
    years = int(years)
    if years not in (2, 10):
        raise ValueError('years must be 2 or 10')
    db = connect(database_path(years))
    requested_end = date.fromisoformat(end) if end else date.today()
    requested_start = requested_end - timedelta(days=years * 366 + 440)
    market_end = sync_market(db, requested_start, requested_end)
    cutoff = market_end - timedelta(days=round(years * 365.25))
    days = _trading_days(db, cutoff, market_end)
    if len(days) < years * 200:
        raise ValueError(f'insufficient common sessions for {years} year backtest')
    sim = ModelSimulation(db, days)
    for day in days:
        before = len(sim.events)
        if not sim.day(day):
            del sim.events[before:]
            raise RuntimeError(f'model backtest stopped on {day.isoformat()}: {sim.gaps[-1:]}' )
    benchmark, switches = _benchmark_rows(db, days)
    metrics = _metrics(sim, benchmark)
    with db:
        for table in ('events', 'daily_nav', 'data_gaps', 'open_positions'):
            db.execute(f'DELETE FROM {table}')
        db.executemany('INSERT INTO events VALUES(NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', sim.events)
        db.executemany('INSERT INTO daily_nav VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', sim.nav_rows)
        db.executemany('INSERT OR IGNORE INTO data_gaps VALUES(?,?,?)', sim.gaps)
        db.executemany('INSERT INTO open_positions VALUES(?,?,?,?,?,?,?,?,?)', [
            (p['contract'], p['expiration'], p['strike'], p['right'], p['side'], p['count'],
             p['entry'], p['delta'], p['lot_cost']) for p in sim.positions.values()])
        db.execute('CREATE TABLE IF NOT EXISTS model_benchmark(date TEXT PRIMARY KEY,value REAL NOT NULL,source TEXT NOT NULL)')
        db.execute('DELETE FROM model_benchmark')
        db.executemany('INSERT INTO model_benchmark VALUES(?,?,?)', [(x['date'], x['value'], x['source']) for x in benchmark])
        config = {'years': years, 'start_date': days[0].isoformat(), 'end_date': days[-1].isoformat(),
                  'model_version': MODEL_VERSION, 'option_pricing': 'CRR American, VIX sigma, 2% half-spread',
                  'collateral_proxy': 'BIL before 2022-12-28; BOXX total-return leg thereafter',
                  'benchmark_proxy': 'QQQ before 2024-01-29; QQQI total-return leg thereafter',
                  'benchmark_switches': switches, 'initial_capital': INITIAL_CASH}
        set_meta(db, 'model_backtest_config', json.dumps(config, ensure_ascii=False, sort_keys=True))
        set_meta(db, 'model_backtest_metrics', json.dumps(metrics, ensure_ascii=False, sort_keys=True))
        set_meta(db, 'model_backtest_fingerprint', _fingerprint(db))
        set_meta(db, 'model_backtest_completed_at', datetime.now().isoformat())
    return {'years': years, 'start_date': days[0].isoformat(), 'end_date': days[-1].isoformat(),
            'events': len(sim.events), 'metrics': metrics, 'fingerprint': get_meta(db, 'model_backtest_fingerprint')}


def load_backtest(years):
    path = database_path(years)
    if not path.exists():
        return None
    db = connect(path)
    config = get_meta(db, 'model_backtest_config')
    metrics = get_meta(db, 'model_backtest_metrics')
    if not config or not metrics:
        return None
    nav = [dict(row) for row in db.execute('SELECT * FROM daily_nav ORDER BY date')]
    benchmark = [dict(row) for row in db.execute('SELECT * FROM model_benchmark ORDER BY date')]
    events = [dict(row) for row in db.execute('SELECT * FROM events ORDER BY date,id')]
    return {'config': json.loads(config), 'metrics': json.loads(metrics), 'fingerprint': get_meta(db, 'model_backtest_fingerprint'),
            'nav': nav, 'benchmark': benchmark, 'events': events}
