#!/usr/bin/env python3
"""Analyze actual wall-clock counters and per-frame optical-independent timing."""
import argparse
import csv
import json
from pathlib import Path
import statistics
import os
import re
import subprocess


def percentile(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(q * (len(values) - 1)))] if values else None


def analyze(root):
    summary_file = root / "summary.json"
    if not summary_file.exists():
        return {"completed": False, "error": (root / "native.log").read_text()[-2000:]}
    summary = json.loads(summary_file.read_text())
    cmd = json.loads((root / "command.json").read_text())
    warmup = int(cmd[cmd.index("--warmup") + 1])
    seconds = int(cmd[cmd.index("--seconds") + 1])
    with (root / "rates.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    stable = [r for r in rows if float(r["elapsed_s"]) >= warmup]
    rates = {}
    if len(stable) >= 2:
        a, b = stable[0], stable[-1]
        elapsed = float(b["elapsed_s"]) - float(a["elapsed_s"])
        for key in ["captured", "encoded", "decoded", "displayed"]:
            rates[key + "_fps"] = (int(b[key]) - int(a[key])) / elapsed
        rates["actual_mbps"] = (int(b["bytes"]) - int(a["bytes"])) * 8 / elapsed / 1e6
    buckets = {}
    driver_flags = {}
    timing_file = root / "timing.json"
    timing = json.loads(timing_file.read_text()) if timing_file.exists() else {}
    steady_start = timing.get("start_monotonic_us", 0) + warmup * 1000000
    frame_ids = set()
    duplicate_ids = 0
    invalid_timing = 0
    outcomes = {}
    submission_timeline = []
    targeted_frames = 0
    missed_targets = 0
    commit_lead_ms = []
    with (root / "frames.csv").open() as stream:
        for row in csv.DictReader(stream):
            frame_id = row.get("id")
            if frame_id is not None:
                duplicate_ids += frame_id in frame_ids
                frame_ids.add(frame_id)
            outcomes[row["outcome"]] = outcomes.get(row["outcome"], 0) + 1
            if int(row.get("enc_in_us", 0)):
                submission_timeline.append({k: int(row.get(k, 0)) for k in
                    ("id", "dq_us", "copy_start_us", "enc_in_us", "flip_event_us")})
            if row["outcome"] != "displayed":
                continue
            chain = [int(row.get(k, 0)) for k in ("dq_us", "copy_start_us", "copy_end_us", "enc_in_us",
                "enc_out_us", "dec_in_us", "dec_out_us", "display_in_us", "flip_event_us", "flip_received_us")]
            chain = [v for v in chain if v]
            invalid_timing += any(b < a for a, b in zip(chain, chain[1:]))
            driver_flags[row["flags"]] = driver_flags.get(row["flags"], 0) + 1
            t = int(row["dq_us"])
            if t < steady_start:
                continue
            target = int(row.get("display_target_us", 0))
            if target:
                targeted_frames += 1
                missed_targets += int(row["flip_event_us"]) - target > 1000000 / 60 / 2
                if row.get("display_in_us"):
                    commit_lead_ms.append((target - int(row["display_in_us"])) / 1000)
            bucket = t // 60000000
            latency = (int(row["flip_event_us"]) - t) / 1000
            buckets.setdefault(bucket, []).append(latency)
    windows = [{"minute_monotonic": k, "n": len(v), "p50_ms": percentile(v, .5), "p95_ms": percentile(v, .95)}
               for k, v in sorted(buckets.items())]
    telemetry = [json.loads(s) for s in (root / "telemetry.jsonl").read_text().splitlines() if s]
    temps = [s["temperature_c"] for s in telemetry if s.get("temperature_c") is not None]
    rss = [s["VmRSS"] for s in telemetry if "VmRSS" in s]
    cpu = []
    manifest_file = root / "manifest.json"
    manifest = json.loads(manifest_file.read_text()) if manifest_file.exists() else {}
    hz = manifest.get("clock_ticks_per_second", 100)
    for a, b in zip(telemetry, telemetry[1:]):
        if "cpu_ticks" in a and "cpu_ticks" in b:
            cpu.append((b["cpu_ticks"] - a["cpu_ticks"]) * 100 / hz / (b["monotonic_s"] - a["monotonic_s"]))
    latency = summary.get("latency", {}).get("dq_to_flip_ms", {}).get("p95")
    wanted = ["captured_fps", "displayed_fps"] if summary["codec"] == "direct" else [
        "captured_fps", "encoded_fps", "decoded_fps", "displayed_fps"]
    restoration = json.loads((root / "restoration.json").read_text()) if (root / "restoration.json").exists() else {}
    complete = restoration.get("returncode") == 0 and not summary["interrupted"] and (
        seconds == 0 or summary["elapsed_s"] >= seconds + warmup)
    audit = {"unique_frame_ids": len(frame_ids), "duplicate_ids": duplicate_ids,
             "nonmonotonic_display_timelines": invalid_timing, "outcomes": outcomes,
             "all_capture_ids_accounted": len(frame_ids) == summary.get("captured", len(frame_ids))}
    deadline_audit = {"targeted_frames": targeted_frames, "missed_targets": missed_targets,
                      "missed_target_percent": 100 * missed_targets / targeted_frames if targeted_frames else None,
                      "commit_lead_p50_ms": percentile(commit_lead_ms, .5),
                      "commit_lead_min_ms": min(commit_lead_ms) if commit_lead_ms else None,
                      "counter_matches_trace": summary.get("missed_targets") == missed_targets
                        if "missed_targets" in summary else None}
    recovery = None
    if "--inject-stall-ms" in cmd and int(cmd[cmd.index("--inject-stall-ms") + 1]) > 0:
        stall_ms = int(cmd[cmd.index("--inject-stall-ms") + 1])
        after = int(cmd[cmd.index("--stall-after") + 1])
        nominal = timing.get("start_monotonic_us", 0) + after * 1000000
        submissions = sorted(submission_timeline, key=lambda r: r["enc_in_us"])
        gaps = [(b["enc_in_us"] - a["enc_in_us"], b) for a, b in zip(submissions, submissions[1:])
                if nominal - 500000 <= b["enc_in_us"] <= nominal + (stall_ms + 500) * 1000]
        if gaps:
            gap, resumed = max(gaps, key=lambda x: x[0])
            fresh = next((r for r in submissions if r["enc_in_us"] >= resumed["enc_in_us"] and r["flip_event_us"]), None)
            recovery = {"injected_stall_ms": stall_ms, "observed_submission_gap_ms": gap / 1000,
                        "first_fresh_frame_id": fresh["id"] if fresh else None,
                        "first_fresh_frame_raw_wait_ms": (fresh["copy_start_us"] - fresh["dq_us"]) / 1000 if fresh else None,
                        "first_fresh_frame_dq_to_flip_ms": (fresh["flip_event_us"] - fresh["dq_us"]) / 1000 if fresh else None,
                        "first_flip_after_submission_resumes_ms": (fresh["flip_event_us"] - resumed["enc_in_us"]) / 1000 if fresh else None,
                        "stall_observed": gap / 1000 >= stall_ms * .9}
    quality = {}
    sdk = Path("/userdata/av_dev/ffmpeg_pipeline/runtime")
    env = dict(os.environ, LD_LIBRARY_PATH=str(sdk / "lib"))
    sample = root / ("sample.hevc" if summary["codec"] == "hevc" else "sample.h264")
    if sample.exists():
        probe = subprocess.run([str(sdk / "bin/ffprobe"), "-v", "error", "-show_entries",
            "stream=codec_name,width,height,has_b_frames,r_frame_rate,color_space,color_range", "-of", "json", str(sample)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
        quality["stream_probe"] = json.loads(probe.stdout) if probe.returncode == 0 else {"error": probe.stderr}
        for metric in ("psnr", "ssim"):
            if not (root / "quality-decoded.nv12").exists():
                break
            cmd = [str(sdk / "bin/ffmpeg"), "-hide_banner", "-nostdin", "-threads", "2"]
            for name in ("quality-source.nv12", "quality-decoded.nv12"):
                cmd += ["-f", "rawvideo", "-pixel_format", "nv12", "-video_size", "3840x2160", "-i", str(root / name)]
            cmd += ["-filter_complex_threads", "2", "-lavfi", metric, "-frames:v", "1", "-f", "null", "-"]
            check = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30)
            (root / (metric + ".log")).write_text(check.stdout)
            match = re.search(r"average:([\d.]+)" if metric == "psnr" else r"All:([\d.]+)", check.stdout)
            quality[metric] = float(match[1]) if match else None
    result = {"completed": complete, "scope": "V4L2_DQBUF_TO_DRM_FLIP_NOT_OPTICAL",
              "summary": summary, "rates": rates, "latency_by_minute": windows,
              "v4l2_flag_counts": driver_flags,
              "max_temperature_c": max(temps) if temps else None,
              "max_rss_mib": max(rss) / 1024 if rss else None,
              "mean_cpu_one_core_percent": statistics.mean(cpu) if cpu else None,
              "quality_matched_single_frame": quality,
              "frame_audit": audit,
              "deadline_audit": deadline_audit,
              "stall_recovery": recovery,
              "mpp_fd_observed": any(s.get("mpp_fd_observed") for s in telemetry),
              "restored": restoration.get("restored", False),
              "meets_internal_targets": complete and latency is not None and latency <= 100
                  and all(rates.get(k, 0) >= 59 for k in wanted)
                  and summary["errors"] == 0 and summary["driver_gaps"] == 0
                  and duplicate_ids == 0 and invalid_timing == 0 and audit["all_capture_ids_accounted"]
                  and deadline_audit["counter_matches_trace"] is not False
                  and restoration.get("restored", False)}
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("output", type=Path)
    args = p.parse_args()
    result = analyze(args.output)
    (args.output / "analysis.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), "completed": result.get("completed"),
                      "rates": result.get("rates"), "meets_internal_targets": result.get("meets_internal_targets")}))
