#!/bin/bash
set -euo pipefail
root="$(cd -- "$(dirname -- "$0")" && pwd)"
python3 - "$root/build/imx415_low_latency" <<'PY'
import os
from pathlib import Path
import sys
target = str(Path(sys.argv[1]).resolve())
for entry in Path('/proc').iterdir():
    if not entry.name.isdigit():
        continue
    try:
        actual = os.readlink(entry / 'exe')
        if actual == target or actual == target + ' (deleted)':
            raise SystemExit('Refusing to replace running native preview, PID ' + entry.name)
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        pass
PY
cmake -S "$root" -B "$root/build" -DCMAKE_BUILD_TYPE=Release
cmake --build "$root/build" -j4
