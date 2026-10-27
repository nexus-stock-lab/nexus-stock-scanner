import copy
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import exchange_calendars as xcals
from historical_probe import decision
from strategy_rules import financial_quality, technical_levels, earnings_clear, build_plan, VERSION

AT=decision('2026-10-23T23:45:00+09:00')

def bars():
    cal=xcals.get_calendar('XNYS',start='2026-06-01',end='2026-10-23')
    dates=[d.date().isoformat() for d in cal.sessions if d.date().isoformat()<'2026-10-23'][-60:]
    return [{'date':d,'low':90+i*.01,'high':120+i*.01,'close':100+i*.01} for i,d in enumerate(dates)]

def inputs():
    stamp=(AT-timedelta(hours=1)).isoformat()
    record={'value':True,'source':'https://issuer.example/ir','evidence_sha256':'a'*64,'excerpt':'confirmed',
            'rationale':'Fixture official review','published_at':stamp,'checked_at':stamp}
    review={'symbol':'TEST','rules_version':VERSION,'financial_evidence_sha256':'b'*64,
            'guidance_not_lowered':dict(record),'decline_explanation_verified':dict(record),
            'no_material_business_impairment':dict(record),'next_earnings':{**record,'date':'2026-11-10'}}
    financials={'symbol':'TEST','revenue':110,'prior_revenue':100,'operating_income':-5,
                'prior_operating_income':-10,'evidence_sha256':'b'*64,'checked_at':stamp}
    row={'symbol':'TEST','signal':'反発初動','avg_dollar_volume_20':3_000_000,'close':100}
    return row,bars(),financials,review

class ApprovedRulesTests(unittest.TestCase):
    def test_positive_sales_and_positive_or_improving_income(self):
        self.assertTrue(financial_quality(101,100,1,3)['pass'])
        self.assertTrue(financial_quality(101,100,-1,-3)['pass'])
        self.assertFalse(financial_quality(100,100,1,3)['pass'])
        self.assertFalse(financial_quality(101,100,-4,-3)['pass'])
        self.assertFalse(financial_quality(101,100,0,0)['pass'])
    def test_missing_nonfinite_boolean_financials_rejected(self):
        for v in (float('nan'),float('inf'),True,None):
            with self.subTest(value=v), self.assertRaises((ValueError,TypeError)):
                financial_quality(v,100,1,0)
    def test_twenty_and_sixty_completed_sessions(self):
        data=bars(); data[0]['high']=150; data[0]['close']=110
        data.append({'date':'2026-10-23','low':1,'high':9999,'close':100})
        levels=technical_levels(data,AT)
        self.assertEqual(levels['target_price'],150)
        self.assertAlmostEqual(levels['stop_price'],90.4)
    def test_missing_or_duplicate_daily_bar_blocks(self):
        for data in (bars()[:-1],bars()+[bars()[0]]):
            with self.assertRaises(ValueError): technical_levels(data,AT)
    def test_earnings_three_sessions_and_holiday_weekend(self):
        self.assertFalse(earnings_clear('2026-10-28',AT))
        self.assertTrue(earnings_clear('2026-10-29',AT))
        self.assertFalse(earnings_clear('2026-10-23',AT))
        with self.assertRaises(ValueError): earnings_clear('2026-10-22',AT)
    def test_build_positive_with_complete_evidence(self):
        row,b,f,r=inputs()
        p=build_plan(row,b,f,r,AT,(AT-timedelta(minutes=5)).isoformat(),'fixture://frozen-bars')
        self.assertEqual(p['rules_version'],VERSION)
        self.assertEqual(p['signal'],'反発初動')
        self.assertTrue(all(c['status']=='PASS' for c in p['checks'].values()))
    def test_unknown_or_false_semantic_review_never_passes(self):
        for name in ('guidance_not_lowered','decline_explanation_verified','no_material_business_impairment'):
            row,b,f,r=inputs();r[name]['value']=False
            with self.assertRaises(ValueError):build_plan(row,b,f,r,AT,AT.isoformat(),'fixture')
    def test_changed_financials_require_new_review(self):
        row,b,f,r=inputs();f['evidence_sha256']='c'*64
        with self.assertRaises(ValueError):build_plan(row,b,f,r,AT,AT.isoformat(),'fixture')
    def test_late_plan_and_late_review_are_rejected(self):
        row,b,f,r=inputs()
        with self.assertRaises(ValueError):build_plan(row,b,f,r,AT,(AT+timedelta(seconds=1)).isoformat(),'fixture')
        r['guidance_not_lowered']['checked_at']=(AT+timedelta(seconds=1)).isoformat()
        with self.assertRaises(ValueError):build_plan(row,b,f,r,AT,AT.isoformat(),'fixture')
