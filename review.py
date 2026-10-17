"""NEXUS secondary research triage. No orders or hypothetical fills."""
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

OUT = Path('output')


def review(row):
    reasons = []
    missing = ['売上成長率', '財務健全性', '直近決算材料', '下落理由', '最新の板・約定価格']
    try:
        price = float(row['close'])
        turnover = float(row['avg_dollar_volume_20'])
        volume = float(row['volume_ratio'])
        rebound = float(row['rebound_pct'])
        drawdown = float(row['drawdown_pct'])
        date = str(row['price_date'])
        if price < 2:
            reasons.append('株価2ドル未満')
        if turnover < 2_000_000:
            reasons.append('20日平均売買代金200万ドル未満')
        if volume < 1.2:
            reasons.append('出来高倍率1.2倍未満')
        if rebound > 45 or drawdown > -18:
            reasons.append('底値圏条件から逸脱')
        if date != datetime.now(ZoneInfo('America/New_York')).date().isoformat():
            # This is informational, not a disqualification: weekend/holidays and after-hours exist.
            missing.append('株価データ日付と米国現地日の一致確認')
    except (TypeError, ValueError, KeyError):
        reasons.append('価格または流動性データ不正')
    return pd.Series({
        'secondary_status': '要確認（注意あり）' if reasons else '二次調査候補',
        'risk_flags': '、'.join(reasons),
        'unverified': '、'.join(missing),
        'fundamentals_status': '未確認',
        'earnings_status': '未確認',
        'decline_cause_status': '未確認',
        'entry_status': '未判定',
        'entry_price': '', 'stop_price': '', 'take_profit_price': '', 'risk_reward': '',
        'virtual_fill': False,
    })


def main():
    source = OUT / 'candidates.csv'
    if not source.exists():
        raise FileNotFoundError('Run scanner.py first: output/candidates.csv is missing')
    df = pd.read_csv(source)
    if df.empty:
        df = df.copy()
        for col in ['secondary_status', 'risk_flags', 'unverified', 'fundamentals_status',
                    'earnings_status', 'decline_cause_status', 'entry_status',
                    'entry_price', 'stop_price', 'take_profit_price', 'risk_reward', 'virtual_fill']:
            df[col] = pd.Series(dtype='object')
    else:
        df = pd.concat([df.drop(columns=['virtual_fill', 'entry_status'], errors='ignore'),
                        df.apply(review, axis=1)], axis=1)
    df.to_csv(OUT / 'secondary_review.csv', index=False, encoding='utf-8-sig')
    top = df.sort_values('priority', ascending=False).head(30) if not df.empty else df
    top.to_csv(OUT / 'review_top30.csv', index=False, encoding='utf-8-sig')
    now = datetime.now(timezone.utc)
    jst = now.astimezone(ZoneInfo('Asia/Tokyo'))
    ny = now.astimezone(ZoneInfo('America/New_York'))
    report = {
        'generated_utc': now.isoformat(),
        'generated_jst': jst.isoformat(),
        'reference_jst': '23:45 (this is NOT a verified 23:45 price snapshot)',
        'us_open_offset_minutes_at_2345_jst': 75 if ny.dst().total_seconds() else 15,
        'secondary_reviewed': len(df),
        'secondary_research_candidates': int((df['secondary_status'] == '二次調査候補').sum()),
        'caution': int((df['secondary_status'] == '要確認（注意あり）').sum()),
        'confirmed_signal': int((df['signal'] == '反発確認').sum()) if 'signal' in df else 0,
        'early_signal': int((df['signal'] == '反発初動').sum()) if 'signal' in df else 0,
        'hypothetical_entries': 0,
        'note': 'Price-only triage. Financials, news, intraday 23:45 snapshot, stop/target and R:R remain unverified. Do not treat candidates as approved trades.'
    }
    (OUT / 'secondary_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
