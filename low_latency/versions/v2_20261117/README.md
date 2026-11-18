# IMX415 / RK3588 原生低延迟编解码和 HDMI 显示

程序在板端运行，Windows 保存源码与实验副本。原 `live_hdmi.py` 保留作为对照。

2026-11-17第二轮最终版本已完成60分钟持续验证：采集/编码/解码约59.99fps，显示59.81fps，取帧到翻页P95 41.6ms、P99 42.6ms，驱动戳到翻页P95 73.6ms；无驱动缺帧和解码错误。最高49.92℃，原生进程平均占用单核20.54%。详见 [第二轮实测报告](V2_RESULTS.md)。这是程序内计时，尚未做光学端到端测量。

```text
IMX415 → RKISP /dev/video44 NV12M 3840×2160@60
 → 最新原始帧槽（1张）
 → 双预分配输入缓冲，整理 NV12M → 连续 NV12
 → MPP 异步 H.264/H.265 硬编 → 按序完整 access unit
 → 同版本 MPP 全分辨率硬解 → 最新解码帧槽（1张）
 → DMA-BUF 导入 DRM/KMS → 1080p60 HDMI 翻页
```

当前 HDMI 屏幕只公布 1080p60 模式。采集、编码、解码都是4K60，最终显示平面缩放到1080p。`direct` 仅是跳过编解码的诊断模式。

视频左上角有中文状态文字，背景完全透明，仅文字带细描边。状态使用独立ARGB硬件平面双缓冲，每秒更新；FPS按近期约3秒实际计数计算。显示采集/编码/解码/显示FPS、当前取帧到翻页延迟、CPU系统占用和进程单核占用、GPU负载、NPU三核负载、温度、内存、RSS和主动丢弃/错误计数。GPU/NPU值来自板端驱动节点；读不到时显示`--`，不填入估计值。运行Cairo/Pango文字渲染的是监督进程，视频由C++/MPP/DRM处理。

## 板端手动启停

在 `root@ema` 终端执行：

```bash
cd /userdata/av_dev/mpp_decode_bench/low_latency
python3 control.py start
python3 control.py status
python3 monitor.py
python3 control.py stop
```

默认使用通过验收后生成的 `recommended.json`；没有配置时用HEVC、6个采集缓冲、20Mbps、GOP60、60fps、无B帧。`start` 默认持续运行，SSH断开后继续，不安装开机启动项。显式指定示例：

```bash
python3 control.py start --codec h264 --buffers 6 --seconds 60
```

第二轮新增显示调度选项：

```bash
python3 control.py start --display-scheduling deadline --display-margin-us 3500
python3 control.py start --display-scheduling immediate
```

`deadline`在预计下一次刷新前选取最新的待显示帧，`immediate`收到候选后立即提交。两者都使用常规垂直同步翻页。正式默认值读取验证通过后写入的推荐配置；第二条可显式回退到即时提交策略。原始版本源码和可执行文件备份在 `versions/v1_20261117/`。

第二轮的4缓冲连续测试曾发生采集驱动缺帧，已保留为失败数据；后续对照改用更大的采集池验证。缓冲分配数量不等于排队帧数，应用层原始帧槽与显示槽始终各最多一张。新版结论以 [第二轮实测报告](V2_RESULTS.md) 为准，不与旧版60分钟测试混淆。

第二轮已采用的配置还包含8个采集缓冲、`--cpu-affinity big` 和 `--capture-scheduling raw-rr`。大核根据系统 `cpu_capacity` 识别，本板为CPU4–7；仅约束视频进程及其线程。RAW调度复用已有 `/userdata/av_dev/ffmpeg_pipeline/camera_control/raw_scheduler.py` 模块，校验rkaiq进程和线程身份后，临时把三个阻塞式RAW传输线程设为RR优先级5，退出恢复原策略。未修改系统IRQ或开机配置。正常调度下仅增加缓冲仍曾缺帧，失败数据一并保留。推荐文件已经更新为上述通过验证的配置。

透明HUD提前准备，画布1152×160，每秒更新；视频提交不再承担文字缓冲的准备操作。

