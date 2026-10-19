"""Compare archived model Delta with matching external chain rows.

CSV columns: date,expiration,strike,right,delta. This is a diagnostic only;
external vendor Greeks must never be used as fill prices or 2026 main data.
"""
import argparse
import csv
import statistics
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from strategy.db import connect
from strategy.engine import Simulation


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('csv_file')
    parser.add_argument('--db',default=None)
    args=parser.parse_args()
    db=connect(args.db);sim=Simulation(db);errors=[];unmatched=0
    with open(args.csv_file,newline='') as f:
        for row in csv.DictReader(f):
            try:
                day=date.fromisoformat(row['date'][:10])
                right=row['right'].upper()[0]
                contract=f"SPY:{row['expiration'][:10]}:{right}:{float(row['strike']):g}"
                q=sim.quote(day,contract);underlying=sim.bar(day,'SPY')
                if not q or not underlying:
                    unmatched+=1;continue
                calculated=sim.delta(day,q,underlying['close'])
                errors.append(abs(calculated-float(row['delta'])))
            except (ValueError,KeyError,ZeroDivisionError):unmatched+=1
    if errors:
        print(f'matched={len(errors)} unmatched={unmatched} mean_absolute_delta_error={statistics.mean(errors):.5f} max_error={max(errors):.5f}')
    else:
        print(f'no matching archived contracts; unmatched={unmatched}')
        raise SystemExit(2)


if __name__=='__main__':main()
