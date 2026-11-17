"""Regression coverage for delayed prepare, completed candles, and gap reporting."""
import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import exchange_calendars as xcals
import pandas as pd

from daily_runner import daily_bar_gaps, digest_symbol, freeze_research
from entry_schedule import next_slot, session_date
from scanner import completed_daily_prices
from strategy_rules import technical_levels


class CompletedSessionTests(unittest.TestCase):
    def test_partial_regular_session_excluded_but_closed_session_retained(self):
        prices=pd.DataFrame({'Close':[10,11,12]},index=pd.to_datetime(['2026-11-13','2026-11-16','2026-11-17']))
        for stamp,expected in [('2026-11-16T20:59:59+00:00',['2026-11-13']),
                               ('2026-11-16T21:00:00+00:00',['2026-11-13','2026-11-16'])]:
            with self.subTest(stamp=stamp):
                actual=completed_daily_prices(prices,datetime.fromisoformat(stamp))
                self.assertEqual([d.date().isoformat() for d in actual.index],expected)

    def test_early_close_and_dst_use_calendar_not_fixed_utc_hour(self):
        for day,before,after in [('2026-11-27','17:59:59','18:00:00'),
                                 ('2026-07-02','19:59:59','20:00:00')]:
            prices=pd.DataFrame({'Close':[10]},index=pd.to_datetime([day]).tz_localize('America/New_York'))
            self.assertTrue(completed_daily_prices(prices,datetime.fromisoformat(day+'T'+before+'+00:00')).empty)
            self.assertEqual(len(completed_daily_prices(prices,datetime.fromisoformat(day+'T'+after+'+00:00'))),1)

    def test_non_session_today_is_not_accepted(self):
        prices=pd.DataFrame({'Close':[10,11]},index=pd.to_datetime(['2026-11-13','2026-11-14']))
        actual=completed_daily_prices(prices,datetime.fromisoformat('2026-11-14T23:00:00+00:00'))
        self.assertEqual(len(actual),1)


class DelayedPrepareTests(unittest.TestCase):
    def test_midnight_jst_targets_existing_future_v3_slot_in_same_ny_session(self):
        # #108 started after JST midnight but while NY November 16 was open.
        now=datetime.fromisoformat('2026-11-17T04:18:07+09:00')
        at=next_slot(now)
        self.assertEqual(at.isoformat(),'2026-11-17T04:45:00+09:00')
        cal=xcals.get_calendar('XNYS',start='2026-06-01',end='2026-11-16')
        dates=[s.date().isoformat() for s in cal.sessions if s.date()<at.astimezone(__import__('zoneinfo').ZoneInfo('America/New_York')).date()][-60:]
        bars=[{'date':d,'low':9,'close':10,'high':11} for d in dates]
        self.assertEqual(technical_levels(bars,at)['price_date'],'2026-11-13')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'input';out=Path(tmp)/'output';out.mkdir()
            with (out/'secondary_review.csv').open('w') as f:
                writer=csv.DictWriter(f,fieldnames=['symbol','secondary_status'])
                writer.writeheader();writer.writerow({'symbol':'TEST','secondary_status':'二次調査候補'})
            folder=out/'daily_bars';folder.mkdir()
            (folder/(digest_symbol('TEST')+'.json')).write_text(json.dumps({'bars':bars}))
            with patch('daily_runner.public_json',return_value={}),patch('daily_runner.collect_financials',return_value={}),patch('issuer_ir.collect_issuer_ir',return_value={}),patch('daily_runner.update_scenarios'),patch('daily_runner.datetime') as clock:
                clock.now.return_value=now
                snap=freeze_research(now,root,out,collect_official=True)
            self.assertEqual(snap['decision_time_jst'],at.isoformat())
            self.assertTrue((root/'research'/'2026-11-16.json').exists())
            self.assertFalse((root/'research'/'2026-11-17.json').exists())

    def test_missing_dates_reported_without_relaxing_validation(self):
        at=datetime.fromisoformat('2026-11-17T23:45:00+09:00')
        cal=xcals.get_calendar('XNYS',start='2026-06-01',end='2026-11-17')
        dates=[s.date().isoformat() for s in cal.sessions if s.date().isoformat()<'2026-11-17'][-60:]
        bars=[{'date':d,'low':9,'close':10,'high':11} for d in dates if d not in ('2026-11-16','2026-09-01')]
        gaps=daily_bar_gaps(bars,at)
        self.assertEqual(gaps['missing_20_sessions'],['2026-11-16'])
        self.assertEqual(gaps['missing_60_sessions'],['2026-09-01','2026-11-16'])
        with self.assertRaisesRegex(ValueError,'no missing bars'):
            technical_levels(bars,at)

    def test_after_close_and_weekend_skip_to_next_valid_session(self):
        for stamp,expected in [('2026-11-17T21:01:00+00:00','2026-11-18'),
                               ('2026-11-21T12:00:00+00:00','2026-11-23')]:
            now=datetime.fromisoformat(stamp)
            self.assertEqual(session_date(next_slot(now)),expected)
            self.assertGreater(next_slot(now),now)
