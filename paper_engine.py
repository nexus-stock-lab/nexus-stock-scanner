"""Evidence-gated paper trading only. No broker or messaging interfaces."""
import hashlib
import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo('Asia/Tokyo')
SIGNALS = ('反発確認', '反発初動')
CHECKS = ('fundamentals', 'earnings', 'decline_cause', 'liquidity', 'stop', 'target', 'signal')


def instant(value):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError('timezone required')
    return dt.astimezone(JST)


def positive(value):
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError('finite positive price required')
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def quote_price(quote, symbol, at, now):
    """Only exact timestamped trades; minute OHLC is deliberately ineligible."""
    if quote.get('symbol') != symbol or quote.get('kind') != 'trade':
        raise ValueError('exact trade evidence required; bars are not ticks')
    if instant(quote['market_time']) != at or instant(quote['retrieved_at']) > now:
        raise ValueError('quote timestamp mismatch or future retrieval')
    if instant(quote['retrieved_at']) < at:
        raise ValueError('retrieval precedes market event')
    if not quote.get('source') or not quote.get('evidence_uri'):
        raise ValueError('source and archived evidence required')
    return positive(quote['price'])


def register(ledger, plans, quotes, now):
    """Plans must have been checked before the fixed decision time (no hindsight)."""
    result = [dict(t) for t in ledger]
    audit = []
    for plan in plans:
        symbol = plan.get('symbol', '')
        try:
            if not isinstance(symbol, str) or not symbol.strip() or symbol != symbol.strip().upper():
                raise ValueError('nonempty canonical symbol required')
            at = instant(plan['decision_time_jst'])
            if (at.hour, at.minute, at.second, at.microsecond) != (23, 45, 0, 0) or at > now:
                raise ValueError('decision must be past 23:45:00 JST')
            trade_id = symbol + '-' + at.isoformat()
            if any(t['trade_id'] == trade_id for t in result):
                continue
            if len(result) >= 30:
                raise ValueError('30 trade lifetime limit')
            if any(t['symbol'] == symbol and t['status'] == 'OPEN' for t in result):
                raise ValueError('symbol already open')
            if plan['signal'] not in SIGNALS:
                raise ValueError('invalid signal')
            if instant(plan['prepared_at']) > at:
                raise ValueError('plan prepared after decision time')
            # Checks are evidence-bearing records, never inferred from a positive revenue number.
            for key in CHECKS:
                check = plan['checks'][key]
                if check['status'] != 'PASS' or not check['source'] or not check['rationale']:
                    raise ValueError('unverified check: ' + key)
                if instant(check['checked_at']) > at:
                    raise ValueError('check completed after decision: ' + key)
            matched = [q for q in quotes if q.get('symbol') == symbol
                       and instant(q['market_time']) == at]
            if len(matched) != 1:
                raise ValueError('one unambiguous exact quote required')
            q = matched[0]
            entry = quote_price(q, symbol, at, now)
            stop, target = positive(plan['stop_price']), positive(plan['target_price'])
            if not stop < entry < target:
                raise ValueError('stop < entry < target required')
            rr = (target - entry) / (entry - stop)
            if rr < 1.5:
                raise ValueError('R:R below 1.5')
            result.append(dict(trade_id=trade_id, symbol=symbol, signal=plan['signal'],
                               entry_time_jst=at.isoformat(), entry_price=entry,
                               quote_source=q['source'], stop_price=stop, target_price=target,
                               rr_at_entry=rr, deadline_jst=(at + timedelta(days=90)).isoformat(),
                               status='OPEN', exit_time_jst='', exit_price='', exit_reason='',
                               return_pct='', r_multiple='', plan_sha256=digest(plan),
                               entry_evidence_sha256=digest(q), tracked_through_jst=at.isoformat()))
            audit.append({'symbol': symbol, 'status': 'REGISTERED', 'trade_id': trade_id})
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            audit.append({'symbol': symbol, 'status': 'BLOCKED', 'reason': str(exc)})
    return result, audit


