"""Stage research before decision, collect SIP history afterwards; never auto-PASS research.

No order API, posting or notification client. Only existing candidate rules are used.
"""
import argparse
import csv
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from historical_probe import probe, timestamp_ns
from market_calendar import JST, latest_due_decision, next_decision, session_at
from paper_engine import digest, run, settle, instant, validate_ledger
from price_policy import history_candidate
from strategy_rules import VERSION, build_plan
from official_data import collect_financials, public_json, verify_review_documents
from market_provider import verified_reference, corporate_state, collect_interval, normalized_stream

PENDING = ('Official guidance/decline-cause review where evidence is missing',)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def freeze_research(now, root=Path('input'), out=Path('output'), collect_official=False):
    """Immutable date-keyed research snapshot, not an approved plan or fill."""
    at = next_decision(now)
    date = at.date().isoformat()
    path = root/'research'/f'{date}.json'
    if path.exists():
        return json.loads(path.read_text())
    with (out/'secondary_review.csv').open(encoding='utf-8-sig', newline='') as src:
        rows = list(csv.DictReader(src))
    seen = set()
    for row in rows:
        if row['symbol'] in seen:
            raise ValueError('duplicate research symbol')
        seen.add(row['symbol'])
    value = {'prepared_at': now.isoformat(), 'decision_time_jst': at.isoformat(),
             'kind': 'RESEARCH_ONLY', 'source_sha256': digest(rows), 'rows': rows,
             'pending_rule_definitions': list(PENDING)}
    value['market_data'] = {}
    value['financials'] = {}
    value['financial_audit'] = []
    eligible = [r for r in rows if r.get('secondary_status')=='二次調査候補']
    for row in eligible:
        bars_path=out/'daily_bars'/(digest_symbol(row['symbol'])+'.json')
        if bars_path.exists():
            value['market_data'][row['symbol']]=json.loads(bars_path.read_text())
    monitored=list(eligible)
    ledger_path=root/'trades.csv'
    if ledger_path.exists():
        known={r['symbol'] for r in monitored}
        for trade in csv.DictReader(ledger_path.read_text(encoding='utf-8-sig').splitlines()):
            if trade['status']=='OPEN' and trade['symbol'] not in known:
                monitored.append({'symbol':trade['symbol']});known.add(trade['symbol'])
    if collect_official and monitored:
        try:
            mapping=public_json('https://www.sec.gov/files/company_tickers.json')
        except Exception:
            mapping=None
            value['financial_audit'].append({'status':'SEC_MAPPING_UNAVAILABLE'})
        if mapping is not None:
            for row in monitored:
                try:
                    collected=datetime.now(JST)
                    if collected>at:
                        raise ValueError('official data collection missed decision')
                    value['financials'][row['symbol']]=collect_financials(row['symbol'],at,collected,root,ticker_map=mapping)
                except Exception as exc:
                    value['financial_audit'].append({'symbol':row['symbol'],'status':'UNVERIFIED','reason':type(exc).__name__})
    if collect_official:
        update_scenarios(value['financials'],datetime.now(JST),root)
    value['prepared_at']=datetime.now(JST).isoformat() if collect_official else now.isoformat()
    if timestamp_ns(value['prepared_at'])>timestamp_ns(at.isoformat()):
        raise ValueError('preparation missed decision; no retrospective research snapshot')
    value['snapshot_sha256']=digest({k:v for k,v in value.items() if k!='snapshot_sha256'})
    write_json(path, value)
    return value


def digest_symbol(symbol):
    import hashlib
    return hashlib.sha256(symbol.encode()).hexdigest()


