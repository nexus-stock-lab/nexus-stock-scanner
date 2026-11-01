"""Split-only economic price basis for virtual, unsized strategy returns.

Observed prices are never rewritten. Fractional economic units are theoretical;
this is not a broker quantity, rounding, or cash-in-lieu execution model.
Cash dividends, due bills and other actions remain blocked.
"""
import hashlib
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID
from zoneinfo import ZoneInfo

from historical_probe import timestamp_ns

NY=ZoneInfo('America/New_York')
SOURCE='https://data.alpaca.markets/v1/corporate-actions'
VERSION='nexus_split_basis_v1'


def sha(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def decimal(value):
    if isinstance(value,bool):raise ValueError('split rate is not a boolean')
    try:r=Decimal(str(value))
    except InvalidOperation:raise ValueError('split rate invalid') from None
    if not r.is_finite() or r<=0:raise ValueError('split rate must be finite and positive')
    return r


def day(value):
    if not isinstance(value,str) or len(value)!=10:raise ValueError('split ex-date required')
    return date.fromisoformat(value)


def prepare_split_history(corporate,symbol,entry,end,now,evidence_uri):
    if corporate.get('clear') is True:
        if corporate.get('actions'):raise ValueError('corporate clear flag contradicts actions')
        return None
    if (corporate.get('complete') is not True or corporate.get('source')!=SOURCE or
            not evidence_uri or not corporate.get('pages')):
        raise ValueError('complete provider corporate evidence required')
    if not timestamp_ns(end.isoformat())<=timestamp_ns(corporate['retrieved_at'])<=timestamp_ns(now.isoformat()):
        raise ValueError('corporate retrieval time invalid')
    if timestamp_ns(corporate['asof'])<timestamp_ns(end.isoformat()):
        raise ValueError('corporate coverage incomplete')
    first,last=entry.astimezone(NY).date(),end.astimezone(NY).date()
    # Reconstruct relevant actions from the complete archived response rather
    # than trusting an actions list that may have omitted a dividend or merger.
    relevant=[]
    for i,page in enumerate(corporate['pages']):
        if not isinstance(page.get('corporate_actions'),dict):raise ValueError('corporate page invalid')
        if bool(page.get('next_page_token'))!=(i<len(corporate['pages'])-1):
            raise ValueError('corporate pagination incomplete')
        for kind,rows in page['corporate_actions'].items():
            if not isinstance(rows,list):raise ValueError('corporate rows invalid')
            for row in rows:
                effective=row.get('ex_date') or row.get('effective_date') or row.get('process_date')
                if not effective or first<=day(effective)<=last:
                    relevant.append({**row,'kind':kind})
    if sorted(map(sha,relevant))!=sorted(map(sha,corporate.get('actions',[]))):
        raise ValueError('corporate action selection mismatch')
    if not relevant:raise ValueError('unresolved corporate action state')
    events={}
    for row in relevant:
        if row['kind'] not in ('forward_splits','reverse_splits'):
            raise ValueError('non-split corporate action needs reconciliation')
        if row.get('symbol')!=symbol or row.get('due_bill_redemption_date'):
            raise ValueError('split identity or due-bill terms unresolved')
        ex=day(row.get('ex_date'))
        if not first<ex<=last:raise ValueError('entry-day or out-of-range split needs reconciliation')
        old,new=decimal(row.get('old_rate')),decimal(row.get('new_rate'))
        ratio=new/old
        if ((row['kind']=='forward_splits' and ratio<=1) or
                (row['kind']=='reverse_splits' and ratio>=1)):
            raise ValueError('split direction contradicts ratio')
        identifier=str(UUID(row['id']))
        event={'id':identifier,'kind':row['kind'],'ex_date':ex.isoformat(),
               'old_rate':str(old),'new_rate':str(new),'source_action_sha256':sha(row)}
        if identifier in events and events[identifier]!=event:
            raise ValueError('conflicting split announcement')
        events[identifier]=event
    # Two distinct split records on one ex-date require explicit reconciliation.
    if len({e['ex_date'] for e in events.values()})!=len(events):
        raise ValueError('ambiguous same-day split records')
    proof={'version':VERSION,'symbol':symbol,'entry_time_jst':entry.isoformat(),
           'coverage_end':end.isoformat(),'retrieved_at':corporate['retrieved_at'],
           'source':SOURCE,'evidence_uri':evidence_uri,'source_sha256':sha(corporate),
           'events':sorted(events.values(),key=lambda e:(e['ex_date'],e['id']))}
    proof['history_sha256']=sha(proof['events'])
    proof['sha256']=sha(proof)
    return proof


def validate_split_history(proof,trade,end,now):
    if (not isinstance(proof,dict) or proof.get('version')!=VERSION or
            proof.get('symbol')!=trade['symbol'] or
            proof.get('entry_time_jst')!=trade['entry_time_jst'] or
            proof.get('source')!=SOURCE or not proof.get('evidence_uri')):
        raise ValueError('split history identity invalid')
    if sha({k:v for k,v in proof.items() if k!='sha256'})!=proof.get('sha256'):
        raise ValueError('split history proof mismatch')
    if not timestamp_ns(end)<=timestamp_ns(proof['coverage_end'])<=timestamp_ns(proof['retrieved_at'])<=timestamp_ns(now.isoformat()):
        raise ValueError('split history time invalid')
    events=proof['events']
    if not isinstance(events,list) or not events or sha(events)!=proof.get('history_sha256'):
        raise ValueError('split events proof mismatch')
    first=datetime.fromisoformat(trade['entry_time_jst']).astimezone(NY).date()
    last=datetime.fromisoformat(proof['coverage_end']).astimezone(NY).date()
    ids=set();dates=set()
    for e in events:
        identifier=str(UUID(e['id']));ex=day(e['ex_date'])
        if identifier in ids or ex in dates or not first<ex<=last:
            raise ValueError('split event identity or date invalid')
        ids.add(identifier);dates.add(ex)
        ratio=decimal(e['new_rate'])/decimal(e['old_rate'])
        if not ((e['kind']=='forward_splits' and ratio>1) or (e['kind']=='reverse_splits' and ratio<1)):
            raise ValueError('split event direction invalid')
    return proof


def split_factor(proof,market_time):
    trading_day=datetime.fromisoformat(market_time.replace('Z','+00:00')).astimezone(NY).date()
    factor=Decimal(1)
    for e in proof['events']:
        if day(e['ex_date'])<=trading_day:factor*=decimal(e['new_rate'])/decimal(e['old_rate'])
    return float(factor)


def split_replay_request(trade,proof):
    marker=trade.get('tracked_corporate_sha256')
    if proof is None:
        if marker:raise ValueError('previously observed split history disappeared')
        return None
    if marker==proof['history_sha256']:return None
    cursor=trade.get('tracked_through_jst') or trade['entry_time_jst']
    cursor_day=datetime.fromisoformat(cursor).astimezone(NY).date()
    if any(day(e['ex_date'])<=cursor_day for e in proof['events']):
        return {'previous_cursor':cursor,'split_history_sha256':proof['history_sha256'],
                'reason':'New or revised split in previously tracked history'}
    return None
