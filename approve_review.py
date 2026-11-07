"""Explicit human approval of an immutable source-backed review draft."""
import json
import os
import re
from datetime import datetime
from pathlib import Path
from historical_probe import decision, timestamp_ns
from market_calendar import JST
from paper_engine import digest
from entry_schedule import session_date,session_review_covers
from review_intake import load_confirmed_review, NAMES


def approve(review_id, actor, now, root=Path('input')):
    if not re.fullmatch(r'[0-9a-f]{64}',review_id):raise ValueError('invalid review ID')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',actor):raise ValueError('invalid approver')
    path=root/'review_drafts'/(review_id+'.json')
    draft=json.loads(path.read_text(encoding='utf-8'))
    if digest(draft)!=review_id:raise ValueError('draft changed; approval rejected')
    if draft.get('kind')!='SOURCE_REVIEW_DRAFT':raise ValueError('not a review draft')
    packet=draft['packet'];at=decision(packet['decision_time_jst'])
    scoped=packet.get('approval_scope')=='US_REGULAR_SESSION'
    if packet.get('approval_scope') and not (scoped and session_review_covers(packet,at)):
        raise ValueError('invalid session approval scope or expiry')
    if timestamp_ns(now.isoformat())>timestamp_ns(at.isoformat()):raise ValueError('decision passed; no retrospective approval')
    if not re.fullmatch(r'[A-Z][A-Z0-9.]{0,14}',packet['symbol']):raise ValueError('invalid symbol')
    day=session_date(at) if scoped else at.date().isoformat()
    research=json.loads((root/'research'/(day+'.json')).read_text())
    if research.get('snapshot_sha256')!=digest({k:v for k,v in research.items() if k!='snapshot_sha256'}):
        raise ValueError('research integrity failed')
    financials=research['financials'][packet['symbol']]
    packet={**packet,'kind':'CONFIRMED_SOURCE_REVIEW','reviewer_id':actor,'reviewed_at':now.isoformat()}
    packet['assertions']={name:{**packet['assertions'][name],'checked_at':now.isoformat()} for name in NAMES}
    dest=root/'review_inbox'/day/(packet['symbol']+'.json')
    # Validate in an isolated inbox first; invalid approval cannot replace an existing review.
    import tempfile,shutil
    with tempfile.TemporaryDirectory() as directory:
        temporary=Path(directory)
        shutil.copytree(root/'official_documents',temporary/'official_documents')
        inbox=temporary/'review_inbox'/day;inbox.mkdir(parents=True)
        (inbox/(packet['symbol']+'.json')).write_text(json.dumps(packet),encoding='utf-8')
        load_confirmed_review(packet['symbol'],financials,at,now,temporary,session_scope=scoped)
    if dest.exists():
        previous=json.loads(dest.read_text())
        if all(previous.get(k)==packet.get(k) for k in ('rules_version','decision_time_jst','approval_scope','financial_evidence_sha256')) and {name:{k:v for k,v in item.items() if k!='checked_at'} for name,item in previous['assertions'].items()}=={name:{k:v for k,v in item.items() if k!='checked_at'} for name,item in packet['assertions'].items()}:
            return previous
        raise ValueError('review already approved; refusing replacement')
    dest.parent.mkdir(parents=True,exist_ok=True)
    dest.write_text(json.dumps(packet,ensure_ascii=False,indent=2),encoding='utf-8')
    return packet


if __name__=='__main__':
    result=approve(os.environ.get('REVIEW_ID',''),os.environ.get('GITHUB_ACTOR',''),datetime.now(JST))
    print('Approved source review for '+result['symbol']+'; no trade registered.')
