"""Scenario monitoring regressions; all evidence/positions are fictional and temporary."""
import csv
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from daily_runner import update_scenarios
from paper_engine import digest
from strategy_rules import VERSION
from test_price_policy import AT


class ScenarioMonitoringTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)/'input';self.root.mkdir()
        self.now=AT+timedelta(days=1)
        self.trade={'trade_id':'TEST-entry','symbol':'TEST','entry_time_jst':AT.isoformat(),'status':'OPEN'}
        self.save_ledger()

    def tearDown(self):
        self.temp.cleanup()

    def save_ledger(self):
        with (self.root/'trades.csv').open('w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=list(self.trade));w.writeheader();w.writerow(self.trade)

    def negative_review(self,checked,name='guidance_not_lowered'):
        document={'source':'https://issuer.example/ir','text':'Revenue guidance lowered. Material impairment.',
                  'official_source_verified':True,'retrieved_at':(checked-timedelta(minutes=1)).isoformat(),
                  'published_at':(checked-timedelta(minutes=2)).isoformat()}
        sha=digest(document);folder=self.root/'official_documents';folder.mkdir(exist_ok=True)
        (folder/f'{sha}.json').write_text(json.dumps(document))
        return {'symbol':'TEST','rules_version':VERSION,name:{'value':False,'source':document['source'],
                'evidence_sha256':sha,'excerpt':document['text'],'rationale':'Source-backed fixture review',
                'published_at':document['published_at'],'checked_at':checked.isoformat()}}

    def financial_failure(self):
        document={'source':'https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json',
                  'retrieved_at':self.now.isoformat(),'payload':{'fixture':True}}
        sha=digest(document);folder=self.root/'official_evidence';folder.mkdir()
        (folder/f'{sha}.json').write_text(json.dumps(document))
        return {'TEST':{'symbol':'TEST','quality':{'pass':False},'source':document['source'],
                       'evidence_sha256':sha,'published_at':(self.now-timedelta(minutes=2)).isoformat(),
                       'checked_at':self.now.isoformat(),'revenue':99,'prior_revenue':100,
                       'operating_income':-2,'prior_operating_income':-1}}

    def save_reviews(self,reviews):
        (self.root/'official_reviews.json').write_text(json.dumps(reviews))

    def event(self):
        return json.loads((self.root/'scenario_exits.json').read_text())['TEST-entry']

    def test_pre_entry_review_cannot_erase_new_financial_failure(self):
        self.save_reviews({'old':{'TEST':self.negative_review(AT-timedelta(days=1))}})
        update_scenarios(self.financial_failure(),self.now,self.root)
        self.assertEqual(self.event()['trigger'],'QUARTERLY_FINANCIALS')
        self.assertEqual(self.event()['checked_at'],self.now.isoformat())

    def test_earliest_post_entry_confirmation_independent_of_review_order(self):
        first=AT+timedelta(hours=1);last=AT+timedelta(hours=2)
        for order in ((first,last),(last,first)):
            with self.subTest(order=order):
                (self.root/'scenario_exits.json').unlink(missing_ok=True)
                self.save_reviews({str(i):{'TEST':self.negative_review(t)} for i,t in enumerate(order)})
                update_scenarios({},self.now,self.root)
                self.assertEqual(self.event()['checked_at'],first.isoformat())

    def test_material_business_impairment_triggers_exit(self):
        self.save_reviews({'new':{'TEST':self.negative_review(AT+timedelta(hours=1),'no_material_business_impairment')}})
        update_scenarios({},self.now,self.root)
        self.assertEqual(self.event()['trigger'],'no_material_business_impairment')

    def test_unknown_or_unarchived_review_never_triggers_exit(self):
        review=self.negative_review(AT+timedelta(hours=1))
        review['guidance_not_lowered']['evidence_sha256']='0'*64
        self.save_reviews({'new':{'TEST':review}})
        audit=update_scenarios({},self.now,self.root)
        self.assertEqual(json.loads((self.root/'scenario_exits.json').read_text()),{})
        self.assertEqual(audit[0]['status'],'UNVERIFIED_REVIEW_EXIT')

    def test_corrupt_financial_source_never_triggers_exit(self):
        financials=self.financial_failure()
        (self.root/'official_evidence'/f"{financials['TEST']['evidence_sha256']}.json").write_text('{}')
        audit=update_scenarios(financials,self.now,self.root)
        self.assertEqual(json.loads((self.root/'scenario_exits.json').read_text()),{})
        self.assertEqual(audit[0]['status'],'UNVERIFIED_FINANCIAL_EXIT')

    def test_future_review_never_triggers_exit(self):
        self.save_reviews({'future':{'TEST':self.negative_review(self.now+timedelta(hours=1))}})
        update_scenarios({},self.now,self.root)
        self.assertEqual(json.loads((self.root/'scenario_exits.json').read_text()),{})

    def test_existing_trigger_preserved_on_repeated_monitoring(self):
        financials=self.financial_failure()
        update_scenarios(financials,self.now,self.root)
        original=self.event()
        update_scenarios(financials,self.now+timedelta(hours=1),self.root)
        self.assertEqual(self.event(),original)
