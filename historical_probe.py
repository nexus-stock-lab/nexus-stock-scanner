"""Read-only SIP history diagnostic. Never emits fills or verified_evidence.json.

A credentialed smoke test and approval of the as-of-price convention are still
required. No trading SDK, account endpoint, or order operation is used.
"""
import argparse
import hashlib
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

JST = ZoneInfo('Asia/Tokyo')
BASE = 'https://data.alpaca.markets/v2/stocks/'
SYMBOL = re.compile(r'[A-Z][A-Z0-9.-]{0,14}\Z')
STAMP = re.compile(r'(.+T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})\Z')


def timestamp_ns(value):
    """Retain nanoseconds: datetime.fromisoformat alone truncates after 6 digits."""
    if not isinstance(value, str):
        raise ValueError('timestamp must be text')
    match = STAMP.fullmatch(value)
    if not match:
        raise ValueError('timezone-bearing RFC3339 timestamp required')
    dt = datetime.fromisoformat(match[1] + ('+00:00' if match[3] == 'Z' else match[3]))
    epoch = datetime(1970,1,1,tzinfo=timezone.utc)
    delta = dt.astimezone(timezone.utc) - epoch
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + int((match[2] or '').ljust(9,'0'))


def decision(value):
    timestamp_ns(value)
    dt = datetime.fromisoformat(value).astimezone(JST)
    if (dt.hour,dt.minute,dt.second,dt.microsecond) != (23,45,0,0) or timestamp_ns(value) % 1_000_000_000:
        raise ValueError('decision time must be 23:45:00 JST')
    return dt


def request_params(at, now, window_seconds):
    if now.tzinfo is None:
        raise ValueError('now needs timezone')
    # Diagnostic safety margin; this is acquisition timing, not a strategy rule.
    if now < at + timedelta(minutes=16):
        raise ValueError('wait until 16 minutes after decision for free historical SIP access')
    if not isinstance(window_seconds,int) or not 1 <= window_seconds <= 300:
        raise ValueError('probe window must be 1..300 seconds')
    iso = lambda t:t.astimezone(timezone.utc).isoformat().replace('+00:00','Z')
    return dict(start=iso(at-timedelta(seconds=window_seconds)),end=iso(at),
                feed='sip',sort='desc',limit=1000,asof=at.date().isoformat())


def fetch_page(symbol, params, key, secret):
    """The only network operation: GET on a fixed market-data host, no redirects."""
    from urllib.request import HTTPRedirectHandler, build_opener
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    req = Request(BASE + symbol + '/trades?' + urlencode(params), method='GET',
                  headers={'APCA-API-KEY-ID':key,'APCA-API-SECRET-KEY':secret,'Accept':'application/json'})
    with build_opener(NoRedirect()).open(req,timeout=30) as response:
        return json.load(response)


def probe(symbol, at, now, key, secret, window_seconds=60, max_pages=10, fetch=fetch_page):
    if not SYMBOL.fullmatch(symbol):
        raise ValueError('invalid canonical symbol')
    params = request_params(at,now,window_seconds)
    if not key or not secret:
        return {'status':'MISSING_CREDENTIALS','paper_fill_created':False,'pages':[]}
    if not isinstance(max_pages,int) or not 1 <= max_pages <= 20:
        raise ValueError('page budget must be 1..20')
    pages, records, tokens = [], [], set()
    lower, upper = timestamp_ns(params['start']),timestamp_ns(params['end'])
    previous = None
    for _ in range(max_pages):
        try:
            payload = fetch(symbol,params,key,secret)
        except HTTPError as exc:
            return dict(status='HTTP_ERROR',http_status=exc.code,pages=pages,paper_fill_created=False)
        except (URLError,TimeoutError,OSError,ValueError):
            return dict(status='FETCH_ERROR',pages=pages,paper_fill_created=False)
        pages.append(payload)
        try:
            if payload['symbol'] != symbol or not isinstance(payload['trades'],list):
                raise ValueError('symbol or records mismatch')
            for trade in payload['trades']:
                stamp = timestamp_ns(trade['t'])
                price = float(trade['p'])
                if isinstance(trade['p'],bool) or not math.isfinite(price) or price<=0:
                    raise ValueError('invalid price')
                if not lower <= stamp <= upper or (previous is not None and stamp>previous):
                    raise ValueError('out-of-range or unsorted trade')
                previous = stamp
                records.append(trade)
            token = payload.get('next_page_token')
            if not token:
                return dict(status='HISTORY_RECEIVED' if records else 'NO_TRADES',symbol=symbol,
                            decision_time_jst=at.isoformat(),window_seconds=window_seconds,
                            feed='sip',complete_pagination=True,records=len(records),pages=pages,
                            latest_market_time=records[0]['t'] if records else None,
                            paper_fill_created=False,
                            warning='Diagnostic only. Sale conditions, cancellations, corrections and as-of fill policy are not validated.')
            if not isinstance(token,str) or token in tokens:
                raise ValueError('invalid or repeated pagination token')
            tokens.add(token)
            params={**params,'page_token':token}
        except (KeyError,TypeError,ValueError,OverflowError):
            return dict(status='INVALID_RESPONSE',pages=pages,paper_fill_created=False)
    return dict(status='INCOMPLETE_PAGINATION',pages=pages,paper_fill_created=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--symbol',required=True)
    parser.add_argument('--decision',required=True,help='ISO time, exactly 23:45 JST')
    args=parser.parse_args()
    now=datetime.now(timezone.utc)
    result=probe(args.symbol,decision(args.decision),now,
                 os.getenv('ALPACA_DATA_KEY',''),os.getenv('ALPACA_DATA_SECRET',''))
    result['retrieved_at']=datetime.now(timezone.utc).isoformat()
    out=Path('output'); out.mkdir(exist_ok=True)
    content=json.dumps(result,ensure_ascii=False,indent=2)
    name=hashlib.sha256(content.encode()).hexdigest()
    (out/f'historical_probe_{name}.json').write_text(content,encoding='utf-8')
    # Do not print raw server content, request headers or credentials into CI logs.
    print(json.dumps({k:result[k] for k in ('status','paper_fill_created','records') if k in result}))

if __name__=='__main__':
    main()
