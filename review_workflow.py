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
            if now > at:
                raise ValueError('past decision: no retrospective approval; retain research only')
            # Validate in a temporary intake before touching the real inbox.
            with tempfile.TemporaryDirectory() as folder:
                staging = Path(folder)
                for name in ('official_documents','issuer_ir_sources'):
                    if (root/name).exists():
                        (staging/name).symlink_to((root/name).resolve(), target_is_directory=True)
                candidate = staging/'review_inbox'/at.date().isoformat()/(symbol+'.json')
                write_verified(candidate, packet)
                validated = load_confirmed_review(symbol, financials, at, now, staging)
                earnings_review(validated['next_earnings'], at)
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


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('research', type=Path)
    parser.add_argument('--root', type=Path, default=Path('input'))
    parser.add_argument('--promote', action='store_true', help='Save only explicitly reviewed and intake-validated PASS packets')
    args = parser.parse_args()
    report = run(args.research, datetime.now(ZoneInfo('Asia/Tokyo')), args.root, args.promote)
    print(json.dumps({k:report[k] for k in ('financial_pass_count','counts','saved_count','local_readback_count','remote_verified_count')}, ensure_ascii=False))
