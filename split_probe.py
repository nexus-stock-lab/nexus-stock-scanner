"""Read-only AAPL 2020 split benchmark. Does not invoke the paper ledger."""
import json
import os
from datetime import datetime,timedelta,timezone
from pathlib import Path
from urllib.error import HTTPError

from historical_probe import decision,timestamp_ns
from market_provider import corporate_state,collect_interval,normalized_stream
from corporate_actions import prepare_split_history,validate_split_history,split_factor

ENTRY=decision('2020-08-28T23:45:00+09:00')
AFTER=decision('2020-08-31T23:45:00+09:00')
BENCHMARK_SOURCE='https://www.sec.gov/Archives/edgar/data/320193/000032019320000060/a8-kexhibit991q3202062.htm'
SAFE_REASONS={
    'benchmark split not confirmed':'BENCHMARK_SPLIT_NOT_CONFIRMED',
    'benchmark trade not confirmed':'BENCHMARK_TRADE_NOT_CONFIRMED',
    'split identity or due-bill terms unresolved':'SPLIT_IDENTITY_OR_DUE_BILL_UNRESOLVED',
    'complete provider corporate evidence required':'CORPORATE_EVIDENCE_INCOMPLETE',
    'non-split corporate action needs reconciliation':'NON_SPLIT_ACTION_PRESENT',
    'entry-day or out-of-range split needs reconciliation':'SPLIT_DATE_UNRESOLVED',
    'corporate action selection mismatch':'CORPORATE_SELECTION_MISMATCH',
    'corporate retrieval time invalid':'CORPORATE_ACQUISITION_TIME_INVALID',
    'benchmark acquisition time invalid':'TRADE_ACQUISITION_TIME_INVALID',
}


def check(root,key,secret,fetch_corporate=corporate_state,fetch_interval=collect_interval,clock=None):
    clock=clock or (lambda:datetime.now(timezone.utc))
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    result={'mode':'READ_ONLY_SPLIT_BASIS','paper_fills_created':0,'may_register':False,
            'benchmark_source':BENCHMARK_SOURCE,'status':'HELD'}
    try:
        end=AFTER+timedelta(minutes=1)
        result['stage']='CORPORATE_FETCH'
        ca=fetch_corporate('AAPL',ENTRY,end,key,secret)
        path=root/'corporate.json';path.write_text(json.dumps(ca),encoding='utf-8')
        result['stage']='CORPORATE_VERIFICATION'
        result['corporate_actions_observed']=len(ca.get('actions',[]))
        result['corporate_actions']=[{k:a.get(k) for k in (
            'kind','id','symbol','ex_date','old_rate','new_rate','due_bill_redemption_date')}
            for a in ca.get('actions',[])[:20]]
        proof=prepare_split_history(ca,'AAPL',ENTRY,end,clock(),str(path))
        if proof is None or len(proof['events'])!=1:
            raise ValueError('benchmark split not confirmed')
        validate_split_history(proof,{'symbol':'AAPL','entry_time_jst':ENTRY.isoformat()},end.isoformat(),clock())
        if (proof['events'][0]['ex_date']!='2020-08-31' or
                split_factor(proof,AFTER.isoformat())!=4):
            raise ValueError('benchmark split not confirmed')
        (root/'split_history.json').write_text(json.dumps(proof),encoding='utf-8')
        result['observations']=[]
        for label,at in (('before',ENTRY),('after',AFTER)):
            result['stage']=label.upper()+'_TRADE_FETCH'
            raw=fetch_interval('AAPL',at,at+timedelta(minutes=1),clock(),key,secret)
            result['stage']=label.upper()+'_TRADE_VERIFICATION'
            if not timestamp_ns((at+timedelta(minutes=1)).isoformat())<=timestamp_ns(raw['retrieved_at'])<=timestamp_ns(clock().isoformat()):
                raise ValueError('benchmark acquisition time invalid')
            path=root/(label+'.json');path.write_text(json.dumps(raw),encoding='utf-8')
            stream=normalized_stream(raw,ca,str(path))
            if not stream['trades']:raise ValueError('benchmark trade not confirmed')
            q=stream['trades'][0];factor=split_factor(proof,q['market_time'])
            result['observations'].append({'period':label,'market_time':q['market_time'],
                'observed_price':q['price'],'share_factor':factor,
                'equivalent_price':q['price']*factor,'eligible_records':len(stream['trades'])})
        result.update(status='VERIFIED_SPLIT_BASIS',stage='COMPLETE',split_history_sha256=proof['sha256'])
    except Exception as exc:
        result['reason']=('HTTP_'+str(exc.code) if isinstance(exc,HTTPError) else
                          SAFE_REASONS.get(str(exc),'SPLIT_OR_TRADE_EVIDENCE_UNVERIFIED'))
    (root/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result


def main():
    result=check(Path('output/split_probe'),os.getenv('ALPACA_DATA_KEY',''),os.getenv('ALPACA_DATA_SECRET',''))
    text=json.dumps(result,ensure_ascii=False,indent=2)
    if os.getenv('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a') as f:
            f.write('\n## 株式分割の実データ診断（登録なし）\n\n```json\n'+text+'\n```\n')
    print(text)
    if result['status']!='VERIFIED_SPLIT_BASIS':raise SystemExit(1)


if __name__=='__main__':main()
