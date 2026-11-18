#!/usr/bin/env python3
"""Manual native latency preview, durable state restoration and telemetry."""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "active.json"
UNIT = "imx415-low-latency.service"
SDK = Path("/userdata/av_dev/ffmpeg_pipeline/runtime")


def command(args, check=True, **kw):
    return subprocess.run(args, check=check, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, **kw)


def atomic_json(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as stream:
        stream.write(json.dumps(value, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    tmp.replace(path)
    directory_fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def restore():
    if not STATE.exists():
        return
    state = json.loads(STATE.read_text())
    native_pid = state.get("native_pid")
    if native_pid:
        try:
            if Path(f"/proc/{native_pid}/exe").resolve() == ROOT / "build/imx415_low_latency":
                raise RuntimeError("Stop the native process before restoring devices")
        except FileNotFoundError:
            pass
    overlay_path = Path(state.get("overlay_file", "/nonexistent"))
    if overlay_path.parent == Path("/run") and re.fullmatch(r"imx415-latency-overlay-\d+\.argb", overlay_path.name):
        try:
            overlay_path.unlink()
        except FileNotFoundError:
            pass
    if state.get("restored"):
        return
    output = Path(state["output"])
    # The unit invokes this again after forced termination of its supervisor.
    result = command(["v4l2-ctl", "-d", state["device"],
                      "--set-fmt-video=" + state["camera_format"]], check=False)
    state["camera_format_restored"] = result.returncode == 0
    (output / "restore-camera.log").write_text(result.stdout)
    if state["desktop_was_active"]:
        result = command(["systemctl", "start", state["display_service"]], check=False)
        state["desktop_restored"] = result.returncode == 0
        # Reapply the original monitor layout after GDM's new X server is ready.
        env = dict(os.environ, DISPLAY=":0", XAUTHORITY="/run/user/1000/gdm/Xauthority")
        for _ in range(20):
            current = command(["xrandr", "--query"], check=False, env=env)
            if current.returncode == 0:
                result = command(["xrandr"] + state.get("xrandr_restore", []), check=False, env=env)
                state["display_layout_restored"] = result.returncode == 0
                (output / "restore-display.log").write_text(result.stdout)
                break
            time.sleep(.5)
    else:
        state["desktop_restored"] = True
    state["restored"] = bool(state.get("camera_format_restored") and state.get("desktop_restored")
                             and (not state["desktop_was_active"] or state.get("display_layout_restored")))
    state["restored_at"] = datetime.datetime.now().astimezone().isoformat()
    atomic_json(STATE, state)
    atomic_json(output / "restoration.json", state)


def xrandr_layout(text):
    args = []
    for line in text.splitlines():
        m = re.match(r"(\S+) connected (primary )?(\d+)x(\d+)\+(\d+)\+(\d+)", line)
        if m:
            args += ["--output", m[1], "--mode", m[3] + "x" + m[4], "--pos", m[5] + "x" + m[6]]
            if m[2]:
                args += ["--primary"]
    return args


def telemetry(pid):
    result = {"monotonic_s": time.monotonic()}
    try:
        status = Path(f"/proc/{pid}/status").read_text()
        for key in ("VmRSS", "VmSize", "Threads"):
            match = re.search(r"^" + key + r":\s*(\d+)", status, re.M)
            if match:
                result[key] = int(match[1])
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        result["cpu_ticks"] = int(stat[11]) + int(stat[12])
        result["mpp_fd_observed"] = any("mpp_service" in os.readlink(p)
                                         for p in Path(f"/proc/{pid}/fd").iterdir())
    except (OSError, ProcessLookupError):
        pass
    values = []
    for zone in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
        try:
            values.append(int(zone.read_text()) / 1000)
        except (OSError, ValueError):
            pass
    result["temperature_c"] = max(values) if values else None
    for key in ("MemAvailable", "MemFree"):
        match = re.search(key + r":\s*(\d+)", Path("/proc/meminfo").read_text())
        result[key + "_kib"] = int(match[1]) if match else None
    return result


def run(args):
    from overlay import Panel
    profile_file = ROOT / "recommended.json"
    profile = json.loads(profile_file.read_text()) if profile_file.exists() else {}
    args.codec = args.codec or profile.get("codec", "hevc")
    args.buffers = args.buffers or profile.get("buffers", 6)
    lock = Path("/tmp/imx415-live-mpp-bench.lock").open("a+")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    sys.path.insert(0, str(SDK.parent))
    from camera_control.support import camera_owners, sensor_frame_rate
    owners = camera_owners(args.device)
    if owners:
        raise RuntimeError("Camera occupied: " + repr(owners))
    fps = sensor_frame_rate(args.device)
    if fps is None or fps < 59:
        raise RuntimeError("Sensor does not report 60fps: " + repr(fps))
    output = args.output or ROOT / "results" / datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    original = command(["v4l2-ctl", "-d", args.device, "--get-fmt-video"]).stdout
    (output / "camera-before.txt").write_text(original)
    size = re.search(r"Width/Height\s*:\s*(\d+)/(\d+)", original)
    pixel = re.search(r"Pixel Format\s*:\s*'([^']+)'", original)
    if not size or not pixel:
        raise RuntimeError("Cannot save camera format")
    service = command(["systemctl", "show", "display-manager", "-p", "Id", "--value"]).stdout.strip()
    env = dict(os.environ, DISPLAY=":0", XAUTHORITY="/run/user/1000/gdm/Xauthority")
    xr = command(["xrandr", "--query"], check=False, env=env)
    (output / "xrandr-before.txt").write_text(xr.stdout)
    state = {"output": str(output), "device": args.device,
             "camera_format": f"width={size[1]},height={size[2]},pixelformat={pixel[1]}",
             "display_service": service,
             "desktop_was_active": command(["systemctl", "is-active", "--quiet", service], check=False).returncode == 0,
             "xrandr_restore": xrandr_layout(xr.stdout), "sensor_reported_fps": fps,
             "restored": False, "supervisor_pid": os.getpid()}
    state["boot_id"] = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    atomic_json(STATE, state)
    overlay_file = Path(f"/run/imx415-latency-overlay-{os.getpid()}.argb")
    state["overlay_file"] = str(overlay_file)
    panel = Panel(overlay_file, output, args.codec, os.sysconf("SC_CLK_TCK"))
    panel.update({"monotonic_s": time.monotonic()})
    cmd = [str(ROOT / "build/imx415_low_latency"), "--out", str(output), "--codec", args.codec,
           "--device", args.device, "--seconds", str(args.seconds), "--warmup", str(args.warmup),
           "--buffers", str(args.buffers), "--bitrate", str(args.bitrate),
           "--immediate-out", str(args.immediate_out), "--low-delay", str(args.low_delay),
           "--max-age-ms", str(args.max_age_ms), "--inject-stall-ms", str(args.inject_stall_ms),
           "--stall-after", str(args.stall_after), "--split-ctus", str(args.split_ctus)]
    cmd += ["--inject-display-error-after", str(args.inject_display_error_after)]
    cmd += ["--overlay-file", str(overlay_file)]
    atomic_json(output / "command.json", cmd)
    manifest = {"started_at": datetime.datetime.now().astimezone().isoformat(),
                "kernel": os.uname().release, "machine": os.uname().machine,
                "clock_ticks_per_second": os.sysconf("SC_CLK_TCK"), "sha256": {}}
    for path in [ROOT / "main.cpp", ROOT / "control.py", ROOT / "analyze.py", ROOT / "overlay.py", ROOT / "build/imx415_low_latency", SDK / "lib/librockchip_mpp.so"]:
        manifest["sha256"][str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    atomic_json(output / "manifest.json", manifest)
    stopped = False
    child = None
    def stop(signum, frame):
        nonlocal stopped
        stopped = True
        if child and child.poll() is None:
            child.send_signal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    rc = 1
    try:
        if state["desktop_was_active"]:
            command(["systemctl", "stop", service])
        native_env = dict(os.environ, LD_LIBRARY_PATH=str(SDK / "lib"))
        native_env.pop("LD_PRELOAD", None)
        with (output / "native.log").open("w") as log, (output / "telemetry.jsonl").open("w") as samples:
            child = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=native_env)
            state["native_pid"] = child.pid
            atomic_json(STATE, state)
            term_at = None
            while child.poll() is None:
                sample = telemetry(child.pid)
                status_file = output / "status.json"
                try:
                    status = json.loads(status_file.read_text()) if status_file.exists() else {}
                except (OSError, ValueError):
                    status = {}
                sample = panel.update(sample, status)
                samples.write(json.dumps(sample) + "\n")
                samples.flush()
                if sample.get("temperature_c", 0) is not None and sample.get("temperature_c", 0) >= 72:
                    state["stop_reason"] = "temperature_limit"
                    stopped = True
                if stopped:
                    if term_at is None:
                        child.terminate()
                        term_at = time.monotonic()
                    elif time.monotonic() - term_at > 8:
                        child.kill()
                time.sleep(1)
            rc = child.returncode
            state["returncode"] = rc
            atomic_json(STATE, state)
    finally:
        if child and child.poll() is None:
            child.kill()
            child.wait()
        restore()
        if overlay_file.exists():
            overlay_file.unlink()
    command([sys.executable, str(ROOT / "analyze.py"), str(output)], check=False)
    print(json.dumps({"output": str(output), "returncode": rc}))
    return rc


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["run", "start", "stop", "status", "restore"])
    p.add_argument("--output", type=Path)
    p.add_argument("--codec", choices=["hevc", "h264", "direct"])
    p.add_argument("--device", default="/dev/video44")
    for name, default in [("seconds", 0), ("warmup", 5), ("buffers", None), ("bitrate", 20000000),
                          ("immediate-out", 1), ("low-delay", 1), ("max-age-ms", 100),
                          ("inject-stall-ms", 0), ("stall-after", 10), ("split-ctus", 0),
                          ("inject-display-error-after", 0)]:
        p.add_argument("--" + name, type=int, default=default)
    return p


def main():
    args = parser().parse_args()
    if args.action == "restore":
        restore()
    elif args.action == "run":
        return run(args)
    elif args.action == "start":
        if command(["systemctl", "is-active", "--quiet", "imx415-latency-benchmark.service"], check=False).returncode == 0:
            raise RuntimeError("Acceptance benchmark owns the camera; stop it before starting another preview")
        active = command(["systemctl", "is-active", "--quiet", UNIT], check=False).returncode == 0
        if active:
            raise RuntimeError("Native preview already running")
        cmd = ["systemd-run", "--unit=" + UNIT, "--collect", "--property=KillMode=mixed",
               "--property=TimeoutStopSec=20", "--property=WorkingDirectory=" + str(ROOT),
               "--property=ExecStopPost=/usr/bin/python3 " + str(ROOT / "control.py") + " restore",
               sys.executable, str(ROOT / "control.py"), "run"] + sys.argv[2:]
        print(command(cmd).stdout)
    elif args.action == "stop":
        if command(["systemctl", "is-active", "--quiet", "imx415-latency-benchmark.service"], check=False).returncode == 0:
            print(command(["systemctl", "stop", "imx415-latency-benchmark.service"], check=False).stdout)
        print(command(["systemctl", "stop", UNIT], check=False).stdout)
    else:
        state = json.loads(STATE.read_text()) if STATE.exists() else {}
        state["service_active"] = command(["systemctl", "is-active", "--quiet", UNIT], check=False).returncode == 0
        pid = state.get("native_pid")
        try:
            state["native_process_running"] = bool(pid and Path(f"/proc/{pid}/exe").resolve() == ROOT / "build/imx415_low_latency")
        except FileNotFoundError:
            state["native_process_running"] = False
        if state.get("output"):
            path = Path(state["output"]) / "status.json"
            if path.exists():
                state["native_status"] = json.loads(path.read_text())
                state["status_age_seconds"] = time.time() - path.stat().st_mtime
                state["metrics_stale"] = not state["native_process_running"] or state["status_age_seconds"] > 3
                state["native_status"]["reported_running"] = state["native_status"].get("running")
                state["native_status"]["running"] = state["native_process_running"]
        print(json.dumps(state, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
