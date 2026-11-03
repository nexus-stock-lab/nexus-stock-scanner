"""Explicit review intake tests; fictional records stay in temporary folders."""
import copy
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from paper_engine import digest
from review_intake import load_confirmed_review, NAMES
from strategy_rules import VERSION, build_plan
from test_strategy_rules import AT, inputs


class ReviewIntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.row,self.bars,self.financials,review=inputs()
        self.now=AT-timedelta(minutes=5)
        self.document={'source':'https://www.sec.gov/Archives/edgar/data/1/000000000126000001/review.htm',
            'issuer_symbol':'TEST','issuer_cik':'1','official_source_verified':True,
            'published_at':(AT-timedelta(hours=3)).isoformat(),
            'retrieved_at':(AT-timedelta(hours=2)).isoformat(),
            'text':'confirmed next earnings November 10, 2026'}
        self.packet={'kind':'CONFIRMED_SOURCE_REVIEW','symbol':'TEST','rules_version':VERSION,
            'decision_time_jst':AT.isoformat(),'financial_evidence_sha256':self.financials['evidence_sha256'],
            'reviewed_at':(AT-timedelta(hours=1)).isoformat(),'reviewer_id':'fixture-reviewer',
            'assertions':{name:review[name] for name in NAMES}}
        self.save_document()
        self.path=self.root/'review_inbox'/AT.date().isoformat()/'TEST.json'
        self.path.parent.mkdir(parents=True)
        self.save()
    def tearDown(self):self.temp.cleanup()
    def save_document(self):
        sha=digest(self.document);folder=self.root/'official_documents';folder.mkdir(exist_ok=True)
        (folder/(sha+'.json')).write_text(json.dumps(self.document))
        for r in self.packet['assertions'].values():
            r.update(evidence_sha256=sha,source=self.document['source'],published_at=self.document['published_at'])
    def save(self):self.path.write_text(json.dumps(self.packet))
    def load(self):return load_confirmed_review('TEST',self.financials,AT,self.now,self.root)

    def test_confirmed_review_connects_to_plan_without_ledger_or_network(self):
        r=self.load()
        plan=build_plan(self.row,self.bars,self.financials,r,AT,self.now.isoformat(),'fixture://bars')
        self.assertEqual(plan['official_review']['review_packet_sha256'],digest(self.packet))
        self.assertEqual(r['reviewer_id'],'fixture-reviewer')
        self.assertFalse((self.root/'trades.csv').exists())
        self.assertFalse((self.root/'official_reviews.json').exists())

    def test_raw_ai_shadow_cannot_be_promoted(self):
        self.packet['kind']='SHADOW_ONLY';self.save()
        with self.assertRaisesRegex(ValueError,'explicit semantic confirmation'):self.load()

    def test_all_required_assertions_must_be_confirmed(self):
        original=copy.deepcopy(self.packet)
        for name in NAMES:
            self.packet=copy.deepcopy(original);self.packet['assertions'][name]['value']=False;self.save()
            with self.subTest(name=name),self.assertRaises(ValueError):self.load()
        del self.packet['assertions']['next_earnings'];self.save()
        with self.assertRaisesRegex(ValueError,'all four assertions'):self.load()

    def test_changed_financials_rules_and_decision_are_rejected(self):
        for field,value in (('financial_evidence_sha256','c'*64),('rules_version','old'),
                            ('decision_time_jst',(AT+timedelta(days=1)).isoformat()),('symbol','OTHER')):
            original=self.packet[field];self.packet[field]=value;self.save()
            with self.subTest(field=field),self.assertRaises(ValueError):self.load()
            self.packet[field]=original

    def test_future_review_late_use_and_preacquisition_review_are_rejected(self):
        for delta in (1,-180):
            stamp=(self.now+timedelta(minutes=delta)).isoformat()
            self.packet['reviewed_at']=stamp
            for r in self.packet['assertions'].values():r['checked_at']=stamp
            self.save()
            with self.subTest(delta=delta),self.assertRaises(ValueError):self.load()
        self.setUp_late_packet()
        with self.assertRaises(ValueError):
            load_confirmed_review('TEST',self.financials,AT,AT+timedelta(seconds=1),self.root)
    def setUp_late_packet(self):
        stamp=(AT-timedelta(hours=1)).isoformat();self.packet['reviewed_at']=stamp
        for r in self.packet['assertions'].values():r['checked_at']=stamp
        self.save()

    def test_other_issuer_and_fabricated_official_host_are_rejected(self):
        for source in ('https://www.sec.gov/Archives/edgar/data/2/000000000226000001/review.htm',
                       'https://issuer.example/ir',
                       'https://www.sec.gov/Archives/edgar/data/1/000000000126000001/review.htm?token=x'):
            self.document['source']=source;self.save_document();self.save()
            with self.subTest(source=source),self.assertRaises(ValueError):self.load()

    def test_missing_packet_returns_none_but_duplicate_keys_and_bad_symbols_block(self):
        self.path.unlink();self.assertIsNone(self.load())
        self.path.write_text('{"kind":"SHADOW_ONLY","kind":"CONFIRMED_SOURCE_REVIEW"}')
        with self.assertRaisesRegex(ValueError,'duplicate JSON key'):self.load()
        with self.assertRaisesRegex(ValueError,'symbol invalid'):
            load_confirmed_review('../TEST',self.financials,AT,self.now,self.root)
