#!/usr/bin/env python3
"""Check DMA-input accounting and save additional release acceptance evidence."""
import argparse
import json
from pathlib import Path
from control import atomic_json
from evaluate_v3 import cadence


def audit(output):
    result=json.loads((output/'analysis.json').read_text())
    s=result['summary']
    assert result['completed'] and result['meets_internal_targets'] and result['restored']
    assert s.get('overlay_errors',0)==0
    details={'passed':True,'encoder_input':s.get('encoder_input','copy'),'cadence':cadence(output)}
    if s['codec']!='direct':
        shared=s.get('input_shared_frames',0)
        copied=s.get('input_copied_frames',0)
        assert s['encoded']<=shared+copied<=s['encoded']+2,'Input/encoded accounting mismatch'
        if s.get('encoder_input')=='dmabuf':
            assert s.get('capture_layout')=='nv12' and copied==0 and shared>0
            assert 0<s['camera_dma_imports']<=s['capture_buffers_actual']
            assert 0<s['held_camera_peak']<=2
            quality=result.get('quality_matched_single_frame',{})
            assert quality.get('psnr') is not None and quality.get('ssim') is not None,'Missing matching-source quality evidence'
            streams=quality.get('stream_probe',{}).get('streams',[])
            assert len(streams)==1 and streams[0]['width']==3840 and streams[0]['height']==2160 and streams[0]['has_b_frames']==0
            details.update(camera_imports=s['camera_dma_imports'],camera_buffers=s['capture_buffers_actual'],
                           held_camera_peak=s['held_camera_peak'],input_shared_frames=shared,
                           input_copied_frames=copied,matching_source_quality=quality)
        else:
            assert shared==0 and copied>0
    atomic_json(output/'extra-acceptance.json',details)
    return details


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('output',type=Path)
    args=p.parse_args();print(json.dumps(audit(args.output)))
