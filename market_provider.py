"""Fixed-host GET-only Alpaca adapter. No broker API or order operation."""
import json
import math
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, HTTPRedirectHandler, build_opener
from zoneinfo import ZoneInfo

from historical_probe import SYMBOL, timestamp_ns
from price_policy import ELIGIBLE, KNOWN, history_candidate
from market_calendar import session_at

NANO = 1_000_000_000
NY = ZoneInfo('America/New_York')


def get_json(path, params, key, secret):
    allowed = path == '/v1/corporate-actions' or (path.startswith('/v2/stocks/') and
              path.endswith('/trades') and SYMBOL.fullmatch(path[len('/v2/stocks/'):-len('/trades')]))
    if not allowed:
        raise ValueError('only historical trades and corporate-action data GETs allowed')
    if not key or not secret:
        raise ValueError('Alpaca data credentials missing')
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):
            return None
    request = Request('https://data.alpaca.markets'+path+'?'+urlencode(params),method='GET',
                      headers={'APCA-API-KEY-ID':key,'APCA-API-SECRET-KEY':secret,'Accept':'application/json'})
    with build_opener(NoRedirect()).open(request,timeout=30) as response:
        return json.load(response)


def eligible(row):
    update = row.get('u')
    if update in ('canceled','incorrect'):
        return False
    if update not in (None,'corrected'):
        raise ValueError('unknown correction state')
    tape, conditions = row.get('z'), row.get('c')
    if tape not in ELIGIBLE or not isinstance(conditions,list) or not conditions:
        raise ValueError('missing sale conditions/tape')
    if any(not isinstance(c,str) or c not in KNOWN for c in conditions):
        raise ValueError('unknown sale condition')
    if not set(conditions) <= ELIGIBLE[tape]:
        return False
    if 'i' not in row or not row.get('x'):
        raise ValueError('missing trade identity')
    price = float(row['p'])
    if isinstance(row['p'],bool) or not math.isfinite(price) or price <= 0:
        raise ValueError('invalid market price')
    return True


def verified_reference(result, retrieved_at, evidence_uri):
    quote = history_candidate(result,retrieved_at,evidence_uri)
    at = datetime.fromisoformat(quote['decision_time_jst'])
    if not session_at(at)['open']:
        raise ValueError('decision outside regular trading session')
    quote.update(provider_verified=True,
                 provider_validation='Alpaca historical SIP; complete pagination; sale/update conditions; XNYS regular session',
                 warning='Paper reference as observed at retrieval; later vendor revisions may differ.')
    return quote


def corporate_state(symbol, entry, end, key, secret, fetch=get_json):
    """Any potentially effective corporate action requires explicit reconciliation.

    No split-adjusted fills or dividend cash flows are invented. The provider
    may report actions late; archive the as-observed response and its timestamp.
    """
    entry_day = entry.astimezone(NY).date()
    end_day = end.astimezone(NY).date()
    params={'symbols':symbol,'start':str(entry_day-timedelta(days=365)), 'end':str(end_day),
            'data_quality':'all','limit':1000,'sort':'asc'}
    pages, tokens, actions = [],set(),[]
    for _ in range(20):
        payload=fetch('/v1/corporate-actions',params,key,secret)
        data=payload.get('corporate_actions')
        if not isinstance(data,dict):
            raise ValueError('corporate-action schema not verified')
        pages.append(payload)
        for kind, rows in data.items():
            if not isinstance(rows,list):
                raise ValueError('invalid corporate-action records')
            for row in rows:
                if not isinstance(row,dict):
                    raise ValueError('invalid corporate action')
                symbols={str(v) for k,v in row.items() if k=='symbol' or k.endswith('_symbol')}
                if symbol not in symbols:
                    raise ValueError('corporate-action symbol mismatch')
                effective=row.get('ex_date') or row.get('effective_date') or row.get('process_date')
                if not effective or entry_day <= datetime.fromisoformat(effective).date() <= end_day:
                    actions.append({'kind':kind,**row})
        token=payload.get('next_page_token')
        if not token:
            return {'clear':not actions,'actions':actions,'pages':pages,'asof':end.isoformat(),
                    'warning':'Corporate-action publication may lag. Any effective reported action blocks automatic settlement.'}
        if not isinstance(token,str) or token in tokens:
            raise ValueError('invalid corporate-action page token')
        tokens.add(token);params={**params,'page_token':token}
    raise ValueError('corporate-action history pagination incomplete')


