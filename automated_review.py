"""Owner-authorized machine semantic review; no plans, prices or ledger writes.

Two separate source-only interpretations are required. Agreement reduces, but
does not eliminate, model error; quotation checks are not proof of truth.
Human/codex attestations are never forged or overwritten.
"""
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from gemini_review import (MODEL, INSTRUCTIONS, SCHEMA, NAMES, ReviewBlocked,
                           public_bundle, post_json, reserve_request, validate_assessment,
                           write_json, strict_json)
from paper_engine import digest
from historical_probe import timestamp_ns
from strategy_rules import VERSION

# Full UTF-8 source text, with headroom below the model's documented input
# limit of 1,048,576 tokens even at one token per byte.
MAX_AUTOMATED_CONTEXT_BYTES = 1_024_000

REVIEWER = 'gemini-delegated'
DELEGATION = 'owner_20261120_official_paper_automation'
VERSION_ID = 'source_only_two_pass_v1'
TRANSIENT_PROVIDER_ERRORS = {
    'GEMINI_HTTP_500', 'GEMINI_HTTP_502', 'GEMINI_HTTP_503', 'GEMINI_HTTP_504',
    'GEMINI_TIMEOUT_UNVERIFIED', 'GEMINI_NETWORK_UNVERIFIED',
}
CONSECUTIVE_PROVIDER_FAILURE_LIMIT = 2
FALLBACK_MODEL = 'gemini-3.6-flash'


def interpret(bundle, key, role, *, model=MODEL, diagnostics=None):
    instruction = INSTRUCTIONS + ('\nIndependently perform the four semantic assessments. '
        'Act as a skeptical verifier: check contrary evidence, scope and uncertainties. '
        'Do not assume positive financial numbers establish any qualitative assertion.'
        if role == 'verifier' else '\nPerform the primary semantic assessment.')
    value, sha = post_json({'model': model, 'input': instruction+'\nPUBLIC_SOURCE_DATA:\n'+
        json.dumps(bundle, ensure_ascii=False), 'store': False, 'stream': False,
        'background': False, 'generation_config': {'max_output_tokens':4096, 'thinking_level':'low'},
        'response_format': {'type':'text','mime_type':'application/json','schema':SCHEMA}}, key, timeout=300, diagnostics=diagnostics)
    validate_assessment(value, bundle)
    return {'assessment': value, 'provider_response_sha256': sha, 'model': model}


def assertions_from_proof(proof):
    """Revalidate both actual responses, citations and time on every intake."""
    bundle=proof['bundle']
    if proof.get('version')!=VERSION_ID or proof.get('model')!=MODEL:
        raise ValueError('automated review version/model mismatch')
    if not proof.get('free_tier_confirmed'):
        raise ValueError('automated review free tier not confirmed')
    for response in proof['responses']:
        if response.get('model', MODEL) not in (MODEL, FALLBACK_MODEL):
            raise ValueError('unexpected semantic review model')
        validate_assessment(response['assessment'],bundle)
        if not __import__('re').fullmatch('[0-9a-f]{64}',str(response.get('provider_response_sha256'))):
            raise ValueError('actual provider response hash required')
    if len(proof['responses'])!=2:
        raise ValueError('two semantic responses required')
    if len({r['provider_response_sha256'] for r in proof['responses']})!=2:
        raise ValueError('two distinct provider responses required')
    first, second=[{x['name']:x for x in r['assessment']['assertions']} for r in proof['responses']]
    docs={x['document_id']:x for x in bundle['documents']}
    assertions={}
    for name in NAMES:
        a,b=first[name],second[name]
        if a['verdict']!=b['verdict'] or a['date']!=b['date']:
            value=None;reason='Independent semantic passes disagree; approval withheld.'
        elif a['verdict']=='UNKNOWN':
            value=None;reason=a['rationale']+' / verifier: '+b['rationale']
        else:
            value=a['verdict']=='SUPPORTED';reason=a['rationale']+' / verifier: '+b['rationale']
        item={'value':value,'rationale':reason}
        if value is not None:
            citation=a['citations'][0];doc=docs[citation['document_id']]
            item.update(source=doc['source'],evidence_sha256=citation['document_id'],
                excerpt=citation['excerpt'],published_at=doc['published_at'],checked_at=proof['checked_at'])
            if name=='next_earnings':item['date']=a['date']
        assertions[name]=item
    return assertions


