#!/usr/bin/env python3
"""Verify transient service state semantics without opening any camera device."""
import json
from pathlib import Path
import subprocess

result=subprocess.run(['systemctl','is-active','imx415-v3-hook-probe.service'],text=True,stdout=subprocess.PIPE)
evidence={'state_during_stop_hook':result.stdout.strip(),'is_active_returncode':result.returncode}
assert result.returncode!=0, evidence
(Path(__file__).resolve().parent/'results/v3-stop-hook-probe.json').write_text(json.dumps(evidence,indent=2)+'\n')
