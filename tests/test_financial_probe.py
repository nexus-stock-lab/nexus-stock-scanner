import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from urllib.error import HTTPError
from financial_probe import check, safe_reason


class FinancialProbeTests(unittest.TestCase):
    def test_verified_numbers_never_become_formal_entry(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); calls=[]
            def collect(symbol,at,now,destination,**kwargs):
                calls.append(symbol)
                return dict(period_end='2026-06-30',prior_period_end='2025-06-30',
                            revenue=110,prior_revenue=100,operating_income=2,
                            prior_operating_income=1,quality={'pass':True},
                            evidence_sha256='a'*64,submissions_sha256='b'*64,
                            source='https://data.sec.gov/example',cik='0000000001')
            r=check(['AAPL','AAPL'],root,fetch=lambda _: {},collect=collect)
            self.assertEqual(calls,['AAPL'])
            self.assertEqual(r['status'],'NUMBERS_VERIFIED')
            self.assertFalse(r['may_register'])
            self.assertEqual(r['paper_fills_created'],0)
            self.assertEqual(r['results'][0]['formal_review_status'],'NOT_EVALUATED')
            self.assertEqual(list(root.iterdir()),[])

    def test_bad_symbol_does_not_block_other_symbols_or_leak_exception(self):
        def collect(symbol,*args,**kwargs):
            if symbol=='APLD':raise ValueError('conflicting reported quarter values; review restatement')
            raise RuntimeError('secret token=do-not-echo')
        r=check(['APLD','NBIS'],fetch=lambda _: {},collect=collect)
        self.assertEqual([x['reason'] for x in r['results']],
                         ['COMPARABLE_QUARTER_CONFLICT','SEC_DATA_UNVERIFIED'])
        self.assertNotIn('do-not-echo',str(r));self.assertFalse(r['may_register'])

    def test_mapping_failure_never_calls_collector(self):
        def fetch(_):raise HTTPError('https://private/?key=secret',403,'secret',{},None)
        def collect(*args,**kwargs):self.fail('collector must not run')
        r=check(['AAPL'],fetch=fetch,collect=collect)
        self.assertEqual(r['reason'],'SEC_HTTP_403')
        self.assertNotIn('secret',str(r))

    def test_invalid_symbols_rejected_before_network(self):
        for symbols in ([],['aapl'],['AAPL?key=secret'],['AAPL\n'],['X'*17],['A'+str(i) for i in range(21)]):
            with self.subTest(symbols=symbols),self.assertRaises(ValueError):
                check(symbols,fetch=lambda _: self.fail('network called'))

    def test_known_failure_and_unknown_exception_are_distinguished_safely(self):
        self.assertEqual(safe_reason(ValueError('newer official results/amendment require financial reconciliation')),
                         'NEW_RESULTS_NEED_RECONCILIATION')
        self.assertEqual(safe_reason(ValueError('private')), 'SEC_DATA_UNVERIFIED')
