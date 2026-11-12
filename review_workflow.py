"""Complete financial-pass review inventory; never infer semantic approval.

SOURCE_ASSESSMENT files are explicit reviewer attestations, not AI shadow output.
Unknown/invalid issuers are isolated. Only the existing intake can clear a pass.
This module never calls prices, plans, orders, schedules or the trade ledger.
"""
import argparse
import json
import re
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from historical_probe import timestamp_ns
from official_data import verify_review_documents
from paper_engine import digest
from review_intake import NAMES, DELEGATION, load_confirmed_review, unique_object
from strategy_rules import VERSION, checked, earnings_review, financial_quality


def read(path):
    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique_object)


def write_verified(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    if read(temporary) != value:
        raise ValueError('saved JSON readback mismatch')
    temporary.replace(path)
    if read(path) != value:
        raise ValueError('persisted JSON readback mismatch')


def inventory(research, now, root=Path('input'), promote=False):
    """Review every financial PASS, independent of row order, R:R or AI budget.

    Past-day reports are allowed; retrospective promotion is never allowed.
    Numeric validation is unchanged and does not repair source extraction errors.
    """
    root = Path(root)
    if research.get('snapshot_sha256') != digest({k:v for k,v in research.items() if k!='snapshot_sha256'}):
        raise ValueError('research snapshot proof mismatch')
    at = datetime.fromisoformat(research['decision_time_jst'])
    day = at.astimezone(ZoneInfo('America/New_York')).date().isoformat()
    report = {'kind':'SOURCE_REVIEW_REPORT', 'research_sha256':research['snapshot_sha256'],
              'decision_time_jst':at.isoformat(), 'observed_at':now.isoformat(),
              'rules_version':VERSION, 'results':[], 'saved_count':0,
              'local_readback_count':0, 'remote_verified_count':0,
              'remote_verification':'NOT_PERFORMED_BY_THIS_LOCAL_PROCESS'}
    for symbol, financials in sorted(research.get('financials', {}).items()):
        if financials.get('quality', {}).get('pass') is not True:
            continue
        result = {'symbol':symbol, 'status':'INSUFFICIENT_EVIDENCE',
                  'financial_evidence_sha256':financials.get('evidence_sha256'),
                  'reasons':[], 'assertions':{},
                  'official_documents':financials.get('official_documents', []),
                  'issuer_ir':research.get('issuer_ir', {}).get(symbol, {})}
        report['results'].append(result)
        try:
            if not re.fullmatch(r'[A-Z][A-Z0-9.]{0,14}', symbol) or financials.get('symbol') != symbol:
                raise ValueError('financial issuer identity mismatch')
            quality = financial_quality(*(financials[k] for k in
                       ('revenue','prior_revenue','operating_income','prior_operating_income')))
            if not quality['pass']:
                raise ValueError('recorded financial PASS disagrees with existing numeric rule')
            # An inbox packet is already an explicit attestation; revalidate it.
            packet_path = root/'review_inbox'/day/(symbol+'.json')
            if packet_path.exists():
                packet = read(packet_path)
                reviewed = datetime.fromisoformat(packet['reviewed_at'])
                if reviewed > now:
                    raise ValueError('review is future')
                review = load_confirmed_review(symbol, financials, at, min(now, at), root,
                                               session_scope=bool(packet.get('approval_scope')))
                earnings_review(review['next_earnings'], at)
                result.update(status='PASS', packet_sha256=digest(packet),
                              assertions=packet['assertions'], reasons=['Existing intake and earnings policy verified'])
                continue
            path = root/'source_assessments'/day/(symbol+'.json')
            batch_path = root/'source_assessments'/(day+'.json')
            if path.exists():
                assessment = read(path)
            elif batch_path.exists():
                assessment = read(batch_path).get(symbol)
            else:
                assessment = None
            if assessment is None:
                result['reasons'] = ['No explicit source assessment: '+', '.join(NAMES)]
                continue
            if assessment.get('reviewer_id') == 'unassigned':
                if (assessment.get('kind') != 'SOURCE_ASSESSMENT' or assessment.get('symbol') != symbol or
                    assessment.get('research_sha256') != research['snapshot_sha256'] or
                    assessment.get('financial_evidence_sha256') != financials['evidence_sha256'] or
                    assessment.get('reviewed_at') is not None or
                    set(assessment.get('assertions',{})) != set(NAMES) or
                    any(x.get('value') is not None for x in assessment['assertions'].values())):
                    raise ValueError('unsigned assessment cannot contain a decision')
                result['assertions'] = assessment['assertions']
                result['reasons'] = [name+': '+str(item.get('rationale','審査未完了'))
                                     for name,item in assessment['assertions'].items()]
                continue
            if (assessment.get('kind') != 'SOURCE_ASSESSMENT' or
                assessment.get('symbol') != symbol or assessment.get('rules_version') != VERSION or
                assessment.get('research_sha256') != research['snapshot_sha256'] or
                assessment.get('financial_evidence_sha256') != financials['evidence_sha256'] or
                assessment.get('reviewer_id') != 'codex-delegated' or
                assessment.get('delegation_id') != DELEGATION):
                raise ValueError('assessment identity, research binding or reviewer invalid')
            stamp = assessment['reviewed_at']
            if timestamp_ns(stamp) > timestamp_ns(now.isoformat()):
                raise ValueError('assessment time is future')
            assertions = assessment['assertions']
            if set(assertions) != set(NAMES):
                raise ValueError('assessment requires all four assertion states')
            result['assertions'] = assertions
            rejected, unknown = [], []
            for name in NAMES:
                item = assertions[name]
                if not isinstance(item, dict) or not isinstance(item.get('rationale'), str) or not item['rationale'].strip():
                    raise ValueError('assertion requires an explicit reason: '+name)
                value = item.get('value')
                if value is None:
                    unknown.append(name+': '+item['rationale'])
                    continue
                if type(value) is not bool or item.get('checked_at') != stamp:
                    raise ValueError('invalid assertion state or check time: '+name)
                checked(item, now, required=False)
                verify_review_documents({'symbol':symbol, name:item}, root,
                                        names=(name,), financials=financials)
                if value is False:
                    rejected.append(name+': '+item['rationale'])
            if rejected:
                result.update(status='FAIL', reasons=rejected+unknown)
                continue
            if unknown:
                result['reasons'] = unknown
                continue
            # Reuse the exact LINC packet schema, without generating timestamps,
            # quotations, hashes, decisions or semantic answers for the reviewer.
            packet = {k:assessment[k] for k in ('symbol','rules_version','financial_evidence_sha256',
                       'reviewed_at','reviewer_id','delegation_id','assertions')}
            packet.update(kind='DELEGATED_SOURCE_REVIEW', decision_time_jst=at.isoformat())
            validation_at = at
            scoped = assessment.get('approval_scope') is not None
            if scoped:
                from entry_schedule import next_slot, session_date, session_review_covers
                packet.update(approval_scope=assessment['approval_scope'],
                              decision_time_jst=assessment.get('decision_time_jst'))
                validation_at = next_slot(now)
                if session_date(validation_at) != day or not session_review_covers(packet,validation_at):
                    raise ValueError('no future checkpoint in explicitly approved session')
            elif now > at:
                raise ValueError('past decision: no retrospective approval; retain research only')
            # Validate in a temporary intake before touching the real inbox.
            with tempfile.TemporaryDirectory() as folder:
                staging = Path(folder)
                for name in ('official_documents','issuer_ir_sources'):
                    if (root/name).exists():
                        (staging/name).symlink_to((root/name).resolve(), target_is_directory=True)
                candidate = staging/'review_inbox'/(day if scoped else at.date().isoformat())/(symbol+'.json')
                write_verified(candidate, packet)
                validated = load_confirmed_review(symbol, financials, validation_at, now, staging,session_scope=scoped)
                earnings_review(validated['next_earnings'], validation_at)
            result.update(status='PASS', packet_sha256=digest(packet), reasons=['All four explicit assertions validated'])
            if promote:
                write_verified(packet_path, packet)
                result['saved_path'] = str(packet_path)
                report['saved_count'] += 1
                report['local_readback_count'] += 1
        except Exception as exc:
            # An issuer failure cannot prevent later issuers from being reviewed.
            result.update(status='INSUFFICIENT_EVIDENCE', reasons=[type(exc).__name__+': '+str(exc)])
    report['counts'] = {status:sum(r['status']==status for r in report['results'])
                        for status in ('PASS','FAIL','INSUFFICIENT_EVIDENCE')}
    report['financial_pass_count'] = len(report['results'])
    return report


def run(research_path, now, root=Path('input'), promote=False):
    report = inventory(read(Path(research_path)), now, root, promote)
    day = datetime.fromisoformat(report['decision_time_jst']).astimezone(ZoneInfo('America/New_York')).date()
    write_verified(Path(root)/'review_reports'/(str(day)+'.json'), report)
    return report


def collect_assessments(research, now, root=Path('input'), *, sec_collector=None,
                        ir_collector=None, clock=None):
    """Collect every financial PASS and publish an explicit four-item work queue.

    The Work reviewer supplies semantic decisions. Collection never claims to be
    that reviewer, never promotes shadow output, and never overwrites an attestation.
    Collection receipts are separate from the immutable financial snapshot.
    """
    from official_data import collect_company_documents
    from issuer_ir import collect_issuer_ir
    root = Path(root)
    if research.get('snapshot_sha256') != digest({k:v for k,v in research.items() if k!='snapshot_sha256'}):
        raise ValueError('research snapshot proof mismatch')
    at = datetime.fromisoformat(research['decision_time_jst'])
    day = at.astimezone(ZoneInfo('America/New_York')).date().isoformat()
    sec_collector = sec_collector or collect_company_documents
    ir_collector = ir_collector or collect_issuer_ir
    clock = clock or (lambda: datetime.now(now.tzinfo))
    queue = {'kind':'SOURCE_REVIEW_QUEUE', 'research_sha256':research['snapshot_sha256'],
             'decision_time_jst':at.isoformat(), 'started_at':now.isoformat(),
             'may_register':False, 'results':[]}
    for symbol, financials in sorted(research.get('financials', {}).items()):
        if financials.get('quality', {}).get('pass') is not True:
            continue
        item = {'symbol':symbol, 'documents':[], 'errors':[]}
        queue['results'].append(item)
        current = clock()
        if current > at:
            item['errors'].append('PAST_DECISION_NO_RETROSPECTIVE_COLLECTION_OR_APPROVAL')
        else:
            for name, collector in (('sec',sec_collector), ('issuer_ir',ir_collector)):
                try:
                    receipt = collector(symbol,at,clock(),root)
                    if name == 'sec' and int(receipt['cik']) != int(financials['cik']):
                        raise ValueError('SEC issuer identity changed; fresh financial research required')
                    item[name] = receipt
                    item['documents'].extend(receipt.get('official_documents' if name=='sec' else 'documents', []))
                except Exception as exc:
                    # No raw provider response, contact header or credential in logs.
                    item['errors'].append(name+':'+type(exc).__name__)
        item['documents'] = financials.get('official_documents', []) + item['documents']
        item['assertion_names'] = list(NAMES)
        item['status'] = 'EXPLICIT_SEMANTIC_REVIEW_REQUIRED'
        path = root/'source_assessments'/day/(symbol+'.json')
        batch = read(root/'source_assessments'/(day+'.json')) if (root/'source_assessments'/(day+'.json')).exists() else {}
        if path.exists() or symbol in batch:
            item['assessment_status'] = 'EXISTING_ATTESTATION_PRESERVED'
            continue
        # A truthful incomplete SOURCE_ASSESSMENT, not a fabricated reviewer signature.
        stamp = clock().isoformat()
        pending = {'kind':'SOURCE_ASSESSMENT', 'symbol':symbol, 'rules_version':VERSION,
                   'research_sha256':research['snapshot_sha256'],
                   'financial_evidence_sha256':financials.get('evidence_sha256'),
                   'reviewed_at':None, 'generated_at':stamp, 'reviewer_id':'unassigned',
                   'assertions':{name:{'value':None, 'rationale':'公式資料の明示的な定性審査が未完了。収集だけでは承認しない。'} for name in NAMES}}
        write_verified(path,pending)
        item['assessment_status'] = 'PENDING_CREATED'
    queue['completed_at'] = clock().isoformat()
    queue['financial_pass_count'] = len(queue['results'])
    sha = digest(queue)
    write_verified(root/'source_review_runs'/day/(sha+'.json'),queue)
    return queue


def connected_run(research_path, now, root=Path('input'), out=Path('output'),
                  *, collect=False, promote=True, clock=None, **collectors):
    """Review-only entry point; no prices, plan construction, fills or ledger writes."""
    research = read(Path(research_path))
    queue = collect_assessments(research,now,root,clock=clock,**collectors) if collect else None
    completed = clock() if clock else datetime.now(now.tzinfo)
    report = run(research_path,completed,root,promote)
    status = {'kind':'CONNECTED_SOURCE_REVIEW_STATUS', 'observed_at':completed.isoformat(),
              'research_path':str(research_path),'research_sha256':research['snapshot_sha256'],
              'collection':queue, 'review':report,
              'semantic_review':'EXPLICIT_WORK_REVIEWER_REQUIRED_FOR_PENDING_ITEMS',
              'ledger_writes':0, 'orders':0}
    write_verified(Path(out)/'source_review_status.json',status)
    lines = ['# 公式資料審査', '',
             '財務通過: '+str(report['financial_pass_count'])+' / '+str(report['counts']),
             '承認保存（ローカル）: '+str(report['saved_count']),
             'GitHub再取得の証明は source_review_persistence.json を確認。', '',
             '未審査・根拠不足は承認しません。以下を Work の審査担当が確認します。', '']
    for result in report['results']:
        lines.extend(['## '+result['symbol']+' — '+result['status'],
                      *['- '+reason for reason in result['reasons']], ''])
    Path(out).mkdir(parents=True,exist_ok=True)
    (Path(out)/'source_review_summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return status


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('research', type=Path, nargs='?')
    parser.add_argument('--collect', action='store_true')
    parser.add_argument('--out', type=Path, default=Path('output'))
    parser.add_argument('--root', type=Path, default=Path('input'))
    parser.add_argument('--promote', action='store_true', help='Save only explicitly reviewed and intake-validated PASS packets')
    args = parser.parse_args()
    now = datetime.now(ZoneInfo('Asia/Tokyo'))
    if args.research is None:
        from market_calendar import next_decision
        args.research = args.root/'research'/(next_decision(now).date().isoformat()+'.json')
    if not args.research.exists():
        args.out.mkdir(parents=True,exist_ok=True)
        write_verified(args.out/'source_review_status.json', {'status':'CURRENT_RESEARCH_MISSING', 'research_path':str(args.research), 'orders':0, 'ledger_writes':0})
        (args.out/'source_review_summary.md').write_text('# 公式資料審査\n\n当日分の財務調査が未保存です。過去日の承認は作りません。\n',encoding='utf-8')
        raise SystemExit(0)
    report = connected_run(args.research, now, args.root, args.out, collect=args.collect, promote=args.promote)['review']
    print(json.dumps({k:report[k] for k in ('financial_pass_count','counts','saved_count','local_readback_count','remote_verified_count')}, ensure_ascii=False))
