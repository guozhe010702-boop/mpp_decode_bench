#!/usr/bin/env python3
"""Generate a reviewable Markdown report from measured board results."""
import argparse
import json
from pathlib import Path
import statistics
import re


def number(value, digits=2):
    return "--" if value is None else f"{value:.{digits}f}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("benchmark", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    state = json.loads((args.benchmark / "benchmark.json").read_text())
    rows = []
    for saved in state["results"]:
        path = Path(saved["output"]) / "analysis.json"
        row = dict(saved)
        if path.exists():
            row.update(json.loads(path.read_text()))
        rows.append(row)
    lines = ["# RK3588 / IMX415 4K60 低延迟实测", "",
        "状态：" + ("完整矩阵已完成。" if state.get("completed") else "矩阵尚未完整完成，以下为已完成项目。"), "",
        "主延迟口径为应用取帧（V4L2 DQBUF）到DRM实际翻页事件；不包含取帧前的曝光/传感器/ISP处理及面板响应，不代表光学端到端延迟。", "",
        "全链路为3840×2160、8bit NV12、60fps、20Mbps、GOP60、无B帧；HDMI缩放到当前显示器支持的1920×1080@60。最后的编码对比、阻塞和耐久测试均启用左上角透明状态文字。", "",
        "## 测试结果", "",
        "| 项目 | 采集fps | 编码fps | 解码fps | 显示fps | P50 ms | P95 ms | P99 ms | 稳态驱动缺帧 | 通过 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for r in rows:
        rate = r.get("rates", {})
        lat = r.get("summary", {}).get("latency", {}).get("dq_to_flip_ms", {})
        values = [r["name"]] + [number(rate.get(k + "_fps")) for k in ("captured", "encoded", "decoded", "displayed")]
        values += [number(lat.get(k)) for k in ("p50", "p95", "p99")]
        values += [str(r.get("summary", {}).get("driver_gaps", "--")), "是" if r.get("meets_internal_targets") else "否"]
        lines.append("| " + " | ".join(values) + " |")
    lines += ["", "预热后独立计数/墙钟差计算实际FPS；直显模式跳过编解码。分位数为固定内存直方图，按0.1ms向上取整；微秒原始时间保存在frames.csv。主动丢帧与启动历史帧单独计数，不混作驱动缺帧。", "",
              "## 推荐配置与耐久结果", "",
              f"选择编码：`{state.get('selected_codec', '待完成')}`；采集缓冲：`{state.get('selected_buffers', '待完成')}`。"]
    for r in rows:
        if r["name"] != "endurance":
            continue
        summary = r["summary"]
        lines += ["", f"实际运行 {number(summary['elapsed_s'])} 秒；显示 {summary['displayed']} 帧；最高温度 {number(r.get('max_temperature_c'))}℃；进程CPU平均 {number(r.get('mean_cpu_one_core_percent'))}%（单核口径）；原生进程峰值RSS {number(r.get('max_rss_mib'))} MiB。",
            "", "| 阶段 | P50 ms | P95 ms | P99 ms | 最大 ms |", "|---|---:|---:|---:|---:|"]
        for name, label in [("raw_wait_ms", "编码前等待"), ("layout_copy_ms", "布局整理"), ("encode_ms", "编码"),
                            ("decode_ms", "解码"), ("display_wait_ms", "显示前等待"), ("kms_flip_ms", "KMS翻页等待"),
                            ("driver_timestamp_to_flip_ms", "驱动时间戳到翻页（单独口径）")]:
            lat = summary["latency"].get(name, {})
            lines.append("| " + label + " | " + " | ".join(number(lat.get(k)) for k in ("p50", "p95", "p99", "max")) + " |")
        windows = [w for w in r.get("latency_by_minute", []) if w["n"] > 3000]
        if windows:
            lines += ["", f"超过3000帧的分钟窗口P95范围 {number(min(w['p95_ms'] for w in windows))}–{number(max(w['p95_ms'] for w in windows))}ms；首个有效窗口 {number(windows[0]['p95_ms'])}ms，最后一个有效窗口 {number(windows[-1]['p95_ms'])}ms。"]
        audit = r.get("frame_audit", {})
        lines += ["", "逐帧核对：`" + json.dumps(audit, ensure_ascii=False) + "`。",
            "", f"整段主动丢弃：编码前替换 {summary['raw_drops']}，显示前替换 {summary['display_drops']}，过期/无效时间戳 {summary['stale_drops']}；退出中的在途帧见帧去向记录。"]
        samples = [json.loads(s) for s in (Path(r["output"]) / "telemetry.jsonl").read_text().splitlines() if s]
        steady = [s for s in samples if s.get("VmRSS") is not None][300:]
        if len(steady) >= 600:
            a = statistics.mean(s["VmRSS"] for s in steady[:300]) / 1024
            b = statistics.mean(s["VmRSS"] for s in steady[-300:]) / 1024
            lines += ["", f"预热和初期缓存建立后，前5分钟/末5分钟原生RSS均值为 {number(a)}/{number(b)} MiB。RSS不包含全部DMA内存；整板内存另见telemetry.jsonl。"]
        checkpoints = Path(r["output"]) / "resource-checkpoints.jsonl"
        if checkpoints.exists():
            checks = [json.loads(s) for s in checkpoints.read_text().splitlines() if s]
            lines += ["", "资源快照：文件描述符数量 " + str([s["fd_count"] for s in checks])
                + "；各次硬编/硬解会话内存（MiB） " + str([re.findall(r"memory: (\d+) MiB", s["mpp_sessions"]) for s in checks])
                + "。这些是会话缓冲内存，不是VPU忙碌百分比。"]
    lines += ["", "## 阻塞恢复和画质核对", ""]
    for r in rows:
        if r.get("stall_recovery"):
            lines += ["阻塞恢复：`" + json.dumps(r["stall_recovery"], ensure_ascii=False) + "`。", ""]
    lines += ["| 编码测试 | 实际码率 Mbps | PSNR dB | SSIM |", "|---|---:|---:|---:|"]
    for r in rows:
        if r["name"] not in ("hevc-1", "hevc-2", "hevc-3", "h264-1", "h264-2", "h264-3"):
            continue
        q = r.get("quality_matched_single_frame", {})
        lines.append(f"| {r['name']} | {number(r['rates'].get('actual_mbps'))} | {number(q.get('psnr'))} | {number(q.get('ssim'), 4)} |")
    lines += ["", "PSNR/SSIM来自每轮预热期同一帧的原始NV12和MPP硬解输出。各轮为实拍场景，不能替代同一固定运动素材的严格画质排名；面板色彩响应未做色卡校准。码流元数据与全分辨率由ffprobe及运行时尺寸检查共同验证。", "",
        "## 延迟原因与采取的策略", "",
        "4K NV12每帧约12.44MB，60fps每次完整读/写一遍约746MB/s；额外拷贝和排队都可能增加耗时。16个分配缓冲不等于排队16帧，需要以逐帧时间实际测量。",
        "", "本次使用同进程MPP编解码、最新帧优先的有界队列、两个异步编码输入缓冲和KMS直显，避免中间封装/字节流解析与桌面合成带来的额外等待。仅在原始帧和解码帧边界丢旧帧，保持压缩参考帧顺序。",
        "", "新程序最初的同步MPP基线只有约49fps：默认encode_put_frame会等待硬件完成，拷贝和编码串行。设置非阻塞输入以选择MPP异步模式后，两者可以重叠，达到约60fps。该结论针对新程序的初始基线；原FFmpeg路径已经使用异步编码，不能把这一问题误归因于原FFmpeg程序。",
        "", "旧桌面链路缺少逐帧翻页计时，无法给出可信的端到端优化百分比。新链路计时区分了布局整理、硬编、硬解、显示队列和刷新等待；剩余曝光/ISP/屏幕响应需要光学测量。整帧路径达标时不启用slice，也不额外加入预测或插帧模型。", "",
        "## 生命周期与边界", "",
        "每轮测试结束恢复摄像头格式、桌面服务和显示布局，restoration.json保存结果。软件显示错误注入、手动停止及强制终止的补充测试单独存放。没有实际拔插HDMI线，不能将软件错误注入写成物理热插拔测试。", "",
        "源码与操作见[README](README.md)，原始数据位于 `" + str(args.benchmark) + "`。"]
    note_path = args.benchmark.parent.parent / "exit-recovery-note.json"
    if note_path.exists():
        note = json.loads(note_path.read_text())
        lines += ["", "## 退出处理修正与补充复测", "", note["incident"], "", note["changes"], "", note["validation_scope"], ""]
        lines += ["| 补充检查 | 结果 |", "|---|---|"]
        for item in note.get("lifecycle_tests", []):
            lines.append("| " + item["test"] + " | " + ("通过" if item["passed"] else "未通过") + " |")
        regression = note.get("performance_regression", {})
        lines += ["", "修正后的短时性能回归：`" + json.dumps(regression, ensure_ascii=False) + "`。",
            "", "首次被重启打断的补充测试数据位于 `results/lifecycle-20261117`；正常复测位于 `results/lifecycle-retest-20261117`，关键退出步骤保存于各次运行的 `lifecycle.log`。"]
    args.output.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
