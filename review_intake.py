"""Intake for explicitly confirmed, source-backed qualitative reviews.

This is a repository-authored review interface, NOT a Gemini promotion path.
It verifies provenance and binding, not the truth of a reviewer's reasoning.
No orders, ledger updates, network access or automatic semantic clearance.
"""
import json
import re
from pathlib import Path

from historical_probe import timestamp_ns
from official_data import verify_review_documents
from paper_engine import digest
from strategy_rules import VERSION, checked

NAMES=('guidance_not_lowered','decline_explanation_verified',
       'no_material_business_impairment','next_earnings')


def unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('review packet duplicate JSON key')
        result[key]=value
    return result


def load_confirmed_review(symbol,financials,at,now,root=Path('input')):
    """Return None if no packet exists; reject invalid packets without fallback.

    Writing CONFIRMED_SOURCE_REVIEW is an explicit semantic attestation by the
    repository author. Raw or merely quote-validated AI output is not accepted.
    A caller must still run build_plan for the numeric, calendar and price rules.
    """
    if not isinstance(symbol,str) or not re.fullmatch(r'[A-Z][A-Z0-9.]{0,14}',symbol):
        raise ValueError('review packet symbol invalid')
    path=root/'review_inbox'/at.date().isoformat()/(symbol+'.json')
    if not path.exists():return None
    packet=json.loads(path.read_text(encoding='utf-8'),object_pairs_hook=unique_object)
    required={'kind','symbol','rules_version','decision_time_jst',
              'financial_evidence_sha256','reviewed_at','reviewer_id','assertions'}
    if not isinstance(packet,dict) or set(packet)!=required:
        raise ValueError('review packet schema invalid')
    if packet['kind']!='CONFIRMED_SOURCE_REVIEW':
        raise ValueError('review packet needs explicit semantic confirmation')
    if packet['symbol']!=symbol or financials.get('symbol')!=symbol:
        raise ValueError('review packet issuer mismatch')
    if packet['rules_version']!=VERSION or packet['decision_time_jst']!=at.isoformat():
        raise ValueError('review packet decision or rules mismatch')
    if (not re.fullmatch(r'[0-9a-f]{64}',str(packet['financial_evidence_sha256'])) or
            packet['financial_evidence_sha256']!=financials.get('evidence_sha256')):
        raise ValueError('review packet does not cover current financial evidence')
    if not isinstance(packet['reviewer_id'],str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',packet['reviewer_id']):
        raise ValueError('review packet needs opaque reviewer identifier')
    if not timestamp_ns(packet['reviewed_at'])<=timestamp_ns(now.isoformat())<=timestamp_ns(at.isoformat()):
        raise ValueError('review packet time invalid')
    assertions=packet['assertions']
    if not isinstance(assertions,dict) or set(assertions)!=set(NAMES):
        raise ValueError('review packet requires all four assertions')
    for name in NAMES:
        item=assertions[name]
        if not isinstance(item,dict) or item.get('checked_at')!=packet['reviewed_at']:
            raise ValueError('review packet assertion review time mismatch')
        checked(item,at)
        if name=='next_earnings' and not isinstance(item.get('date'),str):
            raise ValueError('review packet earnings date missing')
    review={'symbol':symbol,'rules_version':VERSION,
            'financial_evidence_sha256':packet['financial_evidence_sha256'],
            **assertions,'review_packet_sha256':digest(packet),
            'reviewer_id':packet['reviewer_id'],'reviewed_at':packet['reviewed_at']}
    verify_review_documents(review,root,financials=financials)
    return review
