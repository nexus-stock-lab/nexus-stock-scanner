"""Fictional split evidence. No external requests and no production positions."""
import copy
import csv
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from corporate_actions import (prepare_split_history,validate_split_history,split_factor,
                               split_replay_request,SOURCE,sha)
from paper_engine import instant,register,settle,validate_ledger
from test_paper_engine import AT,fixture

END=instant(AT)+timedelta(days=5)
NOW=END+timedelta(minutes=30)


def action(kind='forward_splits',new=2,old=1,ex='2026-10-26',identifier=1):
    return {'kind':kind,'id':f'00000000-0000-0000-0000-{identifier:012d}',
            'symbol':'TEST','ex_date':ex,'old_rate':old,'new_rate':new}


def corporate(actions,end=END,now=NOW):
    grouped={}
    for a in actions:grouped.setdefault(a['kind'],[]).append({k:v for k,v in a.items() if k!='kind'})
    return {'clear':not actions,'actions':copy.deepcopy(actions),
            'pages':[{'corporate_actions':grouped,'next_page_token':None}],
            'complete':True,'source':SOURCE,'asof':end.isoformat(),'retrieved_at':now.isoformat()}


class SplitTests(unittest.TestCase):
    def setUp(self):
        p,q=fixture();self.rows=register([], [p],[q],NOW)[0]
    def proof(self,actions=None):
        return prepare_split_history(corporate(actions or [action()]),'TEST',instant(AT),END,NOW,'fixture://corporate')
    def stream(self,proof,events):
        quotes=[]
        for stamp,price in events:
            q=fixture()[1];q.update(market_time=stamp,price=price,retrieved_at=NOW.isoformat())
            quotes.append(q)
        return {'coverage_start':AT,'coverage_end':END.isoformat(),'complete':True,
                'corporate_actions_clear':False,'source':'fixture SIP','evidence_uri':'fixture://stream',
                'split_history':proof,'trades':quotes}
    def settle(self,proof,events):
        stream=self.stream(proof,events)
        return settle(self.rows,{self.rows[0]['trade_id']:stream},NOW)

    def test_split_price_drop_is_not_a_stop_and_raw_price_is_preserved(self):
        proof=self.proof()
        rows,audit=self.settle(proof,[('2026-10-26T14:45:00Z',50)])
        self.assertEqual(rows[0]['status'],'OPEN');self.assertFalse(audit)
        rows,audit=self.settle(proof,[('2026-10-26T14:45:00Z',62.5)])
        self.assertEqual((rows[0]['exit_reason'],rows[0]['exit_price']),('TARGET',62.5))
        self.assertEqual((rows[0]['exit_equivalent_price'],rows[0]['exit_share_factor']),(125,2))
        self.assertAlmostEqual(rows[0]['return_pct'],25)
        self.assertAlmostEqual(rows[0]['r_multiple'],2.5)
        validate_ledger(rows,NOW)

    def test_pre_split_print_uses_original_basis_and_first_trigger_wins(self):
        rows,_=self.settle(self.proof(),[('2026-10-23T14:46:00Z',85),('2026-10-26T14:45:00Z',62.5)])
        self.assertEqual(rows[0]['exit_reason'],'STOP')
        self.assertEqual((rows[0]['exit_price'],rows[0]['exit_share_factor']),(85,1))

    def test_reverse_split_and_multiple_splits_preserve_economic_return(self):
        proof=self.proof([action('reverse_splits',1,10)])
        rows,_=self.settle(proof,[('2026-10-26T14:45:00Z',1250)])
        self.assertEqual(rows[0]['exit_price'],1250)
        self.assertAlmostEqual(rows[0]['exit_equivalent_price'],125)
        proof=self.proof([action(),action(new=3,ex='2026-10-27',identifier=2)])
        self.assertEqual(split_factor(proof,'2026-10-26T14:45:00Z'),2)
        self.assertEqual(split_factor(proof,'2026-10-27T14:45:00Z'),6)
        rows,_=self.settle(proof,[('2026-10-27T14:45:00Z',21)])
        self.assertAlmostEqual(rows[0]['return_pct'],26)

    def test_dividend_other_actions_missing_terms_and_invalid_rates_hold(self):
        cases=[{**action(),'kind':'cash_dividends'}, {**action(),'kind':'name_changes'},
               {**action(),'ex_date':None},{**action(),'id':''},
               {**action(),'due_bill_redemption_date':'2026-10-27'},
               action(new=0),action(new=True),action(new='NaN'),action(new=0.5),
               action('reverse_splits',2,1),action(ex='2026-10-23')]
        for a in cases:
            with self.subTest(action=a),self.assertRaises((ValueError,TypeError)):
                self.proof([a])

    def test_incomplete_hidden_action_conflicting_and_same_day_records_hold(self):
        c=corporate([action()]);c['pages'][0]['next_page_token']='next'
        with self.assertRaises(ValueError):prepare_split_history(c,'TEST',instant(AT),END,NOW,'fixture')
        c=corporate([action(),{**action(identifier=2),'kind':'cash_dividends'}]);c['actions']=c['actions'][:1]
        with self.assertRaisesRegex(ValueError,'selection mismatch'):
            prepare_split_history(c,'TEST',instant(AT),END,NOW,'fixture')
        for actions in ([action(),action(new=3)],[action(),action(identifier=2)]):
            with self.assertRaises(ValueError):self.proof(actions)
        proof=self.proof([action(),action()])
        self.assertEqual(len(proof['events']),1)

    def test_proof_identity_time_and_tampering_are_rejected(self):
        original=self.proof()
        for key,value in (('symbol','OTHER'),('retrieved_at',(NOW+timedelta(seconds=1)).isoformat()),
                          ('history_sha256','a'*64)):
            p=copy.deepcopy(original);p[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):
                validate_split_history(p,self.rows[0],END.isoformat(),NOW)
        p=copy.deepcopy(original);p['events'][0]['new_rate']='99'
        with self.assertRaises(ValueError):validate_split_history(p,self.rows[0],END.isoformat(),NOW)

    def test_published_due_bill_calendar_does_not_move_split_price_basis(self):
        a={**action(),'record_date':'2026-10-21','payable_date':'2026-10-23',
           'due_bill_redemption_date':'2026-10-27'}
        proof=self.proof([a]);validate_split_history(proof,self.rows[0],END.isoformat(),NOW)
        self.assertEqual(split_factor(proof,'2026-10-26T14:45:00Z'),2)
        self.assertEqual(proof['events'][0]['due_bill_calendar']['due_bill_redemption_date'],'2026-10-27')
        rows,_=self.settle(proof,[('2026-10-26T14:45:00Z',50)])
        self.assertEqual(rows[0]['status'],'OPEN')
        for changed in ({**a,'payable_date':None},{**a,'record_date':'2026-10-28'},
                        {**a,'due_bill_redemption_date':'2026-10-25'}):
            with self.subTest(action=changed),self.assertRaises(ValueError):self.proof([changed])

    def test_late_or_corrected_split_requires_replay_and_history_must_not_disappear(self):
        self.rows[0]['tracked_through_jst']='2026-10-27T14:00:00+00:00'
        proof=self.proof();request=split_replay_request(self.rows[0],proof)
        stream=self.stream(proof,[('2026-10-23T14:46:00Z',85)])
        rows,audit=settle(self.rows,{self.rows[0]['trade_id']:stream},NOW)
        self.assertEqual(rows[0]['status'],'OPEN');self.assertEqual(audit[0]['status'],'BLOCKED')
        stream['corporate_replay']=request
        rows,audit=settle(self.rows,{self.rows[0]['trade_id']:stream},NOW)
        self.assertEqual(rows[0]['exit_price'],85)
        self.assertEqual(audit[0]['status'],'VERIFIED_CORPORATE_REPLAY')
        self.rows[0]['tracked_corporate_sha256']=proof['history_sha256']
        self.assertIsNone(split_replay_request(self.rows[0],proof))
        with self.assertRaises(ValueError):split_replay_request(self.rows[0],None)
        changed=self.proof([action(new=3)])
        self.assertIsNotNone(split_replay_request(self.rows[0],changed))

    def test_persisted_adjusted_results_survive_csv_and_detect_corruption(self):
        rows,_=self.settle(self.proof(),[('2026-10-26T14:45:00Z',62.5)])
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'trades.csv'
            with path.open('w',newline='') as f:
                w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
            restored=list(csv.DictReader(path.read_text().splitlines()))
            validate_ledger(restored,NOW)
            restored[0]['exit_share_factor']='3'
            with self.assertRaises(ValueError):validate_ledger(restored,NOW)

    def test_real_data_probe_is_read_only_with_injected_benchmark_data(self):
        from split_probe import check,ENTRY,AFTER
        from datetime import datetime
        now=NOW
        a=action(new=4,ex='2020-08-31');a['symbol']='AAPL'
        def fetch_corporate(*args):return corporate([a],AFTER+timedelta(minutes=1),now)
        def fetch_interval(symbol,start,end,*args):
            stamp=start+timedelta(seconds=1)
            return {'symbol':symbol,'start':start.isoformat(),'end':end.isoformat(),
                    'complete':True,'feed':'sip','retrieved_at':now.isoformat(),
                    'trades':[{'t':stamp.isoformat(),'p':500 if start==ENTRY else 125,
                               'i':1,'x':'V','z':'C','c':['@']}]}
        with tempfile.TemporaryDirectory() as d:
            r=check(Path(d)/'probe','fixture','fixture',fetch_corporate,fetch_interval,clock=lambda:now)
            self.assertEqual(r['status'],'VERIFIED_SPLIT_BASIS')
            self.assertEqual([q['share_factor'] for q in r['observations']],[1,4])
            self.assertEqual([q['equivalent_price'] for q in r['observations']],[500,500])
            self.assertFalse(r['may_register']);self.assertEqual(r['paper_fills_created'],0)
            self.assertFalse(list(Path(d).rglob('trades.csv')))

    def test_probe_failure_does_not_emit_exception_secrets(self):
        from split_probe import check
        def fail(*args):raise RuntimeError('private-key-must-not-appear')
        with tempfile.TemporaryDirectory() as d:
            r=check(d,'fixture','fixture',fetch_corporate=fail)
            self.assertEqual(r['status'],'HELD')
            self.assertNotIn('private-key-must-not-appear',str(r))

    def test_split_basis_keeps_ninety_day_and_scenario_exit_rules(self):
        deadline=instant(self.rows[0]['deadline_jst']);now=deadline+timedelta(days=1)
        ca=corporate([action()],now,now)
        proof=prepare_split_history(ca,'TEST',instant(AT),now,now,'fixture://corporate')
        q=fixture()[1];q.update(market_time=deadline.isoformat(),retrieved_at=now.isoformat(),price=50)
        stream={'coverage_start':AT,'coverage_end':now.isoformat(),'complete':True,
                'source':'fixture','evidence_uri':'fixture://stream','corporate_actions_clear':False,
                'split_history':proof,'trades':[q]}
        rows,_=settle(self.rows,{self.rows[0]['trade_id']:stream},now)
        self.assertEqual(rows[0]['exit_reason'],'MAX_90_DAYS')
        self.assertEqual(rows[0]['exit_price'],50)
        self.assertEqual(rows[0]['exit_equivalent_price'],100)
        self.assertEqual(rows[0]['deadline_jst'],self.rows[0]['deadline_jst'])
        stream=self.stream(self.proof(),[('2026-10-26T14:45:00Z',50)])
        stream['scenario_exit']={'verified':True,'source':'fixture official disclosure',
            'evidence_uri':'fixture://scenario','rationale':'Fixture deterioration',
            'published_at':'2026-10-26T14:40:00Z','checked_at':'2026-10-26T14:44:00Z'}
        rows,_=settle(self.rows,{self.rows[0]['trade_id']:stream},NOW)
        self.assertEqual(rows[0]['exit_reason'],'SCENARIO_DETERIORATION')