运行期间临时停止 GDM，独占显示设备；DSI桌面也会暂时消失。停止、正常结束或错误退出后恢复摄像头格式、桌面服务和原HDMI/DSI布局。不要直接启动裸C++程序：控制器负责互斥、摄像头占用检查、传感器帧率检查、温度保护与恢复。

如果监督进程被强制终止，临时systemd服务的 `ExecStopPost` 会再次执行恢复。若显示器已被物理拔掉，恢复原布局可能暂时失败，插回后可执行：

```bash
python3 control.py restore
```

不自动恢复此前已经停用的YOLO推理服务。

## 编译和复现实验

```bash
bash build_board.sh
python3 control.py run --codec hevc --seconds 30 --output /userdata/latency-example
python3 benchmark.py --output /userdata/latency-acceptance
```

输出目录必须不存在。构建使用现有私有SDK和系统libdrm，不替换系统MPP、GStreamer或内核。`build/` 是板端ARM64构建目录。

完整矩阵：直接显示30秒；4/6/16采集缓冲各60秒；H.265和H.264各三轮、每轮300秒，交错顺序；H.264关闭立即输出对照30秒；注入250ms处理阻塞30秒；优选配置连续3600秒。每轮另加5秒预热、桌面恢复及离线分析。完整矩阵约需100分钟。

补充验证和生成报告（均在板端执行）：

```bash
python3 -m unittest test_contracts -v
python3 validate_lifecycle.py --output /userdata/latency-lifecycle
python3 report.py /userdata/latency-acceptance --output RESULTS.md
```

第二轮同版本交错对照（即时提交、截止时间调度、截止时间调度、即时提交，各180秒），再持续运行1200秒和测试250ms阻塞恢复：

```bash
python3 compare_v2.py --output /userdata/latency-v2
python3 report_v2.py /userdata/latency-v2 --output V2_RESULTS.md
```

必须先停止其他预览或基准测试。比较程序不会自动覆盖推荐配置；只有对照、持续运行及退出恢复均通过才发布新配置。交错对照后的20分钟测试之外，最终相同版本另做了3600秒持续验证，数据位于 `results/v2-final-60min-20261117/`。

生命周期验证必须在预览和矩阵均停止时运行，会测试显示错误注入、手动启停、重复启动拒绝和监督进程SIGKILL后的恢复。它不包含物理拔插HDMI线。`monitor.py`只读最近一分钟的数据，输出实际FPS、P95、温度、内存和错误计数。

只有每轮恢复成功才继续。缓冲选择满足帧率和延迟条件的最小数量；编码选择三轮最差P95较低者，如果两者差距不足一个60Hz显示周期，保留HEVC。最终耐久测试通过才写推荐配置。

## 延迟与帧率口径

- 主指标：应用 `VIDIOC_DQBUF` 返回后的 `CLOCK_MONOTONIC` 到 DRM page-flip 事件时间。驱动必须报告DRM事件使用单调时钟。
- 驱动时间戳单独保留原值、V4L2标志及其到翻页的耗时；启动时的历史帧、序号跳变单独报告。该时间戳的采样位置由驱动决定，不等同于曝光开始。
- 主指标不包含取帧前的曝光、传感器读取、ISP处理，也不包含屏幕面板实际响应；150ms光学端到端目标需要另做慢动作拍摄验证。
- 不通过 `setpts` 重写真实帧来源。编码PTS携带内部帧编号，解码后精确匹配；不匹配立即失败。
- 各阶段独立计数，以预热后实际墙钟时间差计算FPS。编码器写入“60fps”本身不作为验收证据。
- 预热后目标：采集/编码/解码/显示平均各≥59fps，取帧到翻页P95≤100ms，解码错误和驱动序号缺口均为0。主动丢帧另报。
- C++使用固定内存直方图统计整段延迟，分位数按0.1ms向上取整；CSV保留微秒原始数据。预览时间增长不会扩大统计内存。

## 排队、缓冲和所有权

采集槽与显示槽满时替换旧帧；旧相机帧立即QBUF，旧解码帧释放引用。编码器最多两个输入在途，缓冲等到该帧最后一个输出包完成才复用。压缩帧按序完整送解码器，不随机丢参考帧；异常时退出并恢复，重新启动会从IDR开始。

