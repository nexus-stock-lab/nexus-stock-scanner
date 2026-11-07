"""Prospective hourly paper entry; no broker orders or retrospective plans."""
import json
from datetime import datetime,timedelta
from pathlib import Path
from entry_schedule import NY,JST,next_slot,session_date,session_slots,plan_path
from daily_runner import prepare_plans,reconcile,write_json
from paper_engine import run,digest


def prepare_next(now,root=Path('input'),out=Path('output'),clock=None):
    at=next_slot(now)
    if session_date(at)!=session_date(now):
        return {'status':'WAIT_NEXT_US_SESSION','next_checkpoint_jst':at.isoformat()}
    report=prepare_plans(now,root,out,at=at,rolling=True)
    completed=clock() if clock else now
    if completed>at:
        # Never publish a plan whose actual preparation completed after cutoff.
        plan_path(root,at).unlink(missing_ok=True)
        raise ValueError('plan preparation completed after checkpoint; no retrospective plan')
    report['prepared_at']=completed.isoformat()
    report['sha256']=digest({k:v for k,v in report.items() if k!='sha256'})
    write_json(plan_path(root,at),report)
    write_json(out/'plan_status.json',report)
    return report


def cycle(now,root=Path('input'),out=Path('output'),clock=None,
          prepare_fn=prepare_next,reconcile_fn=reconcile,run_fn=run):
    """Process pre-existing plans in chronological order, then prepare the future.

    Missing earlier plans are skipped, never reconstructed using known prices.
    Session review approval and evidence hashes remain mandatory at every slot.
    """
    results=[];missing=[]
    for at in session_slots(now.astimezone(NY).date()):
        if now<at+timedelta(minutes=16):continue
        if not plan_path(root,at).exists():
            missing.append(at.isoformat());continue
        frozen=json.loads(plan_path(root,at).read_text())
        receipt_path=Path(root)/'cycle_receipts'/session_date(at)/(at.astimezone(NY).strftime('%H%M')+'.json')
        if receipt_path.exists():
            receipt=json.loads(receipt_path.read_text())
            if (receipt.get('sha256')!=digest({k:v for k,v in receipt.items() if k!='sha256'})
                    or receipt.get('plan_sha256')!=digest(frozen)):
                raise ValueError('checkpoint receipt or reconciled plan changed')
            continue
        report=reconcile_fn(now,root,out,at=at,rolling=True,clock=clock)
        results.append(report)
        if report.get('status')=='VERIFIED_INPUT_READY':
            run_fn(clock() if clock else now,root=root,out=out)
        complete=(report.get('status')=='NO_VERIFIED_PLANS' or
                  (report.get('status')=='VERIFIED_INPUT_READY' and
                   all(p.get('status')=='REFERENCE_OBSERVED' for p in report.get('prices',[]))))
        if complete:
            receipt={'decision_time_jst':at.isoformat(),'plan_sha256':digest(frozen),
                     'observed_at':now.isoformat(),'report':report}
            receipt['sha256']=digest(receipt)
            write_json(receipt_path,receipt)
    upcoming=prepare_fn(clock() if clock else now,root,out,clock=clock)
    result={'mode':'HOURLY_REGULAR_SESSION','paper_only':True,
            'observed_at':now.isoformat(),'reconciliations':results,
            'missing_predecision_plans':missing,'next_preparation':upcoming}
    write_json(out/'cycle_status.json',result)
    return result
