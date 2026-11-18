#!/usr/bin/env python3
"""Sequential ABBA-style codec comparison, capture buffers and endurance."""
import argparse
import datetime
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--round-seconds", type=int, default=300)
    p.add_argument("--endurance-seconds", type=int, default=3600)
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    results = []
    state = {"running": True, "started_at": datetime.datetime.now().astimezone().isoformat(), "results": results}

    def save():
        path = args.output / "benchmark.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(state, indent=2) + "\n")
        temp.replace(path)

    def case(name, codec, seconds, buffers=6, extra=None):
        state["current_case"] = name
        state["case_started_at"] = datetime.datetime.now().astimezone().isoformat()
        save()
        output = args.output / name
        cmd = [sys.executable, str(ROOT / "control.py"), "run", "--codec", codec,
               "--seconds", str(seconds), "--buffers", str(buffers), "--output", str(output)] + (extra or [])
        with (args.output / (name + ".log")).open("w") as log:
            run = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
        analysis = output / "analysis.json"
        result = json.loads(analysis.read_text()) if analysis.exists() else {"completed": False}
        result.update(name=name, output=str(output), returncode=run.returncode)
        results.append(result)
        save()
        if run.returncode or not result.get("restored"):
            raise RuntimeError("Case failed; stop matrix: " + name)
        print(json.dumps({"case": name, "rates": result["rates"], "pass": result["meets_internal_targets"]}), flush=True)
        return result

    try:
        case("direct-6", "direct", 30)
        buffer_trials = [case("buffer-" + str(n), "hevc", 60, n) for n in (4, 6, 16)]
        passing = [r for r in buffer_trials if r["meets_internal_targets"]]
        if not passing:
            raise RuntimeError("No capture buffer configuration passes throughput/latency")
        buffers = min(r["summary"]["capture_buffers_actual"] for r in passing)
        state["selected_buffers"] = buffers
        # Interleave the two codecs to reduce temperature/time-of-day bias.
        codec_trials = []
        for name, codec in [("hevc-1", "hevc"), ("h264-1", "h264"), ("h264-2", "h264"),
                            ("hevc-2", "hevc"), ("hevc-3", "hevc"), ("h264-3", "h264")]:
            codec_trials.append(case(name, codec, args.round_seconds, buffers))
        candidates = {}
        for codec in ("hevc", "h264"):
            rows = [r for r in codec_trials if r["summary"]["codec"] == codec]
            if len(rows) == 3 and all(r["meets_internal_targets"] for r in rows):
                candidates[codec] = max(r["summary"]["latency"]["dq_to_flip_ms"]["p95"] for r in rows)
        if not candidates:
            raise RuntimeError("Neither codec passes all three rounds")
        selected = min(candidates, key=candidates.get)
        if "hevc" in candidates and candidates["hevc"] - min(candidates.values()) < 1000 / 60:
            selected = "hevc"
        state["selected_codec"] = selected
        state["selection_worst_round_p95_ms"] = candidates
        save()
        case("h264-immediate-off", "h264", 30, buffers, ["--immediate-out", "0"])
        case("stall-250ms", selected, 30, buffers, ["--inject-stall-ms", "250", "--stall-after", "15"])
        final = case("endurance", selected, args.endurance_seconds, buffers)
        if not final["meets_internal_targets"]:
            raise RuntimeError("Endurance missed targets; do not publish profile")
        (ROOT / "recommended.json").write_text(json.dumps({"codec": selected, "buffers": buffers,
            "bitrate": 20000000, "acceptance_output": str(args.output),
            "endurance_seconds": args.endurance_seconds}, indent=2) + "\n")
        state["completed"] = True
    except Exception as exc:
        state["completed"] = False
        state["error"] = str(exc)
        raise
    finally:
        state["running"] = False
        state["finished_at"] = datetime.datetime.now().astimezone().isoformat()
        save()


if __name__ == "__main__":
    main()
