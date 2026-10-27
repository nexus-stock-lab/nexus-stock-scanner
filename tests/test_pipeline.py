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

from daily_runner import freeze_research, prepare_plans, reconcile, track_positions
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
