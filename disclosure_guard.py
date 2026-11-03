"""Veto-only SEC freshness check; never supplies a qualitative PASS."""
from historical_probe import timestamp_ns
from official_data import accepted_filings


def check_disclosures(submissions, cik, reviewed_at, decision_at):
    """Reject new issuer disclosures between review and the fixed decision.

    Caller must archive the authenticated public response. Passing means only
    that this response contains no newer filings, not that IR/news is covered.
    Missing timestamps or malformed arrays fail closed through ValueError.
    """
    try:
        if int(submissions['cik']) != int(cik):
            raise ValueError('issuer mismatch')
        reviewed=timestamp_ns(reviewed_at)
        if reviewed > timestamp_ns(decision_at.isoformat()):
            raise ValueError('review after decision')
        filings=accepted_filings(submissions,decision_at)
        newer=sorted(k for k,v in filings.items() if timestamp_ns(v['accepted_at'])>reviewed)
        return {'status':'BLOCKED_NEW_DISCLOSURES' if newer else 'NO_NEW_SEC_FILINGS_OBSERVED',
                'new_accessions':newer,'may_register':False,'ir_coverage_verified':False}
    except (KeyError,TypeError,IndexError,ValueError,OverflowError):
        return {'status':'BLOCKED_INVALID_DISCLOSURE_EVIDENCE','new_accessions':[],
                'may_register':False,'ir_coverage_verified':False}


def fetch_disclosures(plan, at, root, clock, fetch=None):
    from official_data import public_json, store_json
    fetch=fetch or public_json
    try:
        cik=f"{int(plan['financials']['cik']):010d}"
        if not 0<int(cik)<10_000_000_000:raise ValueError('invalid issuer')
        reviewed=min((plan['official_review'][name]['checked_at'] for name in
            ('guidance_not_lowered','decline_explanation_verified','no_material_business_impairment','next_earnings')),
            key=timestamp_ns)
        url=f'https://data.sec.gov/submissions/CIK{cik}.json'
        raw=fetch(url);retrieved=clock()
        if timestamp_ns(retrieved.isoformat())<timestamp_ns(at.isoformat()):
            raise ValueError('retrieval before decision')
        sha=store_json(root,url,raw,retrieved.isoformat())
        result=check_disclosures(raw,cik,reviewed,at)
        result.update(source=url,evidence_sha256=sha,retrieved_at=retrieved.isoformat())
        return result
    except Exception:
        return {'status':'BLOCKED_DISCLOSURE_ACQUISITION','may_register':False}
