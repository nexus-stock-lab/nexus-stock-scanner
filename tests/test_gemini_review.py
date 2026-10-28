"""No-network safety tests. Fictional public documents stay inside temporary directories."""
import copy
import io
import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from historical_probe import decision
from paper_engine import digest
from gemini_review import (MODEL,ENDPOINT,NAMES,public_bundle,validate_assessment,request_assessment,
                           review_one,shadow_reviews,ReviewBlocked,strict_json)

AT=decision('2026-10-29T23:45:00+09:00');NOW=AT-timedelta(hours=1)
TEXT='Official revenue guidance is reaffirmed. Earnings will be released on November 12, 2026.'


def assessment(symbol='AAPL'):
    return {'symbol':symbol,'assertions':[{'name':name,'verdict':'UNKNOWN',
            'rationale':'Insufficient public evidence in this limited selection','date':'','citations':[]} for name in NAMES]}


class Response:
    url=ENDPOINT
    def __init__(self,payload):self.raw=json.dumps(payload).encode()
    def read(self,limit):return self.raw[:limit]
    def __enter__(self):return self
    def __exit__(self,*args):pass


class GeminiSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'input';self.root.mkdir()
        self.out=Path(self.temp.name)/'output'
        d={'source':'https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/earnings.htm',
           'published_at':(NOW-timedelta(hours=2)).isoformat(),'retrieved_at':(NOW-timedelta(hours=1)).isoformat(),
           'official_source_verified':True,'text':TEXT,'raw_sha256':'1'*64}
        sha=digest(d);folder=self.root/'official_documents';folder.mkdir()
        (folder/f'{sha}.json').write_text(json.dumps(d))
        self.financials={'symbol':'AAPL','cik':'0000320193','quality':{'pass':True},
            'private_field':'MUST_NOT_SEND','official_documents':[{'status':'ARCHIVED_NOT_REVIEWED',
                'evidence_sha256':sha,'source':d['source'],'published_at':d['published_at']}]}
        self.bundle=public_bundle('AAPL',self.financials,AT,NOW,self.root)

    def tearDown(self):self.temp.cleanup()

    def supported(self):
        v=assessment();item=v['assertions'][0];item['verdict']='SUPPORTED'
        item['citations']=[{'document_id':self.bundle['documents'][0]['document_id'],
                            'excerpt':'Official revenue guidance is reaffirmed.'}]
        return v

    def requester(self,bundle,key):return validate_assessment(assessment(),bundle)

    def test_payload_whitelist_excludes_accounts_environment_and_extra_fields(self):
        sent={}
        def transport(req,timeout):
            sent.update(url=req.full_url,headers=dict(req.header_items()),payload=json.loads(req.data))
            return Response({'status':'completed','model':MODEL,'steps':[{'type':'model_output','content':[
                            {'type':'text','text':json.dumps(assessment())}]}]})
        with patch.dict(os.environ,{'ALPACA_DATA_KEY':'private-alpaca','SECRET_EMAIL':'private-email'}):
            result=request_assessment(self.bundle,'fixture-key',transport)
        encoded=json.dumps(sent['payload'])
        for secret in ('private-alpaca','private-email','MUST_NOT_SEND','fixture-key'):
            self.assertNotIn(secret,encoded)
        self.assertEqual(sent['url'],ENDPOINT)
        self.assertFalse(sent['payload']['store'])
        self.assertNotIn('tools',sent['payload'])
        self.assertFalse(result['may_register']);self.assertFalse(result['semantic_verified'])

    def test_supported_with_exact_citation_still_cannot_register(self):
        r=validate_assessment(self.supported(),self.bundle)
        self.assertTrue(r['quote_provenance_verified']);self.assertFalse(r['may_register'])
        self.assertFalse(r['semantic_verified'])

    def test_unknown_is_not_reported_as_verified_citation(self):
        r=validate_assessment(assessment(),self.bundle)
        self.assertFalse(r['quote_provenance_verified']);self.assertEqual(r['citation_count'],0)

    def test_fabricated_quote_and_wrong_document_rejected(self):
        for change in ('quote','document'):
            v=self.supported();c=v['assertions'][0]['citations'][0]
            c['excerpt' if change=='quote' else 'document_id']='fabricated evidence'
            with self.subTest(change=change),self.assertRaises(ReviewBlocked):validate_assessment(v,self.bundle)

    def test_supported_without_citation_rejected(self):
        v=self.supported();v['assertions'][0]['citations']=[]
        with self.assertRaises(ReviewBlocked):validate_assessment(v,self.bundle)

    def test_wrong_symbol_missing_duplicate_and_extra_assertions_rejected(self):
        for change in ('symbol','missing','duplicate','extra'):
            v=assessment()
            if change=='symbol':v['symbol']='MSFT'
            if change=='missing':v['assertions'].pop()
            if change=='duplicate':v['assertions'][1]=copy.deepcopy(v['assertions'][0])
            if change=='extra':v['assertions'][0]['value']=True
            with self.subTest(change=change),self.assertRaises(ReviewBlocked):validate_assessment(v,self.bundle)

    def test_announced_date_must_be_in_quote_and_not_stale(self):
        v=assessment();item=v['assertions'][-1];item.update(verdict='SUPPORTED',date='2026-11-12',citations=[{
            'document_id':self.bundle['documents'][0]['document_id'],'excerpt':TEXT}])
        self.assertFalse(validate_assessment(v,self.bundle)['may_register'])
        for date in ('2026-11-13','2026-01-01','not-a-date'):
            item['date']=date
            with self.subTest(date=date),self.assertRaises(ReviewBlocked):validate_assessment(v,self.bundle)

    def test_source_cannot_be_changed_or_point_to_another_issuer(self):
        for change in ('hash','host','cik','future'):
            f=copy.deepcopy(self.financials);sha=f['official_documents'][0]['evidence_sha256'];p=self.root/'official_documents'/f'{sha}.json'
            original=p.read_text();d=json.loads(original)
            if change=='hash':p.write_text('{}')
            else:
                if change=='host':d['source']=d['source'].replace('www.sec.gov','evil.example')
                if change=='cik':f['cik']='789019'
                if change=='future':d['retrieved_at']=(NOW+timedelta(hours=1)).isoformat()
                newsha=digest(d);(p.parent/f'{newsha}.json').write_text(json.dumps(d))
                f['official_documents'][0].update(evidence_sha256=newsha,source=d['source'])
            with self.subTest(change=change),self.assertRaises(ReviewBlocked):public_bundle('AAPL',f,AT,NOW,self.root)
            p.write_text(original)

    def test_oversize_source_is_held_not_truncated(self):
        f=copy.deepcopy(self.financials);d=json.loads(next((self.root/'official_documents').glob('*.json')).read_text())
        d['text']='x'*500_001;sha=digest(d);(self.root/'official_documents'/f'{sha}.json').write_text(json.dumps(d))
        f['official_documents'][0]['evidence_sha256']=sha
        with self.assertRaisesRegex(ReviewBlocked,'NO_TRUNCATION'):public_bundle('AAPL',f,AT,NOW,self.root)

    def test_empty_and_postdecision_sources_never_sent(self):
        f=copy.deepcopy(self.financials);f['official_documents']=[]
        with self.assertRaises(ReviewBlocked):public_bundle('AAPL',f,AT,NOW,self.root)
        with self.assertRaises(ReviewBlocked):public_bundle('AAPL',self.financials,AT,AT+timedelta(seconds=1),self.root)

    def test_missing_free_confirmation_and_key_never_call_api(self):
        for confirmed,key in ((False,'fixture'),('true','fixture'),(True,'')):
            with self.subTest(confirmed=confirmed,key=key),self.assertRaises(ReviewBlocked):
                review_one(self.bundle,NOW,self.root,key,confirmed,lambda *a:self.fail('must not send'))

    def test_cache_avoids_repeated_requests_and_detects_corruption(self):
        calls=[]
        def request(*a):calls.append(1);return self.requester(*a)
        result=review_one(self.bundle,NOW,self.root,'fixture',True,request)
        review_one(self.bundle,NOW,self.root,'fixture',True,request)
        self.assertEqual(len(calls),1)
        p=self.root/'gemini_reviews'/f"{result['request_sha256']}.json";v=json.loads(p.read_text());v['result']['may_register']=True;p.write_text(json.dumps(v))
        with self.assertRaises(ReviewBlocked):review_one(self.bundle,NOW,self.root,'fixture',True,request)

    def test_failed_requests_count_towards_daily_budget(self):
        def fail(*args):raise ReviewBlocked('GEMINI_HTTP_429')
        for i in range(10):
            with self.assertRaisesRegex(ReviewBlocked,'429'):
                review_one(self.bundle,NOW+timedelta(seconds=i*8),self.root,'fixture',True,fail)
        with self.assertRaisesRegex(ReviewBlocked,'DAILY_REVIEW_BUDGET'):
            review_one(self.bundle,NOW+timedelta(seconds=90),self.root,'fixture',True,lambda *a:self.fail('budget exhausted'))
        self.assertFalse((self.root/'trades.csv').exists());self.assertFalse((self.root/'official_reviews.json').exists())

    def test_network_and_model_failures_never_expose_error_body(self):
        def bad(req,timeout):raise HTTPError(ENDPOINT,401,'fixture-secret',{},io.BytesIO(b'fixture-secret'))
        with self.assertRaises(ReviewBlocked) as error:request_assessment(self.bundle,'fixture-secret',bad)
        self.assertEqual(str(error.exception),'GEMINI_HTTP_401')
        for payload in ([],{'status':'in_progress','model':MODEL},{'status':'completed','model':'gemini-4-argon'}):
            with self.subTest(payload=payload),self.assertRaises(ReviewBlocked):
                request_assessment(self.bundle,'fixture',lambda *a,**kw:Response(payload))

    def test_redirect_and_tool_output_rejected(self):
        response=Response({});response.url='https://evil.example/'
        with self.assertRaisesRegex(ReviewBlocked,'ENDPOINT_MISMATCH'):
            request_assessment(self.bundle,'fixture',lambda *a,**kw:response)
        response=Response({'status':'completed','model':MODEL,'steps':[{'type':'tool_call'}]})
        with self.assertRaises(ReviewBlocked):request_assessment(self.bundle,'fixture',lambda *a,**kw:response)

    def test_strict_json_rejects_duplicate_keys_and_nonfinite(self):
        for text in ('{"a":1,"a":2}','{"a":NaN}','{"a":Infinity}'):
            with self.subTest(text=text),self.assertRaises(ReviewBlocked):strict_json(text)

    def test_shadow_records_are_never_promoted_to_official_reviews(self):
        research={'decision_time_jst':AT.isoformat(),'rows':[{'symbol':'AAPL','secondary_status':'二次調査候補'}],
                  'financials':{'AAPL':self.financials}}
        research['snapshot_sha256']=digest(research)
        with patch.dict(os.environ,{'GEMINI_API_KEY':'fixture','NEXUS_GEMINI_FREE_TIER_CONFIRMED':'true'}):
            result=shadow_reviews(research,NOW,self.root,self.out,enabled=True,clock=lambda:NOW,requester=self.requester)
        self.assertEqual(result['results'][0]['status'],'SHADOW_RESPONSE_VALIDATED')
        for name in ('official_reviews.json','trades.csv','plans'):
            self.assertFalse((self.root/name).exists())
        self.assertFalse(result['may_register'])

    def test_disabled_or_missing_configuration_performs_no_request(self):
        research={'rows':[]}
        for enabled,env in ((False,{'GEMINI_API_KEY':'fixture'}),(True,{}),(True,{'GEMINI_API_KEY':'fixture'})):
            with self.subTest(enabled=enabled,env=env),patch.dict(os.environ,env,clear=True):
                result=shadow_reviews(research,NOW,self.root,self.out,enabled=enabled,requester=lambda *a:self.fail('no request'))
                self.assertIn(result['status'],('DISABLED','GEMINI_KEY_MISSING','FREE_TIER_NOT_CONFIRMED'))


    def test_even_all_supported_shadow_answers_do_not_create_a_formal_plan(self):
        from daily_runner import prepare_plans
        research={'decision_time_jst':AT.isoformat(),'prepared_at':NOW.isoformat(),'rows':[
                  {'symbol':'AAPL','secondary_status':'二次調査候補'}],
                  'financials':{'AAPL':self.financials},'market_data':{}}
        research['snapshot_sha256']=digest(research)
        def supported(bundle,key):
            value=assessment()
            for item in value['assertions']:
                item['verdict']='SUPPORTED'
                item['citations']=[{'document_id':bundle['documents'][0]['document_id'],'excerpt':TEXT}]
                if item['name']=='next_earnings':item['date']='2026-11-12'
            return validate_assessment(value,bundle)
        with patch.dict(os.environ,{'GEMINI_API_KEY':'fixture','NEXUS_GEMINI_FREE_TIER_CONFIRMED':'true'}):
            shadow_reviews(research,NOW,self.root,self.out,enabled=True,clock=lambda:NOW,requester=supported)
        folder=self.root/'research';folder.mkdir()
        (folder/f'{AT.date().isoformat()}.json').write_text(json.dumps(research))
        plans=prepare_plans(NOW,self.root,self.out)
        self.assertEqual(plans['plans'],[])
        self.assertIn('evidence missing',plans['audit'][0]['reason'])

    def test_probe_without_key_cannot_collect_or_register(self):
        import contextlib
        import gemini_probe
        old=os.getcwd()
        try:
            os.chdir(self.temp.name)
            with patch.dict(os.environ,{},clear=True),patch('gemini_probe.collect_financials',side_effect=AssertionError('no network')):
                with contextlib.redirect_stdout(io.StringIO()):gemini_probe.main()
            result=json.loads(Path('output/gemini_probe.json').read_text())
            self.assertEqual(result['status'],'GEMINI_KEY_MISSING')
            self.assertEqual(result['paper_fills_created'],0)
            self.assertFalse(Path('input/trades.csv').exists())
        finally:os.chdir(old)
