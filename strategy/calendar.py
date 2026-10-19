"""NYSE full-day closures for the simulation era (2025 onward)."""
from datetime import date, timedelta, datetime, time
from zoneinfo import ZoneInfo


def observed(day):
    if day.weekday() == 5:
        return day-timedelta(days=1)
    if day.weekday() == 6:
        return day+timedelta(days=1)
    return day


def nth_weekday(year, month, weekday, n):
    day = date(year, month, 1)
    return day+timedelta(days=(weekday-day.weekday()) % 7 + (n-1)*7)


def last_weekday(year, month, weekday):
    day = date(year+1, 1, 1)-timedelta(days=1) if month == 12 else date(year, month+1, 1)-timedelta(days=1)
    return day-timedelta(days=(day.weekday()-weekday) % 7)


def easter(year):
    a=year % 19; b=year//100; c=year % 100; d=b//4; e=b % 4
    f=(b+8)//25; g=(b-f+1)//3; h=(19*a+b-d-g+15) % 30
    i=c//4; k=c % 4; l=(32+2*e+2*i-h-k) % 7; m=(a+11*h+22*l)//451
    month=(h+l-7*m+114)//31; day=(h+l-7*m+114) % 31+1
    return date(year, month, day)


def closures(year):
    return {
        observed(date(year,1,1)), observed(date(year+1,1,1)),
        nth_weekday(year,1,0,3), nth_weekday(year,2,0,3),
        easter(year)-timedelta(days=2), last_weekday(year,5,0),
        observed(date(year,6,19)), observed(date(year,7,4)),
        nth_weekday(year,9,0,1), nth_weekday(year,11,3,4),
        observed(date(year,12,25)),
    }


def is_session(day):
    return day.weekday() < 5 and day not in closures(day.year)


def sessions(start, end):
    day=start
    while day<=end:
        if is_session(day):
            yield day
        day+=timedelta(days=1)


def last_session_of_month(day):
    nxt = date(day.year+1,1,1) if day.month == 12 else date(day.year,day.month+1,1)
    probe=nxt-timedelta(days=1)
    while not is_session(probe):
        probe-=timedelta(days=1)
    return probe


def latest_publishable_session(now=None):
    now=now or datetime.now(ZoneInfo('America/New_York'))
    if now.tzinfo is None:now=now.replace(tzinfo=ZoneInfo('America/New_York'))
    now=now.astimezone(ZoneInfo('America/New_York'))
    probe=now.date()
    if now.time()<time(18,0) or not is_session(probe):probe-=timedelta(days=1)
    while not is_session(probe):probe-=timedelta(days=1)
    return probe
