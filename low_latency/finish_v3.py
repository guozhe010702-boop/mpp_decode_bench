#!/usr/bin/env python3
"""Transient service stop hook: restore devices, then publish only verified success."""
import json
from pathlib import Path
import subprocess
import sys
import traceback
from control import atomic_json, restore

ROOT=Path(__file__).resolve().parent


def main():
    state={'stage':'restoring','published':False}
    try:
        restore()
        state['stage']='checking_full_acceptance'
        atomic_json(ROOT/'v3-release-job.json',state)
        from finalize_v3 import main as finalize
        finalize()
        state['published']=True
        subprocess.run([sys.executable,str(ROOT/'report_v3.py'),'--output',str(ROOT/'V3_RESULTS.md')],check=True)
        state.update(stage='complete',published=True)
    except Exception as error:
        state.update(stage='published_report_error' if state['published'] else 'not_published',
                     error=str(error),traceback=traceback.format_exc())
        # A manual stop or failed endurance must never replace the validated default.
        print(json.dumps(state),file=sys.stderr)
    finally:
        atomic_json(ROOT/'v3-release-job.json',state)
    return 0 if state['published'] else 1


if __name__=='__main__': sys.exit(main())
