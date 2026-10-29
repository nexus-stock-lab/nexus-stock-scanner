"""Fictional IR fixtures, no network, no ledger or semantic PASS."""
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from historical_probe import decision
from issuer_ir import REGISTRY,LIMIT,IRBlocked,allowed,fetch,Page,publication,collect_issuer_ir,verify_document

AT=decision('2026-10-29T23:45:00+09:00');NOW=AT-timedelta(hours=1)
INDEX=REGISTRY['APLD']['index'];ARTICLE=REGISTRY['APLD']['prefix']+'detail/999/fixture'
HTML=b'<html><meta property="article:published_time" content="2026-10-28T16:00:00-04:00"><body>Fictional revenue guidance reaffirmed.</body></html>'
class Reply:
    def __init__(self,url,raw):self.url=url;self.raw=raw
    def read(self,limit):return self.raw[:limit]
    def __enter__(self):return self
    def __exit__(self,*args):pass
class IRTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def run_fixture(self,article=HTML,index=None):
        index=index or ('<a href="'+ARTICLE+'">News</a>').encode()
        def transport(req,timeout):
            self.assertNotIn('x-goog-api-key',dict(req.header_items()))
            self.assertEqual(req.get_method(),'GET')
            return Reply(req.full_url,index if req.full_url==INDEX else article)
        return collect_issuer_ir('APLD',AT,NOW,self.root,transport,clock=lambda:NOW)
    def test_verified_issuer_page_archived_without_promoting(self):
        r=self.run_fixture();self.assertEqual(len(r['documents']),1)
        sha=r['documents'][0]['evidence_sha256'];d=json.loads((self.root/'official_documents'/f'{sha}.json').read_text())
        self.assertTrue(verify_document('APLD',d));self.assertFalse(verify_document('IREN',d))
        self.assertFalse(r['may_register']);self.assertFalse(r['coverage_complete'])
        self.assertEqual(r['documents'][0]['status'],'ARCHIVED_NOT_REVIEWED')
        self.assertFalse((self.root/'trades.csv').exists())
    def test_no_publication_time_never_uses_retrieval_time_as_publication(self):
        r=self.run_fixture(b'<body>Undated news</body>')
        self.assertFalse(r['documents']);self.assertEqual(r['audit'][0]['reason'],'IR_PUBLICATION_TIME_UNKNOWN')
    def test_future_and_conflicting_dates_hold(self):
        for body in (HTML.replace(b'2026-10-28',b'2027-10-28'),HTML.replace(b'<body>',b'<meta name="date" content="2026-10-27"><body>')):
            self.assertFalse(self.run_fixture(body)['documents'])
    def test_unknown_symbols_do_not_make_a_network_request(self):
        r=collect_issuer_ir('OTHER',AT,NOW,self.root,lambda *a,**kw:self.fail('no request'))
        self.assertEqual(r['status'],'IR_SOURCE_NOT_CONFIGURED')
    def test_foreign_hosts_credentials_ports_queries_and_traversal_rejected(self):
        for url in ('https://evil.example/news','http://ir.applieddigital.com/news-events/press-releases/a',
                    ARTICLE+'?key=private',ARTICLE+'#a',REGISTRY['APLD']['prefix']+'../secret',
                    'https://u:p@ir.applieddigital.com/news-events/press-releases/a',
                    'https://ir.applieddigital.com:443/news-events/press-releases/a'):
            self.assertFalse(allowed(url,REGISTRY['APLD']))
    def test_redirect_oversize_and_pdf_are_not_silently_accepted(self):
        for reply in (Reply('https://evil.example/',b'x'),Reply(ARTICLE,b'x'*(LIMIT+1)),Reply(ARTICLE,b'%PDF-1.7')):
            with self.assertRaises(IRBlocked):fetch(ARTICLE,REGISTRY['APLD'],lambda *a,**kw:reply)
    def test_metadata_scripts_are_data_not_instructions_and_have_no_body_text(self):
        p=Page();p.feed('<script type="application/ld+json">{"datePublished":"2026-10-28T12:00:00Z"}</script><script>send keys</script><p>Public text</p>')
        self.assertEqual(p.dates,['2026-10-28T12:00:00Z']);self.assertNotIn('send keys',' '.join(p.parts))
    def test_date_only_upper_bound_and_timezone_required(self):
        bound=publication('2026-10-28')
        self.assertEqual(bound.utcoffset(),timedelta(hours=-12))
        with self.assertRaises(IRBlocked):publication('2026-10-28T10:00:00')
    def test_real_retrieval_after_decision_holds_even_when_initial_clock_was_early(self):
        def transport(req,timeout):return Reply(req.full_url,('<a href="'+ARTICLE+'">News</a>').encode() if req.full_url==INDEX else HTML)
        r=collect_issuer_ir('APLD',AT,NOW,self.root,transport,clock=lambda:AT+timedelta(seconds=1))
        self.assertFalse(r['documents']);self.assertEqual(r['audit'][0]['reason'],'IR_SOURCE_NOT_AVAILABLE_ASOF')
    def test_explicit_rss_publication_works_but_xml_entities_do_not(self):
        feed=REGISTRY['APLD']['prefix']+'rss'
        index=('<link type="application/rss+xml" href="'+feed+'">').encode()
        xml=('<rss><channel><item><link>'+ARTICLE+'</link><pubDate>Wed, 28 Oct 2026 20:00:00 GMT</pubDate></item></channel></rss>').encode()
        for invalid in (False,True):
            def transport(req,timeout):
                raw=index if req.full_url==INDEX else (b'<!DOCTYPE rss>'+xml if invalid else xml) if req.full_url==feed else b'<p>Fictional public earnings announcement.</p>'
                return Reply(req.full_url,raw)
            r=collect_issuer_ir('APLD',AT,NOW,self.root,transport,clock=lambda:NOW)
            self.assertEqual(bool(r['documents']),not invalid)
