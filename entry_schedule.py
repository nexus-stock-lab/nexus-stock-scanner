"""Prospective hourly entry checkpoints in regular US sessions (owner v3)."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

NY=ZoneInfo('America/New_York')
JST=ZoneInfo('Asia/Tokyo')


def session_date(at):
    return at.astimezone(NY).date().isoformat()


def session_slots(day):
    import exchange_calendars as xcals
    cal=xcals.get_calendar('XNYS',start=str(day-timedelta(days=7)),end=str(day+timedelta(days=7)))
    if not cal.is_session(str(day)):
        return []
    row=cal.schedule.loc[str(day)]
    opened=row['open'].to_pydatetime();closed=row['close'].to_pydatetime()
    local=opened.astimezone(NY).replace(minute=45,second=0,microsecond=0)
    result=[]
    while local<closed:
        if local>=opened:result.append(local.astimezone(JST))
        local+=timedelta(hours=1)
    return result


def next_slot(now):
    if now.tzinfo is None:raise ValueError('timezone required')
    day=now.astimezone(NY).date()
    for offset in range(10):
        for at in session_slots(day+timedelta(days=offset)):
            if at>now:return at
    raise ValueError('no upcoming regular-session checkpoint')


def plan_path(root,at):
    return Path(root)/'plans'/session_date(at)/(at.astimezone(NY).strftime('%H%M')+'.json')


def session_review_covers(packet,at):
    """Daily approval is explicit; never infer this scope from an exact-time review."""
    if packet.get('approval_scope')!='US_REGULAR_SESSION':return False
    expiry=datetime.fromisoformat(packet['decision_time_jst'])
    slots=session_slots(at.astimezone(NY).date())
    return bool(slots and expiry==slots[-1] and at in slots)
