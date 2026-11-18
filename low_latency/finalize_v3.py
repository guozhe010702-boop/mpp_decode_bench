#!/usr/bin/env python3
"""Publish a candidate only after comparison, fault recovery and full endurance pass."""
import datetime
import json
from pathlib import Path
import statistics
import subprocess
import sys
from control import atomic_json
from audit_v3 import audit

ROOT=Path(__file__).resolve().parent


def read(path): return json.loads(path.read_text())


def active(unit):
    return subprocess.run(['systemctl','is-active','--quiet',unit]).returncode==0


def main():
    results=ROOT/'results'
    candidate=read(ROOT/'v3-candidate.json')
    state=read(results/'v3-dma-comparison-20261117/experiment.json')
    initial=read(results/'v3-initial-comparison-20261117/experiment.json')
    stress=read(results/'v3-dma-stress-20261117/experiment.json')
    assert state['completed'] and initial['completed'] and stress['completed']
    for row in state['results']+stress['results']:
        assert row['meets_internal_targets'] and row['restored'],row['name']
        audit(Path(row['output']))
    hud=next(r for r in stress['results'] if r['name']=='dma-hud-delay300ms')
    stalled=next(r for r in stress['results'] if r['name']=='dma-processing-stall250ms')
    assert hud['summary']['max_overlay_worker_ms']>=250 and hud['cadence']['interval_max_ms']<50
    assert stalled['stall_recovery']['stall_observed'] and stalled['stall_recovery']['first_fresh_frame_dq_to_flip_ms']<=100
    lifecycle=read(results/'v3-lifecycle-20261117/lifecycle.json')
    assert len(lifecycle)==4 and all(row['passed'] for row in lifecycle)
    keys=('codec','buffers','capture_layout','encoder_input','overlay_preparation','decoder_output','display_margin_us')
    assert all(all(row['requested_profile'][key]==candidate[key] for key in keys) for row in lifecycle)
    output=results/'v3-final-60min-20261117'
    final=read(output/'analysis.json');s=final['summary']
    assert final['completed'] and final['meets_internal_targets'] and final['restored'] and s['elapsed_s']>=3600
    assert s['codec']==candidate['codec'] and s['capture_buffers_actual']==candidate['buffers']
    for key in ('capture_layout','encoder_input','overlay_preparation','decoder_output','display_margin_us'):
        assert s[key]==candidate[key],key
    assert s['overlay_updates']>=.8*s['elapsed_s'],'Status overlay stopped updating'
    extra=audit(output)
    manifest=read(output/'manifest.json')
    baseline_manifest=read(results/'v3-dma-comparison-20261117/hevc-dma-wait-1/manifest.json')
    assert manifest['sha256']==baseline_manifest['sha256'],'Core source or binary changed after comparison'
    boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    assert boot==manifest['boot_id']==state['boot_id']==initial['boot_id']==stress['boot_id'],'Unexpected reboot'
    sys.path.insert(0,'/userdata/av_dev/ffmpeg_pipeline')
    from camera_control.raw_scheduler import LinuxTasks
    from camera_control.support import camera_owners
    tasks=LinuxTasks();workers=[dict(row,**tasks.get(row['tid'])) for row in tasks.discover()]
    restored=read(ROOT/'active.json')
    final_state={'boot_id':boot,'same_build_as_comparison':True,'camera_owners':camera_owners('/dev/video44'),
        'raw_workers':workers,'raw_scheduler_journal_exists':(ROOT/'state/raw-scheduler.json').exists(),
        'restored':restored.get('restored'),'capture_scheduler_restored':restored.get('capture_scheduler_restored'),
        'desktop_active':active(restored['display_service']),'desktop_was_active':restored['desktop_was_active'],
        'preview_active':active('imx415-low-latency.service'),'benchmark_active':active('imx415-latency-benchmark.service')}
    assert not final_state['camera_owners'] and not final_state['raw_scheduler_journal_exists']
    assert final_state['restored'] and final_state['capture_scheduler_restored']
    assert not final_state['preview_active'] and not final_state['benchmark_active']
    assert not final_state['desktop_was_active'] or final_state['desktop_active']
    assert all(row['policy']==0 and row['priority']==0 for row in workers)
    atomic_json(output/'final-state.json',final_state)
    old=ROOT/'recommended-before-v3.json'
    if not old.exists(): old.write_bytes((ROOT/'recommended.json').read_bytes())
    profile=dict(candidate,validation_status='passed',endurance_status='passed',continuous_test_seconds=3600,
                 endurance_seconds=3600,validation_output=str(results/'v3-dma-comparison-20261117'),endurance_output=str(output))
    base=[row for row in state['results'] if row['name'].startswith('nv12m-copy-poll')]
    new=[row for row in state['results'] if row['name'].startswith('hevc-dma-wait')]
    h264=[row for row in state['results'] if row['name'].startswith('h264-dma-wait')]
    release={'status':'published','published_at':datetime.datetime.now().astimezone().isoformat(),
        'profile':profile,'baseline_mean_cpu_one_core_percent':statistics.mean(row['mean_cpu_one_core_percent'] for row in base),
        'dma_mean_cpu_one_core_percent':statistics.mean(row['mean_cpu_one_core_percent'] for row in new),
        'baseline_round_mean_p95_ms':statistics.mean(row['summary']['latency']['dq_to_flip_ms']['p95'] for row in base),
        'dma_round_mean_p95_ms':statistics.mean(row['summary']['latency']['dq_to_flip_ms']['p95'] for row in new),
        'h264_round_mean_p95_ms':statistics.mean(row['summary']['latency']['dq_to_flip_ms']['p95'] for row in h264),
        'final_rates':final['rates'],'final_internal_latency':s['latency']['dq_to_flip_ms'],
        'final_driver_timestamp_latency':s['latency']['driver_timestamp_to_flip_ms'],
        'final_cpu_one_core_percent':final['mean_cpu_one_core_percent'],'final_temperature_c':final['max_temperature_c'],
        'final_rss_mib':final['max_rss_mib'],'extra_acceptance':extra,'final_state':final_state,
        'notes':['Round P95 means are not merged-frame quantiles.','No optical end-to-end claim.',
                 'The earlier spontaneous reboot root cause remains unconfirmed.']}
    atomic_json(ROOT/'v3-release.json',release)
    atomic_json(ROOT/'recommended.json',profile)
    print(json.dumps(release))


if __name__=='__main__': main()
