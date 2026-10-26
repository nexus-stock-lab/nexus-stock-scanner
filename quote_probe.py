"""Diagnostic only: archives one-minute bars; NEVER creates verified_evidence.json."""
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def main():
    import yfinance as yf
    now = datetime.now(ZoneInfo('Asia/Tokyo'))
    out = Path('output'); out.mkdir(exist_ok=True)
    report={'retrieved_at':now.isoformat(), 'source':'Yahoo via yfinance', 'interval':'1m',
            'exact_2345_trade_verified':False,
            'reason':'Minute OHLC does not identify the transaction price at exactly 23:45:00.'}
    try:
        bars=yf.Ticker('AAPL').history(period='5d',interval='1m',auto_adjust=False,prepost=False)
        if bars.index.tz is None or bars.empty:
            raise ValueError('empty bars or missing timezone')
        bars.index=bars.index.tz_convert('Asia/Tokyo')
        bars.to_csv(out/'quote_probe_AAPL_1m.csv')
        selected=bars[(bars.index.hour==23)&(bars.index.minute==45)]
        report.update(rows=len(bars), bars_labeled_2345=len(selected), latest_bar=bars.index[-1].isoformat())
    except Exception as exc:
        report['fetch_error']=str(exc)[:300]
    (out/'quote_probe.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':
    main()
