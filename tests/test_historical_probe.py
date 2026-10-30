import unittest
from datetime import timedelta
from urllib.error import HTTPError
from historical_probe import decision, timestamp_ns, probe, request_params

AT=decision('2026-10-23T23:45:00+09:00')
NOW=AT+timedelta(minutes=20)


def page(trades=None,token=None):
    return dict(symbol='AAPL',trades=trades if trades is not None else
                [dict(t='2026-10-23T14:44:59.999999999Z',p=100,c=['@'])],next_page_token=token)


class HistoryTests(unittest.TestCase):
    def run_probe(self, pages):
        values=iter(pages)
        return probe('AAPL',AT,NOW,'fixture','fixture',fetch=lambda *args:next(values))
    def test_nanos_retained(self):
        self.assertEqual(timestamp_ns('2026-10-23T14:45:00.000000001Z')-
                         timestamp_ns('2026-10-23T14:45:00Z'),1)
        self.assertEqual(timestamp_ns('2026-10-23T23:45:00+09:00'),timestamp_ns('2026-10-23T14:45:00Z'))
    def test_decision_seconds_and_timezone(self):
        for value in ['2026-10-23T23:45:00','2026-10-23T23:46:00+09:00','2026-10-23T23:45:00.000000001+09:00']:
            with self.subTest(value=value),self.assertRaises(ValueError):decision(value)
    def test_free_delay_and_cutoff(self):
        with self.assertRaises(ValueError):request_params(AT,AT+timedelta(minutes=15),60)
        p=request_params(AT,NOW,60)
        self.assertEqual(p['end'],'2026-10-23T14:45:00Z'); self.assertEqual(p['feed'],'sip')
    def test_missing_credentials_no_network(self):
        result=probe('AAPL',AT,NOW,'','',fetch=lambda *a:self.fail('network called'))
        self.assertEqual(result['status'],'MISSING_CREDENTIALS')
    def test_pagination_and_no_fills(self):
        pages=[page(token='next'),page([dict(t='2026-10-23T14:44:58Z',p=99)])]
        r=self.run_probe(pages);self.assertEqual(r['records'],2)
        self.assertTrue(r['complete_pagination']);self.assertFalse(r['paper_fill_created'])
    def test_bad_prices_symbols_and_future_nanos(self):
        bad=[page([dict(t='2026-10-23T14:45:00.000000001Z',p=100)]),
             page([dict(t='2026-10-23T14:44:59Z',p='nan')]),
             dict(symbol='MSFT',trades=[]),
             page([dict(t='2026-10-23T14:44:59Z',p=True)])]
        for response in bad:
            with self.subTest(response=response):
                self.assertEqual(self.run_probe([response])['status'],'INVALID_RESPONSE')
    def test_repeat_token_and_budget(self):
        self.assertEqual(self.run_probe([page(token='x'),page(token='x')])['status'],'INVALID_RESPONSE')
        r=probe('AAPL',AT,NOW,'fixture','fixture',max_pages=1,fetch=lambda *a:page(token='x'))
        self.assertEqual(r['status'],'INCOMPLETE_PAGINATION')
    def test_errors_do_not_expose_credentials(self):
        def fail(*args):raise HTTPError('https://example.test',403,'secret fixture',None,None)
        r=probe('AAPL',AT,NOW,'sensitive-key','sensitive-secret',fetch=fail)
        self.assertEqual(r['http_status'],403);self.assertNotIn('sensitive',str(r))
    def test_empty_is_not_a_price(self):
        self.assertEqual(self.run_probe([page([])])['status'],'NO_TRADES')
    def test_invalid_symbol_before_request(self):
        with self.assertRaises(ValueError):probe('../orders',AT,NOW,'fixture','fixture')

if __name__=='__main__':unittest.main()
