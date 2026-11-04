import unittest
from datetime import datetime
from strategy_rules import earnings_review

class RecentEarningsTests(unittest.TestCase):
    def record(self):
        return {'value':True,'source':'https://www.sec.gov/fixture','evidence_sha256':'a'*64,
            'excerpt':'Results announced October 28, 2026','rationale':'Human checked latest IR; next date unannounced',
            'published_at':'2026-10-28T21:00:00Z','checked_at':'2026-11-05T10:00:00Z',
            'date':'','next_date_unannounced':True,'last_earnings_date':'2026-10-28'}
    def test_explicit_recent_results_no_invented_next_date(self):
        result=earnings_review(self.record(),datetime.fromisoformat('2026-11-05T23:45:00+09:00'))
        self.assertEqual(result['elapsed_calendar_days'],8)
        self.assertIsNone(result['next_earnings_date'])
    def test_30_day_boundary_and_31_day_rejection(self):
        self.assertEqual(earnings_review(self.record(),datetime.fromisoformat('2026-11-27T23:45:00+09:00'))['elapsed_calendar_days'],30)
        with self.assertRaises(ValueError):earnings_review(self.record(),datetime.fromisoformat('2026-11-28T23:45:00+09:00'))
    def test_no_missing_confirmation_or_fabricated_date(self):
        at=datetime.fromisoformat('2026-11-05T23:45:00+09:00')
        for change in ({'next_date_unannounced':False},{'last_earnings_date':'2026-11-01'},
                       {'last_earnings_date':'2026-11-06'},{'date':'2026-11-10'}):
            with self.subTest(change=change),self.assertRaises((ValueError,TypeError)):
                earnings_review({**self.record(),**change},at)
    def test_known_earnings_still_excludes_three_sessions(self):
        r={**self.record(),'date':'2026-11-10','next_date_unannounced':False,'excerpt':'Next earnings November 10, 2026'}
        with self.assertRaises(ValueError):earnings_review(r,datetime.fromisoformat('2026-11-05T23:45:00+09:00'))
