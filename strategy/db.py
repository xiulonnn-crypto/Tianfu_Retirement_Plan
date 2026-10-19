"""Raw market archive and deterministic simulation ledger."""
import os
import sqlite3
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "tianfu.sqlite3"


def connect(path=None):
    path = Path(path or os.environ.get("TIANFU_DB", DEFAULT_DB))
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    init(db)
    return db


def init(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS bars (
      date TEXT NOT NULL, symbol TEXT NOT NULL, close REAL NOT NULL,
      dividend REAL NOT NULL DEFAULT 0, source TEXT NOT NULL, source_time TEXT,
      PRIMARY KEY(date,symbol));
    CREATE TABLE IF NOT EXISTS option_quotes (
      date TEXT NOT NULL, contract TEXT NOT NULL, expiration TEXT NOT NULL,
      strike REAL NOT NULL, right TEXT NOT NULL CHECK(right IN ('P','C')),
      bid REAL NOT NULL, ask REAL NOT NULL, quote_time TEXT,
      source TEXT NOT NULL, delta REAL, delta_method TEXT,
      PRIMARY KEY(date,contract));
    CREATE INDEX IF NOT EXISTS option_by_date ON option_quotes(date,right,expiration);
    CREATE TABLE IF NOT EXISTS fx (
      date TEXT PRIMARY KEY, usd_cny REAL NOT NULL, source TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS rates (
      date TEXT PRIMARY KEY, annual_rate REAL NOT NULL, source TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS events (
      id INTEGER PRIMARY KEY, event_key TEXT UNIQUE NOT NULL, date TEXT NOT NULL,
      kind TEXT NOT NULL, symbol TEXT, contract TEXT, quantity REAL,
      price REAL, cash_change REAL NOT NULL DEFAULT 0, fee REAL NOT NULL DEFAULT 0,
      reason TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}', quote_date TEXT,
      quote_source TEXT, rule_version TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS events_by_date ON events(date,id);
    CREATE TABLE IF NOT EXISTS daily_nav (
      date TEXT PRIMARY KEY, cash REAL NOT NULL, boxx_shares INTEGER NOT NULL,
      spy_shares INTEGER NOT NULL, boxx_value REAL NOT NULL, spy_value REAL NOT NULL,
      long_put_value REAL NOT NULL, short_liability REAL NOT NULL,
      nav REAL NOT NULL, realized_profit REAL NOT NULL, distributions_usd REAL NOT NULL,
      distributions_cny REAL NOT NULL, arrears_cny REAL NOT NULL,
      put_collateral REAL NOT NULL, drawdown REAL NOT NULL,
      adjusted_return REAL NOT NULL, rule_version TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS data_gaps (
      date TEXT NOT NULL, category TEXT NOT NULL, detail TEXT NOT NULL,
      PRIMARY KEY(date,category,detail));
    CREATE TABLE IF NOT EXISTS open_positions (
      contract TEXT PRIMARY KEY, expiration TEXT NOT NULL, strike REAL NOT NULL,
      right TEXT NOT NULL, side TEXT NOT NULL, contracts INTEGER NOT NULL,
      entry_price REAL NOT NULL, entry_delta REAL NOT NULL, cost_basis REAL);
    CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)


def upsert_bar(db, date, symbol, close, source, dividend=0, source_time=None):
    if close is None or float(close) <= 0:
        raise ValueError("invalid close")
    db.execute("""INSERT INTO bars VALUES(?,?,?,?,?,?) ON CONFLICT(date,symbol)
      DO UPDATE SET close=excluded.close,dividend=excluded.dividend,
      source=excluded.source,source_time=excluded.source_time""",
      (date, symbol, float(close), float(dividend), source, source_time))


def upsert_quote(db, date, contract, expiration, strike, right, bid, ask,
                 source, quote_time=None, delta=None, delta_method=None):
    if right not in ("P", "C") or bid is None or ask is None or bid < 0 or ask <= 0 or bid > ask:
        raise ValueError("invalid option quote")
    db.execute("""INSERT INTO option_quotes VALUES(?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(date,contract) DO UPDATE SET bid=excluded.bid,ask=excluded.ask,
      quote_time=excluded.quote_time,source=excluded.source,delta=excluded.delta,
      delta_method=excluded.delta_method""",
      (date, contract, expiration, float(strike), right, float(bid), float(ask),
       quote_time, source, delta, delta_method))


def set_meta(db, key, value):
    db.execute("INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def get_meta(db, key, default=None):
    row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    return row[0] if row else default
