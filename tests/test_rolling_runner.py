"""Hourly entries use fictional evidence only, under temporary directories."""
import contextlib
import csv
import io
import json
import unittest
from datetime import date,timedelta,timezone
from unittest.mock import Mock
from pathlib import Path

from entry_schedule import session_slots,next_slot,session_date,plan_path
from rolling_runner import cycle,prepare_next
from daily_runner import prepare_plans,reconcile
from approve_review import approve
from review_intake import load_confirmed_review
from paper_engine import digest,run,register
import test_review_intake as review_fixtures
from test_strategy_rules import AT
from test_price_policy import history


class ScheduleTests(unittest.TestCase):
    def test_dst_midnight_early_close_and_weekend(self):
        winter=session_slots(date(2026,11,5));summer=session_slots(date(2026,7,6))
        self.assertEqual((len(winter),winter[0].hour,winter[-1].hour),(7,23,5))
        self.assertEqual((len(summer),summer[0].hour,summer[-1].hour),(7,22,4))
        self.assertEqual(session_date(winter[-1]),'2026-11-05')
        self.assertEqual(len(session_slots(date(2026,11,27))),4)
        self.assertEqual(session_slots(date(2026,11,7)),[])
        self.assertEqual(session_slots(date(2026,12,25)),[])
    def test_next_slot_never_selects_a_known_past_price(self):
        self.assertEqual(next_slot(AT),AT+timedelta(hours=1))
        self.assertGreater(next_slot(session_slots(date(2026,10,23))[-1]),AT+timedelta(days=2))


