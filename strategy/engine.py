"""End-of-day, cash-secured, event-sourced simulation.

All derived tables are rebuilt atomically from archived raw inputs. No transaction is
executed using a synthetic fill. EOD bid/ask are quote-based simulation assumptions.
"""
import json
import math
from datetime import date, timedelta
from . import RULE_VERSION
from .calendar import sessions, last_session_of_month,latest_publishable_session
from .pricing import implied_delta,approximate_delta

INITIAL_CASH = 250000.0
COMMISSION = 0.65  # USD per contract; configurable
DELTA_TOLERANCE = .03
NORMAL_PUT_CONTRACTS = 3


def put_target(vix):
    return .12 if vix>22 else .16


def harvest_count(vix, count):
    return count if vix>50 else math.ceil(count/2) if vix>40 else 0


def profit_exit_count(fraction, count, half_taken=False):
    if fraction>=.60:return count
    if fraction>=.55 and not half_taken:return math.ceil(count/2)
    return 0


def call_target(_spot, _effective_cost):
    """Covered Calls use the strategy's fixed absolute Delta target."""
    return .25


def target_delta_ok(actual, target):
    """Allow normal strike-grid rounding while rejecting a different risk bucket."""
    return abs(abs(actual)-target)<=DELTA_TOLERANCE


def insurance_trigger(prior_vix, prior_qqqm, ma20):
    return prior_vix<15 and prior_qqqm>ma20*.98


def _round(x):
    return round(float(x), 8)


