import copy
import json
import unittest
from datetime import timedelta
from approve_review import approve
from paper_engine import digest
import test_review_intake as fixtures
from test_strategy_rules import AT

class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.ReviewIntakeTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.f.path.unlink()
        research={'financials':{'TEST':self.f.financials}}
        research['snapshot_sha256']=digest(research)
        folder=self.f.root/'research';folder.mkdir()
        (folder/(AT.date().isoformat()+'.json')).write_text(json.dumps(research))
    def draft(self,packet=None):
        draft={'kind':'SOURCE_REVIEW_DRAFT','packet':packet or self.f.packet}
        key=digest(draft);folder=self.f.root/'review_drafts';folder.mkdir(exist_ok=True)
        path=folder/(key+'.json');path.write_text(json.dumps(draft));return key,path
    def test_explicit_approval_records_actor_and_validated_sources_without_fill(self):
        key,_=self.draft();p=approve(key,'owner',self.f.now,self.f.root)
        self.assertEqual(p['reviewer_id'],'owner')
        self.assertEqual(p['reviewed_at'],self.f.now.isoformat())
        self.assertIsNotNone(self.f.load())
        self.assertEqual(approve(key,'owner',self.f.now+timedelta(seconds=1),self.f.root),p)
        self.assertFalse((self.f.root/'trades.csv').exists())
    def test_changed_draft_and_late_approval_leave_inbox_empty(self):
        key,path=self.draft();path.write_text('{}')
        with self.assertRaises(ValueError):approve(key,'owner',self.f.now,self.f.root)
        key,_=self.draft()
        with self.assertRaises(ValueError):approve(key,'owner',AT+timedelta(seconds=1),self.f.root)
        self.assertFalse(self.f.path.exists())
    def test_unverified_assertion_cannot_be_cleared_by_clicking_approve(self):
        packet=copy.deepcopy(self.f.packet);packet['assertions']['decline_explanation_verified']['value']=False
        key,_=self.draft(packet)
        with self.assertRaises(ValueError):approve(key,'owner',self.f.now,self.f.root)
        self.assertFalse(self.f.path.exists())
