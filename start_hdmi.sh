#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if systemctl is-active --quiet imx415-hdmi-live.service; then
    printf '%s\n' 'HDMI preview is already running.'
    exit 0
fi
systemd-run --unit=imx415-hdmi-live --collect \
    --property="WorkingDirectory=$root" --property=KillMode=mixed \
    --property=TimeoutStopSec=15 /usr/bin/python3 "$root/live_hdmi.py" "$@"
printf '%s\n' 'HDMI preview started. Press Escape on the board or run bash stop_hdmi.sh to stop.'
