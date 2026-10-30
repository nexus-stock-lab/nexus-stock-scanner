import unittest
from urllib.error import HTTPError

from integration_probe import sec_error_details


class DiagnosticSafetyTests(unittest.TestCase):
    def test_sec_error_removes_query_and_userinfo(self):
        error = HTTPError('https://user:password@www.sec.gov/files/company_tickers.json?token=secret',
                          403, 'Forbidden', {}, None)
        result = sec_error_details(error)
        self.assertEqual(result, {'type': 'HTTPError', 'http_status': 403,
                                 'endpoint': 'https://www.sec.gov/files/company_tickers.json'})

    def test_unrelated_http_error_does_not_expose_url_or_body(self):
        error = HTTPError('https://elsewhere.example/private?key=secret',
                          401, 'secret', {}, None)
        self.assertEqual(sec_error_details(error), {'type': 'HTTPError', 'http_status': 401})