显示只允许一次未完成翻页，保留一张最新候选帧。当前扫描帧在下一次翻页完成或显示禁用后才释放。相机的双平面NV12M无法直接当作MPP所需的单块连续输入，因此用预分配DMA缓冲整理布局，计入 `layout_copy_ms`；解码到DRM共享DMA-BUF。本实现不宣称整条链路零拷贝。

“主动丢弃”表示待处理槽满时释放旧候选、保留新画面，以控制画面年龄，代价是偶尔跳帧。它与驱动漏帧、解码错误分别统计，不会任意丢弃压缩参考帧。最终60分钟运行中原始候选替换6帧、显示候选替换653帧，合计约占采集216282帧的0.305%；启动时另有4帧无效时间戳、停止时2帧未显示，均在逐帧记录中保留。

退出统一由主线程收尾：停止并等待所有工作线程后，撤销DRM显示，再释放仍被扫描的图像和硬件资源。异常路径也使用相同顺序。`lifecycle.log`以同步写入记录关键阶段，状态JSON进行fsync，便于分析意外重启或断电；`status`会把进程已退出或过期的指标标为陈旧。

MPP异步输入必须在 `mpp_init` **之前**设置 `MPP_SET_INPUT_TIMEOUT=0`。MPP默认同步输入会在 `encode_put_frame` 内等硬件完成，使拷贝与编码串行；仅增加缓冲或输出线程不能解决。MPP输入帧描述符保持到对应输出完成，避免异步访问已经释放的对象。

低延迟模式和关闭重编码均检查返回值并读回配置。H.264启用并单独对比 `MPP_DEC_SET_IMMEDIATE_OUT`；HEVC不套用H.264专用控制。`--split-ctus` 为实验接口，当前会重新组装完整AU后送解码，**不能据此声称实现了端到端slice流水线**。整帧方案达标时不启用slice。

## 输出和检查

每次运行保存：

| 文件 | 内容 |
|---|---|
| `frames.csv` | 帧编号、驱动序号/标志、取帧/拷贝/编解码/提交/翻页时间和最终去向 |
| `rates.csv`、`telemetry.jsonl` | 实际计数、主动丢帧、CPU、RSS、温度、可用内存和MPP设备描述符 |
| `analysis.json`、`summary.json` | 分位数、实际FPS、码率、分钟窗口和验收判定 |
| `manifest.json`、`command.json`、`mpp-config.txt` | 二进制/源码/MPP SHA256、参数、配置读回 |
| `sample.hevc` / `sample.h264` | 启动前6秒的码流，用ffprobe核对4K60、无B帧、色彩标记 |
| `quality-source.nv12`、`quality-decoded.nv12` | 预热期内同一帧的原图和硬解结果，PSNR/SSIM离线校验 |
| `restoration.json` | 摄像头、桌面及布局恢复结果 |

单帧PSNR/SSIM用于检查传输和编解码损失，不代表对固定运动场景的完整画质评价。RSS不含全部驱动管理的DMA内存，不能用它代替整板内存或MPP缓冲占用。

故障注入：`--inject-stall-ms 250 --stall-after 15` 检查追帧；`--inject-display-error-after 10` 检查显示失败后的清理。后者是程序错误注入，不是物理HDMI热插拔验证。

## 参考

实现复用本工作区 `examples/mpp_c_api/mpp_h265.c` 的MPP配置和NV12布局整理方法，以及 `Low latency stream` 的异步缓冲生命周期思路；未引入原RTSP/桌面软件解码路径。

- [Rockchip MPP开发参考](https://github.com/rockchip-linux/mpp/blob/develop/doc/Rockchip_Developer_Guide_MPP_CN.md)
- [MPP源码：同步/异步接口实现](https://github.com/rockchip-linux/mpp/blob/develop/mpp/mpp.c)
- [Linux V4L2缓冲与时间戳](https://www.kernel.org/doc/html/latest/userspace-api/media/v4l/buffer.html)
- [Linux DRM用户态接口](https://docs.kernel.org/gpu/drm-uapi.html)
- [GStreamer队列与丢弃策略](https://gstreamer.freedesktop.org/documentation/coreelements/queue.html)