def collect_interval(symbol,start,end,now,key,secret,max_pages=20,fetch=get_json,clock=None):
    """Inclusive provider query; consumer uses (start,end] to avoid boundary replay."""
    if not SYMBOL.fullmatch(symbol) or not start < end <= now-timedelta(minutes=16):
        raise ValueError('invalid or too-recent history interval')
    iso=lambda dt:dt.astimezone(timezone.utc).isoformat().replace('+00:00','Z')
    params={'start':iso(start),'end':iso(end),'feed':'sip','sort':'asc','limit':10000,'asof':'-'}
    pages, seen_tokens, rows = [],set(),[]
    previous=None
    for _ in range(max_pages):
        payload=fetch('/v2/stocks/'+symbol+'/trades',params,key,secret)
        if payload.get('symbol')!=symbol or not isinstance(payload.get('trades'),list):
            raise ValueError('invalid history response')
        pages.append(payload)
        for row in payload['trades']:
            stamp=timestamp_ns(row['t'])
            if not timestamp_ns(params['start']) <= stamp <= timestamp_ns(params['end']) or (previous is not None and stamp<previous):
                raise ValueError('out-of-range or unordered history')
            previous=stamp
            if isinstance(row['p'],bool) or not math.isfinite(float(row['p'])) or float(row['p'])<=0:
                raise ValueError('invalid history price')
            rows.append(row)
        token=payload.get('next_page_token')
        if not token:
            return {'symbol':symbol,'start':iso(start),'end':iso(end),'pages':pages,
                    'trades':rows,'complete':True,'retrieved_at':(clock() if clock else datetime.now(timezone.utc)).isoformat(),'feed':'sip'}
        if not isinstance(token,str) or token in seen_tokens:
            raise ValueError('repeated or invalid page token')
        seen_tokens.add(token);params={**params,'page_token':token}
    raise ValueError('trade history pagination incomplete; cursor unchanged')


def normalized_stream(raw, corporate, evidence_uri):
    if raw.get('complete') is not True or raw.get('feed')!='sip' or not evidence_uri:
        raise ValueError('complete archived SIP history required')
    start,end=timestamp_ns(raw['start']),timestamp_ns(raw['end'])
    by_time={};seen={}
    import exchange_calendars as xcals
    first=datetime.fromisoformat(raw['start']).astimezone(NY).date()
    last=datetime.fromisoformat(raw['end']).astimezone(NY).date()
    cal=xcals.get_calendar('XNYS',start=str(first-timedelta(days=7)),end=str(last+timedelta(days=7)))
    for row in raw['trades']:
        stamp=timestamp_ns(row['t'])
        if not start <= stamp <= end:
            raise ValueError('history outside coverage')
        if stamp==start or not eligible(row):
            continue
        dt=datetime.fromisoformat(row['t'].replace('Z','+00:00'))
        day=dt.astimezone(NY).date().isoformat()
        if not cal.is_session(day):
            continue
        session=cal.schedule.loc[day]
        if not timestamp_ns(session['open'].isoformat()) <= stamp < timestamp_ns(session['close'].isoformat()):
            continue
        identity=(day,str(row['i']),row['x'],row['z'])
        if identity in seen and seen[identity] != (stamp,float(row['p'])):
            raise ValueError('conflicting trade identity')
        seen[identity]=(stamp,float(row['p']))
        if stamp in by_time and by_time[stamp]['price']!=float(row['p']):
            raise ValueError('ambiguous simultaneous trade price')
        by_time[stamp]={'symbol':raw['symbol'],'kind':'trade','market_time':row['t'],
                        'retrieved_at':raw['retrieved_at'],'price':float(row['p']),
                        'source':'Alpaca historical SIP','evidence_uri':evidence_uri,
                        'trade_id':row['i'],'exchange':row['x'],'tape':row['z'],'update_state':row.get('u')}
    return {'coverage_start':raw['start'],'coverage_end':raw['end'],'complete':True,
            'corporate_actions_clear':corporate.get('clear') is True,
            'source':'Alpaca historical SIP','evidence_uri':evidence_uri,
            'trades':[by_time[t] for t in sorted(by_time)]}