def prepare_plans(now, root=Path('input'), out=Path('output')):
    at=next_decision(now)
    path=root/'research'/f'{at.date().isoformat()}.json'
    report={'decision_time_jst':at.isoformat(),'rules_version':VERSION,'plans':[],'audit':[],
            'prepared_at':now.isoformat()}
    if not path.exists():
        report['audit'].append({'status':'NO_PREDECISION_RESEARCH'})
    else:
        research=json.loads(path.read_text())
        expected=research.get('snapshot_sha256')
        if not expected or digest({k:v for k,v in research.items() if k!='snapshot_sha256'})!=expected:
            raise ValueError('research snapshot proof mismatch')
        reviews_path=root/'official_reviews.json'
        reviews=json.loads(reviews_path.read_text()) if reviews_path.exists() else {}
        for row in research['rows']:
            if row.get('secondary_status')!='二次調査候補':
                continue
            symbol=row['symbol']
            try:
                review=reviews.get(at.date().isoformat(),{}).get(symbol)
                if not review:
                    raise ValueError('official guidance, decline and earnings evidence missing')
                verify_review_documents(review,root)
                md=research['market_data'][symbol]
                if md['symbol']!=symbol or timestamp_ns(md['retrieved_at'])>timestamp_ns(now.isoformat()):
                    raise ValueError('daily bars provenance mismatch')
                plan=build_plan(row,md['bars'],research['financials'][symbol],review,at,
                                now.isoformat(),f"{path}#daily-bars:{symbol}")
                report['plans'].append(plan)
            except (ValueError,KeyError,TypeError,OSError) as exc:
                report['audit'].append({'symbol':symbol,'status':'BLOCKED','reason':str(exc)})
    report['sha256']=digest({k:v for k,v in report.items() if k!='sha256'})
    # Candidate plans may be refreshed before the decision; never after it.
    write_json(root/'plans'/f'{at.date().isoformat()}.json',report)
    write_json(out/'plan_status.json',report)
    return report


