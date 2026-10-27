import unittest
from datetime import timedelta
from historical_probe import decision
from market_provider import eligible, collect_interval, normalized_stream, corporate_state, verified_reference
from test_price_policy import history

AT=decision('2026-10-23T23:45:00+09:00');NOW=AT+timedelta(hours=1)

def row(at=None,price=100,**kw):
    return dict(t=at or AT.isoformat(),p=price,c=['@'],z='C',i=1,x='V',**kw)

class ProviderTests(unittest.TestCase):
    def test_update_conditions(self):
        self.assertTrue(eligible(row(u='corrected')))
        self.assertFalse(eligible(row(u='incorrect')))
        self.assertFalse(eligible(row(u='canceled')))
        self.assertFalse(eligible({**row(),'c':['@','I']}))
        with self.assertRaises(ValueError):eligible(row(u='unknown'))
    def test_read_complete_paginated_history(self):
        pages=[{'symbol':'TEST','trades':[row((AT+timedelta(seconds=1)).isoformat())],'next_page_token':'x'},
               {'symbol':'TEST','trades':[dict(row((AT+timedelta(seconds=2)).isoformat()),i=2)],'next_page_token':None}]
        result=collect_interval('TEST',AT,AT+timedelta(minutes=1),NOW,'k','s',fetch=lambda *args:pages.pop(0))
        self.assertEqual(len(result['trades']),2)
        self.assertTrue(result['complete'])
    def test_incomplete_and_repeated_page_rejected(self):
        page={'symbol':'TEST','trades':[],'next_page_token':'same'}
        with self.assertRaises(ValueError):collect_interval('TEST',AT,AT+timedelta(minutes=1),NOW,'k','s',fetch=lambda *a:page)
    def test_too_recent_interval_never_fetches(self):
        with self.assertRaises(ValueError):collect_interval('TEST',AT,NOW,NOW,'k','s',fetch=lambda *a:self.fail())
    def test_corporate_empty_is_known_asof_clear(self):
        result=corporate_state('TEST',AT,NOW,'k','s',fetch=lambda *a:{'corporate_actions':{},'next_page_token':None})
        self.assertTrue(result['clear'])
    def test_corporate_split_blocks(self):
        result=corporate_state('TEST',AT,NOW,'k','s',fetch=lambda *a:{'corporate_actions':{'forward_splits':[{'symbol':'TEST','ex_date':'2026-10-23'}]}})
        self.assertFalse(result['clear'])
    def test_unknown_corporate_schema_blocks(self):
        with self.assertRaises(ValueError):corporate_state('TEST',AT,NOW,'k','s',fetch=lambda *a:{})
    def test_normalized_stream_discards_boundary_and_canceled_print(self):
        data={'symbol':'TEST','start':AT.isoformat(),'end':(AT+timedelta(minutes=1)).isoformat(),
              'retrieved_at':NOW.isoformat(),'complete':True,'feed':'sip',
              'trades':[row(),row((AT+timedelta(seconds=1)).isoformat(),999,u='canceled'),dict(row((AT+timedelta(seconds=2)).isoformat(),101),i=3)]}
        stream=normalized_stream(data,{'clear':True},'fixture://raw')
        self.assertEqual([q['price'] for q in stream['trades']],[101])
    def test_reference_verifies_only_regular_session_and_valid_history(self):
        q=verified_reference(history(),NOW.isoformat(),'fixture://raw')
        self.assertTrue(q['provider_verified'])
        data=history();data['decision_time_jst']='2026-10-24T23:45:00+09:00'
        data['pages'][0]['trades'][0]['t']='2026-10-24T14:44:59Z'
        with self.assertRaises(ValueError):verified_reference(data,(NOW+timedelta(days=1)).isoformat(),'fixture')
