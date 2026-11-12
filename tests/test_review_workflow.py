"""Isolated workflow tests. No real issuer is approved by these fixtures."""
import copy
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from paper_engine import digest
from review_workflow import inventory, write_verified
from review_intake import NAMES, DELEGATION
from strategy_rules import VERSION
from test_strategy_rules import AT, inputs


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.now=AT-timedelta(minutes=5)
        self.financials=inputs()[2]
        self.financials.update(symbol='TEST',cik='1',quality={'pass':True})
        self.research={'decision_time_jst':AT.isoformat(),'financials':{'TEST':self.financials},'rows':[]}
        self.seal()
    def tearDown(self): self.temp.cleanup()
    def seal(self):
        self.research['snapshot_sha256']=digest({k:v for k,v in self.research.items() if k!='snapshot_sha256'})
    def assessment(self, value=True):
        doc={'source':'https://www.sec.gov/Archives/edgar/data/1/000000000126000001/review.htm',
             'issuer_symbol':'TEST','issuer_cik':'1','official_source_verified':True,
             'published_at':(AT-timedelta(hours=3)).isoformat(),
             'retrieved_at':(AT-timedelta(hours=2)).isoformat(),
             'text':'confirmed next earnings November 10, 2026'}
        sha=digest(doc)
        write_verified(self.root/'official_documents'/(sha+'.json'),doc)
        assertions=copy.deepcopy({n:inputs()[3][n] for n in NAMES})
        stamp=(AT-timedelta(hours=1)).isoformat()
        for item in assertions.values():
            item.update(value=value,source=doc['source'],evidence_sha256=sha,
                        published_at=doc['published_at'],checked_at=stamp,rationale='Fixture explicit semantic review')
        record={'kind':'SOURCE_ASSESSMENT','symbol':'TEST','rules_version':VERSION,
                'research_sha256':self.research['snapshot_sha256'],
                'financial_evidence_sha256':self.financials['evidence_sha256'],
                'reviewed_at':stamp,'reviewer_id':'codex-delegated','delegation_id':DELEGATION,
                'assertions':assertions}
        day=AT.astimezone(__import__('zoneinfo').ZoneInfo('America/New_York')).date().isoformat()
        self.path=self.root/'source_assessments'/day/'TEST.json'
        write_verified(self.path,record)
        return record
    def run_report(self,promote=False,now=None):
        return inventory(self.research,now or self.now,self.root,promote)
    def test_all_financial_passes_including_missing_rows_are_inventoried(self):
        for s in ('AAA','ZZZ'):
            self.research['financials'][s]={**self.financials,'symbol':s}
        self.seal();r=self.run_report()
        self.assertEqual(r['financial_pass_count'],3)
        self.assertEqual(r['counts']['INSUFFICIENT_EVIDENCE'],3)
    def test_unknown_never_creates_an_approval(self):
        self.assessment(None);r=self.run_report(True)
        self.assertEqual(r['counts']['INSUFFICIENT_EVIDENCE'],1)
        self.assertFalse((self.root/'review_inbox').exists())
    def test_supported_false_is_fail_without_approval(self):
        self.assessment(False);r=self.run_report(True)
        self.assertEqual(r['counts']['FAIL'],1)
        self.assertEqual(r['saved_count'],0)
    def test_explicit_valid_pass_saves_exact_linc_schema_and_reads_back(self):
        self.assessment();r=self.run_report(True)
        self.assertEqual(r['saved_count'],1)
        self.assertEqual(r['local_readback_count'],1)
        self.assertEqual(r['remote_verified_count'],0)
        packet=json.loads(Path(r['results'][0]['saved_path']).read_text())
        self.assertEqual(packet['kind'],'DELEGATED_SOURCE_REVIEW')
        self.assertNotIn('research_sha256',packet)
        self.assertEqual(packet['reviewed_at'],(AT-timedelta(hours=1)).isoformat())
        self.assertFalse((self.root/'trades.csv').exists())
    def test_broken_issuer_does_not_block_next_issuer(self):
        self.research['financials']['AAA']={'symbol':'AAA','quality':{'pass':True}}
        self.seal();self.assessment();r=self.run_report(True)
        self.assertEqual([x['status'] for x in r['results']],['INSUFFICIENT_EVIDENCE','PASS'])
    def test_old_research_binding_and_shadow_cannot_promote(self):
        for field,value in (('research_sha256','x'*64),('kind','SHADOW_ONLY'),('reviewer_id','gemini')):
            record=self.assessment();record[field]=value;write_verified(self.path,record)
            self.assertEqual(self.run_report(True)['saved_count'],0)
    def test_fabricated_evidence_and_future_time_are_rejected(self):
        record=self.assessment();record['assertions'][NAMES[0]]['evidence_sha256']='a'*64
        write_verified(self.path,record)
        self.assertEqual(self.run_report(True)['counts']['INSUFFICIENT_EVIDENCE'],1)
        record=self.assessment();record['reviewed_at']=(AT+timedelta(hours=1)).isoformat()
        write_verified(self.path,record)
        self.assertEqual(self.run_report(True)['saved_count'],0)
    def test_no_retrospective_promotion(self):
        self.assessment();r=self.run_report(True,AT+timedelta(minutes=1))
        self.assertEqual(r['saved_count'],0)
        self.assertIn('past decision',r['results'][0]['reasons'][0])
    def test_tampered_snapshot_is_rejected(self):
        self.research['rows']=[{'symbol':'INJECTED'}]
        with self.assertRaisesRegex(ValueError,'proof mismatch'):self.run_report()

    def test_connected_collection_covers_all_passes_and_isolates_sec_failure(self):
        from review_workflow import connected_run
        self.research['financials']['AAA']={**self.financials,'symbol':'AAA'}
        self.seal()
        research_path=self.root/'research.json';write_verified(research_path,self.research)
        calls=[]
        def sec(symbol,at,now,root):
            calls.append(('SEC',symbol))
            if symbol=='AAA':raise OSError('fictional outage')
            return {'cik':'1','official_documents':[]}
        def ir(symbol,at,now,root):
            calls.append(('IR',symbol));return {'documents':[], 'status':'IR_SOURCE_NOT_CONFIGURED'}
        ledger=self.root/'trades.csv';ledger.write_text('protected fixture')
        result=connected_run(research_path,self.now,self.root,self.root/'output',collect=True,
                             clock=lambda:self.now,sec_collector=sec,ir_collector=ir)
        self.assertEqual(len(calls),4)
        self.assertEqual(result['review']['financial_pass_count'],2)
        self.assertEqual(result['review']['counts']['INSUFFICIENT_EVIDENCE'],2)
        self.assertEqual(result['review']['saved_count'],0)
        self.assertEqual(ledger.read_text(),'protected fixture')
        pending=json.loads(next((self.root/'source_assessments').rglob('AAA.json')).read_text())
        self.assertIsNone(pending['reviewed_at'])
        self.assertEqual(pending['reviewer_id'],'unassigned')
        self.assertEqual(set(pending['assertions']),set(NAMES))

    def test_connected_explicit_review_is_preserved_and_promoted(self):
        from review_workflow import connected_run
        self.assessment();original=self.path.read_bytes()
        path=self.root/'research.json';write_verified(path,self.research)
        result=connected_run(path,self.now,self.root,self.root/'output',collect=True,
                             clock=lambda:self.now,
                             sec_collector=lambda *a:{'cik':'1','official_documents':[]},
                             ir_collector=lambda *a:{'documents':[]})
        self.assertEqual(result['review']['saved_count'],1)
        self.assertEqual(self.path.read_bytes(),original)
        from review_intake import load_confirmed_review
        actual=load_confirmed_review('TEST',self.financials,AT,self.now,self.root)
        self.assertEqual(actual['reviewer_id'],'codex-delegated')

    def test_past_collection_never_fetches_or_manufactures_current_approval(self):
        from review_workflow import connected_run
        path=self.root/'research.json';write_verified(path,self.research)
        result=connected_run(path,AT+timedelta(minutes=1),self.root,self.root/'out',collect=True,
                             clock=lambda:AT+timedelta(minutes=1),
                             sec_collector=lambda *a:self.fail('past SEC collection'),
                             ir_collector=lambda *a:self.fail('past IR collection'))
        self.assertIn('PAST_DECISION',result['collection']['results'][0]['errors'][0])
        self.assertEqual(result['review']['saved_count'],0)

    def test_unsigned_record_cannot_smuggle_supported_assertions(self):
        record=self.assessment();record.update(reviewer_id='unassigned',reviewed_at=None)
        write_verified(self.path,record)
        self.assertEqual(self.run_report(True)['saved_count'],0)
        self.assertIn('unsigned',self.run_report()['results'][0]['reasons'][0])


if __name__=='__main__': unittest.main()