def reconcile(now, root=Path('input'), out=Path('output'), fetch_probe=probe, calendar=session_at, clock=None, fetch_corporate=corporate_state):
    at = latest_due_decision(now)
    out.mkdir(exist_ok=True, parents=True)
    # Never reuse an earlier invocation's validated evidence file.
    (out/'verified_evidence.json').unlink(missing_ok=True)
    report = {'decision_time_jst': at.isoformat(), 'paper_only': True,
              'pending_evidence': list(PENDING), 'prices': [], 'registered': None}
    try:
        session = calendar(at)
        report['session'] = session
        if not session['open']:
            report['status'] = 'NON_TRADING_SESSION'
            return report
        path = root/'research'/f'{at.date().isoformat()}.json'
        if not path.exists():
            report['status'] = 'NO_PREDECISION_RESEARCH'
            return report
        research = json.loads(path.read_text())
        if research['kind'] != 'RESEARCH_ONLY' or timestamp_ns(research['prepared_at']) > timestamp_ns(at.isoformat()) or research['decision_time_jst'] != at.isoformat():
            raise ValueError('research was not frozen before decision')
        if digest(research['rows']) != research['source_sha256']:
            raise ValueError('research snapshot hash mismatch')
        # This is an acquisition diagnostic budget, never an entry eligibility rule.
        candidates = [r for r in research['rows'] if r.get('secondary_status') == '二次調査候補']
        report['research_candidates'] = len(candidates)
        plan_path=root/'plans'/f'{at.date().isoformat()}.json'
        frozen=json.loads(plan_path.read_text()) if plan_path.exists() else None
        plans=[]
        if frozen is not None:
            if (frozen.get('rules_version')!=VERSION or frozen['decision_time_jst']!=at.isoformat()
                    or timestamp_ns(frozen['prepared_at'])>timestamp_ns(at.isoformat())
                    or frozen.get('sha256')!=digest({k:v for k,v in frozen.items() if k!='sha256'})):
                raise ValueError('plan snapshot is invalid or late')
            plans=frozen['plans']
        report['plan_audit']=frozen.get('audit',[]) if frozen else [{'status':'NO_PREDECISION_PLANS'}]
        # Every approved plan is priced; the diagnostic budget only limits the
        # remaining unapproved research candidates, never trade eligibility.
        approved={p['symbol'] for p in plans}
        ordered=[r for r in candidates if r['symbol'] in approved]+[r for r in candidates if r['symbol'] not in approved]
        budget = int(os.getenv('NEXUS_PRICE_DIAGNOSTIC_LIMIT', '30'))
        if not 1 <= budget <= 200:
            raise ValueError('diagnostic budget must be 1..200')
        quotes=[]
        requested=ordered[:max(budget,len(approved))]
        for row in requested:
            symbol = row['symbol']
            result = fetch_probe(symbol, at, now, os.getenv('ALPACA_DATA_KEY',''), os.getenv('ALPACA_DATA_SECRET',''))
            result['retrieved_at'] = (clock() if clock else datetime.now(JST)).isoformat()
            raw = root/'market_evidence'/f'{digest(result)}.json'
            write_json(raw, result)
            item = {'symbol': symbol, 'status': result['status'], 'evidence_uri': str(raw)}
            if result['status'] == 'HISTORY_RECEIVED':
                try:
                    quote = verified_reference(result, result['retrieved_at'], str(raw))
                    if symbol in approved:
                        ca=fetch_corporate(symbol,at,at,os.getenv('ALPACA_DATA_KEY',''),os.getenv('ALPACA_DATA_SECRET',''))
                        ca_path=root/'market_evidence'/f'{digest(ca)}.json'
                        write_json(ca_path,ca)
                        if ca.get('clear') is not True:
                            raise ValueError('corporate-action tracking preflight not clear')
                        quote['corporate_evidence_uri']=str(ca_path)
                        quotes.append(quote)
                    item.update(status='REFERENCE_OBSERVED', price=quote['price'], market_time=quote['market_time'],
                                reference_age_seconds=quote['reference_age_seconds'],
                                provider_verified=True, reason='Reference price verified; entry still requires approved evidence-bearing plan')
                except Exception as exc:
                    item.update(status='BLOCKED', reason=str(exc) if isinstance(exc,ValueError) else type(exc).__name__)
            report['prices'].append(item)
        report['price_diagnostics_deferred'] = max(0, len(candidates)-len(requested))
        write_json(out/'verified_evidence.json',{'plans':plans,'quotes':quotes,'streams':{}})
        report['status'] = 'VERIFIED_INPUT_READY' if plans else 'NO_VERIFIED_PLANS'
        return report
    except (ValueError, KeyError, TypeError, OSError) as exc:
        report.update(status='BLOCKED', reason=str(exc))
        return report
    finally:
        write_json(out/'daily_status.json', report)