class RollingTests(unittest.TestCase):
    def setUp(self):
        self.f=review_fixtures.ReviewIntakeTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.root=self.f.root;self.out=self.root/'output';self.out.mkdir()
        self.f.path.unlink()
        self.expiry=session_slots(AT.date())[-1]
        self.packet={**self.f.packet,'approval_scope':'US_REGULAR_SESSION','decision_time_jst':self.expiry.isoformat()}
        row={**self.f.row,'secondary_status':'二次調査候補'}
        research={'kind':'RESEARCH_ONLY','decision_time_jst':AT.isoformat(),
                  'prepared_at':self.f.now.isoformat(),'rows':[row],'source_sha256':digest([row]),
                  'financials':{'TEST':self.f.financials},'market_data':{'TEST':{
                      'symbol':'TEST','retrieved_at':self.f.now.isoformat(),'bars':self.f.bars}}}
        research['snapshot_sha256']=digest(research)
        folder=self.root/'research';folder.mkdir()
        (folder/(AT.date().isoformat()+'.json')).write_text(json.dumps(research))
    def approve(self):
        draft={'kind':'SOURCE_REVIEW_DRAFT','packet':self.packet};key=digest(draft)
        folder=self.root/'review_drafts';folder.mkdir(exist_ok=True)
        (folder/(key+'.json')).write_text(json.dumps(draft))
        return approve(key,'fixture-owner',self.f.now,self.root)
    def plan(self,at):
        return prepare_plans(self.f.now,self.root,self.out,at=at,rolling=True)
    def test_explicit_daily_approval_survives_jst_midnight_but_not_next_session(self):
        self.approve();at=AT+timedelta(hours=1)
        report=self.plan(at)
        self.assertEqual(len(report['plans']),1)
        self.assertTrue(plan_path(self.root,at).exists())
        self.assertEqual(report['plans'][0]['official_review']['review_packet_sha256'],digest(json.loads(self.f.path.read_text())))
        self.assertIsNone(load_confirmed_review('TEST',self.f.financials,AT+timedelta(days=3),self.f.now,self.root,session_scope=True))
    def test_exact_time_approval_cannot_silently_gain_daily_scope(self):
        self.packet.pop('approval_scope');self.packet['decision_time_jst']=AT.isoformat();self.approve()
        self.assertEqual(self.plan(AT+timedelta(hours=1))['plans'],[])
    def test_changed_financial_hash_or_approval_after_slot_cannot_make_plan(self):
        self.approve()
        self.f.financials['evidence_sha256']='c'*64
        with self.assertRaisesRegex(ValueError,'financial evidence'):
            load_confirmed_review('TEST',self.f.financials,AT,self.f.now,self.root,session_scope=True)
        with self.assertRaises(ValueError):self.plan(self.f.now-timedelta(minutes=1))
    def test_invalid_daily_expiry_is_rejected_without_approval(self):
        self.packet['decision_time_jst']=AT.isoformat()
        with self.assertRaises(ValueError):self.approve()
        self.assertFalse(self.f.path.exists())
    def test_owner_delegated_review_is_truthfully_identified_and_source_checked(self):
        self.approve()
        packet=json.loads(self.f.path.read_text())
        packet.update(kind='DELEGATED_SOURCE_REVIEW',reviewer_id='codex-delegated',
                      delegation_id='owner_20261107_paper_only')
        self.f.path.write_text(json.dumps(packet))
        report=self.plan(AT+timedelta(hours=1))
        self.assertEqual(report['plans'][0]['official_review']['review_kind'],'DELEGATED_SOURCE_REVIEW')
        for key,value in [('delegation_id','unapproved'),('reviewer_id','owner')]:
            bad={**packet,key:value};self.f.path.write_text(json.dumps(bad))
            self.assertEqual(self.plan(AT+timedelta(hours=1))['plans'],[])
        packet['assertions']['decline_explanation_verified']['value']=False
        self.f.path.write_text(json.dumps(packet))
        self.assertEqual(self.plan(AT+timedelta(hours=1))['plans'],[])
    def test_hourly_entry_uses_verified_slot_price_after_midnight(self):
        self.approve();at=AT+timedelta(hours=1);self.plan(at);now=at+timedelta(minutes=20)
        row=dict(t=(at-timedelta(seconds=1)).astimezone(timezone.utc).isoformat(),p=100,c=['@'],z='C',i=1,x='V')
        evidence=history([row]);evidence['decision_time_jst']=at.isoformat()
        result=reconcile(now,self.root,self.out,at=at,rolling=True,fetch_probe=lambda *a:evidence,
            clock=lambda:now,fetch_corporate=lambda *a:{'clear':True},
            disclosure_fetcher=lambda *a:{'status':'NO_NEW_SEC_FILINGS_OBSERVED'})
        self.assertEqual(result['status'],'VERIFIED_INPUT_READY')
        with contextlib.redirect_stdout(io.StringIO()):run(now,root=self.root,out=self.out)
        rows=list(csv.DictReader((self.root/'trades.csv').read_text().splitlines()))
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['entry_time_jst'],at.isoformat())
        self.assertEqual(rows[0]['entry_price'],'100.0')
        incoming=json.loads((self.out/'verified_evidence.json').read_text())
        ledger,audit=register(rows,incoming['plans'],incoming['quotes'],now)
        self.assertEqual(len(ledger),1)
        later={**incoming['plans'][0],'decision_time_jst':(at+timedelta(hours=1)).isoformat()}
        closed=[{**rows[0],'status':'CLOSED'}]
        ledger,audit=register(closed,[later],[],now+timedelta(hours=1))
        self.assertEqual(len(ledger),1)
        self.assertIn('already registered',audit[0]['reason'])
    def test_new_disclosure_and_missing_price_never_create_verified_entry(self):
        self.approve();at=AT+timedelta(hours=1);self.plan(at);now=at+timedelta(minutes=20)
        result=reconcile(now,self.root,self.out,at=at,rolling=True,
            fetch_probe=Mock(side_effect=AssertionError('vetoed plan must not request a price')),
            clock=lambda:now,disclosure_fetcher=lambda *a:{'status':'BLOCKED_NEW_DISCLOSURES'})
        self.assertEqual(result['status'],'NO_VERIFIED_PLANS')
        result=reconcile(now,self.root,self.out,at=at,rolling=True,
            fetch_probe=lambda *a:{'status':'MISSING_CREDENTIALS'},clock=lambda:now,
            disclosure_fetcher=lambda *a:{'status':'NO_NEW_SEC_FILINGS_OBSERVED'})
        self.assertEqual(json.loads((self.out/'verified_evidence.json').read_text())['quotes'],[])
        with contextlib.redirect_stdout(io.StringIO()):run(now,root=self.root,out=self.out)
        self.assertEqual(list(csv.DictReader((self.root/'trades.csv').read_text().splitlines())),[])
    def test_missing_plan_is_not_backfilled_and_completed_slot_is_not_requeried(self):
        self.approve();self.plan(AT)
        now=AT+timedelta(hours=1,minutes=20)
        reconcile_mock=Mock(return_value={'status':'NO_VERIFIED_PLANS'})
        prepare_mock=Mock(return_value={'status':'fixture'})
        cycle(now,self.root,self.out,reconcile_fn=reconcile_mock,prepare_fn=prepare_mock)
        self.assertEqual(reconcile_mock.call_count,1)
        self.assertFalse(plan_path(self.root,AT+timedelta(hours=1)).exists())
        cycle(now,self.root,self.out,reconcile_fn=reconcile_mock,prepare_fn=prepare_mock)
        self.assertEqual(reconcile_mock.call_count,1)
    def test_late_plan_completion_removes_unusable_checkpoint(self):
        self.approve()
        with self.assertRaisesRegex(ValueError,'completed after checkpoint'):
            prepare_next(self.f.now,self.root,self.out,clock=lambda:AT+timedelta(seconds=1))
        self.assertFalse(plan_path(self.root,AT).exists())
