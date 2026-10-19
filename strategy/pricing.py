"""CRR American option model with continuous dividend yield."""
import math
from functools import lru_cache
import numpy as np
from scipy.optimize import brentq
from scipy.stats import norm


def european_price_delta(spot,strike,years,rate,dividend_yield,sigma,right):
    root=sigma*math.sqrt(years)
    d1=(math.log(spot/strike)+(rate-dividend_yield+sigma*sigma/2)*years)/root
    d2=d1-root
    if right=='C':
        return (spot*math.exp(-dividend_yield*years)*norm.cdf(d1)
                -strike*math.exp(-rate*years)*norm.cdf(d2),
                math.exp(-dividend_yield*years)*norm.cdf(d1))
    return (strike*math.exp(-rate*years)*norm.cdf(-d2)
            -spot*math.exp(-dividend_yield*years)*norm.cdf(-d1),
            -math.exp(-dividend_yield*years)*norm.cdf(-d1))


def approximate_delta(spot,strike,years,rate,dividend_yield,bid,ask,right):
    mid=(bid+ask)/2
    def diff(vol):return european_price_delta(spot,strike,years,rate,dividend_yield,vol,right)[0]-mid
    if diff(.001)>0 or diff(4)<0:raise ValueError('European IV unavailable')
    vol=brentq(diff,.001,4,xtol=1e-4)
    return european_price_delta(spot,strike,years,rate,dividend_yield,vol,right)[1]


def american_price_delta(spot, strike, years, rate, dividend_yield, sigma, right, steps=160):
    if min(spot, strike, years, sigma) <= 0 or right not in ("P", "C"):
        raise ValueError("invalid model input")
    dt = years / steps
    u = math.exp(sigma * math.sqrt(dt))
    d = 1 / u
    p = (math.exp((rate - dividend_yield) * dt) - d) / (u - d)
    if not 0 < p < 1:
        raise ValueError("invalid risk-neutral probability")
    disc = math.exp(-rate * dt)
    sign = 1 if right == "C" else -1
    indices=np.arange(steps+1,dtype=float)
    prices=spot*(u**(steps-indices))*(d**indices)
    values=np.maximum(0.,sign*(prices-strike))
    first = None
    for n in range(steps-1, -1, -1):
        prices=prices[:-1]/u
        continuation=disc*(p*values[:-1]+(1-p)*values[1:])
        values=np.maximum(continuation,np.maximum(sign*(prices-strike),0.))
        if n == 1:
            first=values.copy()
    return float(values[0]), float((first[0]-first[1]) / (spot*(u-d)))


@lru_cache(maxsize=200000)
def implied_delta(spot, strike, years, rate, dividend_yield, bid, ask, right):
    if bid < 0 or ask < bid or ask <= 0:
        raise ValueError("invalid bid/ask")
    mid = (bid+ask)/2
    intrinsic = max(0., (spot-strike) if right == "C" else (strike-spot))
    if mid + 1e-7 < intrinsic:
        raise ValueError("mid below intrinsic")
    def diff(vol):
        return american_price_delta(spot, strike, years, rate, dividend_yield, vol, right)[0]-mid
    if diff(0.001) > 0 or diff(4.0) < 0:
        raise ValueError("implied volatility root unavailable")
    vol = brentq(diff, 0.001, 4.0, xtol=1e-5)
    return american_price_delta(spot, strike, years, rate, dividend_yield, vol, right)[1], vol