def update_scenarios(financials, now, root):
    """Keep the earliest verified post-entry deterioration; stale reviews cannot overwrite it."""
    path=root/'trades.csv'
    if not path.exists():
        return []
    ledger=list(csv.DictReader(path.read_text(encoding='utf-8-sig').splitlines()))
    event_path=root/'scenario_exits.json'
    events=json.loads(event_path.read_text()) if event_path.exists() else {}
    reviews_path=root/'official_reviews.json'
    reviews=json.loads(reviews_path.read_text()) if reviews_path.exists() else {}
    audit=[]
    for trade in ledger:
        if trade['status']!='OPEN':
            continue
        if trade['trade_id'] in events:
            event=events[trade['trade_id']]
            audit.append({'trade_id':trade['trade_id'],'status':'VERIFIED_EXIT_TRIGGER',
                          'trigger':event.get('trigger','EXISTING_VERIFIED_TRIGGER'),'checked_at':event['checked_at']})
            continue
        entered=timestamp_ns(trade['entry_time_jst'])
        candidates=[]
        f=financials.get(trade['symbol'])
        if f and f.get('quality',{}).get('pass') is False:
            try:
                if f['symbol']!=trade['symbol']:
                    raise ValueError('financial identity mismatch')
                if not timestamp_ns(f['published_at'])<=timestamp_ns(f['checked_at'])<=timestamp_ns(now.isoformat()):
                    raise ValueError('future or inconsistent financial evidence')
                sha=f['evidence_sha256']
                if len(sha)!=64 or any(c not in '0123456789abcdef' for c in sha):
                    raise ValueError('invalid financial evidence hash')
                proof=root/'official_evidence'/f'{sha}.json'
                document=json.loads(proof.read_text())
                if digest(document)!=sha or document['source']!=f['source']:
                    raise ValueError('financial evidence provenance mismatch')
                if timestamp_ns(document['retrieved_at'])>timestamp_ns(now.isoformat()):
                    raise ValueError('financial source acquired in future')
                from strategy_rules import financial_quality
                quality=financial_quality(*(f[k] for k in ('revenue','prior_revenue','operating_income','prior_operating_income')))
                if quality['pass']:
                    raise ValueError('financial deterioration flag disagrees with numbers')
                candidates.append({'verified':True,'source':f['source'],'evidence_uri':str(proof),
                    'published_at':f['published_at'],'checked_at':now.isoformat(),
                    'trigger':'QUARTERLY_FINANCIALS',
                    'rationale':'Approved quarterly revenue/operating-income condition no longer passes'})
            except (ValueError,KeyError,TypeError,OSError) as exc:
                audit.append({'trade_id':trade['trade_id'],'status':'UNVERIFIED_FINANCIAL_EXIT','reason':str(exc)})
        for by_symbol in reviews.values():
            review=by_symbol.get(trade['symbol'],{})
            if review and review.get('symbol')!=trade['symbol']:
                audit.append({'trade_id':trade['trade_id'],'status':'UNVERIFIED_REVIEW_EXIT','reason':'review identity mismatch'})
                continue
            for name, reason in (
                    ('guidance_not_lowered','Official review confirms revenue guidance lowered'),
                    ('no_material_business_impairment','Official review confirms material business impairment')):
                record=review.get(name)
                if not record or record.get('value') is not False:
                    continue
                try:
                    # Reviews already known before entry must never override a newer event.
                    if timestamp_ns(record['checked_at'])<=entered:
                        continue
                    if review.get('rules_version')!=VERSION:
                        raise ValueError('review strategy version mismatch')
                    verify_review_documents(review,root,names=(name,))
                    from strategy_rules import checked
                    checked(record,now,required=False)
                    candidates.append({'verified':True,'source':record['source'],
                        'evidence_uri':str(root/'official_documents'/f"{record['evidence_sha256']}.json"),
                        'published_at':record['published_at'],'checked_at':record['checked_at'],
                        'trigger':name,'rationale':reason})
                except (ValueError,KeyError,TypeError,OSError) as exc:
                    audit.append({'trade_id':trade['trade_id'],'status':'UNVERIFIED_REVIEW_EXIT','reason':str(exc)})
        candidates=[e for e in candidates if entered<timestamp_ns(e['checked_at'])<=timestamp_ns(now.isoformat())]
        if candidates:
            event=min(candidates,key=lambda e:timestamp_ns(e['checked_at']))
            events[trade['trade_id']]=event
            audit.append({'trade_id':trade['trade_id'],'status':'VERIFIED_EXIT_TRIGGER',
                          'trigger':event['trigger'],'checked_at':event['checked_at']})
    write_json(event_path,events)
    write_json(root/'scenario_exit_audit.json',{'generated_at':now.isoformat(),'events':audit})
    return audit