def settle(ledger, streams, now):
    """Require continuous provider-attested raw trades, including the exit trade.

    A gap, bar, or split blocks the position rather than inventing a fill. Streams
    need coverage_start/end and complete=True from a validated provider adapter.
    This repository deliberately supplies no provider capable of that attestation.
    """
    result, audit = [dict(t) for t in ledger], []
    for trade in result:
        if trade['status'] != 'OPEN':
            continue
        try:
            stream = streams[trade['trade_id']]
            start = instant(trade.get('tracked_through_jst') or trade['entry_time_jst'])
            end = instant(stream['coverage_end'])
            if stream.get('complete') is not True or instant(stream['coverage_start']) != start or not start < end <= now:
                raise ValueError('incomplete or noncontiguous coverage')
            if not stream.get('source') or not stream.get('evidence_uri'):
                raise ValueError('coverage evidence required')
            if stream.get('corporate_actions_clear') is not True:
                raise ValueError('corporate actions require reconciliation')
            events = stream['trades']
            times = [instant(q['market_time']) for q in events]
            if times != sorted(times) or len(set(times)) != len(times):
                raise ValueError('unordered or ambiguous trade timestamps')
            # Validate all evidence before mutating this trade.
            prices = [quote_price(q, trade['symbol'], at, now) for q, at in zip(events, times)]
            if any(not start < at <= end for at in times):
                raise ValueError('event outside coverage')
            deadline = instant(trade['deadline_jst'])
            for q, at, price in zip(events, times, prices):
                reason = ('STOP' if price <= float(trade['stop_price']) else
                          'TARGET' if price >= float(trade['target_price']) else
                          'MAX_90_DAYS' if at >= deadline else '')
                if reason:
                    entry = float(trade['entry_price'])
                    trade.update(status='CLOSED', exit_time_jst=at.isoformat(), exit_price=price,
                                 exit_reason=reason, return_pct=100 * (price / entry - 1),
                                 r_multiple=(price - entry) / (entry - float(trade['stop_price'])),
                                 exit_evidence_sha256=digest(q))
                    break
            trade['tracked_through_jst'] = end.isoformat()
            if trade['status'] == 'OPEN' and end >= deadline:
                audit.append({'trade_id': trade['trade_id'], 'status': 'AWAITING_VERIFIED_EXIT'})
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            audit.append({'trade_id': trade['trade_id'], 'status': 'BLOCKED', 'reason': str(exc)})
    return result, audit


def statistics(ledger):
    def group(rows):
        closed = [t for t in rows if t['status'] == 'CLOSED']
        rs = [float(t['r_multiple']) for t in closed]
        returns = [float(t['return_pct']) for t in closed]
        wins, losses = sum(r > 0 for r in rs), sum(r < 0 for r in rs)
        return dict(registered=len(rows), closed=len(closed), open=len(rows)-len(closed),
                    wins=wins, losses=losses, breakeven=len(rs)-wins-losses,
                    win_rate_pct=100*wins/len(rs) if rs else None,
                    expectancy_r=sum(rs)/len(rs) if rs else None,
                    mean_return_pct=sum(returns)/len(returns) if returns else None,
                    mean_holding_days=sum((instant(t['exit_time_jst'])-instant(t['entry_time_jst'])).total_seconds()/86400 for t in closed)/len(closed) if closed else None)
    return dict(total=group(ledger), by_signal={s: group([t for t in ledger if t['signal']==s]) for s in SIGNALS},
                remaining=max(0,30-len(ledger)), complete=len(ledger)==30 and all(t['status']=='CLOSED' for t in ledger))


