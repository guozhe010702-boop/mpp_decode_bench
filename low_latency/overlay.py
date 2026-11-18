"""One-Hz Chinese status panel. Cairo ARGB32 is premultiplied DRM ARGB8888."""
import json
import math
from pathlib import Path
import re
import struct
import time
from collections import deque

import cairo
import gi
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Pango, PangoCairo

WIDTH, HEIGHT = 1152, 160


def text_file(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


class Panel:
    def __init__(self, path, output, codec, hz):
        self.path, self.output, self.codec, self.hz = path, output, codec, hz
        self.revision = 0
        self.previous = None
        self.saved = False
        self.history = deque(maxlen=4)

    def update(self, sample, status=None):
        status = status or {}
        now = sample.get("monotonic_s", time.monotonic())
        cpu_stat = text_file("/proc/stat")
        cpu = [int(v) for v in cpu_stat.splitlines()[0].split()[1:9]] if cpu_stat else None
        sample = dict(sample, cpu=cpu, native=status)
        if status and (not self.history or status.get("elapsed_s", 0) > self.history[-1].get("elapsed_s", 0)):
            self.history.append(status)
        system_cpu = process_cpu = None
        fps = {}
        if self.previous:
            old = self.previous
            dt = now - old["monotonic_s"]
            if dt > 0 and "cpu_ticks" in sample and "cpu_ticks" in old:
                process_cpu = (sample["cpu_ticks"] - old["cpu_ticks"]) * 100 / self.hz / dt
            if cpu and old.get("cpu"):
                deltas = [a - b for a, b in zip(cpu, old["cpu"])]
                if sum(deltas) > 0:
                    system_cpu = 100 * (1 - (deltas[3] + deltas[4]) / sum(deltas))
            before = self.history[0] if len(self.history) > 1 else old.get("native", {})
            period = status.get("elapsed_s", 0) - before.get("elapsed_s", 0)
            if period > .1:
                fps = {k: (status[k] - before[k]) / period for k in ("captured", "encoded", "decoded", "displayed")
                       if k in status and k in before}
        self.previous = sample
        def value(v, suffix="", precision=1):
            return "--" if v is None else f"{v:.{precision}f}" + suffix
        def rate(key):
            return value(fps.get(key))
        memory = text_file("/proc/meminfo") or ""
        total = re.search(r"MemTotal:\s*(\d+)", memory)
        available = sample.get("MemAvailable_kib")
        mem = "--"
        if total and available is not None:
            total = int(total[1])
            mem = f"{(total - available) / 1048576:.2f}/{total / 1048576:.2f} GiB"
        gpu = text_file("/sys/devices/platform/fb000000.gpu/utilisation")
        gpu = gpu + "%" if gpu and gpu.isdigit() else "--"
        npu = text_file("/sys/kernel/debug/rknpu/load") or ""
        cores = re.findall(r"Core\d+:\s*(\d+)%", npu)
        npu = "/".join(cores) + "%" if cores else "--"
        vpu = "工作中" if sample.get("mpp_fd_observed") and fps.get("encoded", 0) > 0 and fps.get("decoded", 0) > 0 else "等待"
        codec = {"hevc": "H.265", "h264": "H.264", "direct": "直显诊断"}[self.codec]
        lines = [
            f"IMX415 · 3840×2160 @ 60   |   MPP {codec}   |   HDMI 1920×1080 @ 60   |   内部延迟 {value(status.get('last_latency_ms'), ' ms')}",
            f"采集 {rate('captured')} FPS     编码 {rate('encoded')} FPS     解码 {rate('decoded')} FPS     显示 {rate('displayed')} FPS     VPU {vpu}",
            f"CPU 系统 {value(system_cpu, '%')}   进程 {value(process_cpu, '%/单核')}     GPU {gpu}     NPU 三核 {npu}     温度 {value(sample.get('temperature_c'), ' ℃')}",
            f"内存 已用/总量 {mem}     进程 RSS {value(sample.get('VmRSS', 0) / 1024, ' MiB')}     主动丢弃 {status.get('raw_drops', 0) + status.get('display_drops', 0) + status.get('stale_drops', 0)} 帧     错误 {status.get('errors', 0)}     状态每秒刷新",
        ]
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, WIDTH, HEIGHT)
        cr = cairo.Context(surface)
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        for i, text in enumerate(lines):
            cr.move_to(18, 7 + i * 40)
            color = (.40, .85, 1) if i == 0 else (.65, 1, .71) if i == 1 else (.94, .95, .98)
            layout = PangoCairo.create_layout(cr)
            layout.set_font_description(Pango.FontDescription("Noto Sans CJK SC 16"))
            layout.set_width((WIDTH - 36) * Pango.SCALE)
            layout.set_ellipsize(Pango.EllipsizeMode.END)
            layout.set_text(text, -1)
            PangoCairo.layout_path(cr, layout)
            cr.set_source_rgba(0, 0, 0, .85)
            cr.set_line_width(2)
            cr.stroke_preserve()
            cr.set_source_rgb(*color)
            cr.fill()
        surface.flush()
        self.revision += 1
        temp = self.path.with_suffix(".tmp")
        temp.write_bytes(struct.pack("<4sIII", b"RKOS", WIDTH, HEIGHT, self.revision) + bytes(surface.get_data()))
        temp.replace(self.path)
        if fps and not self.saved and status.get("elapsed_s", 0) >= 5:
            surface.write_to_png(str(self.output / "status-overlay.png"))
            self.saved = True
        sample["panel_fps"] = fps
        sample["system_cpu_percent"] = system_cpu
        sample["process_cpu_one_core_percent"] = process_cpu
        sample["gpu_percent"] = gpu
        sample["npu_core_percent"] = cores
        sample["panel_lines"] = lines
        (self.output / "panel-status.json").write_text(json.dumps(sample, ensure_ascii=False, indent=2) + "\n")
        return sample
