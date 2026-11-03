"""Agreed NEXUS entry reference: latest valid trade in [23:44, 23:45] JST.

This is a paper reference price, not evidence of an executable market order.
Provider verification remains explicit and defaults to false in diagnostics.
"""
import math
from datetime import timedelta
from historical_probe import timestamp_ns, decision

POLICY = 'nexus_asof_60s_v1'
NANO = 1_000_000_000
# Alpaca Market Data FAQ, stock MINUTE open/close eligibility, checked 2026-10-27.
# Extended-hours prints are excluded because the 23:45 reference is regular hours.
ELIGIBLE = {'A':set(' EFKLOX56'),'B':set(' EFKLOX56'),'C':set('@ABDFKLOXY56')}
KNOWN = set(' @ABCDEFGHIKLMNOPQRTUVWXYZ45679')


def entry_reference_price(quote, symbol, at, now):
    if quote.get('symbol') != symbol or quote.get('kind') != 'trade' or quote.get('entry_policy') != POLICY:
        raise ValueError('approved as-of trade policy required')
    cutoff = timestamp_ns(at.isoformat())
    if timestamp_ns(quote['decision_time_jst']) != cutoff:
        raise ValueError('decision timestamp mismatch')
    age = cutoff - timestamp_ns(quote['market_time'])
    if not 0 <= age <= 60*NANO:
        raise ValueError('trade outside approved 60-second window')
    retrieved = timestamp_ns(quote['retrieved_at'])
    if not cutoff+16*60*NANO <= retrieved <= timestamp_ns(now.isoformat()):
        raise ValueError('verification must be at/after 00:01 and not in future')
    for field in ('history_complete','sale_conditions_verified','latest_valid_trade_verified','provider_verified'):
        if quote.get(field) is not True:
            raise ValueError('unverified entry evidence: '+field)
    if not quote.get('source') or not quote.get('evidence_uri'):
        raise ValueError('source and archived evidence required')
    price = float(quote['price'])
    if isinstance(quote['price'],bool) or not math.isfinite(price) or price <= 0:
        raise ValueError('finite positive reference price required')
    return price


def history_candidate(result, retrieved_at, evidence_uri):
    """Normalize a diagnostic result; never asserts provider readiness.

    An independently validated provider must attest provider_verified separately.
    Unknown conditions or conflicting simultaneous prints block selection.
    """
    if result.get('status')!='HISTORY_RECEIVED' or result.get('complete_pagination') is not True or result.get('feed')!='sip':
        raise ValueError('complete SIP history required')
    at=decision(result['decision_time_jst']); cutoff=timestamp_ns(at.isoformat())
    if timestamp_ns(retrieved_at)<cutoff+16*60*NANO:
        raise ValueError('verification too early')
    rows=[row for page in result['pages'] for row in page['trades']]
    if len(rows)!=result['records'] or result['window_seconds']!=60 or not evidence_uri:
        raise ValueError('history window or record count mismatch')
    valid=[]
    seen={}
    for page in result['pages']:
        if page['symbol']!=result['symbol']:
            raise ValueError('history symbol mismatch')
    if not result['pages'] or result['pages'][-1].get('next_page_token'):
        raise ValueError('unfinished history page')
    previous=None
    for row in rows:
        stamp=timestamp_ns(row['t']); price=float(row['p'])
        if not 0<=cutoff-stamp<=60*NANO or not math.isfinite(price) or price<=0 or isinstance(row['p'],bool):
            raise ValueError('invalid history trade')
        if previous is not None and stamp>previous:
            raise ValueError('history not descending')
        previous=stamp
        update = row.get('u')
        if update in ('canceled', 'incorrect'):
            continue
        if update not in (None, 'corrected'):
            raise ValueError('unknown trade update state')
        tape=row.get('z'); conditions=row.get('c')
        if tape not in ELIGIBLE or not isinstance(conditions,list) or not conditions:
            raise ValueError('missing tape or sale conditions')
        if any(not isinstance(c,str) or c not in KNOWN for c in conditions):
            raise ValueError('unknown sale condition; cannot determine latest valid trade')
        if not set(conditions)<=ELIGIBLE[tape]:
            continue
        if 'i' not in row or not row.get('x'):
            raise ValueError('trade identity missing')
        identity=(str(row['i']),row['x'],tape)
        if identity in seen and seen[identity]!=(stamp,price):
            raise ValueError('conflicting trade revision')
        seen[identity]=(stamp,price)
        valid.append((stamp,row))
    if not valid:
        raise ValueError('no eligible trade in approved window')
    latest=max(stamp for stamp,row in valid)
    latest_rows=[row for stamp,row in valid if stamp==latest]
    if len({float(row['p']) for row in latest_rows})!=1:
        raise ValueError('ambiguous latest trade price')
    row=latest_rows[0]
    return dict(symbol=result['symbol'],kind='trade',entry_policy=POLICY,
                decision_time_jst=at.isoformat(),market_time=row['t'],retrieved_at=retrieved_at,
                price=float(row['p']),source='Alpaca historical SIP',evidence_uri=evidence_uri,
                history_complete=True,sale_conditions_verified=True,latest_valid_trade_verified=True,
                provider_verified=False,trade_id=row['i'],exchange=row['x'],tape=row['z'],conditions=row['c'],
                update_state=row.get('u'),
                reference_age_seconds=(cutoff-latest)/NANO,
                warning='Provider smoke test, cancellation/correction reconciliation and live integration remain unverified.')
