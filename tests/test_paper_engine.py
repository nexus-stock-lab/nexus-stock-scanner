import copy
import unittest
from datetime import timedelta
from paper_engine import CHECKS, instant, register, settle, statistics, merge_unique, validate_ledger

AT='2026-10-23T23:45:00+09:00'
NOW=instant(AT)+timedelta(days=1)


def fixture(symbol='TEST'):
    p=dict(rules_version='nexus_rules_20261027_v1',symbol=symbol,signal='反発初動',decision_time_jst=AT,prepared_at=AT,
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

    def test_thirty_trade_campaign_persists_closes_and_never_reopens_capacity(self):
        """Acceptance path: 30 lifetime entries, both signals, three exits, CSV restart."""
        import contextlib, csv, io, json, os, tempfile
        from pathlib import Path
        from paper_engine import run
        previous=os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory);Path('output').mkdir()
                plans=[];quotes=[]
                for i in range(30):
                    p,q=fixture('TEST'+str(i))
                    p['signal']='反発確認' if i%2 else '反発初動'
                    plans.append(p);quotes.append(q)
                incoming=Path('output/verified_evidence.json')
                incoming.write_text(json.dumps(dict(plans=plans,quotes=quotes,streams={})))
                with contextlib.redirect_stdout(io.StringIO()):run(NOW)
                rows=list(csv.DictReader(Path('input/trades.csv').read_text().splitlines()))
                self.assertEqual(len(rows),30)
                self.assertTrue(all(r['status']=='OPEN' for r in rows))
                streams={};finish=instant(rows[0]['deadline_jst'])+timedelta(hours=1)
                for i,row in enumerate(rows):
                    price=(125,85,105)[i%3]
                    at=instant(row['deadline_jst']) if i%3==2 else instant(AT)+timedelta(minutes=1)
                    stream=self.stream([row],[price],[at])[row['trade_id']]
                    stream['trades'][0]['symbol']=row['symbol']
                    streams[row['trade_id']]=stream
                incoming.write_text(json.dumps(dict(plans=[],quotes=[],streams=streams)))
                with contextlib.redirect_stdout(io.StringIO()):run(finish);run(finish)
                report=json.loads(Path('output/trade_report.json').read_text())
                stats=report['statistics'];total=stats['total']
                self.assertTrue(stats['complete']);self.assertEqual(stats['remaining'],0)
                self.assertEqual((total['registered'],total['closed'],total['open']),(30,30,0))
                self.assertEqual((total['wins'],total['losses']),(20,10))
                self.assertAlmostEqual(total['expectancy_r'],0.5)
                self.assertAlmostEqual(total['mean_return_pct'],5)
                for value in stats['by_signal'].values():
                    self.assertEqual((value['registered'],value['closed']),(15,15))
                    self.assertAlmostEqual(value['expectancy_r'],0.5)
                closed=list(csv.DictReader(Path('input/trades.csv').read_text().splitlines()))
                for reason in ('TARGET','STOP','MAX_90_DAYS'):
                    self.assertEqual(sum(r['exit_reason']==reason for r in closed),10)
                # Closing trades must not free a lifetime slot. A restart must not duplicate entries.
                p,q=fixture('EXTRA')
                incoming.write_text(json.dumps(dict(plans=[p],quotes=[q],streams={})))
                with contextlib.redirect_stdout(io.StringIO()):run(finish)
                after=list(csv.DictReader(Path('input/trades.csv').read_text().splitlines()))
                self.assertEqual(after,closed)
                self.assertEqual(json.loads(Path('output/trade_report.json').read_text())['entry_audit'][-1]['status'],'BLOCKED')
            finally:os.chdir(previous)

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

