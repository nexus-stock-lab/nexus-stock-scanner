import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from official_data import MAX_SEC_DOCUMENT_BYTES, collect_documents


class SecExhibitRowsTests(unittest.TestCase):
    def collect(self, main, attachments=None):
        at = datetime.fromisoformat('2026-11-18T23:45:00+09:00')
        base = 'https://www.sec.gov/Archives/edgar/data/1/000000000126000001/'
        recent = dict(accessionNumber=['0000000001-26-000001'],
                      acceptanceDateTime=[(at-timedelta(days=1)).isoformat()],
                      form=['8-K'], primaryDocument=['report.htm'], items=['2.02'])
        calls = []
        bodies = {'report.htm': main, **(attachments or {})}

        class Response:
            def __init__(self, url): self.url = url
            def read(self, limit): return bodies[self.url.removeprefix(base)][:limit]
            def __enter__(self): return self
            def __exit__(self, *args): pass

        def opening(request, timeout):
            calls.append(request.full_url)
            return Response(request.full_url)

        with tempfile.TemporaryDirectory() as folder, \
                patch('official_data.sec_user_agent', return_value='fixture'), \
                patch('official_data.sec_open', side_effect=opening), \
                patch('official_data.time.sleep'):
            root = Path(folder)
            result = collect_documents({'filings': {'recent': recent}}, '1', at, at,
                                       root, symbol='TEST', clock=lambda: at)
            docs = [json.loads(p.read_text()) for p in (root/'official_documents').glob('*.json')] if (root/'official_documents').exists() else []
        return calls, result, docs

    def test_separate_exhibit_number_cell_collects_descriptive_link_only(self):
        html = b'''<table><tr><td>99.<span>1</span></td><td>
        <a href="a3q2026earningsannouncement.htm">Press release announcing results</a>
        </td></tr><tr><td>104</td><td><a href="cover.htm">Cover</a></td></tr>
        <tr><td>99.10</td><td><a href="other.htm">Other</a></td></tr></table>'''
        calls, result, docs = self.collect(html, {'a3q2026earningsannouncement.htm': b'<p>Full release</p>'})
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1].endswith('a3q2026earningsannouncement.htm'))
        self.assertEqual(len(docs), 2)
        self.assertTrue(all(x['status'] == 'ARCHIVED_NOT_REVIEWED' for x in result))

    def test_complete_document_over_old_four_megabyte_limit_is_archived(self):
        body = b'<p>' + b'x' * 4_150_000 + b' verified ending</p>'
        _, result, docs = self.collect(body)
        self.assertEqual(result[0]['status'], 'ARCHIVED_NOT_REVIEWED')
        self.assertTrue(docs[0]['text'].endswith('verified ending'))

    def test_new_hard_limit_still_rejects_without_truncated_archive(self):
        _, result, docs = self.collect(b'x' * (MAX_SEC_DOCUMENT_BYTES + 1))
        self.assertEqual(result[0]['status'], 'UNVERIFIED')
        self.assertEqual(docs, [])

    def test_exhibit_row_cannot_escape_same_filing(self):
        html = b'<tr><td>99.1</td><td><a href="https://example.com/release.htm">Press release</a></td></tr>'
        calls, _, _ = self.collect(html)
        self.assertEqual(len(calls), 1)
