#!/usr/bin/env python3
"""Same-build immediate/deadline ABBA comparison and sustained validation."""
import argparse
import datetime
import json
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--margin-us", type=int, default=3500)
    p.add_argument("--buffers", type=int, default=8)
    p.add_argument("--capture-scheduling", choices=["normal", "raw-rr"], default="raw-rr")
    p.add_argument("--cpu-affinity", choices=["all", "big"], default="big")
    p.add_argument("--round-seconds", type=int, default=180)
    p.add_argument("--soak-seconds", type=int, default=1200)
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    state = {"running": True, "completed": False, "boot_id": boot, "results": [],
             "started_at": datetime.datetime.now().astimezone().isoformat(), "margin_us": args.margin_us,
             "buffers": args.buffers, "capture_scheduling": args.capture_scheduling,
             "cpu_affinity": args.cpu_affinity}
    child = None
    stopped = False

    def stop(signum, frame):
        nonlocal stopped
        stopped = True
        if child and child.poll() is None:
            child.terminate()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def save():
        path = args.output / "comparison.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2) + "\n")
        tmp.replace(path)

    def case(name, mode, seconds, extra=None):
        nonlocal child
        if stopped:
            raise InterruptedError("Comparison stopped")
        state["current_case"] = name
        state["case_started_at"] = datetime.datetime.now().astimezone().isoformat()
        save()
        output = args.output / name
        cmd = [sys.executable, str(ROOT / "control.py"), "run", "--codec", "hevc", "--buffers", str(args.buffers),
               "--seconds", str(seconds), "--display-scheduling", mode, "--display-margin-us", str(args.margin_us),
               "--capture-scheduling", args.capture_scheduling,
               "--cpu-affinity", args.cpu_affinity,
               "--output", str(output)] + (extra or [])
        with (args.output / (name + ".log")).open("w") as log:
            child = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
            while child.poll() is None:
                time.sleep(.25)
        if stopped:
            raise InterruptedError("Comparison stopped")
        result = json.loads((output / "analysis.json").read_text())
        result.update(name=name, output=str(output))
        state["results"].append(result)
        save()
        assert child.returncode == 0 and result["meets_internal_targets"], name
        assert Path("/proc/sys/kernel/random/boot_id").read_text().strip() == boot, "Boot changed"
        minimum_fps = 59 if extra and "--inject-stall-ms" in extra else 59.8
        assert min(result["rates"][key + "_fps"] for key in ("captured", "encoded", "decoded", "displayed")) >= minimum_fps, name + " FPS below target"
        print(json.dumps({"case": name, "display_fps": result["rates"]["displayed_fps"],
                          "p95_ms": result["summary"]["latency"]["dq_to_flip_ms"]["p95"],
                          "missed_targets": result["summary"].get("missed_targets"), "passed": True}), flush=True)
        return result

    try:
        for name, mode in (("immediate-1", "immediate"), ("deadline-1", "deadline"),
                           ("deadline-2", "deadline"), ("immediate-2", "immediate")):
            case(name, mode, args.round_seconds)
        old = [r["summary"]["latency"]["dq_to_flip_ms"]["p95"] for r in state["results"] if r["name"].startswith("immediate")]
        new = [r["summary"]["latency"]["dq_to_flip_ms"]["p95"] for r in state["results"] if r["name"].startswith("deadline")]
        state["baseline_mean_p95_ms"] = sum(old) / len(old)
        state["deadline_mean_p95_ms"] = sum(new) / len(new)
        state["improvement_ms"] = state["baseline_mean_p95_ms"] - state["deadline_mean_p95_ms"]
        assert state["improvement_ms"] >= 2, "No repeatable latency gain; keep old profile"
        soak = case("deadline-soak", "deadline", args.soak_seconds)
        assert soak["summary"]["latency"]["dq_to_flip_ms"]["p95"] < state["baseline_mean_p95_ms"], "Soak gain did not persist"
        # Stall acceptance has a deliberate temporary FPS reduction; keep its
        # normal >=59fps requirement while checking freshness recovery.
        stall = case("deadline-stall", "deadline", 60, ["--inject-stall-ms", "250", "--stall-after", "15"])
        recovery = stall.get("stall_recovery") or {}
        assert recovery.get("stall_observed") and recovery.get("first_fresh_frame_dq_to_flip_ms", 1000) <= 100, "Freshness did not recover"
        state["completed"] = True
        state["recommended"] = {"codec": "hevc", "buffers": args.buffers, "bitrate": 20000000,
                                "display_scheduling": "deadline", "display_margin_us": args.margin_us,
                                "capture_scheduling": args.capture_scheduling,
                                "cpu_affinity": args.cpu_affinity,
                                "validation_output": str(args.output), "continuous_test_seconds": args.soak_seconds}
    except Exception as exc:
        state["error"] = str(exc)
        raise
    finally:
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        state["running"] = False
        state["finished_at"] = datetime.datetime.now().astimezone().isoformat()
        save()


if __name__ == "__main__":
    main()
