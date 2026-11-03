"""Fictional declared dividends. No actual cash receipt or production ledger."""
import copy
import csv
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from corporate_actions import (prepare_corporate_history,validate_corporate_history,
    declared_dividend_records,read_dividend_records,dividend_report,sha,split_factor)
from paper_engine import instant,register,settle,validate_ledger,statistics
from test_paper_engine import AT,fixture
from test_corporate_actions import corporate,action,END,NOW


def cash(ex='2026-10-26',rate='0.25',identifier=2):
    return {'kind':'cash_dividends','id':f'00000000-0000-0000-0000-{identifier:012d}',
            'symbol':'TEST','currency':'USD','foreign':False,'special':False,
            'ex_date':ex,'record_date':ex,'payable_date':'2026-10-28','rate':rate}


class DividendTests(unittest.TestCase):
    def setUp(self):
        p,q=fixture();self.rows=register([], [p],[q],NOW)[0]
    def proof(self,actions=None):
        return prepare_corporate_history(corporate(actions or [cash()]),'TEST',instant(AT),END,NOW,'fixture://corporate')
    def close(self,price=100,stamp='2026-10-26T14:45:00Z',proof=None):
        q=fixture()[1];q.update(market_time=stamp,price=price,retrieved_at=NOW.isoformat())
        stream={'coverage_start':AT,'coverage_end':END.isoformat(),'complete':True,
                'corporate_actions_clear':False,'split_history':proof or self.proof(),
                'source':'fixture','evidence_uri':'fixture://stream','trades':[q]}
        rows,audit=settle(self.rows,{self.rows[0]['trade_id']:stream},NOW)
        self.assertFalse([a for a in audit if a['status']=='BLOCKED'])
        return rows

    def test_verified_ordinary_cash_keeps_price_basis_and_records_declaration_only(self):
        proof=self.proof();validate_corporate_history(proof,self.rows[0],END.isoformat(),NOW)
        self.assertEqual(split_factor(proof,'2026-10-26T14:45:00Z'),1)
        rows=self.close();self.assertEqual(rows[0]['status'],'OPEN')
        records=read_dividend_records(rows[0])
        self.assertEqual(len(records),1)
        self.assertEqual(records[0]['declared_gross_usd_per_entry_unit'],'0.25')
        self.assertFalse(records[0]['payment_verified']);validate_ledger(rows,NOW)

    def test_ex_dividend_price_stop_is_not_offset_by_declared_cash(self):
        rows=self.close(89.75)
        self.assertEqual(rows[0]['exit_reason'],'STOP')
        self.assertAlmostEqual(rows[0]['return_pct'],-10.25)
        self.assertAlmostEqual(rows[0]['r_multiple'],-1.025)
        self.assertEqual(statistics(rows)['total']['losses'],1)
        self.assertEqual(dividend_report(rows,NOW)['positions'][0]['declared_gross_usd_per_entry_unit'],'0.25')
        self.assertFalse(dividend_report(rows,NOW)['included_in_price_return_or_r'])
        validate_ledger(rows,NOW)

    def test_entry_on_ex_date_and_exit_before_ex_date_do_not_receive_entitlement(self):
        rows=self.close(proof=self.proof([cash(ex='2026-10-23')]))
        self.assertEqual(read_dividend_records(rows[0]),[])
        rows=self.close(125,'2026-10-23T14:46:00Z')
        self.assertEqual(rows[0]['status'],'CLOSED')
        self.assertEqual(read_dividend_records(rows[0]),[])

    def test_dividend_unit_factor_uses_only_splits_before_ex_date(self):
        proof=self.proof([action(),cash(ex='2026-10-27')])
        rows=self.close(50,'2026-10-27T14:45:00Z',proof)
        self.assertEqual(read_dividend_records(rows[0])[0]['declared_gross_usd_per_entry_unit'],'0.50')
        proof=self.proof([cash(),action(ex='2026-10-27')])
        rows=self.close(50,'2026-10-27T14:45:00Z',proof)
        self.assertEqual(read_dividend_records(rows[0])[0]['declared_gross_usd_per_entry_unit'],'0.25')

    def test_duplicate_and_revised_declarations_never_accumulate_twice(self):
        proof=self.proof([cash(),cash()]);rows=self.close(proof=proof)
        records=declared_dividend_records(proof,rows[0],END.isoformat())
        self.assertEqual(len(records),1)
        revised=self.proof([cash(rate='0.30')])
        records=declared_dividend_records(revised,rows[0],END.isoformat())
        self.assertEqual(len(records),1)
        self.assertEqual(records[0]['declared_gross_usd_per_entry_unit'],'0.30')
        with self.assertRaisesRegex(ValueError,'disappeared'):
            declared_dividend_records(None,rows[0],END.isoformat())

    def test_foreign_special_currency_due_bill_and_missing_flags_are_held(self):
        changes=[{'foreign':True},{'special':True},{'currency':'EUR'},
                 {'sub_type':'return_of_capital'},{'sub_type':'interest'},
                 {'due_bill_on_date':'2026-10-24'},{'due_bill_off_date':'2026-10-28'},
                 {'rate':0},{'rate':True},{'rate':'NaN'},{'foreign':None},{'special':None},
                 {'record_date':None},{'payable_date':'2026-10-25'}]
        for change in changes:
            with self.subTest(change=change),self.assertRaises((ValueError,TypeError)):
                self.proof([{**cash(),**change}])

    def test_conflicting_same_day_distributions_and_hidden_cash_are_held(self):
        for actions in ([cash(),cash(rate='0.30')],[cash(),cash(identifier=3)],
                        [cash(),action()]):
            with self.assertRaises(ValueError):self.proof(actions)
        ca=corporate([cash()]);ca['clear']=True;ca['actions']=[]
        with self.assertRaisesRegex(ValueError,'hides relevant actions'):
            prepare_corporate_history(ca,'TEST',instant(AT),END,NOW,'fixture')

    def test_payable_date_reached_never_means_cash_receipt_is_verified(self):
        rows=self.close(proof=self.proof([{**cash(),'payable_date':'2026-10-30'}]))
        before=dividend_report(rows,NOW)
        after=dividend_report(rows,NOW+timedelta(days=30))
        self.assertEqual(before['positions'][0]['records'][0]['status'],'AWAITING_DECLARED_PAYABLE_DATE')
        self.assertEqual(after['positions'][0]['records'][0]['status'],'PAYABLE_DATE_REACHED_PAYMENT_UNVERIFIED')
        self.assertFalse(after['payment_verified'])
        self.assertNotIn('net_amount',str(after))
        with self.assertRaises(ValueError):dividend_report(rows,NOW-timedelta(seconds=1))

    def test_persisted_csv_rejects_fabricated_payment_and_gross_amount(self):
        rows=self.close(125)
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'trades.csv'
            with p.open('w',newline='') as f:
                w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
            restored=list(csv.DictReader(p.read_text().splitlines()));validate_ledger(restored,NOW)
        for change in ({'payment_verified':True},{'declared_gross_usd_per_entry_unit':'99'},
                       {'ex_date':'2026-10-23'}):
            bad=copy.deepcopy(rows);r=json.loads(bad[0]['dividend_records_json']);r[0].update(change)
            bad[0]['dividend_records_json']=json.dumps(r);bad[0]['dividend_records_sha256']=sha(r)
            with self.subTest(change=change),self.assertRaises(ValueError):validate_ledger(bad,NOW)

    def test_dividend_probe_checks_public_declaration_without_ledger_or_paid_status(self):
        from dividend_probe import check,ENTRY,END as END_BENCHMARK
        a=cash(ex='2025-02-10');a.update(symbol='AAPL',payable_date='2025-02-13')
        with tempfile.TemporaryDirectory() as d:
            r=check(d,'fixture','fixture',fetch_corporate=lambda *args:corporate([a],END_BENCHMARK,NOW),clock=lambda:NOW)
            self.assertEqual(r['status'],'VERIFIED_DECLARED_USD_DIVIDEND')
            self.assertFalse(r['payment_verified']);self.assertEqual(r['paper_fills_created'],0)
            self.assertFalse(list(Path(d).rglob('trades.csv')))
            a['currency']=None
            r=check(d,'fixture','fixture',fetch_corporate=lambda *args:corporate([a],END_BENCHMARK,NOW),clock=lambda:NOW)
            self.assertEqual(r['status'],'VERIFIED_PRICE_ONLY_CASH_ACTION_CURRENCY_UNVERIFIED')
            self.assertFalse(r['currency_verified']);self.assertFalse(r['payment_verified'])
            self.assertEqual(r['paper_fills_created'],0)
            self.assertFalse(list(Path(d).rglob('trades.csv')))

    def test_legacy_split_proof_remains_valid_but_cannot_introduce_cash(self):
        from corporate_actions import LEGACY_VERSION
        proof=self.proof([action()]);proof['version']=LEGACY_VERSION
        proof.pop('cash_dividends');proof.pop('cash_dividends_sha256')
        proof['sha256']=sha({k:v for k,v in proof.items() if k!='sha256'})
        validate_corporate_history(proof,self.rows[0],END.isoformat(),NOW)
        proof=self.proof();proof['version']=LEGACY_VERSION
        proof['sha256']=sha({k:v for k,v in proof.items() if k!='sha256'})
        with self.assertRaisesRegex(ValueError,'legacy corporate proof'):
            validate_corporate_history(proof,self.rows[0],END.isoformat(),NOW)

    def test_unknown_currency_is_price_only_and_never_guessed_or_totalled_as_usd(self):
        for currency in (None,''):
            proof=self.proof([{**cash(),'currency':currency}]);rows=self.close(125,proof=proof)
            self.assertEqual(rows[0]['exit_reason'],'TARGET')
            self.assertAlmostEqual(rows[0]['return_pct'],25)
            records=read_dividend_records(rows[0])
            self.assertIsNone(records[0]['currency'])
            self.assertFalse(records[0]['currency_verified'])
            self.assertIsNone(records[0]['declared_gross_usd_per_entry_unit'])
            report=dividend_report(rows,NOW)['positions'][0]
            self.assertEqual(report['currency_unverified_records'],1)
            self.assertIsNone(report['declared_gross_usd_per_entry_unit'])
            self.assertIsNone(report['verified_usd_subtotal_per_entry_unit'])
            validate_ledger(rows,NOW)
        mixed=self.proof([cash(),{**cash(ex='2026-10-27',identifier=3),'currency':None}])
        rows=self.close(125,'2026-10-27T14:45:00Z',mixed)
        report=dividend_report(rows,NOW)['positions'][0]
        self.assertEqual(report['verified_usd_subtotal_per_entry_unit'],'0.25')
        self.assertIsNone(report['declared_gross_usd_per_entry_unit'])

    def test_unknown_currency_cannot_be_forged_into_usd_value_or_legacy_proof(self):
        from corporate_actions import LEGACY_USD_VERSION
        proof=self.proof([{**cash(),'currency':None}]);rows=self.close(proof=proof)
        for change in ({'declared_gross_usd_per_entry_unit':'0.25'},{'currency_verified':True}):
            bad=copy.deepcopy(rows);r=json.loads(bad[0]['dividend_records_json']);r[0].update(change)
            bad[0]['dividend_records_json']=json.dumps(r);bad[0]['dividend_records_sha256']=sha(r)
            with self.assertRaises(ValueError):validate_ledger(bad,NOW)
        proof['version']=LEGACY_USD_VERSION
        proof['sha256']=sha({k:v for k,v in proof.items() if k!='sha256'})
        with self.assertRaisesRegex(ValueError,'legacy dividend proof'):
            validate_corporate_history(proof,self.rows[0],END.isoformat(),NOW)
