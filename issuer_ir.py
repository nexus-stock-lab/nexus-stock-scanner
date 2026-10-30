"""Bounded public issuer-IR collection. Acquisition is never a semantic clearance.

The registry is a source-discovery bootstrap, NOT a trading-universe allowlist.
Unconfigured issuers retain SEC research and an explicit IR coverage gap.
"""
import hashlib
import json
import re
from datetime import datetime, time, timezone, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

from paper_engine import digest
from historical_probe import timestamp_ns

REGISTRY_VERSION='official_ir_20261029_v1'
# Verified against the issuers' own investor pages, not ticker-name search alone.
REGISTRY={
    'APLD':{'index':'https://ir.applieddigital.com/news-events/press-releases',
            'prefix':'https://ir.applieddigital.com/news-events/press-releases/'},
    'IREN':{'index':'https://iren.gcs-web.com/news-releases',
            'prefix':'https://iren.gcs-web.com/news-releases/',
            'feed_prefix':'https://iren.gcs-web.com/rss/'},
    'NBIS':{'index':'https://nebius.com/newsroom','prefix':'https://nebius.com/newsroom/'},
}
LIMIT=4_000_000

class IRBlocked(ValueError):pass
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise IRBlocked('IR_REDIRECT_UNVERIFIED')


def allowed(url,config,index=False,feed=False):
    p=urlsplit(url)
    if p.scheme!='https' or p.username or p.password or p.port or p.query or p.fragment or '..' in p.path or '%' in p.path:
        return False
    if index:return url==config['index']
    prefixes=[config['prefix']]+([config['feed_prefix']] if feed and 'feed_prefix' in config else [])
    return any(url.startswith(prefix) for prefix in prefixes) and p.netloc==urlsplit(config['index']).netloc


def fetch(url,config,transport=None,index=False,feed=False):
    if not allowed(url,config,index,feed):raise IRBlocked('IR_URL_NOT_IN_VERIFIED_REGISTRY')
    # Never forward the SEC contact or any API credential to an issuer site.
    req=Request(url,headers={'User-Agent':'NEXUS-Public-IR-Research/1.0','Accept':'text/html,application/rss+xml,application/xml,text/xml'})
    try:
        with (transport or build_opener(NoRedirect()).open)(req,timeout=20) as response:
            if response.url!=url:raise IRBlocked('IR_REDIRECT_UNVERIFIED')
            raw=response.read(LIMIT+1)
    except HTTPError as exc:raise IRBlocked('IR_HTTP_'+str(int(exc.code))) from None
    except (URLError,TimeoutError,OSError):raise IRBlocked('IR_NETWORK_UNVERIFIED') from None
    if len(raw)>LIMIT:raise IRBlocked('IR_SOURCE_TOO_LARGE_NO_TRUNCATION')
    if raw.startswith(b'%PDF'):raise IRBlocked('IR_PDF_NOT_PARSED')
    return raw


class Page(HTMLParser):
    def __init__(self):
        super().__init__();self.links=[];self.parts=[];self.dates=[];self.skip=0;self.ld=False;self.ldparts=[]
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag in ('script','style'):self.skip+=1
        if tag=='script' and a.get('type')=='application/ld+json':self.ld=True;self.ldparts=[]
        if tag=='meta' and (a.get('property') or a.get('name','')).lower() in ('article:published_time','datepublished','date','pubdate'):
            self.dates.append(a.get('content',''))
        if tag=='a' and a.get('href'):self.links.append((a['href'],'article'))
        if tag=='link' and a.get('type') in ('application/rss+xml','application/atom+xml') and a.get('href'):
            self.links.append((a['href'],'feed'))
    def handle_endtag(self,tag):
        if tag=='script' and self.ld:
            try:
                value=json.loads(''.join(self.ldparts))
                def visit(v):
                    if isinstance(v,dict):
                        if 'datePublished' in v:self.dates.append(v['datePublished'])
                        for x in v.values():visit(x)
                    elif isinstance(v,list):
                        for x in v:visit(x)
                visit(value)
            except (ValueError,TypeError):pass
            self.ld=False
        if tag in ('script','style'):self.skip=max(0,self.skip-1)
    def handle_data(self,data):
        if self.ld:self.ldparts.append(data)
        if not self.skip:self.parts.append(data)


def publication(raw):
    """Date-only disclosures use end-of-day UTC-12, a conservative global upper bound.

    They cannot qualify before that bound; retrieval time is never a publication date.
    """
    if not isinstance(raw,str):raise IRBlocked('IR_PUBLICATION_TIME_UNKNOWN')
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}',raw):
        return datetime.combine(datetime.strptime(raw,'%Y-%m-%d').date(),time.max,timezone(timedelta(hours=-12)))
    try:
        value=datetime.fromisoformat(raw.replace('Z','+00:00'))
    except ValueError:
        try:value=parsedate_to_datetime(raw)
        except (ValueError,TypeError):raise IRBlocked('IR_PUBLICATION_TIME_UNKNOWN') from None
    if not value or value.tzinfo is None:raise IRBlocked('IR_PUBLICATION_TIME_UNKNOWN')
    return value