class ScenarioExitTests(unittest.TestCase):
    entry=EngineTests.entry
    stream=EngineTests.stream
    def test_exit_at_first_trade_after_official_confirmation(self):
        rows=self.entry();stream=self.stream(rows,[105,106,107]);s=next(iter(stream.values()))
        s['scenario_exit']={'verified':True,'source':'fixture://SEC','evidence_uri':'fixture://filing',
                            'rationale':'Approved financial quality failed',
                            'published_at':AT,'checked_at':(instant(AT)+timedelta(minutes=2)).isoformat()}
        closed,a=settle(rows,stream,NOW)
        self.assertEqual(closed[0]['exit_reason'],'SCENARIO_DETERIORATION')
        self.assertEqual(closed[0]['exit_price'],106)
    def test_unverified_or_future_scenario_blocks(self):
        rows=self.entry();stream=self.stream(rows,[105]);s=next(iter(stream.values()))
        s['scenario_exit']={'verified':False}
        closed,a=settle(rows,stream,NOW)
        self.assertEqual(closed[0]['status'],'OPEN')
        self.assertEqual(a[0]['status'],'BLOCKED')

class ScenarioReplayTests(unittest.TestCase):
    entry=EngineTests.entry
    stream=EngineTests.stream

    def replay_case(self):
        from paper_engine import scenario_replay_request
        rows=self.entry()
        rows[0]['tracked_through_jst']=(instant(AT)+timedelta(minutes=10)).isoformat()
        stream=self.stream(rows,[99,98,97]);s=next(iter(stream.values()))
        s['scenario_exit']={'verified':True,'source':'fixture://SEC','evidence_uri':'fixture://archived-filing',
                            'rationale':'Official impairment confirmation','published_at':AT,
                            'checked_at':(instant(AT)+timedelta(minutes=2)).isoformat()}
        s['scenario_replay']=scenario_replay_request(rows[0],s['scenario_exit'],NOW)
        return rows,stream,s

    def test_replay_requires_exact_previous_cursor_and_evidence_hash(self):
        for field,value in (('previous_cursor',AT),('scenario_sha256','0'*64)):
            rows,stream,s=self.replay_case();s['scenario_replay'][field]=value
            after,audit=settle(rows,stream,NOW)
            self.assertEqual(after,rows)
            self.assertEqual(audit[0]['status'],'BLOCKED')

    def test_no_replay_proof_cannot_settle_at_later_convenient_price(self):
        rows,stream,s=self.replay_case();s.pop('scenario_replay')
        s['coverage_start']=rows[0]['tracked_through_jst']
        s['coverage_end']=(instant(AT)+timedelta(minutes=11)).isoformat()
        s['trades'][0]['market_time']=s['coverage_end'];s['trades'][0]['retrieved_at']=NOW.isoformat()
        s['trades']=s['trades'][:1]
        after,audit=settle(rows,stream,NOW)
        self.assertEqual(after,rows)
        self.assertIn('requires verified replay',audit[0]['reason'])

    def test_replay_incomplete_history_does_not_change_marker_or_cursor(self):
        rows,stream,s=self.replay_case();s['complete']=False
        after,audit=settle(rows,stream,NOW)
        self.assertEqual(after,rows)
        self.assertEqual(audit[0]['status'],'BLOCKED')

    def test_new_confirmation_at_cursor_boundary_replays_exact_timestamp(self):
        from paper_engine import scenario_replay_request
        rows,stream,s=self.replay_case()
        rows[0]['tracked_through_jst']=(instant(AT)+timedelta(minutes=2)).isoformat()
        s['scenario_replay']=scenario_replay_request(rows[0],s['scenario_exit'],NOW)
        after,audit=settle(rows,stream,NOW)
        self.assertEqual(after[0]['exit_price'],98)
        self.assertEqual(audit[0]['status'],'VERIFIED_SCENARIO_REPLAY')

    def test_closed_trade_is_never_reopened(self):
        rows,stream,s=self.replay_case()
        closed,_=settle(rows,stream,NOW)
        after,audit=settle(closed,stream,NOW)
        self.assertEqual(after,closed)
        self.assertEqual(audit,[])
