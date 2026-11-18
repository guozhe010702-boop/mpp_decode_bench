# 第二轮：按屏幕刷新截止时间选择最新帧

测试状态：全部完成

保留IMX415 3840×2160@60采集、MPP H.265硬编硬解、20Mbps、GOP60，采集池为8个缓冲。HDMI为当前显示器支持的1920×1080@60，左上角透明状态文字保持开启。

## 改动

显示线程根据上一次实际翻页事件和显示模式周期，计算下一次刷新的提交截止时间。待显示槽始终只有一帧，截止前到达的新帧可以替换旧候选帧；临近刷新时才取出最新候选并提交，保留常规垂直同步翻页。

同时缓存DRM对象的属性ID，减少每帧重复查询。该缓存也用于本轮即时提交对照，因此两组的主要差异是选择帧和提交的时机。没有启用可能产生撕裂的异步扫描切换。

两组均将视频进程限定在由cpu_capacity识别的大核上，并复用项目已有RAW调度模块：仅在运行期间，把已校验身份的rkaiq三个阻塞式RAW传输工作线程设为SCHED_RR优先级5，退出恢复原策略。没有修改系统IRQ亲和性或其他服务的调度策略。增加采集缓冲本身未消除早期缺帧，因此不能把稳定性的改善只归因于缓冲数量。

透明HUD使用1152×160画布，提前在选择视频候选之前准备；翻页提交路径只附加已准备的缓冲，避免每秒文字更新阻塞关键提交时刻。两组使用相同HUD、CPU和采集调度设置。

本轮选用提前量 3.5ms。队列、编码器输入池、解码和扫描帧的所有权约束保持不变，退出仍先停止全部工作线程再撤销显示。

## 同一程序交错对照与持续运行

| 测试 | 显示fps | P50 ms | P95 ms | P99 ms | 驱动戳到翻页P95 ms | 翻页等待P95 ms | 错过目标次数 | 驱动缺帧 | 错误 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| immediate-1 | 59.989 | 44.6 | 51.3 | 51.8 | 83.4 | 16.7 | 0 | 0 | 0 |
| deadline-1 | 59.903 | 32.2 | 40.9 | 42.4 | 72.9 | 3.5 | 18 | 0 | 0 |
| deadline-2 | 59.880 | 33.4 | 41.8 | 42.6 | 73.8 | 3.5 | 22 | 0 | 0 |
| immediate-2 | 59.991 | 43.3 | 51.0 | 51.8 | 99.7 | 16.7 | 0 | 0 | 0 |
| deadline-soak | 59.846 | 33.3 | 41.3 | 42.5 | 90.0 | 3.5 | 187 | 0 | 0 |
| deadline-stall | 59.583 | 36.2 | 39.1 | 39.4 | 71.1 | 3.5 | 11 | 0 | 0 |
| deadline-final-60min | 59.813 | 34.2 | 41.6 | 42.6 | 73.6 | 3.5 | 680 | 0 | 0 |

### 取帧前的时间差

逐帧核对驱动时间戳到DQBUF的时间差，排除启动前300帧，只统计已显示且时间戳有效的帧：

| 测试 | P50 ms | P95 ms |
|---|---:|---:|
| immediate-1 | 32.029 | 32.083 |
| deadline-1 | 32.016 | 32.066 |
| deadline-2 | 32.016 | 32.066 |
| immediate-2 | 48.691 | 48.736 |
| deadline-soak | 48.691 | 48.738 |
| deadline-final-60min | 32.018 | 32.064 |

不同启动轮次存在约一个帧周期的固定差异，成因尚未确认，不能归结为显示策略造成或消除。应在相近取帧前时间差的轮次之间对照驱动戳到翻页指标，具体数值见上表。没有据此宣称光学端到端达标。

早期实验 `v2-comparison-20261117` 出现采集驱动缺帧，判为失败并保留原始数据，没有纳入通过结果：
- deadline-2：采集 58.007fps，驱动缺帧 373，记录于 `/userdata/av_dev/mpp_decode_bench/low_latency/results/v2-comparison-20261117/deadline-2`。

本表中对照和新版使用相同的较大采集池；应用层待处理队列容量仍为一帧。增加缓冲是针对已观察到的采集稳定性问题，不能据此宣称已经确认底层驱动根因。

早期实验 `v2-buffer8-comparison-20261117` 出现采集驱动缺帧，判为失败并保留原始数据，没有纳入通过结果：
- immediate-1：采集 58.566fps，驱动缺帧 257，记录于 `/userdata/av_dev/mpp_decode_bench/low_latency/results/v2-buffer8-comparison-20261117/immediate-1`。

本表中对照和新版使用相同的较大采集池；应用层待处理队列容量仍为一帧。增加缓冲是针对已观察到的采集稳定性问题，不能据此宣称已经确认底层驱动根因。

两轮即时提交的P95均值为 51.15ms，两轮截止时间调度为 41.35ms，减少 9.80ms（19.2%）。这是各轮P95的均值比较，不是合并所有帧后的P95。

## 新版持续运行：deadline-soak