def archive(symbol,url,raw,published,at,now,root):
    config=REGISTRY[symbol]
    if not allowed(url,config):raise IRBlocked('IR_URL_NOT_IN_VERIFIED_REGISTRY')
    if not timestamp_ns(published.isoformat())<=timestamp_ns(now.isoformat())<=timestamp_ns(at.isoformat()):
        raise IRBlocked('IR_SOURCE_NOT_AVAILABLE_ASOF')
    page=Page();page.feed(raw.decode('utf-8',errors='strict'))
    text=' '.join(' '.join(page.parts).split())
    if not text:raise IRBlocked('IR_EMPTY_DOCUMENT')
    document={'source':url,'source_kind':'ISSUER_IR','issuer_symbol':symbol,
              'source_registry_version':REGISTRY_VERSION,'authority_url':config['index'],
              'published_at':published.isoformat(),'retrieved_at':now.isoformat(),
              'official_source_verified':True,'text':text,'raw_sha256':hashlib.sha256(raw).hexdigest()}
    sha=digest(document);p=root/'official_documents'/f'{sha}.json';p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(document,ensure_ascii=False),encoding='utf-8')
    return {'source':url,'published_at':document['published_at'],'evidence_sha256':sha,'status':'ARCHIVED_NOT_REVIEWED'}


def verify_document(symbol,document):
    config=REGISTRY.get(symbol)
    return bool(config and document.get('issuer_symbol')==symbol and
                document.get('source_registry_version')==REGISTRY_VERSION and
                document.get('authority_url')==config['index'] and
                document.get('official_source_verified') is True and allowed(document.get('source',''),config))


def collect_issuer_ir(symbol,at,now,root,transport=None,clock=None):
    report={'symbol':symbol,'kind':'IR_RESEARCH_ONLY','may_register':False,
            'coverage_complete':False,'documents':[],'audit':[],'registry_version':REGISTRY_VERSION}
    config=REGISTRY.get(symbol)
    if not config:
        report['status']='IR_SOURCE_NOT_CONFIGURED';return report
    report['status']='IR_COLLECTION_ATTEMPTED'
    try:
        raw=fetch(config['index'],config,transport,index=True)
        def save_source(url,body):
            value={'source':url,'retrieved_at':(clock() if clock else datetime.now(now.tzinfo)).isoformat(),
                   'issuer_symbol':symbol,'raw_sha256':hashlib.sha256(body).hexdigest(),
                   'payload':body.decode('utf-8',errors='strict'),'registry_version':REGISTRY_VERSION}
            sha=digest(value);folder=root/'issuer_ir_sources';folder.mkdir(parents=True,exist_ok=True)
            (folder/f'{sha}.json').write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
            return sha
        report['index_evidence_sha256']=save_source(config['index'],raw)
        page=Page();page.feed(raw.decode('utf-8',errors='strict'))
        candidates=[];feed_candidates=[]
        for href,kind in page.links:
            url=urljoin(config['index'],href)
            is_feed=kind=='feed' or url.endswith(('/rss','.xml'))
            if allowed(url,config,feed=is_feed):
                (feed_candidates if is_feed else candidates).append(url)
        seen=set();published_by_url={};budget=4
        # A same-host feed supplies explicit publication times, never estimates.
        if feed_candidates:
            feed=feed_candidates[0];feedraw=fetch(feed,config,transport,feed=True);budget-=1
            if b'<!DOCTYPE' in feedraw.upper() or b'<!ENTITY' in feedraw.upper():raise IRBlocked('IR_XML_ENTITIES_REJECTED')
            report['feed_evidence_sha256']=save_source(feed,feedraw)
            tree=ET.fromstring(feedraw)
            items=[]
            for item in tree.findall('.//item'):
                url=item.findtext('link');date=item.findtext('pubDate')
                if url and date and allowed(url,config):
                    try:published_by_url[url]=publication(date);items.append(url)
                    except IRBlocked:continue
            candidates=items+candidates
        for url in candidates:
            if url in seen:continue
            seen.add(url)
            if budget<=0:
                report['audit'].append({'status':'IR_SOURCE_BUDGET_DEFERRED'});break
            budget-=1
            try:
                body=fetch(url,config,transport)
                p=Page();p.feed(body.decode('utf-8',errors='strict'))
                dates=[publication(d) for d in p.dates]
                if url in published_by_url:dates.append(published_by_url[url])
                if not dates:raise IRBlocked('IR_PUBLICATION_TIME_UNKNOWN')
                # Conflicting publication dates are a hold, not a convenient earliest date.
                if len({d.date() for d in dates})!=1:raise IRBlocked('IR_PUBLICATION_TIME_CONFLICT')
                observed=clock() if clock else datetime.now(now.tzinfo)
                report['documents'].append(archive(symbol,url,body,max(dates),at,observed,root))
            except Exception as exc:
                report['audit'].append({'source':url,'status':'IR_UNVERIFIED',
                                        'reason':str(exc) if isinstance(exc,IRBlocked) else type(exc).__name__})
    except Exception as exc:
        report['audit'].append({'status':'IR_INDEX_UNVERIFIED',
                                'reason':str(exc) if isinstance(exc,IRBlocked) else type(exc).__name__})
    # This bounded crawl can never certify the absence of adverse disclosures.
    return report


def main():
    from market_calendar import JST,next_decision
    now=datetime.now(JST);root=Path('input');out=Path('output');out.mkdir(exist_ok=True)
    reports=[collect_issuer_ir(symbol,next_decision(now),now,root) for symbol in REGISTRY]
    result={'mode':'IR_COLLECTION_ONLY','paper_fills_created':0,'may_register':False,
            'results':reports,'limitation':'Initial source adapters cover three issuers only; no semantic clearance or AI transmission.'}
    (out/'issuer_ir_probe.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'summary.md').write_text('# 公式IR収集の診断\n\n仮想登録・Google送信・正式判定への転記は行いません。\n\n```json\n'+json.dumps(result,ensure_ascii=False,indent=2)+'\n```\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
    if not any(r['documents'] for r in reports):raise SystemExit(1)

if __name__=='__main__':main()
