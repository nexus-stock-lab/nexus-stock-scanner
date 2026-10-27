"""Stage research before decision, collect SIP history afterwards; never auto-PASS research.

No order API, posting or notification client. Only existing candidate rules are used.
"""
import argparse
import csv
import json
import os
from datetime import datetime
from pathlib import Path

from historical_probe import probe, timestamp_ns
from market_calendar import JST, latest_due_decision, next_decision, session_at
from paper_engine import digest, run
from price_policy import history_candidate

PENDING = ('fundamentals', 'earnings', 'decline_cause', 'stop', 'target', 'scenario_exit')


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def freeze_research(now, root=Path('input'), out=Path('output')):
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
    write_json(path, value)
    return value


def reconcile(now, root=Path('input'), out=Path('output'), fetch_probe=probe, calendar=session_at):
    at = latest_due_decision(now)
    out.mkdir(exist_ok=True, parents=True)
    # Never reuse an earlier invocation's validated evidence file.
    (out/'verified_evidence.json').unlink(missing_ok=True)
    report = {'decision_time_jst': at.isoformat(), 'paper_only': True,
              'pending_rule_definitions': list(PENDING), 'prices': [], 'registered': 0}
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
        budget = int(os.getenv('NEXUS_PRICE_DIAGNOSTIC_LIMIT', '30'))
        if not 1 <= budget <= 200:
            raise ValueError('diagnostic budget must be 1..200')
        for row in candidates[:budget]:
            symbol = row['symbol']
            result = fetch_probe(symbol, at, now, os.getenv('ALPACA_DATA_KEY',''), os.getenv('ALPACA_DATA_SECRET',''))
            result['retrieved_at'] = datetime.now(JST).isoformat()
            raw = root/'market_evidence'/f'{digest(result)}.json'
            write_json(raw, result)
            item = {'symbol': symbol, 'status': result['status'], 'evidence_uri': str(raw)}
            if result['status'] == 'HISTORY_RECEIVED':
                try:
                    quote = history_candidate(result, result['retrieved_at'], str(raw))
                    item.update(status='REFERENCE_OBSERVED', price=quote['price'], market_time=quote['market_time'],
                                reference_age_seconds=quote['reference_age_seconds'],
                                provider_verified=False, reason='Research decision rules remain unapproved; no paper fill')
                except (ValueError, KeyError, TypeError) as exc:
                    item.update(status='BLOCKED', reason=str(exc))
            report['prices'].append(item)
        report['price_diagnostics_deferred'] = max(0, len(candidates)-budget)
        report['status'] = 'BLOCKED_RULES_UNDEFINED'
        return report
    except (ValueError, KeyError, TypeError, OSError) as exc:
        report.update(status='BLOCKED', reason=str(exc))
        return report
    finally:
        write_json(out/'daily_status.json', report)


def phone_summary(out=Path('output')):
    report = json.loads((out/'trade_report.json').read_text())
    daily = json.loads((out/'daily_status.json').read_text()) if (out/'daily_status.json').exists() else {}
    total = report['statistics']['total']
    lines = ['# NEXUS 仮想30トレード', '',
             f"登録 **{total['registered']}/30**・保有 {total['open']}・決済 {total['closed']}", '',
             '**現在は調査・価格検証段階。自動登録は停止中です。**', '',
             '正式判定は日本時間23:45。直前60秒以内の最終有効取引を翌0:01以降に照合します。', '',
             'Alpaca SIP認証付き取得は2026-10-27にAAPLで成功。取消・訂正前の取引は除外します。', '',
             f"今回の状態: `{daily.get('status', 'RESEARCH_PREPARED')}`", '',
             '未確定: 業績良好・決算回避期間・下落理由の合否・損切り/目標の算出・業績悪化撤退・休日の90日満了。', '',
             '勝率: '+('未集計' if total['win_rate_pct'] is None else f"{total['win_rate_pct']:.1f}%"),
             '期待値R: '+('未集計' if total['expectancy_r'] is None else f"{total['expectancy_r']:.3f}"), '',
             '## 今回取得した仮想基準価格の候補', '',
             '|銘柄|取得状態|価格|取引時刻|', '|---|---|---:|---|']
    for item in daily.get('prices', []):
        lines.append(f"|{item['symbol']}|{item['status']}|{item.get('price','—')}|{item.get('market_time','—')}|")
    if daily.get('price_diagnostics_deferred'):
        lines += ['', f"診断予算により未取得 {daily['price_diagnostics_deferred']}銘柄。見送り・不合格とは扱いません。"]
    lines += ['', '価格候補の取得成功はエントリー合格ではありません。未確認の仮想約定は作りません。', '',
              '実売買・X投稿・外部通知の機能はありません。']
    (out/'summary.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('prepare','reconcile'))
    args = parser.parse_args()
    now = datetime.now(JST)
    Path('output').mkdir(exist_ok=True)
    Path('input/research').mkdir(parents=True, exist_ok=True)
    Path('input/market_evidence').mkdir(parents=True, exist_ok=True)
    if args.phase == 'prepare':
        freeze_research(now)
    else:
        reconcile(now)
    run(now)
    phone_summary()


if __name__ == '__main__':
    main()
