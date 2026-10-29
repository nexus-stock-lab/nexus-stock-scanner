"""Public SEC financial evidence. Qualitative IR assertions require archived review.

Never regards missing words in an SEC filing as proof that risks are absent.
"""
import hashlib
import json
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlparse

from historical_probe import timestamp_ns
from paper_engine import digest
from strategy_rules import VERSION, financial_quality

REVENUE_TAGS = ('RevenueFromContractWithCustomerExcludingAssessedTax',
                'RevenueFromContractWithCustomerIncludingAssessedTax', 'Revenues', 'SalesRevenueNet')


def sec_user_agent():
    """Use the owner's declared contact, never invent an email or log the header."""
    value=os.getenv('NEXUS_SEC_USER_AGENT','').strip()
    if (not value or len(value)>500 or '\r' in value or '\n' in value
            or not re.search(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+',value)):
        raise ValueError('SEC contact not configured: save app name and contact email as NEXUS_SEC_USER_AGENT Secret')
    return value


def public_json(url):
    host = urlparse(url).hostname
    if host not in ('www.sec.gov','data.sec.gov'):
        raise ValueError('SEC host required')
    time.sleep(0.2)  # Below SEC's 10 requests/second ceiling, sequential only.
    request = Request(url, headers={'User-Agent':sec_user_agent(),
                      'Accept':'application/json'})
    with urlopen(request, timeout=15) as response:
        if urlparse(response.url).hostname != host:
            raise ValueError('unexpected SEC redirect')
        return json.load(response)


def store_json(root, url, payload, at):
    value={'source':url,'retrieved_at':at,'payload':payload}
    sha=digest(value)
    path=root/'official_evidence'/f'{sha}.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
    return sha


def accepted_filings(submissions, at):
    recent=submissions['filings']['recent']
    result={}
    for i, accession in enumerate(recent['accessionNumber']):
        accepted=recent['acceptanceDateTime'][i]
        if timestamp_ns(accepted) <= timestamp_ns(at.isoformat()):
            result[accession]={'accepted_at':accepted,'form':recent['form'][i],
                               'report_date':recent['reportDate'][i],
                               'items':recent.get('items',['']*len(recent['accessionNumber']))[i]}
    return result


def quarter_values(units, filings):
    """Use accepted, non-amended consolidated USD facts and derive Q4 as FY-9M."""
    rows=[]
    for row in units:
        if row.get('accn') not in filings or row.get('form') not in ('10-Q','10-K'):
            continue
        if not row.get('start') or not row.get('end'):
            continue
        duration=(date.fromisoformat(row['end'])-date.fromisoformat(row['start'])).days+1
        rows.append({**row,'duration':duration,'accepted_at':filings[row['accn']]['accepted_at']})
    quarters=[dict(row) for row in rows if 70<=row['duration']<=100]
    for annual in (row for row in rows if 330<=row['duration']<=400):
        nine=[r for r in rows if r['start']==annual['start'] and 250<=r['duration']<=300
              and 70<=(date.fromisoformat(annual['end'])-date.fromisoformat(r['end'])).days<=100]
        if nine:
            last=max(nine,key=lambda r:(r['end'],r['accepted_at']))
            quarters.append({**annual,'start':str(date.fromisoformat(last['end'])+timedelta(days=1)),
                             'val':annual['val']-last['val'],
                             'derived_from':[annual['accn'],last['accn']]})
    # Multiple as-of-visible revisions with conflicting values are not silently chosen.
    unique={}
    for row in quarters:
        key=(row['start'],row['end'])
        old=unique.get(key)
        if old and old['val']!=row['val']:
            raise ValueError('conflicting reported quarter values; review restatement')
        unique[key]=row
    return list(unique.values())


def extract_financials(symbol, facts, submissions, at, checked_at, evidence_sha256):
    filings=accepted_filings(submissions,at)
    reports=[f for f in filings.values() if f['form'] in ('10-Q','10-K') and f['report_date']]
    if not reports:
        raise ValueError('no accepted quarterly US-GAAP report')
    latest=max(reports,key=lambda f:(f['report_date'],f['accepted_at']))
    # An earnings release or amendment newer than the last structured report
    # prevents silently evaluating an outdated quarter.
    for filing in filings.values():
        if timestamp_ns(filing['accepted_at']) > timestamp_ns(latest['accepted_at']):
            if filing['form'] in ('10-Q/A','10-K/A') or (filing['form']=='8-K' and '2.02' in filing.get('items','')):
                raise ValueError('newer official results/amendment require financial reconciliation')
    end=latest['report_date']
    gaap=facts['facts']['us-gaap']
    operating=quarter_values(gaap['OperatingIncomeLoss']['units']['USD'],filings)
    for tag in REVENUE_TAGS:
        if tag not in gaap:
            continue
        revenue=quarter_values(gaap[tag]['units'].get('USD',[]),filings)
        current=[r for r in revenue if r['end']==end]
        current_op=[r for r in operating if r['end']==end]
        if len(current)!=1 or len(current_op)!=1 or current[0]['start']!=current_op[0]['start']:
            continue
        prior=[r for r in revenue if 350<=(date.fromisoformat(end)-date.fromisoformat(r['end'])).days<=380
               and 350<=(date.fromisoformat(current[0]['start'])-date.fromisoformat(r['start'])).days<=380]
        if len(prior)!=1:
            continue
        old=prior[0]
        old_op=[r for r in operating if r['start']==old['start'] and r['end']==old['end']]
        if len(old_op)!=1:
            continue
        quality=financial_quality(current[0]['val'],old['val'],current_op[0]['val'],old_op[0]['val'])
        return {'symbol':symbol,'period_end':end,'prior_period_end':old['end'],
                'revenue':current[0]['val'],'prior_revenue':old['val'],
                'operating_income':current_op[0]['val'],'prior_operating_income':old_op[0]['val'],
                'revenue_tag':tag,'quality':quality,'checked_at':checked_at,
                'published_at':latest['accepted_at'],'evidence_sha256':evidence_sha256,
                'rules_version':VERSION}
    raise ValueError('latest report lacks unambiguous comparable quarterly revenue/operating income')


def collect_financials(symbol, at, now, root=Path('input'), fetch=public_json, ticker_map=None):
    if ticker_map is None:
        ticker_map=fetch('https://www.sec.gov/files/company_tickers.json')
    matches=[r for r in ticker_map.values() if r['ticker'].replace('-','.')==symbol]
    if len(matches)!=1:
        raise ValueError('SEC ticker mapping unavailable or ambiguous')
    cik=f"{int(matches[0]['cik_str']):010d}"
    fact_url=f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json'
    sub_url=f'https://data.sec.gov/submissions/CIK{cik}.json'
    facts,submissions=fetch(fact_url),fetch(sub_url)
    if int(facts['cik'])!=int(cik) or int(submissions['cik'])!=int(cik):
        raise ValueError('SEC entity mismatch')
    result=extract_financials(symbol,facts,submissions,at,now.isoformat(),'pending')
    # Permanent proof keeps the actual relevant XBRL facts and source hashes,
    # rather than repeatedly committing an issuer's entire multi-year dataset.
    oldest=date.fromisoformat(result['prior_period_end']).replace(month=1,day=1)
    selected={}
    for tag in (result['revenue_tag'],'OperatingIncomeLoss'):
        selected[tag]=[r for r in facts['facts']['us-gaap'][tag]['units']['USD']
                       if r.get('start') and oldest<=date.fromisoformat(r['start'])
                       and r['end']<=result['period_end']]
    fsha=store_json(root,fact_url,{'selected_us_gaap_usd':selected,'raw_response_sha256':digest(facts)},now.isoformat())
    accepted=accepted_filings(submissions,at)
    selected_accns={r['accn'] for rows in selected.values() for r in rows}
    ssha=store_json(root,sub_url,{'accepted_filings':{k:v for k,v in accepted.items() if k in selected_accns},
                                'raw_response_sha256':digest(submissions)},now.isoformat())
    result.update(evidence_sha256=fsha,submissions_sha256=ssha,source=fact_url,cik=cik)
    result['official_documents']=collect_documents(submissions,cik,at,now,root)
    return result


def collect_company_documents(symbol,at,now,root=Path('input'),fetch=public_json):
    """Qualitative source acquisition only: NEVER a substitute financial PASS."""
    mapping=fetch('https://www.sec.gov/files/company_tickers.json')
    matches=[r for r in mapping.values() if r['ticker'].replace('-','.')==symbol]
    if len(matches)!=1:raise ValueError('SEC ticker mapping unavailable or ambiguous')
    cik=f"{int(matches[0]['cik_str']):010d}"
    url=f'https://data.sec.gov/submissions/CIK{cik}.json'
    submissions=fetch(url)
    if int(submissions['cik'])!=int(cik):raise ValueError('SEC entity mismatch')
    sha=store_json(root,url,{'accepted_filings':accepted_filings(submissions,at),
                           'raw_response_sha256':digest(submissions)},now.isoformat())
    return {'symbol':symbol,'cik':cik,'source':url,'submissions_sha256':sha,
            'financial_status':'NOT_EVALUATED_SOURCE_REVIEW_ONLY',
            'official_documents':collect_documents(submissions,cik,at,now,root)}


def collect_documents(submissions, cik, at, now, root):
    """Archive latest official filings and 99.1 earnings exhibits for review.

    Source collection is automatic. It does not itself assert guidance, explain
    a stock-price decline, or prove the absence of business impairment.
    """
    from html.parser import HTMLParser
    from urllib.parse import urljoin
    class DocumentParser(HTMLParser):
        def __init__(self):
            super().__init__();self.parts=[];self.links=[];self.skip=0;self.anchor=None
        def handle_starttag(self,tag,attrs):
            attrs=dict(attrs)
            if tag in ('script','style'):
                self.skip+=1
            if tag=='a':
                self.anchor=[attrs.get('href',''),'']
        def handle_endtag(self,tag):
            if tag in ('script','style'):
                self.skip=max(0,self.skip-1)
            if tag=='a' and self.anchor is not None:
                self.links.append(tuple(self.anchor));self.anchor=None
        def handle_data(self,data):
            if not self.skip:
                self.parts.append(data)
            if self.anchor is not None:
                self.anchor[1]+=data
    recent=submissions['filings']['recent'];items=[]
    for i,acc in enumerate(recent['accessionNumber']):
        accepted=recent['acceptanceDateTime'][i]
        if timestamp_ns(accepted)>timestamp_ns(at.isoformat()) or recent['form'][i] not in ('8-K','10-Q','10-K','10-Q/A','10-K/A'):
            continue
        items.append((accepted,acc,recent['primaryDocument'][i]))
    items=sorted(items,reverse=True)[:8]
    result=[];attempts=0
    for published,acc,name in items:
        if attempts>=4:
            result.append({"status":"DEFERRED_SOURCE_BUDGET"});break
        base='https://www.sec.gov/Archives/edgar/data/'+str(int(cik))+'/'+acc.replace('-','')+'/'
        queue=[urljoin(base,name)];seen=set()
        while queue and len(seen)<3 and attempts<4:
            url=queue.pop(0)
            if url in seen or not url.startswith(base):
                continue
            seen.add(url)
            try:
                attempts+=1
                req=Request(url,headers={'User-Agent':sec_user_agent()})
                time.sleep(0.2)
                with urlopen(req,timeout=10) as response:
                    if not response.url.startswith(base):
                        raise ValueError('unexpected archive redirect')
                    raw=response.read(4_000_001)
                    if len(raw)>4_000_000:
                        raise ValueError('source too large; do not silently truncate')
                parser=DocumentParser();parser.feed(raw.decode('utf-8',errors='replace'))
                text=' '.join(' '.join(parser.parts).split())
                document={'source':url,'published_at':published,'retrieved_at':now.isoformat(),
                          'official_source_verified':True,'text':text,'raw_sha256':hashlib.sha256(raw).hexdigest()}
                sha=digest(document);path=root/'official_documents'/f'{sha}.json'
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text(json.dumps(document,ensure_ascii=False),encoding='utf-8')
                result.append({'source':url,'evidence_sha256':sha,'published_at':published,'status':'ARCHIVED_NOT_REVIEWED'})
                for href,label in parser.links:
                    if '99.1' in label or '99.01' in label or '99-1' in href.lower() or 'ex99' in href.lower():
                        queue.append(urljoin(url,href))
            except Exception as exc:
                result.append({'source':url,'status':'UNVERIFIED','reason':type(exc).__name__})
    return result


def verify_review_documents(review, root, names=None):
    """Check archived quotation/provenance; semantic assertions remain explicit."""
    for name in names or ('guidance_not_lowered','decline_explanation_verified','no_material_business_impairment','next_earnings'):
        item=review[name]
        sha=item['evidence_sha256']
        if len(sha)!=64 or any(c not in '0123456789abcdef' for c in sha):
            raise ValueError('invalid document hash')
        document=json.loads((root/'official_documents'/f'{sha}.json').read_text())
        if digest(document)!=sha or document['source']!=item['source']:
            raise ValueError('official document provenance mismatch')
        if item['excerpt'] not in document['text'] or not item['excerpt'].strip():
            raise ValueError('review excerpt missing from archived document')
        if document.get('published_at')!=item['published_at'] or timestamp_ns(document['published_at'])>timestamp_ns(document['retrieved_at']):
            raise ValueError('official document publication metadata mismatch')
        if timestamp_ns(document['retrieved_at']) > timestamp_ns(item['checked_at']):
            raise ValueError('review precedes source acquisition')
        if document.get('official_source_verified') is not True:
            raise ValueError('issuer/SEC official source not verified')
    return review
