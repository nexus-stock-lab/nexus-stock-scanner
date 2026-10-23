"""NEXUS v2.5: conservative candidate gate and explicit hypothetical-trade ledger.

Run after scanner.py and review.py. No real orders; no automatic entries.
To record a hypothetical trade, add a row to input/approved_entries.csv with
approval=APPROVED, an observed 23:45 JST quote and its source. The program
validates the plan before recording it. Persist input/trades.csv between runs;
GitHub Actions artifacts alone do not provide durable state.
"""
import csv
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

OUT = Path('output')
IN = Path('input')
IN.mkdir(exist_ok=True)
OUT.mkdir(exist_ok=True)
LEDGER = IN / 'trades.csv'
APPROVALS = IN / 'approved_entries.csv'
WATCH = OUT / 'entry_watchlist.csv'
REPORT = OUT / 'trade_report.json'
MAX_TRADES = 30
JST = ZoneInfo('Asia/Tokyo')
TRADE_FIELDS = ['trade_id','symbol','signal','entry_time_jst','entry_price','quote_source',
                'stop_price','target_price','rr_at_entry','deadline_jst','status',
                'exit_time_jst','exit_price','exit_reason','return_pct','r_multiple']
APPROVAL_FIELDS = ['approval','symbol','signal','entry_time_jst','entry_price','quote_source',
                   'stop_price','target_price','fundamentals_checked','earnings_checked',
                   'decline_cause_checked','notes']


def finite_positive(value):
    try:
        n = float(value)
        return n if math.isfinite(n) and n > 0 else None
    except (TypeError, ValueError):
        return None


def read_csv(path, fields):
    if not path.exists():
        with path.open('w', newline='', encoding='utf-8-sig') as f:
            csv.DictWriter(f, fieldnames=fields).writeheader()
        return pd.DataFrame(columns=fields)
    df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding='utf-8-sig')
    missing = set(fields) - set(df.columns)
    if missing:
        raise ValueError(f'{path}: missing columns: {sorted(missing)}')
    return df


def write_csv(df, path, fields):
    df.reindex(columns=fields).to_csv(path, index=False, encoding='utf-8-sig')


def watchlist():
    path = OUT / 'secondary_review.csv'
    if not path.exists():
        raise FileNotFoundError('Run scanner.py and review.py first')
    df = pd.read_csv(path, encoding='utf-8-sig')
    if df.empty:
        df.to_csv(WATCH, index=False, encoding='utf-8-sig')
        return df
    # A shortlist is NOT permission to enter. Financial and news checks stay mandatory.
    df['candidate_gate'] = df.apply(lambda r:
        '要再確認' if str(r.get('secondary_status', '')) != '二次調査候補'
        else 'R:R再検討' if str(r.get('plan_status', '')) != 'R:R暫定合格（要チャート確認）'
        else '23:45価格・決算・下落理由の確認待ち', axis=1)
    df['hypothetical_entry_approved'] = False
    df.to_csv(WATCH, index=False, encoding='utf-8-sig')
    return df


