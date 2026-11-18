#!/bin/bash
# Read-only; does not stop desktop or start capture.
set -euo pipefail
[ "$(uname -m)" = aarch64 ]
[ "$(id -u)" = 0 ]
root=/userdata/av_dev/mpp_decode_bench/low_latency
sdk=/userdata/av_dev/ffmpeg_pipeline/runtime
for tool in python3 v4l2-ctl systemctl systemd-run taskset xrandr; do command -v "$tool" >/dev/null; done
command -v fuser >/dev/null || command -v lsof >/dev/null
for node in /dev/video44 /dev/v4l-subdev2 /dev/mpp_service; do test -e "$node"; done
ls /dev/dri/card* >/dev/null
test -x "$root/build/imx415_low_latency"
python3 - <<'PY'
import cairo,gi,sys
gi.require_version('Pango','1.0'); gi.require_version('PangoCairo','1.0')
from gi.repository import Pango,PangoCairo
sys.path.insert(0,'/userdata/av_dev/ffmpeg_pipeline')
from camera_control.support import camera_owners,sensor_frame_rate
from camera_control.raw_scheduler import RawScheduler
fps=sensor_frame_rate('/dev/video44')
print('Sensor FPS:',fps,'camera owners:',camera_owners('/dev/video44'))
if fps is None or fps<59: raise SystemExit('Configure the BSP sensor at 60fps first')
PY
LD_LIBRARY_PATH="$sdk/lib" ldd "$root/build/imx415_low_latency"
if LD_LIBRARY_PATH="$sdk/lib" ldd "$root/build/imx415_low_latency" | grep -q 'not found'; then exit 1; fi
sha256sum "$sdk/lib/librockchip_mpp.so"
systemctl is-active rkaiq_3A.service
systemctl show display-manager -p Id --value
fc-match ':lang=zh'
echo 'Preflight passed. Actual startup still validates HDMI planes/NV12/scaling.'
