"""Corporate evidence for virtual, unsized price returns and declared dividends.

Observed prices are never rewritten. Fractional economic units are theoretical;
this is not a broker quantity, rounding, or cash-in-lieu execution model.
Ordinary domestic USD dividends are recorded separately, never treated as paid.
Special/foreign distributions and unverified calendars remain blocked.
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
VERSION='nexus_corporate_basis_v2'
LEGACY_VERSION='nexus_split_basis_v1'


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


def split_bill_calendar(row,ex):
    """Validate published dates without modeling physical share settlement.

    A standard split can retain a due-bill redemption date after ex-date.
    That date is NOT substituted for the start of the split price basis.
    """
    redemption=row.get('due_bill_redemption_date')
    if not redemption:return None
    if not row.get('record_date') or not row.get('payable_date'):
        raise ValueError('split due-bill calendar incomplete')
    record,paid,redeemed=day(row['record_date']),day(row['payable_date']),day(redemption)
    if not record<=paid<=ex<=redeemed:
        raise ValueError('split due-bill date order invalid')
    return {'record_date':record.isoformat(),'payable_date':paid.isoformat(),
            'due_bill_redemption_date':redeemed.isoformat()}


def cash_dividend(row,symbol,first,last):
    if (row.get('kind')!='cash_dividends' or row.get('symbol')!=symbol or row.get('currency')!='USD' or
            row.get('foreign') is not False or row.get('special') is not False or
            row.get('sub_type') not in (None,'') or row.get('due_bill_on_date') or row.get('due_bill_off_date')):
        raise ValueError('ordinary domestic USD cash dividend not verified')
    ex,record,paid=day(row.get('ex_date')),day(row.get('record_date')),day(row.get('payable_date'))
    if not first<=ex<=last or not ex<=record<=paid:
        raise ValueError('cash dividend dates invalid')
    return {'kind':'cash_dividends','id':str(UUID(row['id'])),'symbol':symbol,'currency':'USD',
            'foreign':False,'special':False,'ex_date':ex.isoformat(),'record_date':record.isoformat(),
            'payable_date':paid.isoformat(),'rate':str(decimal(row.get('rate'))),
            'source_action_sha256':sha(row)}


def relevant_actions(corporate,first,last):
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
    return relevant


def prepare_split_history(corporate,symbol,entry,end,now,evidence_uri):
    first,last=entry.astimezone(NY).date(),end.astimezone(NY).date()
    if corporate.get('clear') is True:
        if corporate.get('actions'):raise ValueError('corporate clear flag contradicts actions')
        if corporate.get('pages') and relevant_actions(corporate,first,last):
            raise ValueError('corporate clear flag hides relevant actions')
        return None
    if (corporate.get('complete') is not True or corporate.get('source')!=SOURCE or
            not evidence_uri or not corporate.get('pages')):
        raise ValueError('complete provider corporate evidence required')
    if not timestamp_ns(end.isoformat())<=timestamp_ns(corporate['retrieved_at'])<=timestamp_ns(now.isoformat()):
        raise ValueError('corporate retrieval time invalid')
    if timestamp_ns(corporate['asof'])<timestamp_ns(end.isoformat()):
        raise ValueError('corporate coverage incomplete')
    # Reconstruct relevant actions from the complete archived response rather
    # than trusting an actions list that may have omitted a dividend or merger.
    relevant=relevant_actions(corporate,first,last)
    if sorted(map(sha,relevant))!=sorted(map(sha,corporate.get('actions',[]))):
        raise ValueError('corporate action selection mismatch')
    if not relevant:raise ValueError('unresolved corporate action state')
    events={};dividends={}
    for row in relevant:
        if row['kind']=='cash_dividends':
            event=cash_dividend(row,symbol,first,last);identifier=event['id']
            if identifier in dividends and dividends[identifier]!=event:
                raise ValueError('conflicting cash dividend announcement')
            dividends[identifier]=event
            continue
        if row['kind'] not in ('forward_splits','reverse_splits'):
            raise ValueError('unsupported corporate action needs reconciliation')
        if row.get('symbol')!=symbol:
            raise ValueError('split identity unresolved')
        ex=day(row.get('ex_date'))
        if not first<ex<=last:raise ValueError('entry-day or out-of-range split needs reconciliation')
        bill=split_bill_calendar(row,ex)
        old,new=decimal(row.get('old_rate')),decimal(row.get('new_rate'))
        ratio=new/old
        if ((row['kind']=='forward_splits' and ratio<=1) or
                (row['kind']=='reverse_splits' and ratio>=1)):
            raise ValueError('split direction contradicts ratio')
        identifier=str(UUID(row['id']))
        event={'id':identifier,'kind':row['kind'],'ex_date':ex.isoformat(),
               'old_rate':str(old),'new_rate':str(new),'source_action_sha256':sha(row)}
        if bill is not None:event['due_bill_calendar']=bill
        if identifier in events and events[identifier]!=event:
            raise ValueError('conflicting split announcement')
        events[identifier]=event
    # Two distinct split records on one ex-date require explicit reconciliation.
    if len({e['ex_date'] for e in events.values()})!=len(events):
        raise ValueError('ambiguous same-day split records')
    dividend_dates={e['ex_date'] for e in dividends.values()}
    if len(dividend_dates)!=len(dividends) or dividend_dates & {e['ex_date'] for e in events.values()}:
        raise ValueError('same-day distribution price basis needs reconciliation')
    proof={'version':VERSION,'symbol':symbol,'entry_time_jst':entry.isoformat(),
           'coverage_end':end.isoformat(),'retrieved_at':corporate['retrieved_at'],
           'source':SOURCE,'evidence_uri':evidence_uri,'source_sha256':sha(corporate),
           'events':sorted(events.values(),key=lambda e:(e['ex_date'],e['id']))}
    proof['cash_dividends']=sorted(dividends.values(),key=lambda e:(e['ex_date'],e['id']))
    proof['cash_dividends_sha256']=sha(proof['cash_dividends'])
    proof['history_sha256']=sha(proof['events'])
    proof['sha256']=sha(proof)
    return proof


def validate_split_history(proof,trade,end,now):
    if (not isinstance(proof,dict) or proof.get('version') not in (VERSION,LEGACY_VERSION) or
            proof.get('symbol')!=trade['symbol'] or
            proof.get('entry_time_jst')!=trade['entry_time_jst'] or
            proof.get('source')!=SOURCE or not proof.get('evidence_uri')):
        raise ValueError('split history identity invalid')
    if sha({k:v for k,v in proof.items() if k!='sha256'})!=proof.get('sha256'):
        raise ValueError('split history proof mismatch')
    if not timestamp_ns(end)<=timestamp_ns(proof['coverage_end'])<=timestamp_ns(proof['retrieved_at'])<=timestamp_ns(now.isoformat()):
        raise ValueError('split history time invalid')
    events=proof['events']
    dividends=proof.get('cash_dividends',[])
    if not isinstance(events,list) or not isinstance(dividends,list) or not (events or dividends) or sha(events)!=proof.get('history_sha256'):
        raise ValueError('split events proof mismatch')
    if proof['version']==LEGACY_VERSION and dividends:
        raise ValueError('legacy corporate proof cannot contain dividends')
    if proof['version']==VERSION and sha(dividends)!=proof.get('cash_dividends_sha256'):
        raise ValueError('cash dividend proof mismatch')
    first=datetime.fromisoformat(trade['entry_time_jst']).astimezone(NY).date()
    last=datetime.fromisoformat(proof['coverage_end']).astimezone(NY).date()
    ids=set();dates=set()
    for e in events:
        identifier=str(UUID(e['id']));ex=day(e['ex_date'])
        if identifier in ids or ex in dates or not first<ex<=last:
            raise ValueError('split event identity or date invalid')
        ids.add(identifier);dates.add(ex)
        if e.get('due_bill_calendar') is not None:
            if split_bill_calendar(e['due_bill_calendar'],ex) is None:
                raise ValueError('split due-bill calendar incomplete')
        ratio=decimal(e['new_rate'])/decimal(e['old_rate'])
        if not ((e['kind']=='forward_splits' and ratio>1) or (e['kind']=='reverse_splits' and ratio<1)):
            raise ValueError('split event direction invalid')
    dividend_dates=set()
    for e in dividends:
        cash_dividend(e,trade['symbol'],first,last)
        if e['id']!=str(UUID(e['id'])) or e['id'] in ids or e['ex_date'] in dividend_dates or day(e['ex_date']) in dates:
            raise ValueError('ambiguous dividend identity or price basis')
        ids.add(e['id']);dividend_dates.add(e['ex_date'])
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
        if marker:raise ValueError('previously observed corporate history disappeared')
        return None
    if marker==proof['history_sha256']:return None
    cursor=trade.get('tracked_through_jst') or trade['entry_time_jst']
    cursor_day=datetime.fromisoformat(cursor).astimezone(NY).date()
    if any(day(e['ex_date'])<=cursor_day for e in proof['events']):
        return {'previous_cursor':cursor,'split_history_sha256':proof['history_sha256'],
                'reason':'New or revised split in previously tracked history'}
    return None


def declared_dividend_records(proof,trade,until):
    """Declared gross entitlement per entry unit; NOT proof of payment or net cash."""
    old=read_dividend_records(trade)
    events=proof.get('cash_dividends',[]) if proof is not None else []
    if not {r['id'] for r in old}<={e['id'] for e in events}:
        raise ValueError('previously observed cash dividend disappeared')
    first=datetime.fromisoformat(trade['entry_time_jst']).astimezone(NY).date()
    last=datetime.fromisoformat(until.replace('Z','+00:00')).astimezone(NY).date()
    records=[]
    for e in events:
        ex=day(e['ex_date'])
        if not first<ex<=last:continue
        factor=Decimal(1)
        basis=[]
        for split in proof['events']:
            if day(split['ex_date'])<ex:
                factor*=decimal(split['new_rate'])/decimal(split['old_rate']);basis.append(split)
        records.append({**e,'entry_unit_factor':str(factor),
            'declared_gross_usd_per_entry_unit':str(decimal(e['rate'])*factor),
            'payment_verified':False,'proof_sha256':proof['sha256'],
            'source':proof['source'],'evidence_uri':proof['evidence_uri'],
            'observed_at':proof['retrieved_at'],'basis_splits':basis})
    return records


def read_dividend_records(trade,now=None):
    raw=trade.get('dividend_records_json')
    if not raw:
        if trade.get('dividend_records_sha256'):raise ValueError('persisted dividend evidence missing')
        return []
    records=json.loads(raw)
    if not isinstance(records,list) or sha(records)!=trade.get('dividend_records_sha256'):
        raise ValueError('persisted dividend evidence mismatch')
    first=datetime.fromisoformat(trade['entry_time_jst']).astimezone(NY).date()
    last=datetime.fromisoformat((trade.get('exit_time_jst') if trade['status']=='CLOSED' else
                               trade.get('tracked_through_jst')) or trade['entry_time_jst']).astimezone(NY).date()
    ids=set()
    for r in records:
        cash_dividend(r,trade['symbol'],first,last)
        if r['id'] in ids or not first<day(r['ex_date'])<=last or r.get('payment_verified') is not False:
            raise ValueError('persisted dividend eligibility invalid')
        ids.add(r['id'])
        factor=Decimal(1);split_ids=set()
        for split in r['basis_splits']:
            if split['id'] in split_ids or not first<day(split['ex_date'])<day(r['ex_date']):
                raise ValueError('persisted dividend split basis invalid')
            split_ids.add(split['id'])
            ratio=decimal(split['new_rate'])/decimal(split['old_rate'])
            if not ((split['kind']=='forward_splits' and ratio>1) or (split['kind']=='reverse_splits' and ratio<1)):
                raise ValueError('persisted dividend split direction invalid')
            factor*=ratio
        if factor!=decimal(r['entry_unit_factor']):
            raise ValueError('persisted dividend split factor invalid')
        if decimal(r['rate'])*decimal(r['entry_unit_factor'])!=decimal(r['declared_gross_usd_per_entry_unit']):
            raise ValueError('persisted dividend calculation invalid')
        if r.get('source')!=SOURCE or not r.get('evidence_uri') or not isinstance(r.get('proof_sha256'),str) or len(r['proof_sha256'])!=64 or any(c not in '0123456789abcdef' for c in r['proof_sha256']):
            raise ValueError('persisted dividend provenance invalid')
        observed=timestamp_ns(r['observed_at'])
        if now is not None and observed>timestamp_ns(now.isoformat()):
            raise ValueError('persisted dividend evidence acquired in future')
    return records


def dividend_report(ledger,now):
    today=now.astimezone(NY).date();positions=[]
    for trade in ledger:
        records=read_dividend_records(trade,now)
        if not records:continue
        observations=[]
        for r in records:
            observations.append({**r,'status':'PAYABLE_DATE_REACHED_PAYMENT_UNVERIFIED' if day(r['payable_date'])<=today else
                                 'AWAITING_DECLARED_PAYABLE_DATE'})
        positions.append({'trade_id':trade['trade_id'],'symbol':trade['symbol'],
            'declared_gross_usd_per_entry_unit':str(sum((Decimal(r['declared_gross_usd_per_entry_unit']) for r in records),Decimal(0))),
            'payment_verified':False,'records':observations})
    return {'mode':'DECLARED_ENTITLEMENT_ONLY','included_in_price_return_or_r':False,
            'payment_verified':False,'positions':positions}


# Keep the v1 import names for existing archived streams and diagnostics.
prepare_corporate_history=prepare_split_history
validate_corporate_history=validate_split_history
