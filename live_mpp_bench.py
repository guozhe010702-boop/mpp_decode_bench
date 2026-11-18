#!/usr/bin/env python3
"""Measure live IMX415 capture -> MPP encode -> MPP decode on RK3588."""

import argparse
import csv
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


CAPTURE = re.compile(
    r"AV_CAPTURE frames=(\d+) gaps=(\d+) width=(\d+) height=(\d+) "
    r"errors=(\d+) mono_us=(\d+)"
)


def progress(path):
    if not path.exists():
        return {}
    lines = path.read_text().split("\n")[:-1]
    return dict(line.split("=", 1) for line in lines if "=" in line)


def stop_process(process):
    if process is None or process.poll() is not None:
        return
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def temperatures():
    values = []
    for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
        try:
            values.append(int((zone / "temp").read_text()) / 1000.0)
        except (OSError, ValueError):
            pass
    return max(values) if values else None


def measured_fps(samples, field):
    # Use changing output counters with wall time. Source PTS is not elapsed time.
    points = []
    for sample in samples:
        value = sample.get(field)
        if value is not None and (not points or value != points[-1][1]):
            points.append((sample["elapsed_s"], value))
    if len(points) < 2 or points[-1][0] <= points[0][0]:
        return None
    return (points[-1][1] - points[0][1]) / (points[-1][0] - points[0][0])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="/dev/video44")
    parser.add_argument("--codec", choices=("hevc", "h264"), default="hevc")
    parser.add_argument("--output-mode", choices=("drm", "nv12"), default="drm")
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--warmup", type=float, default=5)
    parser.add_argument("--bitrate", default="20M")
    parser.add_argument("--temperature-limit", type=float, default=72)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--ffmpeg", type=Path,
        default=Path("/userdata/av_dev/ffmpeg_pipeline/runtime/bin/ffmpeg"),
    )
    args = parser.parse_args()
    if args.seconds < 5 or args.warmup < 0:
        parser.error("seconds must be >= 5 and warmup must be >= 0")

    pipeline_root = args.ffmpeg.resolve().parents[2]
    sys.path.insert(0, str(pipeline_root))
    # Reuse the board project's camera discovery and Linux telemetry helpers.
    from camera_control.support import camera_owners, mpp_device_open, sensor_frame_rate
    from camera_control.metrics import memory_usage, proc_usage

    args.output.mkdir(parents=True, exist_ok=False)
    encoder_log = args.output / "encoder.log"
    decoder_log = args.output / "decoder.log"
    encoder_progress = args.output / "encoder.progress"
    decoder_progress = args.output / "decoder.progress"
    result = {
        "scope": "LIVE_IMX415_MPP_ENCODE_DECODE",
        "started_at": datetime.datetime.now().astimezone().isoformat(),
        "width": 3840, "height": 2160, "requested_fps": 60,
        "codec": args.codec, "output_mode": args.output_mode,
        "bitrate_target": args.bitrate,
        "requested_measurement_seconds": args.seconds,
        "warmup_seconds": args.warmup,
        "encoder_mpp_device_observed": False,
        "decoder_mpp_device_observed": False,
        "stop_reason": "initializing", "camera_format_restored": False,
        "kernel": os.uname().release,
        "memory_before": memory_usage(Path("/proc/meminfo").read_text()),
    }
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = ":".join(
        [str(pipeline_root / "runtime/lib"), str(pipeline_root / "runtime/lib64")]
    )
    env.pop("LD_PRELOAD", None)
    common = [str(args.ffmpeg), "-hide_banner", "-nostdin", "-nostats",
              "-loglevel", "info", "-stats_period", "0.5"]
    encoder_command = common + [
        "-progress", str(encoder_progress),
        "-f", "v4l2", "-capture_buffers", "16", "-input_format", "nv12",
        "-video_size", "3840x2160", "-framerate", "60", "-i", args.device,
        "-map", "0:v:0", "-an", "-vf", "setpts=N/(60*TB)",
        "-c:v", args.codec + "_rkmpp", "-rc_mode", "CBR", "-b:v", args.bitrate,
        "-g", "60", "-bf", "0", "-fps_mode", "passthrough",
        "-flush_packets", "1", "-muxdelay", "0", "-muxpreload", "0",
        "-f", "mpegts", "pipe:1",
    ]
    decoder_command = common + [
        "-progress", str(decoder_progress),
        "-hwaccel", "rkmpp", "-hwaccel_output_format", "drm_prime",
        "-c:v", args.codec + "_rkmpp", "-probesize", "2000000",
        "-analyzeduration", "200000", "-f", "mpegts", "-i", "pipe:0",
        "-map", "0:v:0", "-an", "-sn", "-dn",
    ]
    if args.output_mode == "nv12":
        decoder_command += ["-vf", "hwdownload,format=nv12"]
    decoder_command += ["-c:v", "wrapped_avframe", "-fps_mode", "passthrough",
                        "-f", "null", "-"]
    result["commands"] = {"encoder": encoder_command, "decoder": decoder_command}

    encoder = decoder = None
    samples = []
    previous = {}
    measuring_since = None
    first_output_at = None
    saved_format = None
    started = time.monotonic()
    lock = Path("/tmp/imx415-live-mpp-bench.lock").open("a+")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def interrupted(signum, frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, interrupted)
    try:
        owners = camera_owners(args.device)
        if owners:
            raise RuntimeError("Camera device is already open by PIDs " + ",".join(owners))
        result["sensor_reported_fps"] = sensor_frame_rate(args.device)
        if result["sensor_reported_fps"] is None or result["sensor_reported_fps"] < 59:
            raise RuntimeError("Sensor is not currently reporting a 60 fps mode")
        original = subprocess.check_output(
            ["v4l2-ctl", "-d", args.device, "--get-fmt-video"], text=True
        )
        (args.output / "camera-format-before.txt").write_text(original)
        size = re.search(r"Width/Height\s*:\s*(\d+)/(\d+)", original)
        pixel = re.search(r"Pixel Format\s*:\s*'([^']+)'", original)
        if not size or not pixel:
            raise RuntimeError("Could not save the original capture format")
        saved_format = "width={},height={},pixelformat={}".format(
            size[1], size[2], pixel[1]
        )
        with encoder_log.open("w") as enc_log, decoder_log.open("w") as dec_log:
            encoder = subprocess.Popen(
                encoder_command, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=enc_log,
            )
            decoder = subprocess.Popen(
                decoder_command, env=env, stdin=encoder.stdout,
                stdout=subprocess.DEVNULL, stderr=dec_log,
            )
            encoder.stdout.close()
            with (args.output / "samples.csv").open("w", newline="") as csv_file:
                writer = None
                while True:
                    now = time.monotonic()
                    enc = progress(encoder_progress)
                    dec = progress(decoder_progress)
                    capture_text = encoder_log.read_text(errors="replace")
                    captures = CAPTURE.findall(capture_text)
                    capture = list(map(int, captures[-1])) if captures else None
                    if capture:
                        result["actual_capture_dimensions"] = capture[2:4]
                        if capture[2:4] != [3840, 2160]:
                            raise RuntimeError("Capture dimensions differ from requested 3840x2160")
                    ram = memory_usage(Path("/proc/meminfo").read_text())
                    sample = {
                        "elapsed_s": now - started,
                        "capture_frames": capture[0] if capture else None,
                        "capture_gaps": capture[1] if capture else None,
                        "capture_errors": capture[4] if capture else None,
                        "capture_mono_us": capture[5] if capture else None,
                        "encoded_frames": int(enc.get("frame", 0)),
                        "decoded_frames": int(dec.get("frame", 0)),
                        "encoder_cpu_percent_one_core": None,
                        "decoder_cpu_percent_one_core": None,
                        "encoder_rss_mib": None, "decoder_rss_mib": None,
                        "board_available_mib": ram.get("totalBytes", 0) / 1048576
                            - ram.get("usedBytes", 0) / 1048576,
                        "max_temperature_c": temperatures(),
                    }
                    for name, process in (("encoder", encoder), ("decoder", decoder)):
                        usage = proc_usage(process.pid)
                        if usage:
                            if name in previous:
                                old_time, old_usage = previous[name]
                                if usage[0] == old_usage[0]:
                                    sample[name + "_cpu_percent_one_core"] = (
                                        100 * (usage[1] - old_usage[1]) / (now - old_time)
                                    )
                            previous[name] = (now, usage)
                            sample[name + "_rss_mib"] = usage[2] / 1048576
                        result[name + "_mpp_device_observed"] |= mpp_device_open(process.pid)
                    samples.append(sample)
                    if writer is None:
                        writer = csv.DictWriter(csv_file, fieldnames=list(sample))
                        writer.writeheader()
                    writer.writerow(sample)
                    csv_file.flush()
                    if sample["decoded_frames"] > 0 and first_output_at is None:
                        first_output_at = now
                    if first_output_at is not None and now >= first_output_at + args.warmup:
                        if measuring_since is None:
                            measuring_since = now
                        if now - measuring_since >= args.seconds:
                            result["stop_reason"] = "completed"
                            break
                    if encoder.poll() is not None or decoder.poll() is not None:
                        raise RuntimeError("A codec process exited before the measurement completed")
                    if first_output_at is None and now - started > 30:
                        raise RuntimeError("No decoded frame within 30 seconds")
                    if sample["max_temperature_c"] is not None and sample["max_temperature_c"] >= args.temperature_limit:
                        result["stop_reason"] = "temperature_limit"
                        break
                    time.sleep(0.25)
    except KeyboardInterrupt:
        result["stop_reason"] = "interrupted"
    except Exception as error:
        result["stop_reason"] = "error"
        result["error"] = str(error)
    finally:
        stop_process(encoder)
        stop_process(decoder)
        if saved_format:
            restored = subprocess.run(
                ["v4l2-ctl", "-d", args.device, "--set-fmt-video=" + saved_format],
                capture_output=True, text=True,
            )
            result["camera_format_restored"] = restored.returncode == 0
            result["camera_restore_message"] = restored.stderr.strip()
        lock.close()

    steady = [s for s in samples if measuring_since is not None
              and s["elapsed_s"] >= measuring_since - started]
    result["measurement_seconds"] = steady[-1]["elapsed_s"] - steady[0]["elapsed_s"] if len(steady) > 1 else 0
    result["encoder_fps_wall"] = measured_fps(steady, "encoded_frames")
    result["decoder_fps_wall"] = measured_fps(steady, "decoded_frames")
    capture_points = [s for s in steady if s["capture_mono_us"] is not None]
    if len(capture_points) > 1 and capture_points[-1]["capture_mono_us"] > capture_points[0]["capture_mono_us"]:
        first, last = capture_points[0], capture_points[-1]
        result["capture_fps_monotonic"] = 1000000 * (last["capture_frames"] - first["capture_frames"]) / (last["capture_mono_us"] - first["capture_mono_us"])
        result["capture_gaps_during_measurement"] = last["capture_gaps"] - first["capture_gaps"]
        result["capture_errors_during_measurement"] = last["capture_errors"] - first["capture_errors"]
    for field in ("encoder_cpu_percent_one_core", "decoder_cpu_percent_one_core",
                  "encoder_rss_mib", "decoder_rss_mib", "max_temperature_c"):
        values = [s[field] for s in steady if s[field] is not None]
        result[field + "_mean"] = sum(values) / len(values) if values else None
        result[field + "_peak"] = max(values) if values else None
    available = [s["board_available_mib"] for s in steady]
    result["board_available_mib_min"] = min(available) if available else None
    for name, process, path in (("encoder", encoder, encoder_progress),
                                ("decoder", decoder, decoder_progress)):
        result[name + "_exit_code"] = process.returncode if process else None
        result[name + "_final_progress"] = progress(path)
    decoder_text = decoder_log.read_text(errors="replace") if decoder_log.exists() else ""
    result["decoder_output_4k_confirmed"] = bool(
        re.search(r"Video: wrapped_avframe[^\n]*3840x2160", decoder_text)
    )
    result["decoder_error_lines"] = [line for line in decoder_text.splitlines()
        if re.search(r"error|corrupt|invalid data|could not find ref|failed", line, re.I)]
    encoded_count = result["encoder_final_progress"].get("frame")
    decoded_count = result["decoder_final_progress"].get("frame")
    result["final_frame_counts_match"] = (
        encoded_count is not None and encoded_count == decoded_count
    )
    result["output_drops_and_duplicates_zero"] = all(
        result[name + "_final_progress"].get(field) == "0"
        for name in ("encoder", "decoder") for field in ("drop_frames", "dup_frames")
    )
    result["realtime_4k60_pass"] = (
        result["stop_reason"] == "completed"
        and result["measurement_seconds"] >= args.seconds - 1
        and all(result.get(field) is not None and result[field] >= 59
                for field in ("capture_fps_monotonic", "encoder_fps_wall", "decoder_fps_wall"))
        and result.get("capture_gaps_during_measurement") == 0
        and result.get("capture_errors_during_measurement") == 0
        and not result["decoder_error_lines"]
        and result["encoder_mpp_device_observed"]
        and result["decoder_mpp_device_observed"]
        and result["decoder_output_4k_confirmed"]
        and result["final_frame_counts_match"]
        and result["output_drops_and_duplicates_zero"]
    )
    (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2), flush=True)
    return 0 if result["stop_reason"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