class Simulation:
    def __init__(self, db, commission=COMMISSION):
        self.db=db; self.commission=commission
        self.cash=INITIAL_CASH; self.boxx=0; self.spy_lots=[]; self.positions={}
        self.realized=0.; self.withdrawn_usd=0.; self.withdrawn_cny=0.; self.arrears=0.
        self.annual_realized={}; self.annual_insurance_spend={}
        self.peak=INITIAL_CASH; self.events=[]; self.nav_rows=[]; self.gaps=[]
        self.completed=None; self.previous=None; self.slot=0
        self._model_cache={}

    def week_start(self, day):
        """First tradable day of the week.

        The live ledger uses the built-in NYSE calendar.  Historical model
        backtests override this with their archived market sessions.
        """
        monday=day-timedelta(days=day.weekday())
        return next(sessions(monday,day),None)

    def month_end(self, day):
        """Last tradable day of the month for this simulation."""
        return last_session_of_month(day)

    def event(self, day, kind, symbol=None, contract=None, quantity=None, price=None,
              cash_change=0, fee=0, reason="", details=None, quote=None):
        key=f"{day.isoformat()}:{len(self.events):06d}:{kind}"
        item=(key,day.isoformat(),kind,symbol,contract,quantity,price,_round(cash_change),
              _round(fee),reason,json.dumps(details or {},ensure_ascii=False,sort_keys=True),
              quote['date'] if quote else None,quote['source'] if quote else None,RULE_VERSION)
        self.events.append(item)

    def gap(self, day, category, detail):
        self.gaps.append((day.isoformat(),category,str(detail)))

    def bar(self, day, symbol):
        return self.db.execute("SELECT * FROM bars WHERE date=? AND symbol=?",(day.isoformat(),symbol)).fetchone()

    def quote(self, day, contract):
        return self.db.execute("SELECT * FROM option_quotes WHERE date=? AND contract=?",(day.isoformat(),contract)).fetchone()

    def quotes(self, day, right):
        return self.db.execute("SELECT * FROM option_quotes WHERE date=? AND right=?",(day.isoformat(),right)).fetchall()

    def delta(self, day, quote, spot):
        if quote['delta'] is not None and quote['delta_method']=='model-american-crr':
            return quote['delta']
        years=max((date.fromisoformat(quote['expiration'])-day).days,1)/365
        rate,yield_rate=self.model_context(day,spot)
        value,_=implied_delta(round(spot,4),round(quote['strike'],4),round(years,6),
                              round(rate,6),round(yield_rate,6),round(quote['bid'],4),
                              round(quote['ask'],4),quote['right'])
        return value

    def model_context(self,day,spot):
        key=(day.isoformat(),spot)
        if key in self._model_cache:return self._model_cache[key]
        rate=self.db.execute("SELECT date,annual_rate FROM rates WHERE date<=? ORDER BY date DESC LIMIT 1",(day.isoformat(),)).fetchone()
        if not rate or (day-date.fromisoformat(rate['date'])).days>7 or rate['annual_rate'] < -0.05 or rate['annual_rate']>0.3:
            raise ValueError("missing or invalid risk-free rate")
        trailing=self.db.execute("SELECT SUM(dividend) FROM bars WHERE symbol='SPY' AND date>? AND date<=?",
                                 ((day-timedelta(days=365)).isoformat(),day.isoformat())).fetchone()[0] or 0
        yield_rate=trailing/spot
        self._model_cache[key]=(rate['annual_rate'],yield_rate)
        return self._model_cache[key]

    def choose(self, day, right, target, min_dte, max_dte, spot, min_strike=0,
               max_strike=None, record_gap=True):
        choices=[]; errors=0
        for q in self.quotes(day,right):
            if q['contract'] in self.positions:continue
            if q['bid']<=0 or q['ask']<=0:continue
            dte=(date.fromisoformat(q['expiration'])-day).days
            if (not min_dte<=dte<=max_dte or q['strike']<min_strike or
                    (max_strike is not None and q['strike']>max_strike)):
                continue
            try:
                if q['delta'] is not None and q['delta_method']=='model-american-crr':
                    approx=q['delta']
                else:
                    rate,yield_rate=self.model_context(day,spot)
                    approx=approximate_delta(spot,q['strike'],dte/365,rate,yield_rate,
                                             q['bid'],q['ask'],right)
            except (ValueError,OverflowError,ZeroDivisionError):
                errors+=1; continue
            choices.append((abs(abs(approx)-target),q))
        ranked=sorted(choices,key=lambda x:x[0])[:30]
        exact=[]
        for _,q in ranked:
            try:delta=self.delta(day,q,spot)
            except (ValueError,OverflowError,ZeroDivisionError):
                errors+=1;continue
            if (right=='P' and delta>=0) or (right=='C' and delta<=0):
                errors+=1; continue
            dte=(date.fromisoformat(q['expiration'])-day).days
            exact.append((abs(abs(delta)-target),abs(dte-(min_dte+max_dte)/2),q['contract'],q,delta))
        if not exact:
            if record_gap:
                option_name='看跌期权' if right=='P' else '看涨期权'
                self.gap(day,"option_selection",
                         f"{option_name}（{min_dte}–{max_dte} 天到期）：未找到可用于模型 Delta 的有效日终报价；"
                         f"{errors} 条候选合约模型无效")
            return None,None
        _,_,_,q,delta=min(exact,key=lambda x:x[:3])
        return q,delta

    def sell_boxx(self, day, amount, price, reason):
        need=max(0.,amount-self.cash)
        qty=min(self.boxx,math.ceil(need/price)) if need>0 else 0
        if qty:
            self.boxx-=qty; self.cash+=qty*price
            self.event(day,"SELL_BOXX","BOXX",quantity=qty,price=price,cash_change=qty*price,reason=reason)
        return self.cash+1e-7>=amount

    def buy_boxx(self, day, price, reason):
        qty=max(0,math.floor((self.cash+1e-7)/price))
        if qty:
            self.cash-=qty*price; self.boxx+=qty
            self.event(day,"BUY_BOXX","BOXX",quantity=qty,price=price,cash_change=-qty*price,reason=reason)

    def profit(self, day, amount):
        self.realized+=amount
        self.annual_realized[day.year]=self.annual_realized.get(day.year,0.)+amount

    def close_option(self, day, pos, q, count, kind, boxx_price, details=None):
        # Long insurance sells at bid; short options buy at ask.
        try:exit_delta=self.delta(day,q,self.bar(day,'SPY')['close'])
        except (ValueError,OverflowError,ZeroDivisionError) as exc:
            self.gap(day,'option_delta',f'{pos["contract"]}: {exc}')
            return False
        long=pos['side']=='long'; price=q['bid'] if long else q['ask']
        fee=count*self.commission
        change=count*100*price*(1 if long else -1)-fee
        if change<0 and not self.sell_boxx(day,-change,boxx_price,"期权平仓备款"):
            self.event(day,"VIOLATION",reason="平仓资金不足，暂停新开仓",details={'contract':pos['contract']})
            self.gap(day,'violation',f'{pos["contract"]}: insufficient funds to close')
            return False
        self.cash+=change
        earned=(price-pos['entry'])*100*count if long else (pos['entry']-price)*100*count
        self.profit(day,earned-fee)
        pos['count']-=count
        self.event(day,kind,"SPY",pos['contract'],count,price,change,fee,
                   "按历史日终报价模拟成交",dict(details or {},entry=pos['entry'],
                   entry_delta=pos['delta'],exit_delta=exit_delta,
                   delta_method='model-american-crr'),q)
        if pos['count']==0: del self.positions[pos['contract']]
        return True

    def open_option(self, day, q, delta, count, side, kind, boxx_price, details=None):
        price=q['ask'] if side=='long' else q['bid']
        fee=count*self.commission
        change=count*100*price*(1 if side=='short' else -1)-fee
        if change<0 and not self.sell_boxx(day,-change,boxx_price,"购入保险备款"):
            self.event(day,"SKIP",reason="现金及 BOXX 不足以购入期权",details={'contract':q['contract']})
            return False
        self.cash+=change; self.profit(day,-fee)
        self.positions[q['contract']]={'contract':q['contract'],'expiration':q['expiration'],
          'strike':q['strike'],'right':q['right'],'side':side,'count':count,
          'entry':price,'entry_net':price-self.commission/100 if side=='short' else price+self.commission/100,
          'delta':delta,'half_taken':False,'half_harvested':False,
          'lot_cost':(details or {}).get('lot_cost'),
          'lot_id':(details or {}).get('lot_id'),'slot':(details or {}).get('slot')}
        self.event(day,kind,"SPY",q['contract'],count,price,change,fee,
                   "按历史日终报价模拟成交",dict(details or {},target_delta=(details or {}).get('target_delta'),
                   actual_delta=delta,delta_method='model-american-crr',contracts_rounded=count),q)
        return True

    def handle_expiry(self, day, pos, spy, boxx_price):
        count=pos['count']; itm=(pos['right']=='P' and spy<pos['strike']) or (pos['right']=='C' and spy>pos['strike'])
        if pos['side']=='long':
            self.event(day,"VIOLATION",reason="长 PUT 未在到期前一周卖出，停止模拟")
            self.gap(day,'violation',f'{pos["contract"]}: long put reached expiry')
            return False
        elif pos['right']=='P' and itm:
            need=pos['strike']*100*count
            # First liquidate the collateral asset.  If the resulting cash cannot
            # fund every 100-share assignment, buy back only the excess PUTs.
            # Existing SPY lots are never sold to finance a new assignment.
            self.sell_boxx(day,need,boxx_price,"PUT 到期接货备款")
            while pos['count'] and self.cash+1e-7 < pos['strike']*100*pos['count']:
                q=self.quote(day,pos['contract'])
                if not q or not self.close_option(
                    day,pos,q,1,'ASSIGNMENT_DELEVER',boxx_price,
                    {'assignment_deleveraging':True,
                     'reason':'到期接货资金不足，平仓部分 PUT 保留整百股接货'}):
                    self.event(day,"VIOLATION",reason="PUT 到期接货资金不足，无法按报价平仓部分合约",
                               details={'contract':pos['contract']})
                    self.gap(day,'violation',f'{pos["contract"]}: assignment insufficient funds')
                    return False
            if not pos['count']:
                return True
            count=pos['count'];need=pos['strike']*100*count
            self.cash-=need
            cost=pos['strike']-pos['entry_net']
            self.spy_lots.append({'id':f'{day}:{pos["contract"]}','shares':100*count,
                                  'cost':cost,'tax_basis':pos['strike']})
            self.event(day,"PUT_ASSIGN","SPY",pos['contract'],count,pos['strike'],0,0,
                       "价内 PUT 指派完成",{'shares':100*count,'effective_cost':cost})
            self.event(day,"BUY_SPY_ASSIGNMENT","SPY",pos['contract'],100*count,cost,-need,0,
                       "PUT 接货股票买入",{'shares':100*count,'strike':pos['strike'],
                                            'effective_cost':cost})
            self.profit(day,pos['entry']*100*count)
        elif pos['right']=='C' and itm:
            shares=100*count
            if sum(l['shares'] for l in self.spy_lots)<shares:
                self.event(day,"VIOLATION",reason="CALL 缺少备兑股票，停止开仓")
                self.gap(day,'violation',f'{pos["contract"]}: call uncovered')
                return False
            received=pos['strike']*shares; self.cash+=received
            remaining=shares; basis=0.
            lots=[x for x in self.spy_lots if x['id']==pos.get('lot_id')]
            if sum(x['shares'] for x in lots)<shares:
                self.event(day,'VIOLATION',reason='CALL 接货批次备兑不足，停止开仓')
                self.gap(day,'violation',f'{pos["contract"]}: lot uncovered')
                return False
            for lot in lots:
                used=min(remaining,lot['shares']); basis+=used*lot['tax_basis']
                lot['shares']-=used;remaining-=used
                if not remaining:break
            self.spy_lots=[x for x in self.spy_lots if x['shares']]
            self.profit(day,received-basis+pos['entry']*100*count)
            effective_exit=pos['strike']+pos['entry_net']
            self.event(day,"CALL_ASSIGN","SPY",pos['contract'],count,pos['strike'],0,0,
                       "价内 Covered Call 交割完成",{'shares':shares,'basis':basis,
                                                        'effective_exit':effective_exit})
            self.event(day,"SELL_SPY_DELIVERY","SPY",pos['contract'],shares,effective_exit,received,0,
                       "Covered Call 股票交割卖出",{'shares':shares,'strike':pos['strike'],
                                                    'effective_exit':effective_exit,'basis':basis})
        else:
            self.profit(day,pos['entry']*100*count)
            self.event(day,"OPTION_EXPIRY","SPY",pos['contract'],count,0,0,0,"价外期权到期")
        del self.positions[pos['contract']]
        return True

    def obligations(self):
        return sum(p['strike']*100*p['count'] for p in self.positions.values()
                   if p['side']=='short' and p['right']=='P')

    def mark(self, day, bars):
        boxx=bars['BOXX']['close']; spy=bars['SPY']['close']
        long_value=0.; liability=0.
        for p in self.positions.values():
            q=self.quote(day,p['contract'])
            if not q:
                self.gap(day,'option_mark',p['contract']); return None
            if p['side']=='long': long_value+=q['bid']*100*p['count']
            else: liability+=q['ask']*100*p['count']
        nav=self.cash+self.boxx*boxx+sum(l['shares'] for l in self.spy_lots)*spy+long_value-liability
        return nav,long_value,liability

    def settle_month(self, day, boxx_price):
        fx=self.db.execute("SELECT * FROM fx WHERE date<=? ORDER BY date DESC LIMIT 1",(day.isoformat(),)).fetchone()
        if not fx or (day-date.fromisoformat(fx['date'])).days>7:
            self.gap(day,'fx','USD/CNY missing or older than 7 days');return False
        due=20000+self.arrears; usd=due/fx['usd_cny']
        available=max(0.,self.realized-self.withdrawn_usd)
        collateral_headroom=max(0.,self.cash+self.boxx*boxx_price-self.obligations())
        # The three-PUT weekly rule no longer reserves cash or BOXX as a full
        # cash-secured position.  Therefore an outstanding PUT's notional is
        # audit information, not a restriction on paying realised profit.  A
        # partial payment sells BOXX when necessary and carries only the
        # unpaid CNY target into the following month.
        paid_usd=min(usd,available)
        if paid_usd>1e-7 and self.sell_boxx(day,paid_usd,boxx_price,"月度利润提取备款"):
            paid_cny=paid_usd*fx['usd_cny']
            self.cash-=paid_usd;self.withdrawn_usd+=paid_usd;self.withdrawn_cny+=paid_cny
            self.arrears=max(0.,due-paid_cny)
            self.event(day,'WITHDRAW','USD/CNY',quantity=due,price=fx['usd_cny'],
                       cash_change=-paid_usd,
                       reason='月末人民币目标及累计欠额全额提取' if self.arrears<=1e-7 else '月末可分配利润全部提取，剩余金额递延',
                       details={'due_cny':due,'paid_cny':paid_cny,'paid_usd':paid_usd,
                                'arrears_cny':self.arrears,'fx_source':fx['source'],
                                'realized_available':available,'collateral_headroom':collateral_headroom})
        else:
            self.arrears=due
            self.event(day,'DEFER',reason='无可分配已实现净利润，目标全额递延',
                       details={'due_cny':due,'required_usd':usd,'available_usd':available,
                                'collateral_headroom':collateral_headroom})
        return True

    def day(self, day):
        bars={x:self.bar(day,x) for x in ('BOXX','SPY','QQQM','VIX')}
        for x,v in bars.items():
            if not v:
                self.gap(day,'bar',x); return False
        boxx=bars['BOXX']['close']; spy=bars['SPY']['close']; vix=bars['VIX']['close']
        if self.completed is None: self.buy_boxx(day,boxx,"首个交易日将初始资金投入 BOXX")
        # Dividends belong to positions held before today's option settlement.
        dividend=self.boxx*bars['BOXX']['dividend']+sum(x['shares'] for x in self.spy_lots)*bars['SPY']['dividend']
        if dividend:
            self.cash+=dividend;self.profit(day,dividend)
            self.event(day,'DIVIDEND',cash_change=dividend,reason='已持有股票现金分红')
        # Existing positions require a real closing quote until expiration.
        for pos in list(self.positions.values()):
            if day.isoformat()<pos['expiration'] and not self.quote(day,pos['contract']):
                self.gap(day,'option_quote',pos['contract']); return False
        for pos in list(self.positions.values()):
            if day.isoformat()>=pos['expiration']:
                if not self.handle_expiry(day,pos,spy,boxx): return False
                continue
            q=self.quote(day,pos['contract'])
            if pos['side']=='long':
                if (date.fromisoformat(pos['expiration'])-day).days<=7:
                    if not self.close_option(day,pos,q,pos['count'],'HEDGE_EXIT',boxx):return False
                    self.buy_boxx(day,boxx,"保险到期前回流 BOXX")
                    continue
                count=harvest_count(vix,pos['count']) if vix>50 or not pos['half_harvested'] else 0
                if count:
                    if not self.close_option(day,pos,q,count,'HEDGE_HARVEST',boxx):return False
                    if vix<=50 and pos['contract'] in self.positions:
                        pos['half_harvested']=True
                    self.buy_boxx(day,boxx,"保险收割回流 BOXX")
                continue
            # Covered Calls are landlord contracts: keep them through expiry
            # so the assigned SPY lot is sold only by an in-the-money exercise.
            if pos['right']=='C' and pos.get('lot_id'):
                continue
            gross=pos['entry_net']; profit_fraction=(gross-q['ask']-self.commission/100)/gross if gross>0 else -1
            count=0; why=None
            if profit_fraction>=0.60: count=profit_exit_count(profit_fraction,pos['count'],pos['half_taken']); why='60% 全部止盈'
            elif profit_fraction>=0.55 and not pos['half_taken']:
                count=profit_exit_count(profit_fraction,pos['count'],pos['half_taken']); why='55% 向上取整平半'
                pos['half_taken']=True
            elif (date.fromisoformat(pos['expiration'])-day).days<=7:
                itm=(pos['right']=='P' and spy<pos['strike']) or (pos['right']=='C' and spy>pos['strike'])
                if not (pos['right']=='P' and itm): count=pos['count']; why='到期前一周强制结清'
            if count and not self.close_option(day,pos,q,count,'TAKE_PROFIT' if '止盈' in why or '平半' in why else 'FORCE_CLOSE',boxx):return False
        mark=self.mark(day,bars)
        if mark is None:return False
        nav=mark[0]
        # A week has one scheduled ladder turn. A Monday holiday rolls to Tuesday.
        week_start=self.week_start(day)
        if day==week_start:
            target=put_target(vix)
            scheduled_slot=self.slot
            occupied_slots={p.get('slot') for p in self.positions.values()
                            if p['side']=='short' and p['right']=='P' and p.get('slot') is not None}
            if scheduled_slot in occupied_slots:
                # Keep the scheduled four-week rotation, but use a vacant ladder
                # unit when one is available.  This preserves the weekly rent
                # cadence without stacking two puts in the same unit.
                vacant_slot=next((slot for slot in range(4) if slot not in occupied_slots),None)
            else:
                vacant_slot=scheduled_slot
            if vacant_slot is None:
                self.event(day,'SKIP',reason='四周阶梯均有未平仓 PUT，备兑额度已全部占用',
                           details={'scheduled_slot':scheduled_slot,'occupied_slots':sorted(occupied_slots)})
                self.slot=(scheduled_slot+1)%4
                # Continue with landlord and hedge rules.
                q=None;occupied=True
            else:
                occupied=False
            if not occupied:
                liquid=max(0.,self.cash+self.boxx*boxx-self.obligations())
                # Keep coverage figures for audit only.  The unassigned-stage
                # three-PUT rule no longer screens on cash/BOXX collateral.
                ladder_reference=nav*.25
                coverage_limit=liquid
                throttle_limit=coverage_limit
                if vix<15:
                    throttle_limit=min(coverage_limit,max(0.,nav*.21-self.obligations()))
                # Before assignment, the weekly rent unit is exactly three
                # target-Delta puts.  During landlord mode retain the original
                # one-PUT unit, but only when BOXX plus cash has enough unused
                # collateral after existing short-PUT obligations.
                desired_count=1 if self.spy_lots else NORMAL_PUT_CONTRACTS
                q=delta=None;count=0
                candidate,actual=self.choose(day,'P',target,28,35,spy,record_gap=False)
                if candidate and target_delta_ok(actual,target):
                    q,delta,count=candidate,actual,desired_count
                    if self.spy_lots:
                        count=min(count,int((liquid+1e-7)//(q['strike']*100)))
                weekly_floor_override=bool(q and vix<15 and
                                            count*q['strike']*100>throttle_limit)
                # A missing usable chain is a data-quality failure: do not publish
                # a partly simulated day.
                if q is None:
                    available,_=self.choose(day,'P',target,28,35,spy)
                    if available is None:
                        return False
            if not occupied:
                if q is None:
                    self.event(day,'SKIP',reason='无符合目标 Delta 的 28–35 天 SPY PUT',
                               details={'slot':vacant_slot,'coverage_limit':coverage_limit,'vix':vix,
                                        'target_delta':target})
                elif count:
                    self.open_option(day,q,delta,count,'short','SELL_PUT',boxx,
                                     {'target_delta':target,'slot':vacant_slot,
                                      'collateral':count*q['strike']*100,
                                      'weekly_floor_override':weekly_floor_override,
                                      'desired_contracts':desired_count,
                                      'throttle_limit':throttle_limit,
                                      'coverage_limit':coverage_limit,
                                      'ladder_reference':ladder_reference,
                                      'scheduled_slot':scheduled_slot})
                else:
                    self.event(day,'SKIP',reason='无符合目标 Delta 的全额备兑 28–35 天 SPY PUT',
                               details={'slot':vacant_slot,'coverage_limit':coverage_limit,'vix':vix,
                                        'target_delta':target,'actual_delta':delta,
                                        'delta_tolerance':DELTA_TOLERANCE})
                self.slot=(scheduled_slot+1)%4
        # Calls are only written against SPY lots received through PUT
        # assignment.  The ordinary weekly cycle has no uncovered CALL leg.
        if day==week_start:
            for lot in self.spy_lots:
                covered=sum(p['count']*100 for p in self.positions.values()
                            if p['side']=='short' and p['right']=='C' and p.get('lot_id')==lot['id'])
                free=max(0,lot['shares']-covered)
                count=free//100
                if not count:continue
                target=call_target(spy,lot['cost'])
                q,delta=self.choose(day,'C',target,28,35,spy,min_strike=lot['cost'])
                if q and q['contract'] not in self.positions and target_delta_ok(delta,target):
                    self.open_option(day,q,delta,count,'short','SELL_CALL',boxx,
                                     {'target_delta':target,'lot_cost':lot['cost'],'lot_id':lot['id']})
                else:self.event(day,'SKIP',reason='Covered Call 无符合成本与报价条件的合约',
                                details={'target_delta':target,'actual_delta':delta if q else None,
                                         'delta_tolerance':DELTA_TOLERANCE,'lot_cost':lot['cost']})
        if self.previous:
            prior_vix=self.previous['VIX']['close']
            ma=self.db.execute("SELECT close FROM bars WHERE symbol='QQQM' AND date<=? ORDER BY date DESC LIMIT 20",
                               (self.previous_day.isoformat(),)).fetchall()
            if vix<=40 and len(ma)==20 and insurance_trigger(prior_vix,self.previous['QQQM']['close'],sum(x[0] for x in ma)/20):
                hedge_positions=[p for p in self.positions.values() if p['side']=='long' and p['right']=='P']
                # The strategy cannot spend any insurance budget before it has
                # realized profit.  Do not query a hedge chain or turn its
                # absence into a data gap when a purchase is impossible anyway.
                if hedge_positions:
                    self.event(day,'SKIP',reason='已有持仓保险，跳过当日购保',
                               details={'existing_contracts':[p['contract'] for p in hedge_positions]})
                else:
                    annual=max(0.,self.annual_realized.get(day.year,0.))*.016
                    budget=annual-self.annual_insurance_spend.get(day.year,0.)
                    if budget<=self.commission:
                        self.event(day,'SKIP',reason='保险预算不足一张或无有效报价',
                                   details={'annual_budget':annual,'remaining':budget})
                    else:
                        q,delta=self.choose(day,'P',.025,165,200,spy)
                        if q is None:
                            self.event(day,'SKIP',reason='保险无符合报价，跳过当日购保',
                                       details={'annual_budget':annual,'remaining':budget})
                        else:
                            cost=q['ask']*100+self.commission
                            collateral_safe=self.cash+self.boxx*boxx-cost+1e-7>=self.obligations()
                            if q['contract'] not in self.positions and budget+1e-7>=cost and collateral_safe:
                                if self.open_option(day,q,delta,1,'long','BUY_HEDGE',boxx,
                                                    {'target_delta':.025,'annual_budget':annual}):
                                    self.annual_insurance_spend[day.year]=self.annual_insurance_spend.get(day.year,0.)+cost
                            else:self.event(day,'SKIP',reason='保险预算不足一张或无有效报价',
                                            details={'annual_budget':annual,'remaining':budget})
        if day==self.month_end(day):
            if not self.settle_month(day,boxx):return False
        marked=self.mark(day,bars)
        if marked is None:return False
        nav,long_value,liability=marked; self.peak=max(self.peak,nav+self.withdrawn_usd)
        adjusted=(nav+self.withdrawn_usd)/INITIAL_CASH-1
        drawdown=(nav+self.withdrawn_usd)/self.peak-1
        row=(day.isoformat(),_round(self.cash),self.boxx,sum(x['shares'] for x in self.spy_lots),
             _round(self.boxx*boxx),_round(sum(x['shares'] for x in self.spy_lots)*spy),
             _round(long_value),_round(liability),_round(nav),_round(self.realized),
             _round(self.withdrawn_usd),_round(self.withdrawn_cny),_round(self.arrears),
             _round(self.obligations()),_round(drawdown),_round(adjusted),RULE_VERSION)
        self.nav_rows.append(row);self.completed=day;self.previous=bars;self.previous_day=day
        return True


def replay(db, until=None, commission=COMMISSION):
    start=date(2026,1,1)
    end=date.fromisoformat(until) if until else latest_publishable_session()
    sim=Simulation(db,commission)
    for day in sessions(start,end):
        before=len(sim.events)
        if not sim.day(day):
            del sim.events[before:]
            break
    # Atomic replacement ensures reruns neither duplicate nor partially publish events.
    with db:
        db.execute('DELETE FROM events');db.execute('DELETE FROM daily_nav');db.execute('DELETE FROM data_gaps');db.execute('DELETE FROM open_positions')
        db.executemany("INSERT INTO events VALUES(NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",sim.events)
        db.executemany("INSERT INTO daily_nav VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",sim.nav_rows)
        db.executemany("INSERT OR IGNORE INTO data_gaps VALUES(?,?,?)",sim.gaps)
        db.executemany("INSERT INTO open_positions VALUES(?,?,?,?,?,?,?,?,?)",[
            (p['contract'],p['expiration'],p['strike'],p['right'],p['side'],p['count'],
             p['entry'],p['delta'],p['lot_cost']) for p in sim.positions.values()])
    return {'complete_through':sim.completed.isoformat() if sim.completed else None,
            'events':len(sim.events),'gaps':len(sim.gaps)}
