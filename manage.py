"""CLI: init, backfill, daily, replay, status."""
import argparse
import json
from datetime import date,timedelta
from strategy.db import connect
from strategy.engine import replay
from strategy.calendar import latest_publishable_session
from strategy.providers import sync_all


def main():
    parser=argparse.ArgumentParser(description='天府退休计划日终报价模拟')
    parser.add_argument('command',choices=['init','backfill','daily','benchmark','backtest','replay','status'])
    parser.add_argument('--start',default=None,help='YYYY-MM-DD')
    parser.add_argument('--end',default=None,help='YYYY-MM-DD')
    parser.add_argument('--years',default=None,help='模型回测周期，2、10 或 2,10')
    parser.add_argument('--db',default=None)
    parser.add_argument('--no-options',action='store_true',help='只同步股票与汇率，不模拟收益')
    args=parser.parse_args();db=connect(args.db)
    if args.command=='init':print('数据库已初始化');return
    if args.command=='backtest':
        from strategy.backtest import run_backtest
        periods=(2,10) if args.years is None else tuple(int(x) for x in args.years.split(','))
        for years in periods:
            result=run_backtest(years,args.end)
            print(json.dumps(result,ensure_ascii=False,sort_keys=True))
        return
    if args.command=='benchmark':
        from strategy.providers import sync_bars
        start=date.fromisoformat(args.start or '2024-01-01')
        end=date.fromisoformat(args.end) if args.end else latest_publishable_session()
        for error in sync_bars(db,start,end,symbols=('QQQI',)):print('DATA ERROR:',error)
        return
    if args.command in ('backfill','daily'):
        if args.command=='backfill':
            start=date.fromisoformat(args.start or '2025-01-01')
        else:
            last=db.execute('SELECT MAX(date) FROM daily_nav').fetchone()[0]
            start=date.fromisoformat(last) if last else date(2025,1,1)
        end=date.fromisoformat(args.end) if args.end else latest_publishable_session()
        if end<start:parser.error('end before start')
        errors=sync_all(db,start,end,not args.no_options)
        for error in errors:print('DATA ERROR:',error)
        if errors:print(f'{len(errors)} 个来源或交易日抓取失败；未使用估算行情。')
    if args.command in ('backfill','daily','replay'):
        print(replay(db,until=args.end))
    if args.command=='status':
        last=db.execute('SELECT MAX(date) FROM daily_nav').fetchone()[0]
        print('最新完整模拟日:',last or '待回填')
        for row in db.execute('SELECT * FROM data_gaps ORDER BY date DESC LIMIT 10'):
            print(dict(row))


if __name__=='__main__':main()
