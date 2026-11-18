#!/usr/bin/env python3
"""Board fault/stop recovery acceptance; run after the benchmark is stopped."""
import argparse
import fcntl
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
UNIT = "imx415-low-latency.service"
EXTRA_ARGS = []


def command(args):
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def control(*args):
    return command([sys.executable, str(ROOT / "control.py")] + list(args)
                   + (EXTRA_ARGS if args and args[0] in ("start", "run") else []))


def active(unit):
    return command(["systemctl", "is-active", "--quiet", unit]).returncode == 0


def wait_until(predicate, timeout=35):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.5)
    raise RuntimeError("Lifecycle acceptance deadline exceeded")


def main():
    global EXTRA_ARGS
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--profile", type=Path)
    args = p.parse_args()
    if args.profile:
        profile = json.loads(args.profile.read_text())
        for key in ("codec", "buffers", "bitrate", "display_scheduling", "display_margin_us",
                    "capture_scheduling", "cpu_affinity", "overlay_preparation", "decoder_output",
                    "capture_startup_poll_ms", "capture_layout", "encoder_input"):
            if key in profile:
                EXTRA_ARGS += ["--" + key.replace("_", "-"), str(profile[key])]
    if active("imx415-latency-benchmark.service") or active(UNIT):
        raise RuntimeError("Stop existing preview/benchmark before lifecycle acceptance")
    with open('/tmp/imx415-live-mpp-bench.lock','a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('A foreground preview still owns the camera; wait until it exits')
    args.output.mkdir(parents=True, exist_ok=False)
    results = []

    def save():
        if args.profile:
            for result in results:
                result["requested_profile"] = profile
        (args.output / "lifecycle.json").write_text(json.dumps(results, indent=2) + "\n")

    def verify_restore(output):
        data = json.loads((output / "restoration.json").read_text())
        if not data.get("restored"):
            raise RuntimeError("Restoration failed: " + str(output))
        pid = data.get("native_pid")
        if pid and Path(f"/proc/{pid}/exe").resolve() == ROOT / "build/imx415_low_latency":
            raise RuntimeError("Native process leaked")
        overlay = data.get("overlay_file")
        if overlay and Path(overlay).exists():
            raise RuntimeError("Temporary status overlay leaked")
        return data

    try:
        output = args.output / "display-error"
        run = control("run", "--seconds", "20", "--inject-display-error-after", "10", "--output", str(output))
        (args.output / "display-error.log").write_text(run.stdout)
        assert run.returncode == 1, run.stdout
        assert "injected HDMI display failure" in (output / "error.txt").read_text()
        verify_restore(output)
        results.append({"test": "display-error-injection", "passed": True,
                        "physical_hotplug_tested": False, "output": str(output)})
        save()
        for name in ("manual-stop", "restart-stop", "supervisor-sigkill"):
            output = args.output / name
            start = control("start", "--seconds", "0", "--output", str(output))
            assert start.returncode == 0, start.stdout
            def showing():
                path = output / "status.json"
                if not path.exists():
                    return False
                status = json.loads(path.read_text())
                return status.get("displayed", 0) >= 120
            wait_until(showing)
            state = json.loads((ROOT / "active.json").read_text())
            rejected = control("start")
            assert rejected.returncode != 0 and "already running" in rejected.stdout
            assert json.loads((ROOT / "active.json").read_text())["native_pid"] == state["native_pid"]
            if name == "supervisor-sigkill":
                killed = command(["systemctl", "kill", "--kill-who=main", "--signal=SIGKILL", UNIT])
                assert killed.returncode == 0, killed.stdout
                wait_until(lambda: (output / "restoration.json").exists(), timeout=40)
            else:
                stopped = control("stop")
                assert stopped.returncode == 0, stopped.stdout
            wait_until(lambda: not active(UNIT))
            restored = verify_restore(output)
            results.append({"test": name, "passed": True, "output": str(output),
                            "camera_format_restored": restored["camera_format_restored"],
                            "desktop_restored": restored["desktop_restored"],
                            "display_layout_restored": restored["display_layout_restored"]})
            save()
        sys.path.insert(0, "/userdata/av_dev/ffmpeg_pipeline")
        from camera_control.support import camera_owners
        assert not camera_owners("/dev/video44"), "Camera owner leaked"
        print(json.dumps({"passed": True, "tests": len(results)}))
    finally:
        control("stop")
        save()


if __name__ == "__main__":
    main()
