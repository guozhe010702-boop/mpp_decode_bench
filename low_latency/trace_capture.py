#!/usr/bin/env python3
"""Isolated V4L2 trace instance around a normally supervised preview."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent


def percentile(values, q):
    import math
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * q) - 1)] if values else None


def analyze(trace, frames_path=None):
    events = []
    pattern = re.compile(r'\s(\d+\.\d+): (vb2_v4l2_\w+): minor=(\d+).*?timestamp = (\d+).*?sequence = (\d+)')
    for line in trace.splitlines():
        match = pattern.search(line)
        if match:
            stamp, kind, minor, driver, sequence = match.groups()
            events.append(dict(at_us=round(float(stamp)*1e6), kind=kind, minor=int(minor),
                               driver_us=int(driver)//1000, sequence=int(sequence)))
    groups = {}
    for event in events:
        key = (event['minor'], event['sequence'], event['driver_us'])
        groups.setdefault(key, {})[event['kind']] = event
    result = {}
    for minor in (0, 44):
        rows = []
        for (device, sequence, driver), pair in groups.items():
            done = pair.get('vb2_v4l2_buf_done')
            dq = pair.get('vb2_v4l2_dqbuf')
            if device == minor and done and dq and sequence > 300 and 0 <= done['at_us']-driver < 1000000:
                rows.append((done['at_us']-driver, dq['at_us']-done['at_us']))
        result[str(minor)] = {'matched_frames': len(rows)}
        for index, name in enumerate(('driver_to_buf_done_ms', 'buf_done_to_dqbuf_ms')):
            values = [row[index]/1000 for row in rows]
            result[str(minor)][name] = {k: percentile(values, q) for k,q in [('p50',.5),('p95',.95),('p99',.99)]}
    if frames_path:
        import csv
        with frames_path.open() as stream:
            known = {int(row['driver_us']): row for row in csv.DictReader(stream) if int(row['id']) > 300}
        generic = re.compile(r'\s(\d+\.\d+): (vb2_(?:buf_done|dqbuf)): owner = (\w+),.*?index = (\d+), type = (\d+),.*?timestamp = (\d+)')
        pairs = {}
        native_owners = set()
        for line in trace.splitlines():
            match = generic.search(line)
            if not match:
                continue
            stamp, kind, owner, index, type_, driver = match.groups()
            if kind == 'vb2_dqbuf' and 'imx415_low_late-' in line:
                native_owners.add(owner)
            driver_us = int(driver)//1000
            if driver_us not in known or int(type_) != 9:
                continue
            pairs.setdefault((owner,index,driver_us), {})[kind] = round(float(stamp)*1e6)
        values = []
        owners = set()
        for (owner,index,driver), pair in pairs.items():
            if owner in native_owners and 'vb2_buf_done' in pair and 'vb2_dqbuf' in pair and 0 <= pair['vb2_buf_done']-driver < 1000000:
                owners.add(owner)
                values.append(((pair['vb2_buf_done']-driver)/1000,
                               (pair['vb2_dqbuf']-pair['vb2_buf_done'])/1000,
                               (int(known[driver]['dq_us'])-pair['vb2_dqbuf'])/1000))
        result['video44_by_exact_driver_timestamp'] = {'owners': sorted(owners), 'matched_frames': len(values)}
        for i,name in enumerate(('driver_to_buf_done_ms','buf_done_to_kernel_dq_ms','kernel_to_application_dq_ms')):
            result['video44_by_exact_driver_timestamp'][name] = {k:percentile([v[i] for v in values],q) for k,q in [('p50',.5),('p95',.95),('p99',.99)]}
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=int, default=30)
    parser.add_argument('--buffers', type=int, default=8)
    parser.add_argument('--capture-startup-poll-ms', type=int, default=0)
    parser.add_argument('--capture-layout', choices=['nv12m','nv12'])
    parser.add_argument('--encoder-input', choices=['copy','dmabuf'])
    parser.add_argument('--overlay-preparation', choices=['inline','worker'])
    parser.add_argument('--decoder-output', choices=['poll','wait'])
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    instance = Path('/sys/kernel/debug/tracing/instances') / ('imx415_v3_' + str(time.monotonic_ns()))
    instance.mkdir()
    try:
        (instance/'tracing_on').write_text('0')
        (instance/'trace_clock').write_text('mono')
        (instance/'buffer_size_kb').write_text('8192')
        for name in ('vb2_v4l2_buf_done', 'vb2_v4l2_dqbuf', 'vb2_v4l2_qbuf'):
            event = instance/'events/v4l2'/name
            (event/'enable').write_text('1')
        for name in ('vb2_buf_done', 'vb2_dqbuf'):
            (instance/'events/vb2'/name/'enable').write_text('1')
        (instance/'tracing_on').write_text('1')
        command = [sys.executable, str(ROOT/'control.py'), 'run', '--seconds', str(args.seconds),
                   '--buffers', str(args.buffers), '--capture-startup-poll-ms', str(args.capture_startup_poll_ms),
                   '--output', str(args.output/'preview')]
        for name in ('capture_layout','encoder_input','overlay_preparation','decoder_output'):
            value=getattr(args,name)
            if value is not None: command += ['--'+name.replace('_','-'), value]
        with (args.output/'controller.log').open('w') as log:
            run = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
        (instance/'tracing_on').write_text('0')
        trace = (instance/'trace').read_text()
        (args.output/'v4l2-trace.txt').write_text(trace)
        stats = {p.parent.name: p.read_text() for p in (instance/'per_cpu').glob('cpu*/stats')}
        result = {'returncode': run.returncode, 'trace_clock': 'mono', 'per_cpu_stats': stats,
                  'timing': analyze(trace, args.output/'preview/frames.csv')}
        native = result['timing'].get('video44_by_exact_driver_timestamp', {})
        clean = all(not any(int(value) for value in re.findall(r'^(?:overrun|commit overrun|dropped events):\s*(\d+)', text, re.M)) for text in stats.values())
        result['valid_timing_evidence'] = bool(native.get('matched_frames', 0) >= 100 and len(native.get('owners', [])) == 1 and clean)
        (args.output/'trace-analysis.json').write_text(json.dumps(result, indent=2)+'\n')
        print(json.dumps(result))
        return run.returncode or (0 if result['valid_timing_evidence'] else 2)
    finally:
        (instance/'tracing_on').write_text('0')
        instance.rmdir()


if __name__ == '__main__':
    sys.exit(main())
