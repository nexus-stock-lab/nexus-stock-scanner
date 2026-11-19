"""Fictional temporary source reviews; never approves a real issuer."""
import copy
import json
import os
import unittest
from datetime import timedelta
from unittest.mock import patch

from automated_review import run, verify_proof, REVIEWER
from paper_engine import digest
from review_workflow import inventory, write_verified
import test_review_workflow as workflow_fixtures
from test_strategy_rules import AT


class AutomatedReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture=workflow_fixtures.WorkflowTests();self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root=self.fixture.root;record=self.fixture.assessment()
        self.fixture.path.unlink()
        self.document=next((self.root/'official_documents').glob('*.json'))
        doc=json.loads(self.document.read_text())
        self.fixture.financials['official_documents']=[{'status':'ARCHIVED_NOT_REVIEWED',
            'evidence_sha256':self.document.stem,'source':doc['source'],'published_at':doc['published_at']}]
        self.fixture.seal();self.now=AT-timedelta(minutes=5)
        self.tick=0;self.calls=[]
        self.env=patch.dict(os.environ,{'NEXUS_AUTOMATED_REVIEW_ENABLED':'true',
            'GEMINI_API_KEY':'fixture-key','NEXUS_GEMINI_FREE_TIER_CONFIRMED':'true'})
        self.env.start();self.addCleanup(self.env.stop)

    def clock(self):
        self.tick+=1
        return self.now+timedelta(seconds=8*self.tick)

    def request(self,bundle,key,role):
        self.calls.append(role)
        value={'symbol':'TEST','assertions':[{'name':name,'verdict':'SUPPORTED',
            'rationale':'Fictional complete semantic interpretation',
            'date':'2026-11-10' if name=='next_earnings' else '',
            'citations':[{'document_id':self.document.stem,
                          'excerpt':'confirmed next earnings November 10, 2026'}]}
            for name in __import__('gemini_review').NAMES]}
        return {'assessment':value,'provider_response_sha256':digest({'role':role,'value':value})}

    def execute(self,requester=None):
        return run(self.fixture.research,self.now,self.root,requester=requester or self.request,
                   clock=self.clock,sleeper=lambda _:None)

    def test_two_actual_responses_use_existing_linc_intake_and_preserve_ledger(self):
        result=self.execute()
        self.assertEqual(self.calls,['primary','verifier'])
        self.assertEqual(result['results'][0]['status'],'SUPPORTED_PENDING_EXISTING_INTAKE')
        report=inventory(self.fixture.research,AT-timedelta(seconds=1),self.root,promote=True)
        self.assertEqual(report['saved_count'],1)
        packet=json.loads((self.root/'review_inbox'/'2026-10-23'/'TEST.json').read_text())
        self.assertEqual(packet['reviewer_id'],REVIEWER)
        self.assertEqual(packet['kind'],'DELEGATED_SOURCE_REVIEW')
        self.assertTrue(packet['automated_evidence_sha256'])
        from review_intake import load_confirmed_review
        from strategy_rules import build_plan
        from test_strategy_rules import inputs
        from test_paper_engine import fixture as price_fixture
        from paper_engine import register
        row,bars,_,_=inputs()
        review=load_confirmed_review('TEST',self.fixture.financials,AT,
                                    AT-timedelta(seconds=1),self.root,session_scope=True)
        plan=build_plan(row,bars,self.fixture.financials,review,AT,
                        (AT-timedelta(seconds=1)).isoformat(),'fixture://bars')
        quote=price_fixture()[1]
        trades,audit=register([], [plan],[quote],AT+timedelta(hours=1))
        self.assertEqual(len(trades),1)
        self.assertFalse((self.root/'trades.csv').exists())

    def test_disagreement_or_unknown_never_becomes_pass(self):
        def disagree(bundle,key,role):
            r=self.request(bundle,key,role)
            if role=='verifier':r['assessment']['assertions'][0]['verdict']='UNKNOWN'
            return r
        self.execute(disagree)
        r=inventory(self.fixture.research,AT-timedelta(seconds=1),self.root,promote=True)
        self.assertEqual(r['saved_count'],0)
        self.assertEqual(r['counts']['INSUFFICIENT_EVIDENCE'],1)

    def test_changed_proof_and_assertion_cannot_enter_existing_intake(self):
        self.execute()
        path=self.root/'source_assessments'/'2026-10-23'/'TEST.json'
        record=json.loads(path.read_text());record['assertions']['guidance_not_lowered']['rationale']='altered'
        write_verified(path,record)
        r=inventory(self.fixture.research,AT-timedelta(seconds=1),self.root,promote=True)
        self.assertEqual(r['saved_count'],0)

    def test_same_session_cache_avoids_more_network_requests(self):
        self.execute();self.calls=[];self.tick=0
        self.execute()
        self.assertEqual(self.calls,[])

    def test_recollected_receipt_keeps_proof_and_existing_intake_verifiable(self):
        doc=json.loads(self.document.read_text())
        doc['retrieved_at']=(self.now-timedelta(seconds=1)).isoformat()
        sha=digest(doc)
        write_verified(self.root/'official_documents'/(sha+'.json'),doc)
        self.fixture.financials['official_documents'].append({
            **self.fixture.financials['official_documents'][0],'evidence_sha256':sha})
        self.fixture.seal()
        self.execute()
        proof=json.loads(next((self.root/'automated_reviews').glob('*.json')).read_text())
        self.assertEqual(len(proof['bundle']['documents']),1)
        self.assertEqual(inventory(self.fixture.research,AT-timedelta(seconds=1),
                                   self.root,promote=True)['saved_count'],1)

    def test_explicit_codex_review_is_not_overwritten(self):
        original=self.fixture.assessment(None)
        self.execute()
        self.assertEqual(self.calls,[])
        self.assertEqual(json.loads(self.fixture.path.read_text()),original)

    def test_no_free_confirmation_means_no_network_or_approval(self):
        os.environ['NEXUS_GEMINI_FREE_TIER_CONFIRMED']='false'
        self.assertEqual(self.execute()['status'],'FREE_TIER_OR_CREDENTIALS_UNAVAILABLE')
        self.assertEqual(self.calls,[])

    def test_past_session_is_never_rebound(self):
        result=run(self.fixture.research,AT+timedelta(days=3),self.root,
                   requester=lambda *a:self.fail('no past-session requests'))
        self.assertEqual(result['status'],'CURRENT_SESSION_RESEARCH_REQUIRED')

    def test_fabricated_quote_is_held_without_approval(self):
        def invalid(bundle,key,role):
            r=self.request(bundle,key,role)
            r['assessment']['assertions'][0]['citations'][0]['excerpt']='invented unsupported excerpt'
            return r
        self.assertEqual(self.execute(invalid)['results'][0]['status'],'HELD')
        self.assertFalse((self.root/'review_inbox').exists())

    def test_provider_failure_is_paced_before_the_next_issuer(self):
        from gemini_review import ReviewBlocked
        other=copy.deepcopy(self.fixture.financials);other['symbol']='AAA'
        self.fixture.research['financials']['AAA']=other;self.fixture.seal()
        current=[self.now];sleeps=[]
        def sleep(seconds):
            sleeps.append(seconds);current[0]+=timedelta(seconds=seconds)
        def request(bundle,key,role):
            if bundle['symbol']=='AAA':raise ReviewBlocked('GEMINI_HTTP_503')
            return self.request(bundle,key,role)
        result=run(self.fixture.research,self.now,self.root,requester=request,
                   clock=lambda:current[0],sleeper=sleep)
        self.assertEqual(result['results'][0]['reason'],'GEMINI_HTTP_503')
        self.assertEqual(result['results'][1]['status'],'SUPPORTED_PENDING_EXISTING_INTAKE')
        self.assertEqual(sleeps,[7,7,7])
        self.assertEqual(self.calls,['primary','verifier'])
