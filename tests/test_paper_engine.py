import copy
import unittest
from datetime import timedelta
from paper_engine import CHECKS, instant, register, settle, statistics, merge_unique, validate_ledger

AT='2026-10-23T23:45:00+09:00'
NOW=instant(AT)+timedelta(days=1)


def fixture(symbol='TEST'):
    p=dict(symbol=symbol,signal='反発初動',decision_time_jst=AT,prepared_at=AT,
           stop_price=90,target_price=120,
           checks={k:dict(status='PASS',source='fixture://official',rationale='test evidence',checked_at=AT) for k in CHECKS})
    q=dict(symbol=symbol,kind='trade',market_time=AT,retrieved_at=AT,price=100,
           source='fixture',evidence_uri='fixture://raw',entry_policy='nexus_asof_60s_v1',
           decision_time_jst=AT,history_complete=True,sale_conditions_verified=True,
           latest_valid_trade_verified=True,provider_verified=True)
    q['retrieved_at']=(instant(AT)+timedelta(minutes=16)).isoformat()
    return p,q


class EngineTests(unittest.TestCase):
    def entry(self):
        p,q=fixture(); return register([], [p],[q],NOW)[0]
    def test_register_and_idempotence(self):
        p,q=fixture(); rows,a=register([], [p],[q],NOW)
        self.assertEqual(len(rows),1); self.assertEqual(rows[0]['rr_at_entry'],2)
        self.assertEqual(len(register(rows,[p],[q],NOW)[0]),1)
    def test_fail_closed(self):
        for change in ['bar','source','future','rr','check','lateplan','seconds','nan','missing','signal']:
            with self.subTest(change=change):
                p,q=fixture()
                if change=='bar': q['kind']='1m'
                if change=='source': q['evidence_uri']=''
                if change=='future': q['retrieved_at']='2028-01-01T00:00:00+09:00'
                if change=='rr': p['target_price']=110
                if change=='check': p['checks']['earnings']['status']='UNKNOWN'
                if change=='lateplan': p['prepared_at']='2026-10-24T00:00:00+09:00'
                if change=='seconds': p['decision_time_jst']='2026-10-23T23:45:30+09:00'
                if change=='nan': q['price']='nan'
                if change=='missing': q.pop('source')
                if change=='signal': p['signal']='A'
                rows,a=register([], [p],[q],NOW)
                self.assertFalse(rows); self.assertEqual(a[0]['status'],'BLOCKED')
    def test_duplicate_quote_and_open_symbol(self):
        p,q=fixture(); self.assertFalse(register([], [p],[q,q],NOW)[0])
        rows=self.entry(); p['decision_time_jst']='2026-10-24T23:45:00+09:00'
        q['market_time']=p['decision_time_jst']; q['decision_time_jst']=p['decision_time_jst']; q['retrieved_at']=(instant(q['market_time'])+timedelta(minutes=16)).isoformat()
        self.assertEqual(len(register(rows,[p],[q],NOW+timedelta(days=1))[0]),1)
    def test_lifetime_cap(self):
        plans,quotes=zip(*(fixture('T'+str(i)) for i in range(31)))
        rows,a=register([],plans,quotes,NOW); self.assertEqual(len(rows),30)
        self.assertEqual(a[-1]['status'],'BLOCKED')
    def stream(self, rows, prices, times=None):
        times=times or [instant(AT)+timedelta(minutes=i+1) for i in range(len(prices))]
        events=[]
        for at,price in zip(times,prices):
            q=fixture()[1]; q.update(market_time=at.isoformat(),retrieved_at=at.isoformat(),price=price); events.append(q)
        return {rows[0]['trade_id']:dict(coverage_start=AT,coverage_end=times[-1].isoformat(),complete=True,
                    source='fixture',evidence_uri='fixture://coverage',corporate_actions_clear=True,trades=events)}
    def test_stop_gap_uses_observed_price(self):
        rows=self.entry(); closed,a=settle(rows,self.stream(rows,[95,85]),NOW)
        self.assertEqual(closed[0]['exit_price'],85); self.assertEqual(closed[0]['r_multiple'],-1.5)
        self.assertEqual(rows[0]['status'],'OPEN')
    def test_target(self):
        rows=self.entry(); closed,_=settle(rows,self.stream(rows,[125]),NOW)
        self.assertEqual(closed[0]['exit_reason'],'TARGET')
        self.assertEqual(statistics(closed)['by_signal']['反発初動']['wins'],1)
    def test_deadline_and_no_quote(self):
        rows=self.entry(); at=instant(rows[0]['deadline_jst'])
        closed,_=settle(rows,self.stream(rows,[105],[at]),at)
        self.assertEqual(closed[0]['exit_reason'],'MAX_90_DAYS')
        stream=self.stream(rows,[105],[at]); next(iter(stream.values()))['trades']=[]
        pending,a=settle(rows,stream,at)
        self.assertEqual(pending[0]['status'],'OPEN'); self.assertEqual(a[0]['status'],'AWAITING_VERIFIED_EXIT')
    def test_gap_split_and_bad_order_block(self):
        for key,value in [('complete',False),('corporate_actions_clear',False),('coverage_start','2026-10-24T00:00:00+09:00')]:
            rows=self.entry(); stream=self.stream(rows,[85]); next(iter(stream.values()))[key]=value
            pending,a=settle(rows,stream,NOW)
            self.assertEqual(pending[0]['status'],'OPEN'); self.assertEqual(a[0]['status'],'BLOCKED')
    def test_persistence_roundtrip(self):
        import os, json, tempfile
        from pathlib import Path
        from paper_engine import run
        previous=os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory); Path('output').mkdir()
                p,q=fixture()
                Path('output/verified_evidence.json').write_text(json.dumps(dict(plans=[p],quotes=[q],streams={})))
                run(NOW); run(NOW)
                report=json.loads(Path('output/trade_report.json').read_text())
                self.assertEqual(report['statistics']['total']['registered'],1)
                self.assertTrue(Path('input/evidence.json').exists())
            finally:
                os.chdir(previous)

    def test_repeated_identical_evidence_deduplicated(self):
        p,q=fixture()
        self.assertEqual(merge_unique([], [q,q]),[q])
        rows,a=register([], [p],merge_unique([], [q,q]),NOW)
        self.assertEqual(len(rows),1)

    def test_corrupt_ledger_rejected(self):
        for key,value in [('status','UNKNOWN'),('entry_price','nan'),('deadline_jst',AT),('signal','A')]:
            with self.subTest(key=key):
                rows=self.entry(); rows[0][key]=value
                with self.assertRaises(ValueError): validate_ledger(rows,NOW)
        rows=self.entry(); closed,_=settle(rows,self.stream(rows,[125]),NOW)
        closed[0]['r_multiple']=999
        with self.assertRaises(ValueError): validate_ledger(closed,NOW)

    def test_valid_ledger_roundtrip(self):
        rows=self.entry(); validate_ledger(rows,NOW)
        closed,_=settle(rows,self.stream(rows,[125]),NOW)
        validate_ledger(closed,NOW)

    def test_blank_symbol_blocks(self):
        p,q=fixture('')
        self.assertFalse(register([], [p],[q],NOW)[0])

    def test_empty_statistics(self):
        s=statistics([]); self.assertIsNone(s['total']['win_rate_pct']); self.assertEqual(s['remaining'],30)

