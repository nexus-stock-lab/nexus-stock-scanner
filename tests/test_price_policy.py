import copy
import unittest
from datetime import timedelta
from price_policy import entry_reference_price, history_candidate
from historical_probe import decision
from test_paper_engine import fixture

AT=decision('2026-10-23T23:45:00+09:00')
NOW=AT+timedelta(minutes=16)


def history(rows=None):
    rows=rows if rows is not None else [dict(t='2026-10-23T14:44:59.999999999Z',p=100,c=['@'],z='C',i=1,x='V')]
    return dict(status='HISTORY_RECEIVED',symbol='TEST',decision_time_jst=AT.isoformat(),
                feed='sip',complete_pagination=True,window_seconds=60,records=len(rows),
                pages=[dict(symbol='TEST',trades=rows,next_page_token=None)])


class PolicyTests(unittest.TestCase):
    def candidate(self,result=None):
        return history_candidate(result or history(),NOW.isoformat(),'fixture://history')
    def test_approved_60_seconds_boundary(self):
        p,q=fixture()
        q['market_time']='2026-10-23T14:44:00Z'
        self.assertEqual(entry_reference_price(q,'TEST',AT,NOW),100)
        q['market_time']='2026-10-23T14:43:59.999999999Z'
        with self.assertRaises(ValueError):entry_reference_price(q,'TEST',AT,NOW)
    def test_future_nanosecond_rejected(self):
        p,q=fixture();q['market_time']='2026-10-23T14:45:00.000000001Z'
        with self.assertRaises(ValueError):entry_reference_price(q,'TEST',AT,NOW)
    def test_verification_before_0001_rejected(self):
        p,q=fixture();q['retrieved_at']=(NOW-timedelta(microseconds=1)).isoformat()
        with self.assertRaises(ValueError):entry_reference_price(q,'TEST',AT,NOW)
    def test_provider_unverified_rejected(self):
        q=self.candidate()
        self.assertFalse(q['provider_verified'])
        with self.assertRaises(ValueError):entry_reference_price(q,'TEST',AT,NOW)
    def test_latest_valid_trade_not_latest_invalid_print(self):
        result=history([dict(t='2026-10-23T14:45:00Z',p=999,c=['@','I'],z='C',i=2,x='V'),
                        dict(t='2026-10-23T14:44:59Z',p=100,c=['@'],z='C',i=1,x='V')])
        q=self.candidate(result);self.assertEqual(q['price'],100)
        self.assertEqual(q['reference_age_seconds'],1)
    def test_unknown_condition_blocks(self):
        result=history();result['pages'][0]['trades'][0]['c']=['@','?']
        with self.assertRaises(ValueError):self.candidate(result)
    def test_tape_specific_eligibility(self):
        result=history();row=result['pages'][0]['trades'][0]
        row['c']=['B'];row['z']='A'
        with self.assertRaises(ValueError):self.candidate(result)
        row['z']='C';self.assertEqual(self.candidate(result)['price'],100)
    def test_conflicting_simultaneous_prices_rejected(self):
        row=history()['pages'][0]['trades'][0];other={**row,'i':2,'p':101}
        with self.assertRaises(ValueError):self.candidate(history([row,other]))
    def test_incomplete_history_rejected(self):
        for field,value in [('complete_pagination',False),('window_seconds',300),('records',2)]:
            r=history();r[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):self.candidate(r)
    def test_decision_metadata_does_not_shift_to_quote(self):
        from paper_engine import register
        p,q=fixture();q['market_time']='2026-10-23T14:44:59Z'
        rows,a=register([], [p],[q],NOW)
        self.assertEqual(rows[0]['entry_time_jst'],AT.isoformat())
        self.assertEqual(rows[0]['entry_market_time'],q['market_time'])
        self.assertEqual(rows[0]['entry_verified_at'],NOW.isoformat())
    def test_same_latest_price_at_same_timestamp_allowed(self):
        row=history()['pages'][0]['trades'][0];other={**row,'i':2}
        self.assertEqual(self.candidate(history([row,other]))['price'],100)
    def test_partial_sale_condition_proof_blocks(self):
        p,q=fixture();q['sale_conditions_verified']=False
        with self.assertRaises(ValueError):entry_reference_price(q,'TEST',AT,NOW)

if __name__=='__main__':unittest.main()

class UpdateStateTests(unittest.TestCase):
    def candidate(self, rows):
        return history_candidate(history(rows),NOW.isoformat(),'fixture://history')
    def test_canceled_and_incorrect_prints_are_excluded(self):
        for state in ('canceled','incorrect'):
            rows=[dict(t='2026-10-23T14:45:00Z',p=999,c=['@'],z='C',i=2,x='V',u=state),
                  dict(t='2026-10-23T14:44:59Z',p=100,c=['@'],z='C',i=1,x='V')]
            self.assertEqual(self.candidate(rows)['price'],100)
    def test_corrected_print_is_used(self):
        row=dict(t='2026-10-23T14:45:00Z',p=101,c=['@'],z='C',i=3,x='V',u='corrected')
        self.assertEqual(self.candidate([row])['price'],101)
    def test_unknown_update_blocks(self):
        row=dict(t='2026-10-23T14:45:00Z',p=101,c=['@'],z='C',i=3,x='V',u='unknown')
        with self.assertRaises(ValueError): self.candidate([row])
    def test_only_canceled_prints_cannot_supply_a_price(self):
        row=dict(t='2026-10-23T14:45:00Z',p=101,c=['@'],z='C',i=3,x='V',u='canceled')
        with self.assertRaises(ValueError): self.candidate([row])
