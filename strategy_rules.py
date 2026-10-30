"""Owner-approved NEXUS rules v1 (2026-10-27). No orders or inferred clearances."""
import math
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from historical_probe import decision, timestamp_ns

VERSION = 'nexus_rules_20261027_v1'
NY = ZoneInfo('America/New_York')


def number(value):
    if isinstance(value, bool):
        raise ValueError('boolean is not a financial number')
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('finite number required')
    return value


def financial_quality(current_revenue, prior_revenue, operating_income, prior_operating_income):
    revenue, prior, income, old_income = map(number, (current_revenue, prior_revenue, operating_income, prior_operating_income))
    if prior <= 0 or revenue <= 0:
        raise ValueError('positive comparable revenue required')
    return {'pass': revenue > prior and (income > 0 or income > old_income),
            'revenue_growth_pct': 100*(revenue/prior-1),
            'operating_income': income, 'prior_operating_income': old_income}


def earnings_clear(earnings_date, at):
    """Exclude announcement session and its three preceding US sessions."""
    import exchange_calendars as xcals
    day = date.fromisoformat(earnings_date)
    today = at.astimezone(NY).date()
    if day < today:
        raise ValueError('next earnings date is stale')
    cal = xcals.get_calendar('XNYS', start=str(today-timedelta(days=14)), end=str(day+timedelta(days=14)))
    # A non-session announcement is still excluded on its calendar day; count
    # the three actual market sessions preceding it, not three calendar days.
    sessions = cal.sessions_in_range(str(today-timedelta(days=14)), str(day))
    before = [s.date() for s in sessions if s.date() < day]
    if len(before) < 3:
        raise ValueError('earnings session calendar incomplete')
    return not before[-3] <= today <= day


def technical_levels(bars, at):
    """Frozen daily lows/highs, previous 20/60 completed regular sessions only."""
    import exchange_calendars as xcals
    today = at.astimezone(NY).date()
    cal = xcals.get_calendar('XNYS', start=str(today-timedelta(days=130)), end=str(today))
    expected = [s.date().isoformat() for s in cal.sessions if s.date() < today][-60:]
    rows = {}
    for b in bars:
        day = date.fromisoformat(b['date']).isoformat()
        if day >= today.isoformat():
            continue
        if day in rows:
            raise ValueError('duplicate daily bar')
        low, high, close = map(number, (b['low'], b['high'], b['close']))
        if not 0 < low <= close <= high:
            raise ValueError('invalid daily OHLC')
        rows[day] = {'low':low,'high':high,'close':close}
    if len(expected) != 60 or any(day not in rows for day in expected):
        raise ValueError('60 completed US sessions required; no missing bars')
    return {'stop_price': min(rows[d]['low'] for d in expected[-20:]),
            'target_price': max(rows[d]['high'] for d in expected),
            'price_date': expected[-1], 'rules_version':VERSION}


def checked(record, at, required=True):
    """A semantic review must carry archived evidence; UNKNOWN never passes."""
    if required and record.get('value') is not True:
        raise ValueError('official assertion not verified')
    for field in ('source','evidence_sha256','excerpt','rationale'):
        if not isinstance(record.get(field),str) or not record[field].strip():
            raise ValueError('official review missing '+field)
    if not record['source'].startswith('https://'):
        raise ValueError('official HTTPS source required')
    if not timestamp_ns(record['published_at']) <= timestamp_ns(record['checked_at']) <= timestamp_ns(at.isoformat()):
        raise ValueError('official review is future or checked after decision')
    return {'status':'PASS','source':record['source'],'rationale':record['rationale'],
            'checked_at':record['checked_at'],'evidence_sha256':record['evidence_sha256']}


def build_plan(row, bars, financials, review, at, prepared_at, technical_source):
    """Numeric checks plus separate source-backed qualitative clearances."""
    decision(at.isoformat())
    if timestamp_ns(prepared_at) > timestamp_ns(at.isoformat()):
        raise ValueError('plan preparation missed decision')
    if row['symbol'] != review['symbol'] or row['symbol'] != financials['symbol']:
        raise ValueError('review identity mismatch')
    if review.get('rules_version') != VERSION:
        raise ValueError('official review needs current approved rules')
    if review.get('financial_evidence_sha256') != financials['evidence_sha256']:
        raise ValueError('review does not cover latest retrieved financials')
    q = financial_quality(*(financials[k] for k in ('revenue','prior_revenue','operating_income','prior_operating_income')))
    if not q['pass']:
        raise ValueError('financial quality failed')
    if timestamp_ns(financials['checked_at']) > timestamp_ns(at.isoformat()):
        raise ValueError('financials checked after decision')
    levels = technical_levels(bars, at)
    guidance = checked(review['guidance_not_lowered'],at)
    decline = checked(review['decline_explanation_verified'],at)
    checked(review['no_material_business_impairment'],at)
    earning = checked(review['next_earnings'],at,required=False)
    earnings_date = review['next_earnings']['date']
    if not earnings_clear(earnings_date,at):
        raise ValueError('inside 3-session earnings exclusion window')
    if row['signal'] not in ('反発確認','反発初動'):
        raise ValueError('unrecognized signal')
    if number(row['avg_dollar_volume_20']) < 2_000_000 or number(row['close']) < 2:
        raise ValueError('original liquidity/price rule failed')
    evidence_check = {'status':'PASS','source':technical_source,
                      'rationale':'Frozen prior-session scan; original signal and liquidity thresholds',
                      'checked_at':prepared_at}
    checks = {'fundamentals':{**guidance,'rationale':str(q)+'; '+guidance['rationale']},
              'earnings':earning,'decline_cause':decline,
              'liquidity':dict(evidence_check),'signal':dict(evidence_check),
              'stop':{**evidence_check,'rationale':'Minimum low of prior 20 completed US sessions'},
              'target':{**evidence_check,'rationale':'Maximum high of prior 60 completed US sessions'}}
    return {'symbol':row['symbol'],'signal':row['signal'],'decision_time_jst':at.isoformat(),
            'prepared_at':prepared_at,'stop_price':levels['stop_price'],'target_price':levels['target_price'],
            'checks':checks,'rules_version':VERSION,'financials':financials,
            'official_review':review,'technical_levels':levels}
