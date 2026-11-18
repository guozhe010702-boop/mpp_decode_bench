#!/usr/bin/env python3
"""Run a specified finite experiment matrix; never publish defaults automatically."""
import argparse
import csv
import json
import math
from pathlib import Path
import signal
import subprocess
import sys
import time
from control import atomic_json

ROOT = Path(__file__).resolve().parent


def cadence(output):
    timing = json.loads((output/'timing.json').read_text())
    cutoff = timing['start_monotonic_us'] + timing['warmup_seconds']*1000000
    with (output/'frames.csv').open() as stream:
        rows = [{k:(v if k == 'outcome' else int(v)) for k,v in row.items()}
                for row in csv.DictReader(stream) if row['outcome']=='displayed' and int(row['dq_us']) >= cutoff]
    rows.sort(key=lambda row:row['flip_event_us'])
    intervals = sorted((b['flip_event_us']-a['flip_event_us'])/1000 for a,b in zip(rows,rows[1:]))
    misses = [r for r in rows if r['display_target_us'] and r['flip_event_us']>r['display_target_us']+8333]
    return {'displayed_frames':len(rows), 'interval_p99_ms': intervals[math.ceil(len(intervals)*.99)-1],
            'interval_max_ms': max(intervals), 'interval_over25ms':sum(x>25 for x in intervals),
            'interval_over41_7ms':sum(x>41.7 for x in intervals), 'misses':len(misses),
            'misses_with_decode_after_target':sum(r['dec_out_us']>r['display_target_us'] for r in misses),
            'misses_with_commit_after_target':sum(r['display_in_us']>r['display_target_us'] for r in misses)}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--matrix',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    args = p.parse_args()
    args.output=args.output.resolve()
    args.output.mkdir(parents=True,exist_ok=False)
    cases=json.loads(args.matrix.read_text())
    state={'running':True,'completed':False,'results':[], 'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    child=None
    stopped=False
    def stop(signum,frame):
        nonlocal stopped
        stopped=True
        if child and child.poll() is None: child.terminate()
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    def save():
        atomic_json(args.output/'experiment.json',state)
    try:
        for case in cases:
            if stopped: raise InterruptedError('Experiment stopped')
            state['current_case']=case['name'];save()
            output=args.output/case['name']
            cmd=[sys.executable,str(ROOT/'control.py'),'run','--output',str(output)]+case['args']
            with (args.output/(case['name']+'.log')).open('w') as log:
                child=subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT)
                while child.poll() is None: time.sleep(.25)
            if stopped: raise InterruptedError('Experiment stopped')
            result=json.loads((output/'analysis.json').read_text())
            result.update(name=case['name'],output=str(output),cadence=cadence(output),args=case['args'])
            state['results'].append(result);save()
            assert child.returncode==0 and result['meets_internal_targets'],case['name']
            assert Path('/proc/sys/kernel/random/boot_id').read_text().strip()==state['boot_id'],'Boot changed'
            print(json.dumps({'case':case['name'],'fps':result['rates']['displayed_fps'],
                              'p95_ms':result['summary']['latency']['dq_to_flip_ms']['p95'],
                              'cadence':result['cadence']}),flush=True)
        state['completed']=True
    except Exception as error:
        state['error']=str(error);raise
    finally:
        if child and child.poll() is None:
            child.terminate()
            try: child.wait(timeout=12)
            except subprocess.TimeoutExpired: child.kill();child.wait()
        state['running']=False;save()


if __name__=='__main__':
    main()