def refresh_scenarios(now, root=Path('input'), out=Path('output')):
    path=root/'trades.csv'
    if not path.exists():
        return
    rows=[t for t in csv.DictReader(path.read_text(encoding='utf-8-sig').splitlines()) if t['status']=='OPEN']
    if not rows:
        return
    financials={};audit=[]
    try:
        mapping=public_json('https://www.sec.gov/files/company_tickers.json')
    except Exception:
        mapping=None
    for row in rows:
        try:
            if mapping is None:
                raise ValueError('SEC mapping unavailable')
            f=collect_financials(row['symbol'],now,now,root,ticker_map=mapping)
            financials[row['symbol']]=f
            audit.append({'symbol':row['symbol'],'financial_status':'PASS' if f['quality']['pass'] else 'FAILED',
                          'guidance_status':'Requires archived official review'})
        except Exception as exc:
            audit.append({'symbol':row['symbol'],'financial_status':'UNVERIFIED','reason':type(exc).__name__})
    exit_audit=update_scenarios(financials,now,root)
    write_json(out/'scenario_exit_status.json',{'generated_at':now.isoformat(),'events':exit_audit})
    write_json(out/'scenario_monitoring.json',audit)


def track_positions(now, root=Path('input'), out=Path('output'), fetch_interval=collect_interval, fetch_corporate=corporate_state):
    """Bounded, resumable complete history replay; gaps never advance the cursor."""
    path=root/'trades.csv'
    ledger=list(csv.DictReader(path.read_text(encoding='utf-8-sig').splitlines())) if path.exists() else []
    validate_ledger(ledger,now)
    incoming_path=out/'verified_evidence.json'
    incoming=json.loads(incoming_path.read_text()) if incoming_path.exists() else {'plans':[],'quotes':[],'streams':{}}
    report={'positions':[],'rules_version':VERSION,'generated_at':now.isoformat()}
    scenario_path=root/'scenario_exits.json'
    scenarios=json.loads(scenario_path.read_text()) if scenario_path.exists() else {}
    limit=int(os.getenv('NEXUS_TRACK_CHUNKS_PER_RUN','1000'))
    if not 1<=limit<=3000:
        raise ValueError('tracking chunk budget must be 1..3000')
    key,secret=os.getenv('ALPACA_DATA_KEY',''),os.getenv('ALPACA_DATA_SECRET','')
    open_count=sum(t['status']=='OPEN' for t in ledger)
    quota=max(1,limit//max(open_count,1))
    for trade in ledger:
        if trade['status']!='OPEN':
            continue
        try:
            start=instant(trade.get('tracked_through_jst') or trade['entry_time_jst'])
            scenario=scenarios.get(trade['trade_id'])
            if scenario and timestamp_ns(scenario['checked_at'])<timestamp_ns(start.isoformat()):
                raise ValueError('Scenario confirmation predates tracking cursor; historical reconciliation required')
            available=now-timedelta(minutes=16)
            if available<=start:
                report['positions'].append({'trade_id':trade['trade_id'],'status':'AWAITING_DELAYED_DATA'})
                continue
            if limit<=0:
                report['positions'].append({'trade_id':trade['trade_id'],'status':'DEFERRED_BUDGET','tracked_through':start.isoformat()})
                continue
            ca=fetch_corporate(trade['symbol'],instant(trade['entry_time_jst']),available,key,secret)
            ca_path=root/'market_evidence'/f'{digest(ca)}.json'
            write_json(ca_path,ca)
            if ca['clear'] is not True:
                raise ValueError('corporate action needs reconciliation; position held without fabricated settlement')
            events=[]; chunks=[]; cursor=start; used=0
            import exchange_calendars as xcals
            cal=xcals.get_calendar('XNYS',start=str(start.date()-timedelta(days=7)),end=str(available.date()+timedelta(days=7)))
            # No unbounded catch-up or skipping. A large backlog remains visible.
            while cursor<available and limit>0 and used<quota:
                relevant=cal.schedule[cal.schedule['close']>cursor]
                next_open=relevant.iloc[0]['open'].to_pydatetime() if not relevant.empty else available+timedelta(days=1)
                if cursor<next_open-timedelta(microseconds=1):
                    jump=min(next_open-timedelta(microseconds=1),available)
                    gap={'coverage_start':cursor.isoformat(),'coverage_end':jump.isoformat(),
                         'source':'exchange_calendars XNYS','version':cal.__class__.__module__,
                         'reason':'No regular-hours trades while exchange closed'}
                    gap_path=root/'market_evidence'/f'{digest(gap)}.json'
                    write_json(gap_path,gap);chunks.append(str(gap_path));cursor=jump
                    if cursor>=available:
                        break
                end=min(cursor+timedelta(minutes=5),available)
                raw=fetch_interval(trade['symbol'],cursor,end,now,key,secret)
                proof={'symbol':trade['symbol'],'start':raw['start'],'end':raw['end'],
                       'retrieved_at':raw['retrieved_at'],'feed':'sip',
                       'sha256_raw_response':digest(raw),'raw_trade_count':len(raw['trades']),
                       'query':'https://data.alpaca.markets/v2/stocks/'+trade['symbol']+'/trades',
                       'pagination_complete':raw['complete'],'corporate_evidence':str(ca_path)}
                proof_path=root/'market_evidence'/f'{digest(proof)}.json'
                stream=normalized_stream(raw,ca,str(proof_path))
                # Store exact selected exit evidence; source data for non-exit
                # prints are attested by a hash/range rather than bloating git.
                write_json(proof_path,proof)
                events.extend(stream['trades']);chunks.append(str(proof_path));cursor=end;limit-=1;used+=1
                trial={**stream,'coverage_start':start.isoformat(),'trades':events}
                if trade['trade_id'] in scenarios:
                    trial['scenario_exit']=scenarios[trade['trade_id']]
                closed,audit=settle([trade],{trade['trade_id']:trial},now)
                blocked=[a for a in audit if a['status']=='BLOCKED']
                if blocked:
                    raise ValueError(blocked[0].get('reason',blocked[0]['status']))
                if closed[0]['status']=='CLOSED':
                    break
            if cursor>start:
                incoming['streams'][trade['trade_id']]={'coverage_start':start.isoformat(),'coverage_end':cursor.isoformat(),
                    'complete':True,'corporate_actions_clear':True,'source':'Alpaca historical SIP',
                    'evidence_uri':','.join(chunks),'trades':events}
                if trade['trade_id'] in scenarios:
                    incoming['streams'][trade['trade_id']]['scenario_exit']=scenarios[trade['trade_id']]
            report['positions'].append({'trade_id':trade['trade_id'],'status':'HISTORY_READY',
                                        'tracked_through':cursor.isoformat(),'backlog_seconds':max(0,(available-cursor).total_seconds())})
        except Exception as exc:
            # Raw HTTP error bodies/headers are deliberately excluded from logs.
            report['positions'].append({'trade_id':trade['trade_id'],'status':'BLOCKED',
                                        'reason':str(exc) if isinstance(exc,ValueError) else type(exc).__name__})
    write_json(incoming_path,incoming)
    write_json(out/'tracking_status.json',report)
    return report


def phone_summary(out=Path('output')):
    report = json.loads((out/'trade_report.json').read_text())
    daily = json.loads((out/'daily_status.json').read_text()) if (out/'daily_status.json').exists() else {}
    total = report['statistics']['total']
    lines = ['# NEXUS 仮想30トレード', '',
             f"登録 **{total['registered']}/30**・保有 {total['open']}・決済 {total['closed']}", '',
             '**仮想検証のみ。公式根拠・価格の確認がそろった計画だけを登録します。**', '',
             '正式判定は日本時間23:45。直前60秒以内の最終有効取引を翌0:01以降に照合します。', '',
             'Alpaca SIP認証付き取得は2026-10-27にAAPLで成功。取消・訂正前の取引は除外します。', '',
             f"今回の状態: `{daily.get('status', 'RESEARCH_PREPARED')}`", '',
             '承認済み: 売上前年同期比プラス＋営業利益黒字/改善、決算前3営業日回避、20営業日安値ストップ、60営業日高値目標。公式見通し・下落理由が不明なら保留。', '',
             '勝率: '+('未集計' if total['win_rate_pct'] is None else f"{total['win_rate_pct']:.1f}%"),
             '期待値R: '+('未集計' if total['expectancy_r'] is None else f"{total['expectancy_r']:.3f}"), '',
             '## 今回取得した仮想基準価格の候補', '',
             '|銘柄|取得状態|価格|取引時刻|', '|---|---|---:|---|']
    for item in daily.get('prices', []):
        lines.append(f"|{item['symbol']}|{item['status']}|{item.get('price','—')}|{item.get('market_time','—')}|")
    if daily.get('price_diagnostics_deferred'):
        lines += ['', f"診断予算により未取得 {daily['price_diagnostics_deferred']}銘柄。見送り・不合格とは扱いません。"]
    plan_file=out/'plan_status.json'
    if plan_file.exists():
        plan_report=json.loads(plan_file.read_text())
        lines += ['',f"準備済みの正式計画: {len(plan_report['plans'])}件",'', '|銘柄|計画を作れなかった理由|','|---|---|']
        for item in plan_report['audit']:
            lines.append(f"|{item.get('symbol','全体')}|{item.get('reason',item['status'])}|")
    if daily.get('plan_audit'):
        lines += ['', '## 正式計画の確認待ち', '', '|銘柄|理由|','|---|---|']
        for item in daily['plan_audit']:
            lines.append(f"|{item.get('symbol','全体')}|{item.get('reason',item['status'])}|")
    tracking_file=out/'tracking_status.json'
    if tracking_file.exists():
        lines += ['', '## 保有追跡', '', '|取引|状態|未追跡秒数・理由|','|---|---|---|']
        for item in json.loads(tracking_file.read_text())['positions']:
            lines.append(f"|{item['trade_id']}|{item['status']}|{item.get('reason',item.get('backlog_seconds','—'))}|")
    scenario_file=out/'scenario_exit_status.json'
    if scenario_file.exists():
        lines += ['', '## 業績・事業シナリオの撤退確認', '', '|取引|確認状態|内容|','|---|---|---|']
        for item in json.loads(scenario_file.read_text())['events']:
            lines.append(f"|{item['trade_id']}|{item['status']}|{item.get('reason',item.get('trigger','—'))}|")
    monitoring_file=out/'scenario_monitoring.json'
    if monitoring_file.exists():
        lines += ['', '## 保有銘柄の公式資料確認', '', '|銘柄|四半期数値|見通し・未取得理由|','|---|---|---|']
        for item in json.loads(monitoring_file.read_text()):
            lines.append(f"|{item['symbol']}|{item['financial_status']}|{item.get('reason',item.get('guidance_status','未確認'))}|")
    lines += ['', '価格候補の取得成功はエントリー合格ではありません。未確認の仮想約定は作りません。', '',
              '実売買・X投稿・外部通知の機能はありません。']
    (out/'summary.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('prepare','finalize','reconcile','track'))
    args = parser.parse_args()
    now = datetime.now(JST)
    Path('output').mkdir(exist_ok=True)
    Path('input/research').mkdir(parents=True, exist_ok=True)
    Path('input/market_evidence').mkdir(parents=True, exist_ok=True)
    (Path('output')/'verified_evidence.json').unlink(missing_ok=True)
    if args.phase == 'prepare':
        freeze_research(now,collect_official=True)
        prepare_plans(datetime.now(JST))
    elif args.phase == 'finalize':
        prepare_plans(now)
    elif args.phase == 'reconcile':
        reconcile(now)
    else:
        (Path('output')/'verified_evidence.json').unlink(missing_ok=True)
    refresh_scenarios(datetime.now(JST))
    track_positions(datetime.now(JST))
    run(datetime.now(JST))
    status_path=Path('output/daily_status.json')
    if status_path.exists():
        status=json.loads(status_path.read_text())
        trade_report=json.loads(Path('output/trade_report.json').read_text())
        status['registered']=sum(a['status']=='REGISTERED' for a in trade_report['entry_audit'])
        write_json(status_path,status)
    phone_summary()


if __name__ == '__main__':
    main()
