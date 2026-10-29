import copy
import os
from unittest.mock import patch
import unittest
from historical_probe import decision
from official_data import extract_financials, quarter_values, accepted_filings, sec_user_agent

AT=decision('2026-10-23T23:45:00+09:00')

def filings():
    return {'filings':{'recent':{'accessionNumber':['a','b','future'],
           'acceptanceDateTime':['2026-08-01T20:00:00Z','2025-08-01T20:00:00Z','2026-10-23T20:00:00Z'],
           'form':['10-Q','10-Q','10-Q'],'reportDate':['2026-06-30','2025-06-30','2026-09-30']}}}

def factrows(current,prior):
    return [{'start':'2026-04-01','end':'2026-06-30','val':current,'accn':'a','form':'10-Q'},
            {'start':'2025-04-01','end':'2025-06-30','val':prior,'accn':'b','form':'10-Q'}]

def facts():
    return {'facts':{'us-gaap':{'Revenues':{'units':{'USD':factrows(110,100)}},
                              'OperatingIncomeLoss':{'units':{'USD':factrows(-5,-10)}}}}}

class OfficialDataTests(unittest.TestCase):
    def test_asof_accepted_filing_no_future_report(self):
        self.assertNotIn('future',accepted_filings(filings(),AT))
        result=extract_financials('TEST',facts(),filings(),AT,AT.isoformat(),'a'*64)
        self.assertEqual(result['period_end'],'2026-06-30')
        self.assertTrue(result['quality']['pass'])
    def test_revenue_not_enough_if_income_deteriorates(self):
        f=facts();f['facts']['us-gaap']['OperatingIncomeLoss']['units']['USD']=factrows(-15,-10)
        r=extract_financials('TEST',f,filings(),AT,AT.isoformat(),'a'*64)
        self.assertFalse(r['quality']['pass'])
    def test_missing_latest_quarter_never_falls_back_to_old(self):
        s=filings();s['filings']['recent']['acceptanceDateTime'][2]='2026-10-23T13:00:00Z'
        with self.assertRaises(ValueError):extract_financials('TEST',facts(),s,AT,AT.isoformat(),'a'*64)
    def test_cumulative_six_month_fact_not_treated_as_quarter(self):
        f=facts();f['facts']['us-gaap']['Revenues']['units']['USD'][0]['start']='2026-01-01'
        with self.assertRaises(ValueError):extract_financials('TEST',f,filings(),AT,AT.isoformat(),'a'*64)
    def test_conflicting_restatement_blocks(self):
        f=facts();rows=f['facts']['us-gaap']['Revenues']['units']['USD'];rows.append({**rows[0],'val':111})
        with self.assertRaises(ValueError):extract_financials('TEST',f,filings(),AT,AT.isoformat(),'a'*64)
    def test_q4_is_annual_minus_nine_months(self):
        units=[{'start':'2025-01-01','end':'2025-12-31','val':450,'accn':'a','form':'10-K'},
               {'start':'2025-01-01','end':'2025-09-30','val':320,'accn':'b','form':'10-Q'}]
        values=quarter_values(units,{'a':{'accepted_at':'2026-02-01T00:00:00Z'},'b':{'accepted_at':'2025-11-01T00:00:00Z'}})
        self.assertEqual(values[0]['val'],130)
        self.assertEqual(values[0]['start'],'2025-10-01')


class SecContactTests(unittest.TestCase):
    def test_missing_contact_blocks_without_inventing_identity(self):
        with patch.dict(os.environ,{},clear=True),self.assertRaisesRegex(ValueError,'SEC contact not configured'):
            sec_user_agent()

    def test_declared_contact_is_preserved(self):
        with patch.dict(os.environ,{'NEXUS_SEC_USER_AGENT':'NEXUS contact@example.org'}):
            self.assertEqual(sec_user_agent(),'NEXUS contact@example.org')

    def test_header_injection_and_url_only_identity_are_rejected_without_echo(self):
        for value in ('NEXUS contact@example.org\r\nX-Header: private',
                      'NEXUS https://github.com/nexus-stock-lab/nexus-stock-scanner'):
            with self.subTest(value=value),patch.dict(os.environ,{'NEXUS_SEC_USER_AGENT':value}):
                with self.assertRaises(ValueError) as error:
                    sec_user_agent()
                self.assertNotIn(value,str(error.exception))


class SourceOnlyAcquisitionTests(unittest.TestCase):
    def test_document_only_acquisition_never_manufactures_financial_quality(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from official_data import collect_company_documents
        from historical_probe import decision
        at=decision('2026-10-29T23:45:00+09:00')
        values={'https://www.sec.gov/files/company_tickers.json':{'0':{'ticker':'APLD','cik_str':1144879}},
                'https://data.sec.gov/submissions/CIK0001144879.json':{'cik':1144879,'filings':{'recent':{
                    'accessionNumber':[],'acceptanceDateTime':[],'form':[],'reportDate':[]}}}}
        with tempfile.TemporaryDirectory() as directory,patch('official_data.collect_documents',return_value=[]):
            result=collect_company_documents('APLD',at,at,Path(directory),fetch=values.__getitem__)
            self.assertEqual(result['financial_status'],'NOT_EVALUATED_SOURCE_REVIEW_ONLY')
            self.assertNotIn('quality',result);self.assertNotIn('revenue',result)
            self.assertEqual(result['cik'],'0001144879')
            values['https://data.sec.gov/submissions/CIK0001144879.json']['cik']=320193
            with self.assertRaisesRegex(ValueError,'SEC entity mismatch'):
                collect_company_documents('APLD',at,at,Path(directory),fetch=values.__getitem__)