if __name__=='__main__': unittest.main()

class NanosecondExitTests(unittest.TestCase):
    entry = EngineTests.entry
    stream = EngineTests.stream
    def test_submicrosecond_trade_order_retained(self):
        rows=self.entry()
        stream=self.stream(rows,[95,85])
        s=next(iter(stream.values()))
        s['trades'][0].update(market_time='2026-10-23T14:46:00.000000001Z',retrieved_at=NOW.isoformat())
        s['trades'][1].update(market_time='2026-10-23T14:46:00.000000002Z',retrieved_at=NOW.isoformat())
        s['coverage_end']='2026-10-23T14:46:00.000000002Z'
        closed,a=settle(rows,stream,NOW)
        self.assertEqual(closed[0]['status'],'CLOSED')
        self.assertEqual(closed[0]['exit_price'],85)
        self.assertEqual(closed[0]['exit_time_jst'],s['trades'][1]['market_time'])
        self.assertEqual(closed[0]['tracked_through_jst'],s['coverage_end'])
    def test_one_nanosecond_outside_coverage_blocks(self):
        rows=self.entry();stream=self.stream(rows,[85]);s=next(iter(stream.values()))
        s['trades'][0].update(market_time='2026-10-23T14:46:00.000000001Z',retrieved_at=NOW.isoformat())
        s['coverage_end']='2026-10-23T14:46:00Z'
        closed,a=settle(rows,stream,NOW)
        self.assertEqual(closed[0]['status'],'OPEN')
        self.assertEqual(a[0]['status'],'BLOCKED')
