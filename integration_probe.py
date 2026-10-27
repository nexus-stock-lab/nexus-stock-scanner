"""Read-only real-data integration diagnostic. Never writes a paper trade."""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from historical_probe import probe, decision
from market_provider import verified_reference, corporate_state, collect_interval, normalized_stream
from official_data import collect_financials


def main():
    at=decision('2026-10-26T23:45:00+09:00')
    now=datetime.now(timezone.utc)
    key,secret=os.getenv('ALPACA_DATA_KEY',''),os.getenv('ALPACA_DATA_SECRET','')
    out=Path('output/integration');out.mkdir(parents=True,exist_ok=True)
    result={'paper_only':True,'paper_fills_created':0,'decision':at.isoformat()}
    try:
        history=probe('AAPL',at,now,key,secret)
        history['retrieved_at']=datetime.now(timezone.utc).isoformat()
        (out/'history.json').write_text(json.dumps(history))
        q=verified_reference(history,history['retrieved_at'],'output/integration/history.json')
        result['reference']={k:q[k] for k in ('symbol','price','market_time','reference_age_seconds','provider_verified')}
        ca=corporate_state('AAPL',at,at+timedelta(seconds=5),key,secret)
        (out/'corporate.json').write_text(json.dumps(ca))
        result['corporate_actions_clear_asof']=ca['clear']
        raw=collect_interval('AAPL',at,at+timedelta(seconds=5),now,key,secret)
        (out/'interval.json').write_text(json.dumps(raw))
        stream=normalized_stream(raw,ca,'output/integration/interval.json')
        result['exit_history']={'complete':stream['complete'],'eligible_records':len(stream['trades']),
                                'corporate_actions_clear':stream['corporate_actions_clear']}
        result['market_data_status']='VERIFIED_READ_ONLY'
    except Exception as exc:
        result['market_data_status']='BLOCKED'
        result['market_data_error']=str(exc) if isinstance(exc,ValueError) else type(exc).__name__
    try:
        f=collect_financials('AAPL',now,now,out)
        result['sec_status']='VERIFIED_QUARTERLY_NUMBERS'
        result['sec_financials']={k:f[k] for k in ('symbol','period_end','prior_period_end','quality')}
        result['official_documents_archived']=sum(d['status']=='ARCHIVED_NOT_REVIEWED' for d in f.get('official_documents',[]))
    except Exception as exc:
        result['sec_status']='BLOCKED'
        result['sec_error']=str(exc) if isinstance(exc,ValueError) else type(exc).__name__
    result['qualitative_review_status']='No automatic PASS; official guidance/decline assertions must be supported by archived sources'
    (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    summary='## Read-only NEXUS integration\n\n```json\n'+json.dumps(result,ensure_ascii=False,indent=2)+'\n```\n'
    if os.getenv('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a') as f:f.write(summary)
    print(json.dumps(result,ensure_ascii=False))
    if result['market_data_status']!='VERIFIED_READ_ONLY':
        raise SystemExit('Market-data integration not confirmed')

if __name__=='__main__':main()
