"""Read-only SEC numeric acceptance check, independent of Gemini and Alpaca.

Only public evidence is acquired. No plans, orders or ledger writes.
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError

from official_data import collect_financials, public_json


# Only known, constant diagnostics may enter output; arbitrary exception text
# can contain headers, query strings or credentials and must never be echoed.
REASONS = {
    'SEC ticker mapping unavailable or ambiguous': 'SEC_TICKER_AMBIGUOUS',
    'SEC entity mismatch': 'SEC_ENTITY_MISMATCH',
    'no accepted quarterly US-GAAP report': 'US_GAAP_QUARTER_UNAVAILABLE',
    'newer official results/amendment require financial reconciliation': 'NEW_RESULTS_NEED_RECONCILIATION',
    'conflicting reported quarter values; review restatement': 'COMPARABLE_QUARTER_CONFLICT',
    'latest report lacks unambiguous comparable quarterly revenue/operating income': 'COMPARABLE_QUARTER_UNAVAILABLE',
    'positive comparable revenue required': 'COMPARABLE_REVENUE_NONPOSITIVE',
    'SEC contact not configured: save app name and contact email as NEXUS_SEC_USER_AGENT Secret': 'SEC_CONTACT_MISSING',
}


def safe_reason(exc):
    if isinstance(exc, HTTPError):
        return 'SEC_HTTP_' + str(int(exc.code))
    return REASONS.get(str(exc), 'SEC_DATA_UNVERIFIED')


def check(symbols, root=Path('input'), fetch=public_json,
          collect=collect_financials, clock=lambda: datetime.now(timezone.utc)):
    symbols=list(dict.fromkeys(symbols))
    if not 1 <= len(symbols) <= 20 or any(
            not s or len(s)>16 or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-' for c in s)
            for s in symbols):
        raise ValueError('provide 1..20 uppercase symbols')
    started=clock()
    report={'mode':'READ_ONLY_FINANCIAL_CHECK','paper_fills_created':0,
            'may_register':False,'checked_at':started.isoformat(),'results':[]}
    try:
        mapping=fetch('https://www.sec.gov/files/company_tickers.json')
    except Exception as exc:
        report.update(status='HELD',reason=safe_reason(exc))
        return report
    for symbol in symbols:
        try:
            now=clock()
            f=collect(symbol,now,now,root,fetch=fetch,ticker_map=mapping)
            report['results'].append({
                'symbol':symbol,'status':'VERIFIED_QUARTERLY_NUMBERS',
                'financials':{k:f[k] for k in (
                    'period_end','prior_period_end','revenue','prior_revenue',
                    'operating_income','prior_operating_income','quality',
                    'evidence_sha256','submissions_sha256','source','cik',
                    'comparison_basis','comparison_accessions') if k in f},
                'formal_review_status':'NOT_EVALUATED',
                'may_register':False})
        except Exception as exc:
            report['results'].append({'symbol':symbol,'status':'HELD',
                                      'reason':safe_reason(exc),'may_register':False})
    report['status']='NUMBERS_VERIFIED' if all(
        r['status']=='VERIFIED_QUARTERLY_NUMBERS' for r in report['results']) else 'HELD'
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--symbols',default='AAPL,APLD,IREN,NBIS')
    args=parser.parse_args()
    report=check(args.symbols.split(','))
    out=Path('output');out.mkdir(exist_ok=True)
    text=json.dumps(report,ensure_ascii=False,indent=2)
    (out/'financial_probe.json').write_text(text,encoding='utf-8')
    (out/'summary.md').write_text(
        '# SEC 四半期数値の実取得確認\n\n'
        '仮想登録・注文・通知は行いません。数値の取得成功と業績合格・正式判定は別です。\n\n'
        '```json\n'+text+'\n```\n',encoding='utf-8')
    print(text)
    if report['status']=='HELD':raise SystemExit(1)


if __name__=='__main__':main()