def load_json(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def merge_unique(existing, incoming):
    result = list(existing)
    known = {digest(value) for value in result}
    for value in incoming:
        key = digest(value)
        if key not in known:
            result.append(value)
            known.add(key)
    return result


def validate_ledger(ledger, now):
    """Do not silently repair corrupt persisted positions or include them in results."""
    ids, open_symbols = set(), set()
    if len(ledger) > 30:
        raise ValueError('ledger exceeds 30 trades')
    for trade in ledger:
        tid, symbol = trade['trade_id'], trade['symbol']
        if not tid or tid in ids or not symbol or symbol != symbol.strip().upper():
            raise ValueError('invalid or duplicate ledger identity')
        ids.add(tid)
        if trade['status'] not in ('OPEN', 'CLOSED') or trade['signal'] not in SIGNALS:
            raise ValueError('invalid persisted status or signal')
        entry, stop, target = (positive(trade[k]) for k in ('entry_price','stop_price','target_price'))
        at = instant(trade['entry_time_jst'])
        if not stop < entry < target or (target-entry)/(entry-stop) < 1.5:
            raise ValueError('invalid persisted entry plan')
        if at > now or instant(trade['deadline_jst']) != at + timedelta(days=90):
            raise ValueError('invalid persisted entry time or deadline')
        if trade['status'] == 'OPEN':
            if symbol in open_symbols:
                raise ValueError('multiple open positions in one symbol')
            open_symbols.add(symbol)
            tracked = instant(trade.get('tracked_through_jst') or trade['entry_time_jst'])
            if not at <= tracked <= now:
                raise ValueError('invalid tracking cursor')
        else:
            exit_price = positive(trade['exit_price'])
            if not at <= instant(trade['exit_time_jst']) <= now:
                raise ValueError('invalid exit time')
            expected = {'return_pct':100*(exit_price/entry-1), 'r_multiple':(exit_price-entry)/(entry-stop)}
            for key, value in expected.items():
                actual = float(trade[key])
                if not math.isfinite(actual) or not math.isclose(actual,value,rel_tol=1e-9,abs_tol=1e-8):
                    raise ValueError('corrupt persisted result: '+key)


def run(now=None):
    import csv
    from io import StringIO
    now = now or datetime.now(JST)
    root, out = Path('input'), Path('output')
    root.mkdir(exist_ok=True); out.mkdir(exist_ok=True)
    path = root/'trades.csv'
    ledger = list(csv.DictReader(path.read_text(encoding='utf-8-sig').splitlines())) if path.exists() else []
    validate_ledger(ledger, now)
    archive = load_json(root/'evidence.json', {'plans':[], 'quotes':[], 'streams':{}, 'stream_archive':[]})
    archive.setdefault('stream_archive', [])
    incoming = load_json(out/'verified_evidence.json', {'plans':[], 'quotes':[], 'streams':{}})
    # Merge evidence by hash; preserve the exact evidence used for every entry and exit.
    for key in ('plans','quotes'):
        archive[key] = merge_unique(archive[key], incoming[key])
    archive['stream_archive'].extend({'trade_id': key, 'stream': value} for key, value in incoming['streams'].items()
                                     if digest({'trade_id': key, 'stream': value}) not in {digest(v) for v in archive['stream_archive']})
    ledger, entries = register(ledger, archive['plans'], archive['quotes'], now)
    ledger, exits = settle(ledger, incoming['streams'], now)
    fields = ['trade_id','symbol','signal','entry_time_jst','entry_price','quote_source','stop_price',
              'target_price','rr_at_entry','deadline_jst','status','exit_time_jst','exit_price',
              'exit_reason','return_pct','r_multiple','plan_sha256','entry_evidence_sha256',
              'tracked_through_jst','exit_evidence_sha256']
    buf=StringIO(); writer=csv.DictWriter(buf,fieldnames=fields); writer.writeheader(); writer.writerows(ledger)
    # Evidence is persisted before the ledger; replacement of each file is atomic.
    for dest, value in ((root/'evidence.json',json.dumps(archive,ensure_ascii=False,indent=2)),
                        (path,buf.getvalue())):
        tmp=dest.with_suffix(dest.suffix+'.tmp'); tmp.write_text(value,encoding='utf-8'); tmp.replace(dest)
    report={'paper_only':True, 'generated_jst':now.isoformat(), 'statistics':statistics(ledger),
            'entry_audit':entries, 'exit_audit':exits,
            'overdue_open_trades':[t['trade_id'] for t in ledger if t['status']=='OPEN' and instant(t['deadline_jst'])<=now],
            'unimplemented':['Validated exact-trade provider adapter','Automated official financial/news verification',
                             'Approved numeric rules for financial quality and technical stop/target validation',
                             'Scenario deterioration exits and corporate action reconciliation'],
            'price_status':'No verified exact-price adapter configured; no fallback fills.'}
    (out/'trade_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'trade_log.csv').write_text(buf.getvalue(),encoding='utf-8-sig')
    s=report['statistics']['total']
    overdue=len(report['overdue_open_trades'])
    (out/'summary.md').write_text(f"# NEXUS 仮想検証\n\n登録 {s['registered']}/30・保有 {s['open']}・決済 {s['closed']}・90日期限超過 {overdue}\n\n23:45実約定価格の取得アダプター未接続。未確認データで登録しません。\n\n業績・ニュース・支持線確認の完全自動化は未完成です。\n",encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':
    run()
