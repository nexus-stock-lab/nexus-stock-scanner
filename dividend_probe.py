"""Read-only declared cash-dividend benchmark. No account, order or ledger API."""
import json
import os
from datetime import datetime,timezone
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError

from historical_probe import decision
from market_provider import corporate_state
from corporate_actions import prepare_corporate_history,validate_corporate_history

ENTRY=decision('2025-02-07T23:45:00+09:00')
END=decision('2025-02-14T23:45:00+09:00')
BENCHMARK_SOURCE='https://www.apple.com/newsroom/2025/01/apple-reports-first-quarter-results/'
SAFE_REASONS={
    'ordinary domestic USD cash dividend not verified':'ORDINARY_DOMESTIC_USD_NOT_VERIFIED',
    'cash dividend dates invalid':'DIVIDEND_DATES_INVALID',
    'same-day distribution price basis needs reconciliation':'AMBIGUOUS_DISTRIBUTION_BASIS',
    'dividend benchmark not confirmed':'DECLARATION_BENCHMARK_MISMATCH',
}


def check(root,key,secret,fetch_corporate=corporate_state,clock=None):
    clock=clock or (lambda:datetime.now(timezone.utc));root=Path(root);root.mkdir(parents=True,exist_ok=True)
    result={'mode':'READ_ONLY_DECLARED_DIVIDEND','status':'HELD','paper_fills_created':0,
            'may_register':False,'payment_verified':False,'benchmark_source':BENCHMARK_SOURCE}
    try:
        ca=fetch_corporate('AAPL',ENTRY,END,key,secret)
        path=root/'corporate.json';path.write_text(json.dumps(ca),encoding='utf-8')
        result['observed_actions']=[{k:a.get(k) for k in (
            'kind','symbol','id','currency','rate','ex_date','record_date','payable_date',
            'special','foreign','sub_type','due_bill_on_date','due_bill_off_date')}
            for a in ca.get('actions',[])[:20]]
        proof=prepare_corporate_history(ca,'AAPL',ENTRY,END,clock(),str(path))
        if proof is None or proof['events'] or len(proof.get('cash_dividends',[]))!=1:
            raise ValueError('dividend benchmark not confirmed')
        validate_corporate_history(proof,{'symbol':'AAPL','entry_time_jst':ENTRY.isoformat()},END.isoformat(),clock())
        event=proof['cash_dividends'][0]
        if (event['ex_date']!='2025-02-10' or event['record_date']!='2025-02-10' or
                event['payable_date']!='2025-02-13' or Decimal(event['rate'])!=Decimal('0.25')):
            raise ValueError('dividend benchmark not confirmed')
        (root/'corporate_history.json').write_text(json.dumps(proof),encoding='utf-8')
        result.update(status='VERIFIED_DECLARED_USD_DIVIDEND',declaration=event,proof_sha256=proof['sha256'])
    except Exception as exc:
        result['reason']='HTTP_'+str(exc.code) if isinstance(exc,HTTPError) else SAFE_REASONS.get(str(exc),'DIVIDEND_EVIDENCE_UNVERIFIED')
    (root/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result


def main():
    result=check(Path('output/dividend_probe'),os.getenv('ALPACA_DATA_KEY',''),os.getenv('ALPACA_DATA_SECRET',''))
    text=json.dumps(result,ensure_ascii=False,indent=2)
    if os.getenv('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a') as f:
            f.write('\n## 通常配当の公表データ診断（入金未確認・登録なし）\n\n```json\n'+text+'\n```\n')
    print(text)
    if result['status']!='VERIFIED_DECLARED_USD_DIVIDEND':raise SystemExit(1)


if __name__=='__main__':main()
