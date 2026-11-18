#!/usr/bin/env python3
"""Summarize same-build display-scheduling comparison and sustained tests."""
import argparse
import json
from pathlib import Path


def main():
    p = argparse.ArgumentParser()
    p.add_argument("comparison", type=Path)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    state = json.loads((args.comparison / "comparison.json").read_text())
    rows = state["results"]
    endurance = args.comparison.parent / 'v2-final-60min-20261117' / 'analysis.json'
    if endurance.exists():
        final = json.loads(endurance.read_text())
        final.update(name='deadline-final-60min', output=str(endurance.parent))
        rows = rows + [final]
    lines = ["# 第二轮：按屏幕刷新截止时间选择最新帧", "",
        "测试状态：" + ("全部完成" if state.get("completed") else "尚未全部完成"), "",
        f"保留IMX415 3840×2160@60采集、MPP H.265硬编硬解、20Mbps、GOP60，采集池为{state.get('buffers', 4)}个缓冲。HDMI为当前显示器支持的1920×1080@60，左上角透明状态文字保持开启。", "",
        "## 改动", "",
        "显示线程根据上一次实际翻页事件和显示模式周期，计算下一次刷新的提交截止时间。待显示槽始终只有一帧，截止前到达的新帧可以替换旧候选帧；临近刷新时才取出最新候选并提交，保留常规垂直同步翻页。",
        "", "同时缓存DRM对象的属性ID，减少每帧重复查询。该缓存也用于本轮即时提交对照，因此两组的主要差异是选择帧和提交的时机。没有启用可能产生撕裂的异步扫描切换。",
        "", "两组均将视频进程限定在由cpu_capacity识别的大核上，并复用项目已有RAW调度模块：仅在运行期间，把已校验身份的rkaiq三个阻塞式RAW传输工作线程设为SCHED_RR优先级5，退出恢复原策略。没有修改系统IRQ亲和性或其他服务的调度策略。增加采集缓冲本身未消除早期缺帧，因此不能把稳定性的改善只归因于缓冲数量。",
        "", "透明HUD使用1152×160画布，提前在选择视频候选之前准备；翻页提交路径只附加已准备的缓冲，避免每秒文字更新阻塞关键提交时刻。两组使用相同HUD、CPU和采集调度设置。",
        "", f"本轮选用提前量 {state.get('margin_us', 0) / 1000:.1f}ms。队列、编码器输入池、解码和扫描帧的所有权约束保持不变，退出仍先停止全部工作线程再撤销显示。", "",
        "## 同一程序交错对照与持续运行", "",
        "| 测试 | 显示fps | P50 ms | P95 ms | P99 ms | 驱动戳到翻页P95 ms | 翻页等待P95 ms | 错过目标次数 | 驱动缺帧 | 错误 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        s = r["summary"]
        lat = s["latency"]["dq_to_flip_ms"]
        driver = s['latency'].get('driver_timestamp_to_flip_ms', {}).get('p95', 0)
        lines.append(f"| {r['name']} | {r['rates']['displayed_fps']:.3f} | {lat['p50']:.1f} | {lat['p95']:.1f} | {lat['p99']:.1f} | {driver:.1f} | {s['latency']['kms_flip_ms']['p95']:.1f} | {s.get('missed_targets', 0)} | {s['driver_gaps']} | {s['errors']} |")
    audit_path = args.comparison / 'driver-to-dq-audit.json'
    if audit_path.exists():
        lines += ['', '### 取帧前的时间差', '', '逐帧核对驱动时间戳到DQBUF的时间差，排除启动前300帧，只统计已显示且时间戳有效的帧：', '',
                  '| 测试 | P50 ms | P95 ms |', '|---|---:|---:|']
        for row in json.loads(audit_path.read_text()):
            lines.append(f"| {row['name']} | {row['p50_ms']:.3f} | {row['p95_ms']:.3f} |")
        lines += ['', '不同启动轮次存在约一个帧周期的固定差异，成因尚未确认，不能归结为显示策略造成或消除。应在相近取帧前时间差的轮次之间对照驱动戳到翻页指标，具体数值见上表。没有据此宣称光学端到端达标。']
    for failed_name in ("v2-comparison-20261117", "v2-buffer8-comparison-20261117"):
        failed_path = args.comparison.parent / failed_name / "comparison.json"
        if not failed_path.exists() or failed_path.parent == args.comparison:
            continue
        failed = json.loads(failed_path.read_text())
        captures = [r for r in failed.get("results", []) if r.get("summary", {}).get("driver_gaps", 0)]
        if captures:
            lines += ["", f"早期实验 `{failed_name}` 出现采集驱动缺帧，判为失败并保留原始数据，没有纳入通过结果："]
            for r in captures:
                lines.append(f"- {r['name']}：采集 {r['rates']['captured_fps']:.3f}fps，驱动缺帧 {r['summary']['driver_gaps']}，记录于 `{r['output']}`。")
            lines += ["", "本表中对照和新版使用相同的较大采集池；应用层待处理队列容量仍为一帧。增加缓冲是针对已观察到的采集稳定性问题，不能据此宣称已经确认底层驱动根因。"]
    if "improvement_ms" in state:
        a, b = state["baseline_mean_p95_ms"], state["deadline_mean_p95_ms"]
        lines += ["", f"两轮即时提交的P95均值为 {a:.2f}ms，两轮截止时间调度为 {b:.2f}ms，减少 {a-b:.2f}ms（{100*(a-b)/a:.1f}%）。这是各轮P95的均值比较，不是合并所有帧后的P95。"]
    for r in rows:
        if r["name"] in ("deadline-soak", "deadline-final-60min"):
            s = r["summary"]
            lines += ["", "## 新版持续运行：" + r['name'], "",
                f"实际运行 {s['elapsed_s']:.2f}秒，显示 {s['displayed']}帧；最高温度 {r['max_temperature_c']:.2f}℃，原生进程平均CPU {r['mean_cpu_one_core_percent']:.2f}%（单核口径），峰值RSS {r['max_rss_mib']:.2f}MiB。CPU占用不等于整板功耗，本轮未进行瓦特级测量。", "",
                "逐帧核对：`" + json.dumps(r["frame_audit"], ensure_ascii=False) + "`。", "",
                "刷新目标核对：`" + json.dumps(r.get("deadline_audit"), ensure_ascii=False) + "`。"]
            drops = s['raw_drops'] + s['display_drops']
            lines += ['', f"主动候选替换：编码前{s['raw_drops']}帧、解码后{s['display_drops']}帧，合计约占采集帧的{100*drops/s['captured']:.3f}%。这是以少量跳帧换取新鲜画面，和驱动缺帧、解码错误分开统计。HUD准备的最大墙钟耗时为{s.get('max_overlay_prepare_ms', 0):.3f}ms；准备已移到候选选择之前，但不能宣称更新永远不会受调度抖动影响。"]
            driver = s["latency"].get("driver_timestamp_to_flip_ms", {})
            if driver:
                lines += ["", f"驱动时间戳到翻页的P95为 {driver['p95']:.1f}ms，单独报告以检查是否把等待转移到应用取帧之前；驱动时间戳的采样位置尚未做光学校验。"]
            windows = [w for w in r["latency_by_minute"] if w["n"] > 3000]
            if windows:
                lines += ["", f"有效分钟窗口的P95范围 {min(w['p95_ms'] for w in windows):.2f}–{max(w['p95_ms'] for w in windows):.2f}ms；首/末窗口分别为 {windows[0]['p95_ms']:.2f}/{windows[-1]['p95_ms']:.2f}ms。"]
        if r.get("stall_recovery"):
            lines += ["", "阻塞恢复：`" + json.dumps(r["stall_recovery"], ensure_ascii=False) + "`。"]
    lifecycle = args.comparison.parent / 'v2-release-lifecycle-20261117' / 'lifecycle.json'
    if lifecycle.exists():
        lines += ['', '## 退出恢复', '', '`' + json.dumps(json.loads(lifecycle.read_text()), ensure_ascii=False) + '`。', '',
                  '显示错误注入、手动停止、再次启停及监督进程SIGKILL后恢复均单独检查；这不代表物理HDMI断连测试。最终空闲状态另外保存在生命周期目录的final-state.json。']
    lines += ["", "## 口径与限制", "",
        "延迟仍为V4L2取帧到实际DRM翻页事件，不包含曝光、传感器读取、ISP前段和显示面板响应；尚未进行光学端到端测量。错过刷新目标由原始帧时间戳复核，包含源帧晚到或提交迟到，不自动归因于显示硬件。",
        "", "上一轮曾出现一次用户未操作的非正常重启，根因尚未确认。本轮会核对启动ID，不能用短期未复现来宣称其根因已经解决。旧版源码和可执行文件保存在versions/v1_20261117，仍可选择immediate模式回退。",
        "", "内核日志另记录了桌面恢复附近的RGA映射及未连接CIF通道错误，已保存供追查，未将其隐藏或等同于实时链路解码错误。实时C++路径不直接调用RGA。物理HDMI拔插、光学测量和此前意外重启的根因仍未完成验证。",
        "", "原始数据：`" + str(args.comparison) + "`。"]
    args.output.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
