"""NEXUS v2.4: cautious, reproducible secondary screening. No orders/fills."""
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

OUT = Path('output')
OUT.mkdir(exist_ok=True)
MIN_HISTORY = 240
MAX_FUNDAMENTALS = int(os.getenv('NEXUS_FUNDAMENTALS_LIMIT', '30'))
SPAC_PATTERN = re.compile(r'\b(?:acquisition|blank check|special purpose acquisition|SPAC)\b', re.I)


def normalized_signal(value):
    value = str(value)
    if value in ('反発確認', '反発初動'):
        return value
    # Recover UTF-8 Japanese text accidentally decoded as Latin-1 in scanner.py.
    try:
        repaired = value.encode('latin1').decode('utf-8')
        if repaired in ('反発確認', '反発初動'):
            return repaired
    except (UnicodeError, ValueError):
        pass
    return value


def numeric(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def preliminary(row):
    flags = []
    close = numeric(row.get('close'))
    turnover = numeric(row.get('avg_dollar_volume_20'))
    volume = numeric(row.get('volume_ratio'))
    rebound = numeric(row.get('rebound_pct'))
    drawdown = numeric(row.get('drawdown_pct'))
    history = numeric(row.get('history_days'))
    if history is None or history < MIN_HISTORY:
        flags.append('52週相当の株価履歴不足')
    if SPAC_PATTERN.search(str(row.get('name', ''))):
        flags.append('SPAC等の可能性（名称判定）')
    if close is None or close < 2:
        flags.append('株価2ドル未満または不明')
    if turnover is None or turnover < 2_000_000:
        flags.append('平均売買代金200万ドル未満または不明')
    if volume is None or volume < 1.2:
        flags.append('出来高倍率1.2倍未満または不明')
    if rebound is None or drawdown is None or rebound > 45 or drawdown > -18:
        flags.append('底値圏条件から逸脱または不明')
    return flags


def inspect_fundamentals(symbol):
    """Best-effort public data: missing values never count as passed."""
    result = {'revenue_growth_pct': None, 'operating_margin_pct': None,
              'debt_to_equity': None, 'earnings_date': '',
              'fundamentals_status': '未確認', 'fundamentals_notes': ''}
    try:
        ticker = yf.Ticker(str(symbol).replace('.', '-'))
        info = ticker.info or {}
        growth = numeric(info.get('revenueGrowth'))
        margin = numeric(info.get('operatingMargins'))
        debt = numeric(info.get('debtToEquity'))
        result['revenue_growth_pct'] = round(growth * 100, 2) if growth is not None else None
        result['operating_margin_pct'] = round(margin * 100, 2) if margin is not None else None
        result['debt_to_equity'] = round(debt, 2) if debt is not None else None
        if growth is None:
            result['fundamentals_notes'] = '売上成長率を取得できず'
        elif growth <= 0:
            result['fundamentals_notes'] = '直近売上成長率がプラスでない'
            result['fundamentals_status'] = '要注意'
        else:
            result['fundamentals_status'] = '暫定確認（売上成長プラス）'
        try:
            calendar = ticker.calendar
            if isinstance(calendar, dict):
                date = calendar.get('Earnings Date')
                if isinstance(date, (list, tuple)) and date:
                    date = date[0]
                if date is not None:
                    result['earnings_date'] = str(date)[:10]
        except Exception:
            pass
    except Exception as exc:
        result['fundamentals_notes'] = 'データ取得失敗: ' + str(exc)[:100]
    return result


def trade_plan(row):
    """Illustrative daily-bar plan only. NOT a 23:45 entry or fill."""
    entry = numeric(row.get('close'))
    low52 = numeric(row.get('low52'))
    high52 = numeric(row.get('high52'))
    ma20 = numeric(row.get('ma20'))
    if any(v is None for v in (entry, low52, high52, ma20)):
        return {'reference_entry': None, 'stop_price': None, 'take_profit_price': None,
                'risk_reward': None, 'plan_status': '算定不可'}
    # Conservative provisional stop using a reference MA and 52-week low.
    # This is NOT a technically validated swing-low stop; human review required.
    stop = max(low52, min(ma20, entry * 0.93))
    target = high52
    risk = entry - stop
    reward = target - entry
    if risk <= 0 or reward <= 0:
        return {'reference_entry': round(entry, 4), 'stop_price': None,
                'take_profit_price': None, 'risk_reward': None,
                'plan_status': '損益幅が不適切・再検討'}
    rr = reward / risk
    return {'reference_entry': round(entry, 4), 'stop_price': round(stop, 4),
            'take_profit_price': round(target, 4), 'risk_reward': round(rr, 2),
            'plan_status': 'R:R暫定合格（要チャート確認）' if rr >= 1.5 else 'R:R不足'}


def main():
    df = pd.read_csv(OUT / 'candidates.csv')
    if 'signal' in df:
        df['signal'] = df['signal'].map(normalized_signal)
    if df.empty:
        df.to_csv(OUT / 'secondary_review.csv', index=False, encoding='utf-8-sig')
        (OUT / 'secondary_report.json').write_text(json.dumps({'candidates': 0, 'hypothetical_entries': 0}, ensure_ascii=False), encoding='utf-8')
        return
    df = df.sort_values('priority', ascending=False).reset_index(drop=True)
    flags = df.apply(preliminary, axis=1)
    df['risk_flags'] = flags.map(lambda x: '、'.join(x))
    df['secondary_status'] = flags.map(lambda x: '要確認（注意あり）' if x else '二次調査候補')
    plans = pd.DataFrame(df.apply(trade_plan, axis=1).tolist())
    df = pd.concat([df, plans], axis=1)
    for col in ['revenue_growth_pct', 'operating_margin_pct', 'debt_to_equity', 'earnings_date',
                'fundamentals_status', 'fundamentals_notes']:
        df[col] = None if col.endswith('_pct') or col == 'debt_to_equity' else ''
    df['fundamentals_status'] = '未確認'
    # Avoid thousands of slow/rate-limited requests; investigate only top triage candidates.
    eligible_indices = df.index[df['secondary_status'] == '二次調査候補'][:MAX_FUNDAMENTALS]
    for i in eligible_indices:
        details = inspect_fundamentals(df.at[i, 'symbol'])
        for key, value in details.items():
            df.at[i, key] = value
        time.sleep(0.3)
    df['decline_cause_status'] = '未確認（ニュース調査が必要）'
    df['earnings_status'] = df['earnings_date'].map(lambda d: '日付候補取得・要公式確認' if d else '未確認')
    df['entry_status'] = '未判定（23:45時点の価格・ニュース・決算確認待ち）'
    df['virtual_fill'] = False
    df['review_rank_group'] = (df['secondary_status'] != '二次調査候補').astype(int)
    df = df.sort_values(['review_rank_group', 'priority'], ascending=[True, False])
    df.to_csv(OUT / 'secondary_review.csv', index=False, encoding='utf-8-sig')
    df.head(30).to_csv(OUT / 'review_top30.csv', index=False, encoding='utf-8-sig')
    report = {
        'version': '2.4',
        'generated_jst': datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Tokyo')).isoformat(),
        'secondary_reviewed': len(df),
        'secondary_research_candidates': int((df['secondary_status'] == '二次調査候補').sum()),
        'caution': int((df['secondary_status'] != '二次調査候補').sum()),
        'fundamentals_attempted': len(eligible_indices),
        'rr_provisional_pass': int((df['plan_status'] == 'R:R暫定合格（要チャート確認）').sum()),
        'hypothetical_entries': 0,
        'warning': 'Reference prices are prior-session closes. No verified 23:45 quote, news verification, validated stops, or hypothetical fills. Fundamentals may be delayed or missing.'
    }
    (OUT / 'secondary_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
