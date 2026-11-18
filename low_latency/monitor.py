#!/usr/bin/env python3
"""Read-only rolling latency/FPS view of the latest native preview."""
import csv
import io
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def tail_csv(path, limit=1000000):
    with path.open("rb") as stream:
        fields = stream.readline().decode("ascii").strip().split(",")
        data_start = stream.tell()
        stream.seek(0, 2)
        start = max(data_start, stream.tell() - limit)
        stream.seek(start)
        if start > data_start:
            stream.readline()
        text = stream.read().decode("ascii")
    if text and not text.endswith("\n"):
        text = text.rsplit("\n", 1)[0] + "\n" if "\n" in text else ""
    return list(csv.DictReader(io.StringIO(text), fieldnames=fields))


def snapshot():
    state = json.loads((ROOT / "active.json").read_text())
    output = Path(state["output"])
    status = json.loads((output / "status.json").read_text())
    panel = json.loads((output / "panel-status.json").read_text())
    rates = tail_csv(output / "rates.csv", 100000)
    result = {"output": str(output), "status": status,
              "temperature_c": panel.get("temperature_c"), "native_rss_mib": panel.get("VmRSS", 0) / 1024,
              "system_cpu_percent": panel.get("system_cpu_percent"),
              "gpu_percent": panel.get("gpu_percent"), "npu_core_percent": panel.get("npu_core_percent")}
    if rates:
        cutoff = float(rates[-1]["elapsed_s"]) - 60
        recent = [r for r in rates if float(r["elapsed_s"]) >= cutoff]
        if len(recent) >= 2:
            a, b = recent[0], recent[-1]
            period = float(b["elapsed_s"]) - float(a["elapsed_s"])
            result["rolling_fps"] = {k: (int(b[k]) - int(a[k])) / period
                                     for k in ("captured", "encoded", "decoded", "displayed")}
            result["driver_gaps"] = int(b["gaps"])
    frames = tail_csv(output / "frames.csv")
    if frames:
        latest = max(int(f["dq_us"]) for f in frames)
        latency = sorted((int(f["flip_event_us"]) - int(f["dq_us"])) / 1000 for f in frames
                         if f["outcome"] == "displayed" and int(f["dq_us"]) >= latest - 60000000)
        if latency:
            result["rolling_p95_ms"] = latency[int((len(latency) - 1) * .95)]
    return result


if __name__ == "__main__":
    print(json.dumps(snapshot(), ensure_ascii=False))
