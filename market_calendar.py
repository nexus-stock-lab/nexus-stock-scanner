"""Regular US equity sessions; missing calendar data never means open."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from historical_probe import decision

JST = ZoneInfo('Asia/Tokyo')


def latest_due_decision(now):
    local = (now-timedelta(minutes=16)).astimezone(JST)
    at = local.replace(hour=23, minute=45, second=0, microsecond=0)
    if local < at:
        at -= timedelta(days=1)
    return at


def next_decision(now):
    local = now.astimezone(JST)
    at = local.replace(hour=23, minute=45, second=0, microsecond=0)
    return at if now <= at else at + timedelta(days=1)


def session_at(at):
    import exchange_calendars as xcals
    import pandas as pd
    at = decision(at.isoformat())
    nyday = at.astimezone(ZoneInfo('America/New_York')).date()
    cal = xcals.get_calendar('XNYS', start=str(nyday-timedelta(days=7)), end=str(nyday+timedelta(days=7)))
    if not cal.is_session(str(nyday)):
        return {'open': False, 'session': str(nyday), 'reason': 'NON_TRADING_DAY'}
    row = cal.schedule.loc[str(nyday)]
    opened, closed = row['open'].to_pydatetime(), row['close'].to_pydatetime()
    return {'open': opened <= at < closed, 'session': str(nyday),
            'market_open': opened.isoformat(), 'market_close': closed.isoformat(),
            'source': 'exchange_calendars XNYS', 'version': xcals.__version__}
