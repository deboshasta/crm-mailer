# -*- coding: utf-8 -*-
"""US Eastern 'now / today / hour' for the mailer.

Date-based scheduling (which emails are "due today") and the morning self-nags
must behave the same whether this runs on Simon's Eastern-time PC or on a UTC
cloud runner. So instead of datetime.date.today() (which is the runner's clock,
= UTC in the cloud), the mailer asks this module for Eastern time.

Uses zoneinfo when tz data is installed; otherwise falls back to a built-in
US Eastern DST rule that is exact for current years (2nd Sunday of March to
1st Sunday of November = EDT/UTC-4, otherwise EST/UTC-5).
"""
import datetime

_ET = None
try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")          # raises if no tz database present
except Exception:
    _ET = None

def _first_sunday(y, m):
    d = datetime.date(y, m, 1)
    return d + datetime.timedelta(days=(6 - d.weekday()) % 7)

def _second_sunday(y, m):
    return _first_sunday(y, m) + datetime.timedelta(days=7)

def _is_edt(utc):
    y = utc.year
    start = datetime.datetime.combine(_second_sunday(y, 3), datetime.time(7, 0))   # 02:00 EST -> 07:00 UTC
    end   = datetime.datetime.combine(_first_sunday(y, 11), datetime.time(6, 0))   # 02:00 EDT -> 06:00 UTC
    return start <= utc.replace(tzinfo=None) < end

def now():
    """Current Eastern time (aware if zoneinfo present, else naive Eastern)."""
    if _ET is not None:
        return datetime.datetime.now(_ET)
    utc = datetime.datetime.now(datetime.timezone.utc)
    off = -4 if _is_edt(utc) else -5
    return (utc + datetime.timedelta(hours=off)).replace(tzinfo=None)

def today():
    return now().date()

def hour():
    return now().hour
