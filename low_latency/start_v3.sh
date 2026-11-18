#!/bin/sh
# Manual V3 entry point; this does not install a boot service.
set -eu
root="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$root"
exec python3 control.py start --codec hevc --buffers 8 \
    --capture-layout nv12 --encoder-input dmabuf \
    --overlay-preparation worker --decoder-output wait \
    --display-scheduling deadline --display-margin-us 3500 "$@"