实际运行 1205.67秒，显示 72121帧；最高温度 49.00℃，原生进程平均CPU 20.63%（单核口径），峰值RSS 10.30MiB。CPU占用不等于整板功耗，本轮未进行瓦特级测量。

逐帧核对：`{"unique_frame_ids": 72313, "duplicate_ids": 0, "nonmonotonic_display_timelines": 0, "outcomes": {"invalid_driver_timestamp": 5, "displayed": 72121, "display_replaced": 185, "stopped_decoder": 2}, "all_capture_ids_accounted": true}`。

刷新目标核对：`{"targeted_frames": 71841, "missed_targets": 187, "missed_target_percent": 0.26029704486296124, "commit_lead_p50_ms": 3.44, "commit_lead_min_ms": -21.823, "counter_matches_trace": true}`。

主动候选替换：编码前0帧、解码后185帧，合计约占采集帧的0.256%。这是以少量跳帧换取新鲜画面，和驱动缺帧、解码错误分开统计。HUD准备的最大墙钟耗时为0.948ms；准备已移到候选选择之前，但不能宣称更新永远不会受调度抖动影响。

驱动时间戳到翻页的P95为 90.0ms，单独报告以检查是否把等待转移到应用取帧之前；驱动时间戳的采样位置尚未做光学校验。

有效分钟窗口的P95范围 32.17–42.48ms；首/末窗口分别为 33.25/34.69ms。

阻塞恢复：`{"injected_stall_ms": 250, "observed_submission_gap_ms": 252.594, "first_fresh_frame_id": 906, "first_fresh_frame_raw_wait_ms": 2.808, "first_fresh_frame_dq_to_flip_ms": 38.343, "first_flip_after_submission_resumes_ms": 33.007, "stall_observed": true}`。

## 新版持续运行：deadline-final-60min

实际运行 3605.20秒，显示 215617帧；最高温度 49.92℃，原生进程平均CPU 20.54%（单核口径），峰值RSS 10.32MiB。CPU占用不等于整板功耗，本轮未进行瓦特级测量。

逐帧核对：`{"unique_frame_ids": 216282, "duplicate_ids": 0, "nonmonotonic_display_timelines": 0, "outcomes": {"invalid_driver_timestamp": 4, "displayed": 215617, "display_replaced": 653, "raw_replaced": 6, "stopped_decoder": 2}, "all_capture_ids_accounted": true}`。

刷新目标核对：`{"targeted_frames": 215332, "missed_targets": 680, "missed_target_percent": 0.3157914290490963, "commit_lead_p50_ms": 3.44, "commit_lead_min_ms": -16.565, "counter_matches_trace": true}`。

主动候选替换：编码前6帧、解码后653帧，合计约占采集帧的0.305%。这是以少量跳帧换取新鲜画面，和驱动缺帧、解码错误分开统计。HUD准备的最大墙钟耗时为33.148ms；准备已移到候选选择之前，但不能宣称更新永远不会受调度抖动影响。

驱动时间戳到翻页的P95为 73.6ms，单独报告以检查是否把等待转移到应用取帧之前；驱动时间戳的采样位置尚未做光学校验。

有效分钟窗口的P95范围 31.90–42.38ms；首/末窗口分别为 33.01/41.15ms。

## 退出恢复

`[{"test": "display-error-injection", "passed": true, "physical_hotplug_tested": false, "output": "results/v2-release-lifecycle-20261117/display-error"}, {"test": "manual-stop", "passed": true, "output": "results/v2-release-lifecycle-20261117/manual-stop", "camera_format_restored": true, "desktop_restored": true, "display_layout_restored": true}, {"test": "restart-stop", "passed": true, "output": "results/v2-release-lifecycle-20261117/restart-stop", "camera_format_restored": true, "desktop_restored": true, "display_layout_restored": true}, {"test": "supervisor-sigkill", "passed": true, "output": "results/v2-release-lifecycle-20261117/supervisor-sigkill", "camera_format_restored": true, "desktop_restored": true, "display_layout_restored": true}]`。

显示错误注入、手动停止、再次启停及监督进程SIGKILL后恢复均单独检查；这不代表物理HDMI断连测试。最终空闲状态另外保存在生命周期目录的final-state.json。

## 口径与限制

延迟仍为V4L2取帧到实际DRM翻页事件，不包含曝光、传感器读取、ISP前段和显示面板响应；尚未进行光学端到端测量。错过刷新目标由原始帧时间戳复核，包含源帧晚到或提交迟到，不自动归因于显示硬件。

上一轮曾出现一次用户未操作的非正常重启，根因尚未确认。本轮会核对启动ID，不能用短期未复现来宣称其根因已经解决。旧版源码和可执行文件保存在versions/v1_20261117，仍可选择immediate模式回退。

内核日志另记录了桌面恢复附近的RGA映射及未连接CIF通道错误，已保存供追查，未将其隐藏或等同于实时链路解码错误。实时C++路径不直接调用RGA。物理HDMI拔插、光学测量和此前意外重启的根因仍未完成验证。

原始数据：`results/v2-release-comparison-20261117`。
