"""Evidence-gated paper trading only. No broker or messaging interfaces."""
import hashlib
import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from price_policy import POLICY, entry_reference_price
from historical_probe import timestamp_ns
from strategy_rules import VERSION

JST = ZoneInfo('Asia/Tokyo')
SIGNALS = ('反発確認', '反発初動')
CHECKS = ('fundamentals', 'earnings', 'decline_cause', 'liquidity', 'stop', 'target', 'signal')


def instant(value):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError('timezone required')
    return dt.astimezone(JST)


def positive(value):
    if isinstance(value, bool):
        raise ValueError('boolean is not a price')
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
    stamp = timestamp_ns(at if isinstance(at, str) else at.isoformat())
    if timestamp_ns(quote['market_time']) != stamp or timestamp_ns(quote['retrieved_at']) > timestamp_ns(now.isoformat()):
        raise ValueError('quote timestamp mismatch or future retrieval')
    if timestamp_ns(quote['retrieved_at']) < stamp:
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
            if (at.hour, at.minute, at.second, at.microsecond) != (23, 45, 0, 0) or at > now or timestamp_ns(plan['decision_time_jst']) % 1_000_000_000:
                raise ValueError('decision must be past 23:45:00 JST')
            trade_id = symbol + '-' + at.isoformat()
            if any(t['trade_id'] == trade_id for t in result):
                continue
            if len(result) >= 30:
                raise ValueError('30 trade lifetime limit')
            if any(t['symbol'] == symbol and t['status'] == 'OPEN' for t in result):
                raise ValueError('symbol already open')
            if plan.get('rules_version') != VERSION:
                raise ValueError('owner-approved strategy version required')
            if plan['signal'] not in SIGNALS:
                raise ValueError('invalid signal')
            if timestamp_ns(plan['prepared_at']) > timestamp_ns(at.isoformat()):
                raise ValueError('plan prepared after decision time')
            # Checks are evidence-bearing records, never inferred from a positive revenue number.
            for key in CHECKS:
                check = plan['checks'][key]
                if check['status'] != 'PASS' or not check['source'] or not check['rationale']:
                    raise ValueError('unverified check: ' + key)
                if timestamp_ns(check['checked_at']) > timestamp_ns(at.isoformat()):
                    raise ValueError('check completed after decision: ' + key)
            matched = [q for q in quotes if q.get('symbol') == symbol
                       and q.get('decision_time_jst') is not None
                       and timestamp_ns(q['decision_time_jst']) == timestamp_ns(at.isoformat())]
            if len(matched) != 1:
                raise ValueError('one unambiguous verified as-of quote required')
            q = matched[0]
            entry = entry_reference_price(q, symbol, at, now)
            stop, target = positive(plan['stop_price']), positive(plan['target_price'])
            if not stop < entry < target:
                raise ValueError('stop < entry < target required')
            rr = (target - entry) / (entry - stop)
            if rr < 1.5:
                raise ValueError('R:R below 1.5')
            result.append(dict(trade_id=trade_id, symbol=symbol, signal=plan['signal'],rules_version=plan['rules_version'],
                               entry_time_jst=at.isoformat(), entry_price=entry,
                               entry_market_time=q['market_time'], entry_verified_at=q['retrieved_at'],
                               entry_price_policy=POLICY, registered_at_jst=now.isoformat(),
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
    The historical SIP adapter supplies this attestation only after validation.
    """
    result, audit = [dict(t) for t in ledger], []
    for trade in result:
        if trade['status'] != 'OPEN':
            continue
        try:
            stream = streams[trade['trade_id']]
            start = timestamp_ns(trade.get('tracked_through_jst') or trade['entry_time_jst'])
            end = timestamp_ns(stream['coverage_end'])
            if stream.get('complete') is not True or timestamp_ns(stream['coverage_start']) != start or not start < end <= timestamp_ns(now.isoformat()):
                raise ValueError('incomplete or noncontiguous coverage')
            if not stream.get('source') or not stream.get('evidence_uri'):
                raise ValueError('coverage evidence required')
            if stream.get('corporate_actions_clear') is not True:
                raise ValueError('corporate actions require reconciliation')
            events = stream['trades']
            times = [timestamp_ns(q['market_time']) for q in events]
            if times != sorted(times) or len(set(times)) != len(times):
                raise ValueError('unordered or ambiguous trade timestamps')
            # Validate all evidence before mutating this trade.
            prices = [quote_price(q, trade['symbol'], q['market_time'], now) for q in events]
            if any(not start < at <= end for at in times):
                raise ValueError('event outside coverage')
            deadline = timestamp_ns(trade['deadline_jst'])
            scenario = stream.get('scenario_exit')
            scenario_at = None
            if scenario is not None:
                if scenario.get('verified') is not True or not scenario.get('source') or not scenario.get('evidence_uri') or not scenario.get('rationale'):
                    raise ValueError('scenario exit requires verified official evidence')
                scenario_at = timestamp_ns(scenario['checked_at'])
                if not timestamp_ns(scenario['published_at']) <= scenario_at <= timestamp_ns(now.isoformat()):
                    raise ValueError('future or inconsistent scenario evidence')
            for q, at, price in zip(events, times, prices):
                reason = ('STOP' if price <= float(trade['stop_price']) else
                          'TARGET' if price >= float(trade['target_price']) else
                          'MAX_90_DAYS' if at >= deadline else
                          'SCENARIO_DETERIORATION' if scenario_at is not None and at >= scenario_at else '')
                if reason:
                    entry = float(trade['entry_price'])
                    trade.update(status='CLOSED', exit_time_jst=q['market_time'], exit_price=price,
                                 exit_reason=reason, return_pct=100 * (price / entry - 1),
                                 r_multiple=(price - entry) / (entry - float(trade['stop_price'])),
                                 exit_evidence_sha256=digest(q))
                    break
            trade['tracked_through_jst'] = stream['coverage_end']
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
        if trade.get('rules_version') != VERSION:
            raise ValueError('persisted strategy version mismatch')
        if trade.get('entry_price_policy') != POLICY:
            raise ValueError('persisted entry needs approved policy metadata')
        entry_reference_price(dict(symbol=symbol,kind='trade',entry_policy=POLICY,decision_time_jst=at.isoformat(),
                                   market_time=trade['entry_market_time'],retrieved_at=trade['entry_verified_at'],
                                   history_complete=True,sale_conditions_verified=True,latest_valid_trade_verified=True,
                                   provider_verified=True,source=trade['quote_source'],evidence_uri=trade['entry_evidence_sha256'],price=entry),symbol,at,now)
        if not instant(trade['entry_verified_at'])<=instant(trade['registered_at_jst'])<=now:
            raise ValueError('invalid persisted verification/registration timing')
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
    ledger, entries = register(ledger, archive['plans'], archive['quotes'], now)
    ledger, exits = settle(ledger, incoming['streams'], now)
    # The provider inspected every trade. Keep source ranges + full stream hash,
    # compact statistics and the exact selected exit print permanently in git.
    # Non-exit raw tick responses are not retained; their source query, count,
    # and hash allow comparison with a later vendor retrieval (which may revise).
    archived_streams=[]
    for tid, stream in incoming['streams'].items():
        events=stream.get('trades',[])
        selected={t.get('exit_evidence_sha256') for t in ledger if t['trade_id']==tid and t['status']=='CLOSED'}
        summary={k:v for k,v in stream.items() if k!='trades'}
        summary.update(trade_count=len(events),trades_sha256=digest(events),
                       selected_exit_trades=[q for q in events if digest(q) in selected],
                       min_price=min((float(q['price']) for q in events),default=None),
                       max_price=max((float(q['price']) for q in events),default=None))
        archived_streams.append({'trade_id':tid,'stream':summary})
    archive['stream_archive']=merge_unique(archive['stream_archive'],archived_streams)
    fields = ['trade_id','symbol','signal','entry_time_jst','entry_price','quote_source','stop_price',
              'target_price','rr_at_entry','deadline_jst','status','exit_time_jst','exit_price',
              'exit_reason','return_pct','r_multiple','plan_sha256','entry_evidence_sha256',
              'tracked_through_jst','exit_evidence_sha256','entry_market_time','entry_verified_at',
              'entry_price_policy','registered_at_jst','rules_version']
    buf=StringIO(); writer=csv.DictWriter(buf,fieldnames=fields); writer.writeheader(); writer.writerows(ledger)
    # Evidence is persisted before the ledger; replacement of each file is atomic.
    for dest, value in ((root/'evidence.json',json.dumps(archive,ensure_ascii=False,indent=2)),
                        (path,buf.getvalue())):
        tmp=dest.with_suffix(dest.suffix+'.tmp'); tmp.write_text(value,encoding='utf-8'); tmp.replace(dest)
    report={'paper_only':True, 'generated_jst':now.isoformat(), 'statistics':statistics(ledger),
            'entry_audit':entries, 'exit_audit':exits,
            'overdue_open_trades':[t['trade_id'] for t in ledger if t['status']=='OPEN' and instant(t['deadline_jst'])<=now],
            'limitations':['Official guidance/decline reviews require archived source-backed assertions; missing reviews block entry',
                           'Corporate actions needing price/share/cash-flow reconciliation block settlement',
                           'Vendor corrections or corporate-action notices may arrive after retrieval',
                           'Quotes and results are hypothetical references; no execution or fee/slippage model'],
            'entry_price_policy':POLICY,
            'price_status':'Authenticated SIP history acquisition confirmed. Cancellation/update filtering implemented. Verified production provider adapter available; official plan evidence required. No fallback fills.'}
    (out/'trade_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'trade_log.csv').write_text(buf.getvalue(),encoding='utf-8-sig')
    s=report['statistics']['total']
    overdue=len(report['overdue_open_trades'])
    (out/'summary.md').write_text(f"# NEXUS 仮想検証\n\n登録 {s['registered']}/30・保有 {s['open']}・決済 {s['closed']}・90日期限超過 {overdue}\n\n23:45以前60秒以内の最終有効取引を基準価格とし、翌0:01以降に照合。SIP履歴の認証付き取得は確認済みです。\n\n業績・ニュース・支持線確認の完全自動化は未完成です。\n",encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':
    run()
