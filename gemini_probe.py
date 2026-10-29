"""Explicit read-only Gemini smoke test. Does not call the paper ledger engine."""
import json
import os
import time
from datetime import datetime
from pathlib import Path

from market_calendar import JST, next_decision
from official_data import collect_financials
from gemini_review import public_bundle, review_one, connectivity_check, ReviewBlocked, write_json


def main():
    root,out=Path('input'),Path('output');out.mkdir(exist_ok=True)
    now=datetime.now(JST)
    result={'mode':'SHADOW_ONLY','paper_fills_created':0,'may_register':False}
    if not os.getenv('GEMINI_API_KEY'):
        result['status']='GEMINI_KEY_MISSING'
    elif os.getenv('NEXUS_GEMINI_FREE_TIER_CONFIRMED')!='true':
        result['status']='FREE_TIER_NOT_CONFIRMED'
    else:
        try:
            result['stage']='CONNECTIVITY_ONLY'
            result['connection']=connectivity_check(now,root,os.environ['GEMINI_API_KEY'],True)
            # Same project budget includes both calls, with at least seven seconds between them.
            time.sleep(8)
            result['stage']='PUBLIC_DOCUMENT_REVIEW'
            now=datetime.now(JST)
            financials=collect_financials('AAPL',now,now,root)
            observed=datetime.now(JST)
            bundle=public_bundle('AAPL',financials,next_decision(observed),observed,root)
            value=review_one(bundle,observed,root,os.environ['GEMINI_API_KEY'],True)
            result.update(status='VALIDATED_SHADOW_RESPONSE_RECEIVED',symbol='AAPL',
                document_count=len(bundle['documents']),request_sha256=value['request_sha256'],
                verdicts={a['name']:a['verdict'] for a in value['assessment']['assertions']},
                semantic_verified=False)
        except Exception as exc:
            result.update(status='HELD',reason=str(exc) if isinstance(exc,ReviewBlocked) else type(exc).__name__)
    write_json(out/'gemini_probe.json',result)
    summary='# Gemini 公開資料の検証テスト\n\n'
    summary+='仮想登録・決済は行いません。引用一致は内容判断の正しさを保証しません。\n\n```json\n'
    summary+=json.dumps(result,ensure_ascii=False,indent=2)+'\n```\n'
    (out/'summary.md').write_text(summary,encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
    if result['status']=='HELD':raise SystemExit(1)


if __name__=='__main__':main()