def verify_proof(record, financials, at, now, root):
    sha=record.get('automated_evidence_sha256','')
    if not __import__('re').fullmatch('[0-9a-f]{64}',sha):raise ValueError('automated review proof missing')
    proof=strict_json((Path(root)/'automated_reviews'/(sha+'.json')).read_text())
    if digest(proof)!=sha:raise ValueError('automated review proof altered')
    if (proof['symbol']!=record['symbol'] or proof['financial_evidence_sha256']!=financials.get('evidence_sha256')
        or proof['checked_at']!=record['reviewed_at'] or record['reviewer_id']!=REVIEWER
        or record['delegation_id']!=DELEGATION
        or ('research_sha256' in record and proof['research_sha256']!=record['research_sha256'])):
        raise ValueError('automated review binding mismatch')
    if not timestamp_ns(proof['checked_at'])<=timestamp_ns(now.isoformat())<=timestamp_ns(at.isoformat()):
        raise ValueError('automated review cannot authorize a past decision')
    # Rebuild each source from archived bytes and existing SEC/IR validation.
    references=[{'status':'ARCHIVED_NOT_REVIEWED','source':d['source'],
        'published_at':d['published_at'],'evidence_sha256':d['document_id']} for d in proof['bundle']['documents']]
    sec=[d for d in references if d['source'].startswith('https://www.sec.gov/')]
    ir=[d for d in references if d not in sec]
    rebuilt=public_bundle(record['symbol'],{**financials,'official_documents':sec},
        datetime.fromisoformat(proof['bundle']['decision_time_jst']),
        datetime.fromisoformat(proof['checked_at']),Path(root),ir_documents=ir,
        max_context_bytes=MAX_AUTOMATED_CONTEXT_BYTES)
    if rebuilt!=proof['bundle']:raise ValueError('automated review source bundle changed')
    if assertions_from_proof(proof)!=record['assertions']:raise ValueError('automated review assertions changed')
    return proof


