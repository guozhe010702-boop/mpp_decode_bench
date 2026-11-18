#!/usr/bin/env python3
"""Live IMX415 4K60 MPP encode/decode with a fullscreen HDMI preview."""

import argparse
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

from live_mpp_bench import CAPTURE, progress, stop_process, temperatures


ROOT = Path(__file__).resolve().parent
PIPELINE = Path("/userdata/av_dev/ffmpeg_pipeline")


def save_status(value):
    temporary = ROOT / "hdmi_status.tmp"
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(ROOT / "hdmi_status.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=0, help="0 keeps preview running")
    parser.add_argument("--bitrate", default="20M")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.seconds < 0:
        parser.error("seconds must be >= 0")
    output = args.output or ROOT / "results" / datetime.datetime.now().strftime("hdmi-%Y%m%d-%H%M%S")
    output.mkdir(parents=True, exist_ok=False)
    lock = Path("/tmp/imx415-live-mpp-bench.lock").open("a+")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.environ["DISPLAY"] = ":0"
    authorities = sorted(Path("/run/user").glob("*/gdm/Xauthority"))
    if not authorities:
        raise RuntimeError("No desktop Xauthority was found")
    os.environ["XAUTHORITY"] = str(authorities[0])
    os.environ["GDK_BACKEND"] = "x11"
    os.environ["GST_GL_PLATFORM"] = "egl"
    os.environ["GST_GL_API"] = "gles2"
    os.environ["GST_GL_WINDOW"] = "x11"

    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gst", "1.0")
    gi.require_version("GstVideo", "1.0")
    gi.require_version("GdkX11", "3.0")
    from gi.repository import Gtk, Gdk, GdkX11, Gst, GstVideo
    sys.path.insert(0, str(PIPELINE))
    from camera_control.support import camera_owners, sensor_frame_rate, mpp_device_open
    from camera_control.metrics import proc_usage

    Gst.init(None)
    stop_requested = [False]
    def request_stop(*unused):
        stop_requested[0] = True
        return True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    status = {"running": False, "stop_reason": "initializing", "output": str(output),
              "capture_size": [3840, 2160], "decode_size": [3840, 2160],
              "requested_fps": 60, "codec": "hevc", "bitrate": args.bitrate}
    encoder = gst_pipeline = window = None
    saved_format = None
    started = time.monotonic()
    last_at = None
    last_rendered = 0
    previous_cpu = {}
    steady_baseline = None
    last_display_at = None
    decoder_frames = [0]
    queue_overruns = [0]
    try:
        owners = camera_owners("/dev/video44")
        if owners:
            raise RuntimeError("Camera already open by PIDs " + ",".join(owners))
        status["sensor_reported_fps"] = sensor_frame_rate("/dev/video44")
        if status["sensor_reported_fps"] is None or status["sensor_reported_fps"] < 59:
            raise RuntimeError("Sensor is not currently in a 60 fps mode")
        modes = subprocess.check_output(["xrandr", "--query"], text=True)
        match = re.search(r"^(HDMI\S+) connected(?: primary)? (\d+)x(\d+)\+(-?\d+)\+(-?\d+)", modes, re.M)
        if not match:
            raise RuntimeError("No active HDMI desktop output")
        connector, width, height, x, y = match.groups()
        width, height, x, y = map(int, (width, height, x, y))
        status["hdmi_connector"] = connector
        status["hdmi_size"] = [width, height]
        block = modes[modes.index(match.group(0)):]
        current_mode = re.search(r"^\s+\d+x\d+\s+.*?([\d.]+)\*", block, re.M)
        status["hdmi_refresh_hz"] = float(current_mode[1]) if current_mode else None
        original = subprocess.check_output(["v4l2-ctl", "-d", "/dev/video44", "--get-fmt-video"], text=True)
        size = re.search(r"Width/Height\s*:\s*(\d+)/(\d+)", original)
        pixel = re.search(r"Pixel Format\s*:\s*'([^']+)'", original)
        if not size or not pixel:
            raise RuntimeError("Could not save the original capture format")
        saved_format = "width={},height={},pixelformat={}".format(size[1], size[2], pixel[1])
        ffmpeg = str(PIPELINE / "runtime/bin/ffmpeg")
        encoder_command = [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info",
            "-stats_period", "0.5", "-progress", str(output / "encoder.progress"),
            "-f", "v4l2", "-capture_buffers", "16", "-input_format", "nv12",
            "-video_size", "3840x2160", "-framerate", "60", "-i", "/dev/video44",
            "-map", "0:v:0", "-an", "-vf", "setpts=N/(60*TB)",
            "-c:v", "hevc_rkmpp", "-rc_mode", "CBR", "-b:v", args.bitrate,
            "-g", "60", "-bf", "0", "-fps_mode", "passthrough",
            "-flush_packets", "1", "-f", "hevc", "pipe:1"]
        child_env = dict(os.environ, LD_LIBRARY_PATH=str(PIPELINE / "runtime/lib") + ":" + str(PIPELINE / "runtime/lib64"))
        child_env.pop("LD_PRELOAD", None)
        (output / "commands.json").write_text(json.dumps({"encoder": encoder_command}, indent=2))

        window = Gtk.Window(title="IMX415 4K60 MPP / HDMI")
        window.set_default_size(width, height)
        window.move(x, y)
        window.connect("delete-event", request_stop)
        window.connect("key-press-event", lambda widget, event: request_stop() if event.keyval == Gdk.KEY_Escape else False)
        area = Gtk.DrawingArea()
        window.add(area)
        window.show_all()
        screen = window.get_screen()
        monitor = screen.get_monitor_at_point(x + width // 2, y + height // 2)
        window.fullscreen_on_monitor(screen, monitor)
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        video_sink = Gst.ElementFactory.make("glimagesink", "hdmi-screen")
        if video_sink is None:
            raise RuntimeError("glimagesink is unavailable")
        # A live camera and the display clock can drift. Present arriving frames
        # immediately and bound the decoded queue, avoiding age-based QoS drops.
        video_sink.set_property("sync", False)
        video_sink.set_property("qos", False)
        video_sink.set_property("max-lateness", -1)
        GstVideo.VideoOverlay.set_window_handle(video_sink, area.get_window().get_xid())

        with (output / "encoder.log").open("w") as encoder_log, (output / "telemetry.jsonl").open("w") as telemetry:
            encoder = subprocess.Popen(encoder_command, env=child_env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=encoder_log)
            description = (
                "fdsrc fd={} blocksize=65536 ! h265parse "
                "! mppvideodec name=mpp ignore-error=false "
                "! video/x-raw,format=NV12,width=3840,height=2160 "
                "! queue name=display_queue max-size-buffers=2 max-size-bytes=0 "
                "max-size-time=0 leaky=downstream "
                "! fpsdisplaysink name=fps text-overlay=false silent=true sync=false"
            ).format(encoder.stdout.fileno())
            gst_pipeline = Gst.parse_launch(description)
            fps_sink = gst_pipeline.get_by_name("fps")
            fps_sink.set_property("video-sink", video_sink)
            display_queue = gst_pipeline.get_by_name("display_queue")

            def decoded_frame(pad, info):
                decoder_frames[0] += 1
                return Gst.PadProbeReturn.OK

            def queue_full(element):
                queue_overruns[0] += 1

            gst_pipeline.get_by_name("mpp").get_static_pad("src").add_probe(
                Gst.PadProbeType.BUFFER, decoded_frame
            )
            display_queue.connect("overrun", queue_full)
            (output / "gstreamer-pipeline.txt").write_text(description + "\n")
            bus = gst_pipeline.get_bus()
            gst_pipeline.set_state(Gst.State.PLAYING)
            while not stop_requested[0]:
                while Gtk.events_pending():
                    Gtk.main_iteration_do(False)
                message = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
                if message:
                    if message.type == Gst.MessageType.ERROR:
                        error, detail = message.parse_error()
                        raise RuntimeError(str(error) + " " + str(detail))
                    raise RuntimeError("Display pipeline reached EOS")
                if encoder.poll() is not None:
                    raise RuntimeError("Encoder exited; see encoder.log")
                now = time.monotonic()
                if last_at is None or now - last_at >= 1:
                    rendered = int(fps_sink.get_property("frames-rendered"))
                    dropped = int(fps_sink.get_property("frames-dropped"))
                    if rendered > last_rendered:
                        last_display_at = now
                    status.update({"running": rendered > 0, "elapsed_s": now - started,
                        "rendered_frames": rendered,
                        "display_dropped_frames": dropped,
                        "decoded_frames": decoder_frames[0],
                        "display_queue_buffers": display_queue.get_property("current-level-buffers"),
                        "display_queue_time_ms": display_queue.get_property("current-level-time") / 1000000,
                        "display_queue_overruns": queue_overruns[0],
                        "display_clock_sync": video_sink.get_property("sync"),
                        "display_upstream_qos": video_sink.get_property("qos"),
                        "transport": "HEVC elementary stream",
                        "display_fps": (rendered - last_rendered) / (now - last_at) if last_at else None,
                        "encoder_pid": encoder.pid, "renderer_pid": os.getpid(),
                        "encoder_mpp_device_observed": mpp_device_open(encoder.pid),
                        "decoder_mpp_device_observed": mpp_device_open(os.getpid()),
                        "max_temperature_c": temperatures(),
                        "encoder_progress": progress(output / "encoder.progress")})
                    if rendered:
                        status["stop_reason"] = "running"
                    captures = CAPTURE.findall((output / "encoder.log").read_text(errors="replace"))
                    if captures:
                        capture = list(map(int, captures[-1]))
                        status["capture_frames"] = capture[0]
                        status["capture_gaps"] = capture[1]
                        status["actual_capture_size"] = capture[2:4]
                        status["capture_errors"] = capture[4]
                        if now - started >= 5 and rendered > 0:
                            if steady_baseline is None:
                                steady_baseline = (now, rendered, dropped, capture,
                                                   decoder_frames[0], queue_overruns[0])
                            base_at, base_frames, base_drops, base_capture, base_decoded, base_overruns = steady_baseline
                            duration = now - base_at
                            status["steady_elapsed_s"] = duration
                            status["display_fps_average"] = (rendered - base_frames) / duration if duration else None
                            status["decoder_fps_average"] = (decoder_frames[0] - base_decoded) / duration if duration else None
                            status["display_dropped_since_warmup"] = dropped - base_drops
                            status["display_queue_overruns_since_warmup"] = queue_overruns[0] - base_overruns
                            status["capture_gaps_since_warmup"] = capture[1] - base_capture[1]
                            status["capture_errors_since_warmup"] = capture[4] - base_capture[4]
                            if capture[5] > base_capture[5]:
                                status["capture_fps_monotonic"] = 1000000 * (capture[0] - base_capture[0]) / (capture[5] - base_capture[5])
                    caps = gst_pipeline.get_by_name("mpp").get_static_pad("src").get_current_caps()
                    if caps:
                        status["decoder_output_caps"] = caps.to_string()
                    for name, pid in (("encoder", encoder.pid), ("renderer", os.getpid())):
                        usage = proc_usage(pid)
                        if usage:
                            status[name + "_rss_mib"] = usage[2] / 1048576
                            if name in previous_cpu:
                                old_at, old_cpu = previous_cpu[name]
                                status[name + "_cpu_percent_one_core"] = 100 * (usage[1] - old_cpu) / (now - old_at)
                            previous_cpu[name] = (now, usage[1])
                    if rendered > 30 and now - started > 4 and "screenshot" not in status:
                        pixbuf = Gdk.pixbuf_get_from_window(Gdk.get_default_root_window(), x, y, width, height)
                        if pixbuf:
                            screenshot = output / "hdmi-preview.png"
                            pixbuf.savev(str(screenshot), "png", [], [])
                            status["screenshot"] = str(screenshot)
                    save_status(status)
                    telemetry.write(json.dumps(status) + "\n")
                    telemetry.flush()
                    last_at, last_rendered = now, rendered
                    if rendered == 0 and now - started > 25:
                        raise RuntimeError("No displayed frame within 25 seconds")
                    if last_display_at is not None and now - last_display_at > 8:
                        raise RuntimeError("Display output has stalled for more than 8 seconds")
                    if status["max_temperature_c"] is not None and status["max_temperature_c"] >= 72:
                        raise RuntimeError("Temperature reached 72 C")
                if args.seconds and now - started >= args.seconds:
                    break
                time.sleep(0.01)
            status["stop_reason"] = "stopped"
    except Exception as error:
        status["stop_reason"] = "error"
        status["error"] = str(error)
        print(str(error), file=sys.stderr, flush=True)
    finally:
        stop_process(encoder)
        if gst_pipeline is not None:
            gst_pipeline.set_state(Gst.State.NULL)
        if encoder is not None and encoder.stdout is not None:
            encoder.stdout.close()
        if window is not None:
            window.destroy()
        if saved_format:
            restored = subprocess.run(["v4l2-ctl", "-d", "/dev/video44", "--set-fmt-video=" + saved_format],
                                      capture_output=True, text=True)
            status["camera_format_restored"] = restored.returncode == 0
        status["running"] = False
        save_status(status)
        (output / "result.json").write_text(json.dumps(status, indent=2) + "\n")
        lock.close()
    return 1 if status["stop_reason"] == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
