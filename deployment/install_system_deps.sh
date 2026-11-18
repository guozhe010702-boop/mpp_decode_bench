#!/bin/bash
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo 'Run with sudo'; exit 1; }
apt-get update
apt-get install -y python3 python3-gi python3-cairo python3-gi-cairo gir1.2-pango-1.0 fonts-noto-cjk libdrm2 v4l-utils psmisc x11-xserver-utils util-linux build-essential cmake pkg-config libdrm-dev