def run(research, now, root=Path('input'), *, queue=None, requester=interpret,
        clock=None, sleeper=time.sleep):
    from entry_schedule import next_slot, session_date, session_slots
    root=Path(root);clock=clock or (lambda:datetime.now(now.tzinfo))
    at=next_slot(now);day=session_date(at)
    status={'kind':'AUTOMATED_SEMANTIC_REVIEW','observed_at':now.isoformat(),
            'reviewer_id':REVIEWER,'model':MODEL,'results':[],'may_register':False}
    if digest({k:v for k,v in research.items() if k!='snapshot_sha256'})!=research.get('snapshot_sha256'):
        raise ValueError('research snapshot proof mismatch')
    # After the last checkpoint, prepare can already freeze the next
    # session's research. Match its checkpoint, not the wall-clock date.
    if session_date(datetime.fromisoformat(research['decision_time_jst']))!=day:
        status['status']='CURRENT_SESSION_RESEARCH_REQUIRED';return status
    if os.getenv('NEXUS_AUTOMATED_REVIEW_ENABLED')!='true':
        status['status']='DISABLED';return status
    if not os.getenv('GEMINI_API_KEY') or os.getenv('NEXUS_GEMINI_FREE_TIER_CONFIRMED')!='true':
        status['status']='FREE_TIER_OR_CREDENTIALS_UNAVAILABLE';return status
    candidates=sorted(s for s,f in research['financials'].items() if f.get('quality',{}).get('pass') is True)
    state_path=root/'automated_review_state.json'
    state=strict_json(state_path.read_text()) if state_path.exists() else {}
    after=state.get('last_symbol','')
    candidates=[s for s in candidates if s>after]+[s for s in candidates if s<=after]
    collected={x['symbol']:x for x in (queue or {}).get('results',[])}
    retry_at = state.get("provider_retry_at")
    requests_blocked = (
        "GEMINI_PROVIDER_COOLDOWN" if retry_at and timestamp_ns(now.isoformat()) < timestamp_ns(retry_at)
        else None
    )
    transient_failures = 0
    active_model = MODEL
    for symbol in candidates:
        path=root/'source_assessments'/day/(symbol+'.json')
        existing=strict_json(path.read_text()) if path.exists() else {}
        if existing.get('reviewer_id') not in (None,'unassigned',REVIEWER):
            status['results'].append({'symbol':symbol,'status':'EXPLICIT_REVIEW_PRESERVED'});continue
        f=research['financials'][symbol];current=clock()
        provider_held_here = False
        attempted_roles = []
        diagnostics = {}
        try:
            # The collection receipts supplement, never mutate, frozen financials.
            refs=collected.get(symbol,{}).get('documents',[])
            sec=[x for x in refs if x.get('source','').startswith('https://www.sec.gov/')]
            ir=[x for x in refs if x not in sec]
            bundle=public_bundle(symbol,{**f,'official_documents':sec or f.get('official_documents',[])},
                session_slots(at.astimezone(__import__('entry_schedule').NY).date())[-1],current,root,
                ir_documents=ir or research.get('issuer_ir',{}).get(symbol,{}).get('documents',[]),
                deduplicate=True,max_context_bytes=MAX_AUTOMATED_CONTEXT_BYTES)
            key=digest({'version':VERSION_ID,'bundle':bundle,'financial':f['evidence_sha256'],
                        'research':research['snapshot_sha256']})
            cache=root/'automated_review_cache'/(key+'.json')
            if cache.exists():
                proof=strict_json(cache.read_text())
            else:
                responses=[]
                for role in ('primary','verifier'):
                    response_path=root/'automated_review_requests'/(key+'-'+role+'.json')
                    if response_path.exists():
                        response=strict_json(response_path.read_text())
                        validate_assessment(response['assessment'],bundle)
                    else:
                        if requests_blocked:
                            raise ReviewBlocked(requests_blocked)
                        reserve_request(root,clock(),digest({'request':key,'role':role}))
                        try:
                            attempted_roles.append(role)
                            api_key = os.environ['GEMINI_API_KEY']
                            if requester is interpret:
                                diagnostics.clear()
                                if role == 'verifier' and responses[0].get('model') == FALLBACK_MODEL:
                                    active_model = FALLBACK_MODEL
                                try:
                                    response = interpret(bundle, api_key, role, model=active_model,
                                                         diagnostics=diagnostics)
                                except ReviewBlocked as exc:
                                    if str(exc) != 'GEMINI_HTTP_503' or active_model != MODEL:
                                        raise
                                    delay = max(30, diagnostics.get('retry_after_seconds', 0),
                                                float(diagnostics.get('google_retry_delay', '0s')[:-1]))
                                    if delay > 60:
                                        raise
                                    sleeper(delay)
                                    if clock() >= at:
                                        raise
                                    reserve_request(root, clock(), digest({
                                        'request': key, 'role': role, 'model': FALLBACK_MODEL}))
                                    active_model = FALLBACK_MODEL
                                    attempted_roles.append(role)
                                    diagnostics.clear()
                                    response = interpret(bundle, api_key, role, model=active_model,
                                                         diagnostics=diagnostics)
                            else:
                                response = requester(bundle, api_key, role)
                            transient_failures = 0
                            validate_assessment(response['assessment'],bundle)
                            write_json(response_path,response)
                        finally:
                            # A failed provider call also consumed a request slot.
                            # Pace the next issuer instead of cascading rate holds.
                            sleeper(7)
                    responses.append(response)
                proof={'version':VERSION_ID,'symbol':symbol,'model':MODEL,'bundle':bundle,
                    'financial_evidence_sha256':f['evidence_sha256'],'research_sha256':research['snapshot_sha256'],
                    'checked_at':clock().isoformat(),'free_tier_confirmed':True,'responses':responses}
                assertions_from_proof(proof)
                write_json(cache,proof)
            assessment={'kind':'SOURCE_ASSESSMENT','symbol':symbol,'rules_version':VERSION,
                'research_sha256':research['snapshot_sha256'],'financial_evidence_sha256':f['evidence_sha256'],
                'reviewer_id':REVIEWER,'delegation_id':DELEGATION,'reviewed_at':proof['checked_at'],
                'approval_scope':'US_REGULAR_SESSION','decision_time_jst':bundle['decision_time_jst'],
                'automated_evidence_sha256':digest(proof),'assertions':assertions_from_proof(proof)}
            write_json(root/'automated_reviews'/(digest(proof)+'.json'),proof)
            verify_proof(assessment,f,at,clock(),root)
            write_json(path,assessment)
            values=[x['value'] for x in assessment['assertions'].values()]
            status['results'].append({'symbol':symbol,'status':'SUPPORTED_PENDING_EXISTING_INTAKE' if all(x is True for x in values)
                else 'FAIL' if any(x is False for x in values) else 'INSUFFICIENT_EVIDENCE'})
        except (ValueError,KeyError,TypeError,OSError) as exc:
            reason=str(exc) if isinstance(exc,ReviewBlocked) else type(exc).__name__
            status['results'].append({'symbol':symbol,'status':'HELD','reason':reason,'attempted_roles':attempted_roles, **({'provider_diagnostics': diagnostics} if diagnostics else {})})
            if reason in ('GEMINI_HTTP_429','GEMINI_HTTP_401','GEMINI_HTTP_403','DAILY_REVIEW_BUDGET_DEFERRED'):
                # A provider hold must not prevent validation of saved evidence.
                # Resume network work from the first deferred issuer next time.
                requests_blocked = reason
                if reason == 'GEMINI_HTTP_429' and attempted_roles:
                    retry_seconds = max(diagnostics.get('retry_after_seconds', 0),
                                        float(diagnostics.get('google_retry_delay', '0s')[:-1]))
                    if retry_seconds:
                        state['provider_retry_at'] = (clock() + timedelta(seconds=retry_seconds)).isoformat()
                        write_json(state_path, state)
            elif reason in TRANSIENT_PROVIDER_ERRORS:
                transient_failures += 1
                if transient_failures >= CONSECUTIVE_PROVIDER_FAILURE_LIMIT:
                    requests_blocked = 'GEMINI_PROVIDER_DEFERRED'
                    provider_held_here = True
        # Advance through attempted issuers, even during transient failures.
        # A new run starts with untouched work before circling back to retries.
        if not requests_blocked or provider_held_here:
            state['last_symbol']=symbol;write_json(state_path,state)
    status['status']='COMPLETED_WITH_EXPLICIT_HOLDS'
    return status