def approved_trades(approvals, existing, watch):
    accepted, rejected = [], []
    existing_ids = set(existing['trade_id']) if not existing.empty else set()
    existing_symbols = set(existing.loc[existing['status'] == 'OPEN', 'symbol']) if not existing.empty else set()
    eligible = set(watch.loc[watch.get('secondary_status', pd.Series(dtype=str)) == '二次調査候補', 'symbol']) if not watch.empty else set()
    for _, row in approvals.iterrows():
        r = row.to_dict()
        if r['approval'] != 'APPROVED':
            continue
        symbol = r['symbol'].strip().upper()
        reason = None
        try:
            when = datetime.fromisoformat(r['entry_time_jst'])
            if when.tzinfo is None or when.utcoffset() != timedelta(hours=9):
                raise ValueError('timezone must be +09:00')
            if (when.hour, when.minute) != (23, 45):
                raise ValueError('must be 23:45 JST')
            if when > datetime.now(JST) + timedelta(minutes=5):
                raise ValueError('future entry')
        except ValueError as e:
        reason = '日時が不正'
        entry, stop, target = [finite_positive(r[k]) for k in ('entry_price','stop_price','target_price')]
        if not reason and (entry is None or stop is None or target is None or not stop < entry < target):
        reason = '価格または損切り・利確の順序が不正'
        rr = (target - entry) / (entry - stop) if entry and stop and target and stop < entry < target else 0
        if not reason and rr < 1.5:
        reason = 'R:Rが1.5未満'
        if not reason and (not r['quote_source'].strip() or r['quote_source'].strip().lower() in ('yahoo daily','daily close')):
        reason = '23:45時点の価格出典がない'
        if not reason and symbol not in eligible:
        reason = '二次調査候補に存在しない'
        if not reason and r['signal'] not in ('反発確認', '反発初動'):
        reason = '判定区分が不正'
        if not reason and any(r[k] != 'YES' for k in ('fundamentals_checked','earnings_checked','decline_cause_checked')):
        reason = '財務・決算・下落理由の確認が未完了'
        trade_id = f'{symbol}-{r["entry_time_jst"]}'
        if not reason and (trade_id in existing_ids or symbol in existing_symbols):
        reason = '重複登録または同一銘柄保有中'
        if not reason and len(existing) + len(accepted) >= MAX_TRADES:
        reason = '30トレードの上限'
        if reason:
            rejected.append({'symbol':symbol,'reason':reason})
            continue
        accepted.append(dict(trade_id=trade_id,symbol=symbol,signal=r['signal'],
                             entry_time_jst=r['entry_time_jst'],entry_price=entry,
                             quote_source=r['quote_source'],stop_price=stop,target_price=target,
                             rr_at_entry=round(rr,3),deadline_jst=(when + timedelta(days=90)).isoformat(),
                             status='OPEN',exit_time_jst='',exit_price='',exit_reason='',return_pct='',r_multiple=''))
        existing_ids.add(trade_id)
        existing_symbols.add(symbol)
    return accepted, rejected


def summary(ledger, rejected, watch):
    closed = ledger[ledger['status'] == 'CLOSED'].copy() if not ledger.empty else ledger
    vals = pd.to_numeric(closed['r_multiple'], errors='coerce').dropna() if not closed.empty else pd.Series(dtype=float)
    now = datetime.now(JST)
    overdue = 0
    if not ledger.empty:
        for _, row in ledger.loc[ledger['status'] == 'OPEN'].iterrows():
            try:
                overdue += datetime.fromisoformat(row['deadline_jst']) <= now
            except ValueError:
                overdue += 1
    return {'version':'2.5', 'generated_jst':now.isoformat(),
            'watchlist':len(watch), 'recorded_hypothetical_trades':len(ledger),
            'open_trades':int((ledger['status']=='OPEN').sum()) if not ledger.empty else 0,
            'closed_trades':len(closed), 'overdue_90day_review':overdue,
            'wins':int((vals>0).sum()), 'losses':int((vals<0).sum()),
            'win_rate_pct':round(100*(vals>0).sum()/len(vals),2) if len(vals) else None,
            'expectancy_r':round(float(vals.mean()),3) if len(vals) else None,
            'rejected_approval_rows':rejected,
            'important':'No orders. No automatic fills. Trades require explicitly approved 23:45 JST price, source, and checks. Close positions must be recorded explicitly. Persist input/trades.csv across GitHub runs.'}


def main():
    watch = watchlist()
    approvals = read_csv(APPROVALS, APPROVAL_FIELDS)
    ledger = read_csv(LEDGER, TRADE_FIELDS)
    accepted, rejected = approved_trades(approvals, ledger, watch)
    if accepted:
        ledger = pd.concat([ledger, pd.DataFrame(accepted)], ignore_index=True)
        write_csv(ledger, LEDGER, TRADE_FIELDS)
    write_csv(ledger, OUT / 'trade_log.csv', TRADE_FIELDS)
    report = summary(ledger, rejected, watch)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
