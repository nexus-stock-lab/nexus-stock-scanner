"""Explicit read-only Gemini smoke test. Does not call the paper ledger engine."""
import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path

from market_calendar import JST, next_decision
from official_data import collect_financials, collect_company_documents
from gemini_review import public_bundle, review_one, connectivity_check, ReviewBlocked, write_json


def main(symbol="AAPL",include_ir=False):
    root,out=Path('input'),Path('output');out.mkdir(exist_ok=True)
    now=datetime.now(JST)
    result={'mode':'SHADOW_ONLY','paper_fills_created':0,'may_register':False}
    if not os.getenv('GEMINI_API_KEY'):
        result['status']='GEMINI_KEY_MISSING'
    elif os.getenv('NEXUS_GEMINI_FREE_TIER_CONFIRMED')!='true':
        result['status']='FREE_TIER_NOT_CONFIRMED'
    else:
        try:
            if not include_ir:
                result['stage']='CONNECTIVITY_ONLY'
                result['connection']=connectivity_check(now,root,os.environ['GEMINI_API_KEY'],True)
                # Same project budget includes both calls, with at least seven seconds between them.
                time.sleep(8)
            else:
                result['connection']={'status':'NOT_SEPARATELY_PROBED',
                                      'reason':'The SEC+IR generation request itself must return a validated response.'}
            result['stage']='PUBLIC_DOCUMENT_REVIEW'
            now=datetime.now(JST)
            if include_ir:
                financials=collect_company_documents(symbol,now,now,root)
                result['financial_status']='NOT_EVALUATED_SOURCE_REVIEW_ONLY'
            else:
                financials=collect_financials(symbol,now,now,root)
            observed=datetime.now(JST)
            ir_documents=[]
            if include_ir:
                from issuer_ir import collect_issuer_ir
                ir=collect_issuer_ir(symbol,next_decision(observed),observed,root)
                result['ir_collection']={'documents':len(ir['documents']),'audit':ir['audit']}
                ir_documents=ir['documents']
                observed=datetime.now(JST)
            bundle=public_bundle(symbol,financials,next_decision(observed),observed,root,ir_documents)
            value=review_one(bundle,observed,root,os.environ['GEMINI_API_KEY'],True)
            result.update(status='VALIDATED_SHADOW_RESPONSE_RECEIVED',symbol=symbol,
                document_count=len(bundle['documents']),request_sha256=value['request_sha256'],
                verdicts={a['name']:a['verdict'] for a in value['assessment']['assertions']},
                semantic_verified=False)
        except Exception as exc:
            result.update(status='HELD',reason=str(exc) if isinstance(exc,ReviewBlocked) else type(exc).__name__)
            if isinstance(exc,ReviewBlocked) and hasattr(exc,'rejection_evidence_sha256'):
                result['rejection_evidence_sha256']=exc.rejection_evidence_sha256
            if isinstance(exc,ReviewBlocked) and isinstance(getattr(exc,'context_bytes',None),int):
                result['source_context_bytes']=exc.context_bytes
    write_json(out/'gemini_probe.json',result)
    summary='# Gemini 公開資料の検証テスト\n\n'
    summary+='仮想登録・決済は行いません。引用一致は内容判断の正しさを保証しません。\n\n```json\n'
    summary+=json.dumps(result,ensure_ascii=False,indent=2)+'\n```\n'
    (out/'summary.md').write_text(summary,encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
    if result['status']=='HELD':raise SystemExit(1)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--symbol',default='AAPL')
    parser.add_argument('--include-ir',action='store_true')
    args=parser.parse_args()
    main(args.symbol,args.include_ir)
