"""Positive and negative end-to-end tests. All fictional trades stay in temp dirs."""
import contextlib
import csv
import io
import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from daily_runner import freeze_research, prepare_plans, reconcile, track_positions, update_scenarios
from paper_engine import run, digest
from strategy_rules import VERSION
from test_strategy_rules import AT, inputs
from test_price_policy import history

class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.old=os.getcwd();os.chdir(self.temp.name)
        Path('output').mkdir();Path('input').mkdir()
    def tearDown(self):
        os.chdir(self.old);self.temp.cleanup()
    def prepare(self):
        row,bars,financials,review=inputs();row['secondary_status']='二次調査候補'
        with Path('output/secondary_review.csv').open('w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=list(row));w.writeheader();w.writerow(row)
        snapshot=freeze_research(AT-timedelta(hours=1))
        snapshot['market_data']={'TEST':{'symbol':'TEST','retrieved_at':(AT-timedelta(hours=1)).isoformat(),'bars':bars}}
        snapshot['financials']={'TEST':financials}
        document={'source':'https://issuer.example/ir','official_source_verified':True,
                  'retrieved_at':(AT-timedelta(hours=2)).isoformat(),'published_at':(AT-timedelta(hours=3)).isoformat(),'text':'confirmed'}
        sha=digest(document);Path('input/official_documents').mkdir()
        Path('input/official_documents',sha+'.json').write_text(json.dumps(document))
        for name in ('guidance_not_lowered','decline_explanation_verified','no_material_business_impairment','next_earnings'):
            review[name]['evidence_sha256']=sha
            review[name]['published_at']=document['published_at']
        Path('input/official_reviews.json').write_text(json.dumps({AT.date().isoformat():{'TEST':review}}))
        snapshot['snapshot_sha256']=digest({k:v for k,v in snapshot.items() if k!='snapshot_sha256'})
        Path('input/research/2026-10-23.json').write_text(json.dumps(snapshot))
        return prepare_plans(AT-timedelta(minutes=5))
    def enter(self):
        self.assertEqual(len(self.prepare()['plans']),1)
        now=AT+timedelta(minutes=16)
        status=reconcile(now,fetch_probe=lambda *a:history(),calendar=lambda at:{'open':True},clock=lambda:now,fetch_corporate=lambda *a:{'clear':True})
        self.assertEqual(status['status'],'VERIFIED_INPUT_READY')
        with contextlib.redirect_stdout(io.StringIO()):run(now)
        rows=list(csv.DictReader(Path('input/trades.csv').read_text().splitlines()))
        self.assertEqual(len(rows),1)
        return rows
    def test_entry_track_target_statistics_and_replay(self):
        rows=self.enter();now=AT+timedelta(hours=1)
        def fetch(symbol,start,end,*args):
            return {'symbol':symbol,'start':start.isoformat(),'end':end.isoformat(),
                    'complete':True,'feed':'sip','retrieved_at':now.isoformat(),'pages':[],
                    'trades':[{'t':(AT+timedelta(minutes=1)).isoformat(),'p':125,'c':['@'],'z':'C','x':'V','i':2}]}
        report=track_positions(now,fetch_interval=fetch,fetch_corporate=lambda *a:{'clear':True,'pages':[]})
        self.assertEqual(report['positions'][0]['status'],'HISTORY_READY')
        with contextlib.redirect_stdout(io.StringIO()):run(now);run(now)
        stats=json.loads(Path('output/trade_report.json').read_text())['statistics']['total']
        self.assertEqual((stats['registered'],stats['closed'],stats['wins']),(1,1,1))
        self.assertGreater(stats['expectancy_r'],1.5)
        archive=json.loads(Path('input/evidence.json').read_text())['stream_archive'][0]['stream']
        self.assertEqual(archive['trade_count'],1)
        self.assertEqual(archive['selected_exit_trades'][0]['price'],125)
        self.assertNotIn('trades',archive)
    def test_unconfirmed_guidance_prevents_plan_and_registration(self):
        self.prepare()
        p=Path('input/official_reviews.json');reviews=json.loads(p.read_text())
        reviews[AT.date().isoformat()]['TEST']['guidance_not_lowered']['value']=False
        p.write_text(json.dumps(reviews))
        plans=prepare_plans(AT-timedelta(minutes=1))
        self.assertFalse(plans['plans'])
        now=AT+timedelta(minutes=16)
        reconcile(now,fetch_probe=lambda *a:history(),calendar=lambda at:{'open':True},clock=lambda:now,fetch_corporate=lambda *a:{'clear':True})
        with contextlib.redirect_stdout(io.StringIO()):run(now)
        self.assertEqual(json.loads(Path('output/trade_report.json').read_text())['statistics']['total']['registered'],0)
    def test_corporate_action_never_creates_synthetic_exit(self):
        rows=self.enter();now=AT+timedelta(hours=1)
        report=track_positions(now,fetch_interval=lambda *a:self.fail('must not query after unresolved corporate action'),
                               fetch_corporate=lambda *a:{'clear':False,'actions':[{'kind':'split'}]})
        self.assertEqual(report['positions'][0]['status'],'BLOCKED')
        with contextlib.redirect_stdout(io.StringIO()):run(now)
        self.assertEqual(json.loads(Path('output/trade_report.json').read_text())['statistics']['total']['open'],1)

    def prepare_impairment(self):
        checked=AT+timedelta(minutes=2)
        document={'source':'https://issuer.example/ir/impairment','official_source_verified':True,
                  'retrieved_at':(AT+timedelta(minutes=1)).isoformat(),'published_at':AT.isoformat(),
                  'text':'Officially confirmed material impairment'}
        sha=digest(document)
        Path('input/official_documents',sha+'.json').write_text(json.dumps(document))
        review={'symbol':'TEST','rules_version':VERSION,'no_material_business_impairment':{
                'value':False,'source':document['source'],'evidence_sha256':sha,'excerpt':document['text'],
                'rationale':'Fixture official confirmation','published_at':document['published_at'],
                'checked_at':checked.isoformat()}}
        p=Path('input/official_reviews.json');reviews=json.loads(p.read_text())
        reviews['impairment']={'TEST':review};p.write_text(json.dumps(reviews))
        update_scenarios({},AT+timedelta(hours=1),Path('input'))

    def test_official_impairment_exits_at_first_trade_after_confirmation(self):
        self.enter();self.prepare_impairment();now=AT+timedelta(hours=1)
        def fetch(symbol,start,end,*args):
            return {'symbol':symbol,'start':start.isoformat(),'end':end.isoformat(),
                    'complete':True,'feed':'sip','retrieved_at':now.isoformat(),'pages':[],
                    'trades':[{'t':(AT+timedelta(minutes=m)).isoformat(),'p':100-m,
                               'c':['@'],'z':'C','x':'V','i':m} for m in (1,3)]}
        track_positions(now,fetch_interval=fetch,fetch_corporate=lambda *a:{'clear':True,'pages':[]})
        with contextlib.redirect_stdout(io.StringIO()):run(now)
        rows=list(csv.DictReader(Path('input/trades.csv').read_text().splitlines()))
        self.assertEqual(rows[0]['exit_reason'],'SCENARIO_DETERIORATION')
        self.assertEqual(rows[0]['exit_price'],'97.0')
        self.assertEqual(rows[0]['exit_time_jst'],(AT+timedelta(minutes=3)).isoformat())

    def test_late_impairment_evidence_requires_replay_not_later_exit(self):
        rows=self.enter()
        rows[0]['tracked_through_jst']=(AT+timedelta(minutes=10)).isoformat()
        with Path('input/trades.csv').open('w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
        self.prepare_impairment()
        report=track_positions(AT+timedelta(hours=1),
            fetch_interval=lambda *a:self.fail('cannot silently use later trades'),
            fetch_corporate=lambda *a:self.fail('must detect need for historical reconciliation first'))
        self.assertEqual(report['positions'][0]['status'],'BLOCKED')
        self.assertIn('historical reconciliation',report['positions'][0]['reason'])
        with contextlib.redirect_stdout(io.StringIO()):run(AT+timedelta(hours=1))
        self.assertEqual(json.loads(Path('output/trade_report.json').read_text())['statistics']['total']['closed'],0)

    def test_known_impairment_survives_intervals_with_no_trades(self):
        self.enter();self.prepare_impairment()
        now=AT+timedelta(minutes=21)
        def empty(symbol,start,end,*args):
            return {'symbol':symbol,'start':start.isoformat(),'end':end.isoformat(),
                    'complete':True,'feed':'sip','retrieved_at':now.isoformat(),'pages':[],'trades':[]}
        track_positions(now,fetch_interval=empty,fetch_corporate=lambda *a:{'clear':True,'pages':[]})
        with contextlib.redirect_stdout(io.StringIO()):run(now)
        now=AT+timedelta(hours=1)
        def first(symbol,start,end,*args):
            return {'symbol':symbol,'start':start.isoformat(),'end':end.isoformat(),
                    'complete':True,'feed':'sip','retrieved_at':now.isoformat(),'pages':[],
                    'trades':[{'t':(AT+timedelta(minutes=6)).isoformat(),'p':99,
                               'c':['@'],'z':'C','x':'V','i':6}]}
        report=track_positions(now,fetch_interval=first,fetch_corporate=lambda *a:{'clear':True,'pages':[]})
        self.assertEqual(report['positions'][0]['status'],'HISTORY_READY')
        with contextlib.redirect_stdout(io.StringIO()):run(now)
        rows=list(csv.DictReader(Path('input/trades.csv').read_text().splitlines()))
        self.assertEqual(rows[0]['exit_reason'],'SCENARIO_DETERIORATION')
        self.assertEqual(rows[0]['exit_time_jst'],(AT+timedelta(minutes=6)).isoformat())
