import json,tempfile,unittest
from pathlib import Path
from datetime import datetime,timedelta
from unittest.mock import patch
from official_data import collect_documents
from issuer_ir import archive,publication,REGISTRY
class CollectionTests(unittest.TestCase):
 def test_earnings_is_not_starved_by_recent_unrelated_filings(self):
  at=datetime.fromisoformat('2026-11-12T23:45:00+09:00');calls=[]
  recent={'accessionNumber':[f'0000000001-26-00000{i}' for i in range(6)],'acceptanceDateTime':[(at-timedelta(days=i+1)).isoformat() for i in range(6)],'form':['8-K']*6,'primaryDocument':[f'other{i}.htm' for i in range(5)]+['earnings.htm'],'items':['8.01']*5+['2.02']}
  class Response:
   def __init__(self,url):self.url=url
   def read(self,n):return b'<a href="ex99-1.htm">99.1</a>' if self.url.endswith('earnings.htm') else b'<p>Fixture</p>'
   def __enter__(self):return self
   def __exit__(self,*a):pass
  def opening(req,timeout):calls.append(req.full_url);return Response(req.full_url)
  with tempfile.TemporaryDirectory() as d,patch('official_data.sec_user_agent',return_value='fixture'),patch('official_data.sec_open',side_effect=opening),patch('official_data.time.sleep'):
   collect_documents({'filings':{'recent':recent}},'1',at,at-timedelta(minutes=1),Path(d),symbol='TEST',clock=lambda:at-timedelta(seconds=1))
  self.assertTrue(calls[0].endswith('earnings.htm'));self.assertTrue(calls[1].endswith('ex99-1.htm'));self.assertLessEqual(len(calls),8)
 def test_date_only_bound_is_explicit_not_exact_publication_time(self):
  at=datetime.fromisoformat('2026-11-12T23:45:00+09:00')
  with tempfile.TemporaryDirectory() as d:
   x=archive('AVGO',REGISTRY['AVGO']['prefix']+'fixture',b'<p>Fixture</p>',publication('2026-11-09'),at,at-timedelta(minutes=1),Path(d))
   doc=json.loads((Path(d)/'official_documents'/(x['evidence_sha256']+'.json')).read_text());self.assertEqual(doc['publication_precision'],'DATE_ONLY_UPPER_BOUND')
