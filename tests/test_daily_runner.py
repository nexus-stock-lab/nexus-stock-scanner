import copy
import csv
import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from daily_runner import freeze_research, reconcile, digest_symbol
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
    def test_incomplete_bars_cannot_freeze_production_research_or_call_sec(self):
        from test_strategy_rules import bars
        folder=self.out/'daily_bars';folder.mkdir()
        (folder/(digest_symbol('TEST')+'.json')).write_text(json.dumps({'bars':bars()[:-1]}))
        with patch('daily_runner.public_json') as fetch:
            with self.assertRaisesRegex(ValueError,'research not frozen'):
                freeze_research(AT-timedelta(hours=1),self.root,self.out,collect_official=True)
            fetch.assert_not_called()
        self.assertFalse((self.root/'research'/'2026-10-23.json').exists())
        status=json.loads((self.out/'preparation_status.json').read_text())
        self.assertEqual(status['status'],'NO_COMPLETE_BAR_HISTORY_RETRY_REQUIRED')
        self.assertEqual(status['rejected'][0]['symbol'],'TEST')
    def test_later_complete_bars_can_retry_without_replacing_a_snapshot(self):
        from test_strategy_rules import bars
        folder=self.out/'daily_bars';folder.mkdir()
        path=folder/(digest_symbol('TEST')+'.json')
        path.write_text(json.dumps({'bars':bars()[:-1]}))
        with self.assertRaisesRegex(ValueError,'research not frozen'):
            freeze_research(AT-timedelta(hours=1),self.root,self.out,collect_official=True)
        path.write_text(json.dumps({'bars':bars()}))
        with patch('daily_runner.public_json',return_value={}), \
             patch('daily_runner.collect_financials',return_value={}), \
             patch('issuer_ir.collect_issuer_ir',return_value={}), \
             patch('daily_runner.update_scenarios'), \
             patch('daily_runner.datetime') as clock:
            clock.now.return_value=AT-timedelta(hours=1)
            snap=freeze_research(AT-timedelta(hours=1),self.root,self.out,collect_official=True)
        self.assertEqual(snap['market_data']['TEST']['bars'],bars())
        self.assertTrue((self.root/'research'/'2026-10-23.json').exists())
    def test_tampered_snapshot_blocks(self):
        value=freeze_research(AT-timedelta(hours=1),self.root,self.out)
        value['rows'][0]['symbol']='CHANGED'
        (self.root/'research'/'2026-10-23.json').write_text(json.dumps(value))
        self.assertEqual(self.collect()['status'],'BLOCKED')
    def test_changed_financial_or_market_evidence_blocks_before_price_fetch(self):
        original=freeze_research(AT-timedelta(hours=1),self.root,self.out)
        for field in ('financials','market_data','issuer_ir'):
            value=copy.deepcopy(original);value[field]['TEST']={'changed':True}
            (self.root/'research'/'2026-10-23.json').write_text(json.dumps(value))
            result=reconcile(AT+timedelta(minutes=16),self.root,self.out,
                fetch_probe=lambda *a:self.fail('must not fetch altered research'),
                calendar=lambda at:{'open':True})
            self.assertEqual(result['status'],'BLOCKED')
            self.assertFalse((self.out/'verified_evidence.json').exists())

    def test_late_snapshot_blocks(self):
        value=freeze_research(AT-timedelta(hours=1),self.root,self.out)
        value['prepared_at']=(AT+timedelta(seconds=1)).isoformat()
        (self.root/'research'/'2026-10-23.json').write_text(json.dumps(value))
        self.assertEqual(self.collect()['status'],'BLOCKED')
    def test_delayed_scheduled_finalize_does_not_write_next_day_or_replace_plans(self):
        from daily_runner import scheduled_finalize
        folder=self.root/'plans';folder.mkdir(parents=True)
        old=folder/'2026-10-23.json';old.write_text('original')
        for stamp in ('2026-10-23T23:46:00+09:00','2026-10-24T00:23:00+09:00'):
            report=scheduled_finalize(AT.fromisoformat(stamp),self.root,self.out)
            self.assertEqual(report['status'],'MISSED_PREDECISION_FINALIZE')
            self.assertEqual(report['decision_time_jst'],AT.isoformat())
            self.assertEqual(old.read_text(),'original')
            self.assertFalse((folder/'2026-10-24.json').exists())
    def test_on_time_scheduled_finalize_keeps_original_decision(self):
        from daily_runner import scheduled_finalize
        freeze_research(AT-timedelta(hours=1),self.root,self.out)
        result=scheduled_finalize(AT-timedelta(minutes=5),self.root,self.out)
        self.assertEqual(result['decision_time_jst'],AT.isoformat())
        self.assertTrue((self.root/'plans'/'2026-10-23.json').exists())

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
