import copy
import csv
import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from daily_runner import freeze_research, reconcile
from historical_probe import decision
from market_calendar import latest_due_decision, next_decision, session_at
from test_price_policy import history

AT = decision('2026-10-23T23:45:00+09:00')


class CalendarTests(unittest.TestCase):
    def test_due_rollover_at_0001(self):
        self.assertEqual(latest_due_decision(AT+timedelta(minutes=16)), AT)
        self.assertEqual(latest_due_decision(AT+timedelta(minutes=15)), AT-timedelta(days=1))
        self.assertEqual(next_decision(AT+timedelta(seconds=1)), AT+timedelta(days=1))
    def test_holiday_and_weekend(self):
        for date in ('2026-10-24','2026-12-25','2026-11-26'):
            self.assertFalse(session_at(decision(date+'T23:45:00+09:00'))['open'])
    def test_dst_and_early_close(self):
        summer = session_at(decision('2026-10-23T23:45:00+09:00'))
        winter = session_at(decision('2026-11-27T23:45:00+09:00'))
        self.assertIn('13:30',summer['market_open'])
        self.assertIn('14:30',winter['market_open'])
        self.assertIn('18:00',winter['market_close'])


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)/'input'
        self.out = Path(self.temp.name)/'output'
        self.out.mkdir()
        with (self.out/'secondary_review.csv').open('w', newline='') as f:
            w = csv.DictWriter(f,fieldnames=['symbol','secondary_status'])
            w.writeheader(); w.writerow({'symbol':'TEST','secondary_status':'二次調査候補'})
    def tearDown(self):
        self.temp.cleanup()
    def collect(self, **kw):
        return reconcile(AT+timedelta(minutes=16), self.root, self.out,
                         fetch_probe=lambda *args:copy.deepcopy(history()),
                         calendar=lambda at:{'open':True}, **kw)
    def test_freeze_is_immutable_and_quotes_do_not_create_entries(self):
        snap = freeze_research(AT-timedelta(hours=1),self.root,self.out)
        (self.out/'secondary_review.csv').write_text('bad')
        self.assertEqual(freeze_research(AT,self.root,self.out),snap)
        result = self.collect()
        self.assertEqual(result['status'],'NO_VERIFIED_PLANS')
        self.assertEqual(result['prices'][0]['price'],100)
        self.assertIsNone(result['registered'])
        self.assertEqual(json.loads((self.out/'verified_evidence.json').read_text())['plans'],[])
        self.assertEqual(len(list((self.root/'market_evidence').glob('*.json'))),1)
    def test_no_snapshot_never_backfills(self):
        (self.out/'verified_evidence.json').write_text('{}')
        result = self.collect()
        self.assertEqual(result['status'],'NO_PREDECISION_RESEARCH')
        self.assertFalse((self.out/'verified_evidence.json').exists())
    def test_tampered_snapshot_blocks(self):
        value=freeze_research(AT-timedelta(hours=1),self.root,self.out)
        value['rows'][0]['symbol']='CHANGED'
        (self.root/'research'/'2026-10-23.json').write_text(json.dumps(value))
        self.assertEqual(self.collect()['status'],'BLOCKED')
    def test_late_snapshot_blocks(self):
        value=freeze_research(AT-timedelta(hours=1),self.root,self.out)
        value['prepared_at']=(AT+timedelta(seconds=1)).isoformat()
        (self.root/'research'/'2026-10-23.json').write_text(json.dumps(value))
        self.assertEqual(self.collect()['status'],'BLOCKED')
    def test_closed_session_does_not_call_api(self):
        result=reconcile(AT+timedelta(days=1,minutes=16),self.root,self.out,
                         fetch_probe=lambda *args:self.fail('must not fetch'),calendar=lambda at:{'open':False})
        self.assertEqual(result['status'],'NON_TRADING_SESSION')
        self.assertTrue((self.out/'daily_status.json').exists())
    def test_missing_credentials_visible(self):
        freeze_research(AT-timedelta(hours=1),self.root,self.out)
        result=reconcile(AT+timedelta(minutes=16),self.root,self.out,
                         fetch_probe=lambda *args:{'status':'MISSING_CREDENTIALS'}, calendar=lambda at:{'open':True})
        self.assertEqual(result['prices'][0]['status'],'MISSING_CREDENTIALS')


if __name__ == '__main__':
    unittest.main()
