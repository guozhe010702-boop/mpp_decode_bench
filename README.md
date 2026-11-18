# mpp_decode_bench：RK3588 / IMX415 4K60 实时编解码

本项目包含摄像头性能基准、历史桌面显示对照，以及当前推荐的原生低延迟程序 **[low_latency](low_latency/README.md)**。

当前 V3 链路：IMX415 4K60采集 → 连续NV12 DMA-BUF共享 → MPP H.265硬件编码 → MPP硬件解码 → DRM/KMS HDMI显示。视频左上角透明显示实际帧率、CPU、温度、内存等状态；手动启动，不安装开机启动项。

| V3连续60分钟实测 | 结果 |
|---|---:|
| 采集、编码、解码 | 约59.996fps |
| HDMI显示 | 59.960fps |
| 取帧到翻页 P95 / P99 | 37.1 / 38.6ms |
| 原生进程CPU（单核100%口径） | 平均5.55% |
| 驱动缺帧 / 解码错误 | 0 / 0 |

采集与编解码保持3840×2160@60，当前HDMI显示器只支持1920×1080@60，最终由显示平面缩放。上游约32/49ms的启动差异尚未解决；上述内部延迟不等同于光学端到端延迟。完整口径、失败实验和限制见 [V3报告](low_latency/V3_RESULTS.md)。

## 推荐运行方式

本项目针对已经配置好的RK3588板端环境：Linux 5.10.110、IMX415 V4L2驱动、私有MPP SDK、libdrm开发包、Python 3.8+、Cairo/Pango、v4l2-ctl、systemd与GDM/Xrandr恢复工具。它不是面向任意Linux电脑的通用安装包。

部署路径为 `/userdata/av_dev/mpp_decode_bench`，私有SDK位于 `/userdata/av_dev/ffmpeg_pipeline/runtime`。还需要同目录下的 `camera_control` 模块；源码快照位于 [low_latency/dependencies](low_latency/dependencies/README.md)，在缺少该模块的新环境中可将快照复制到 `/userdata/av_dev/ffmpeg_pipeline/camera_control`。SDK二进制及第三方开发库需自行准备，仓库不包含它们。

在板端root终端执行：

```bash
cd /userdata/av_dev/mpp_decode_bench/low_latency
bash build_board.sh
bash start_v3.sh
```

查看状态：`python3 control.py status`；停止：`python3 control.py stop`。停止或异常退出后恢复摄像头格式和原桌面服务。运行期间会临时独占显示设备。

## 目录与验收记录

- `low_latency/`：当前C++ V4L2、MPP、DRM/KMS程序，以及监督、遥测和启停脚本。
- `low_latency/versions/`：V1/V2历史源码快照。
- `low_latency/V3_RESULTS.md`：当前版本完整报告；V1/V2报告同目录保留。
- `evidence/`：精简的性能与恢复验收JSON，不含图像或原始码流。
- 根目录的 `live_mpp_bench.py`、`live_hdmi.py`：历史FFmpeg/GStreamer对照实现。

本仓库上传源码、配置、报告和精简证据。原始逐帧CSV、摄像头图像、码流、大型归档和编译产物保留在原实验目录，未纳入Git。

## 历史FFmpeg基准

本目录使用开发板实际连接的 IMX415，运行 3840×2160、60 fps 请求下的采集、MPP 编码和 MPP 解码链路。默认 H.265、目标码率 20 Mbit/s。

```text
IMX415 /dev/video44 → NV12 采集 → hevc_rkmpp → MPEG-TS 内存管道 → hevc_rkmpp → 解码帧计数
```

`drm` 模式保留解码器输出的 DRM 硬件帧。`nv12` 模式将完整 4K 解码帧下载为 CPU 可访问的 NV12，用于观察后续图像处理所需的数据拷贝开销。两种模式都保留完整 3840×2160 输出。

## 板端运行

目录部署到 `/userdata/av_dev/mpp_decode_bench`。使用现有 `/userdata/av_dev/ffmpeg_pipeline/runtime/bin/ffmpeg`，并复用该工程的相机占用检查、传感器帧率和 Linux 资源采样函数。需要 Python 3.8 或以上以及 `v4l2-ctl`。

```bash
cd /userdata/av_dev/mpp_decode_bench
bash run_board.sh 60 hevc
```

这条命令依次测试两种输出模式，各预热5秒，再测量60秒。可将 `hevc` 换成 `h264`。

本次正式实测使用 `hevc`。详细结果见 `RESULTS.md`。`h264` 参数可用于后续独立复测。

单独运行一种模式：

```bash
python3 live_mpp_bench.py --seconds 60 --output-mode drm --output results/my-run
```

输出目录需要使用新的名称。程序检查相机占用，采样温度达到72℃时停止。正常结束、报错或 Ctrl+C 时都会清理本次创建的编解码进程，并恢复测试前的采集分辨率和像素格式。

## 结果口径

- `result.json` 保存完整命令、实测帧率、CPU、内存、温度、MPP设备占用证据、采集缺口和判定结果。
- `samples.csv` 保存约每250毫秒一次的原始资源与帧计数。
- `encoder.log`、`decoder.log` 保存 FFmpeg 原始日志。两个 `.progress` 文件保存 FFmpeg 进度计数。
- 采集帧率来自已有 FFmpeg 补丁的 `AV_CAPTURE` 有效帧数和单调时钟差分。
- 编码和解码帧率来自各自的真实输出帧计数与墙钟差分。时间戳重基不增加帧数。
- CPU按单核100%统计。RSS为进程常驻内存，板端可用内存采用 `MemAvailable`。
- `realtime_4k60_pass` 要求测量时长满足设置，采集、编码、解码都达到59 fps，测量期采集缺口和错帧为0，两个进程均实际打开MPP设备，输出日志确认4K解码帧，且解码日志无错误。
- 程序还会核对结束时编码帧数与解码帧数相等，并检查补帧、输出丢帧计数为0。编解码进度各自分批上报，短窗口帧率会有轻微波动。
- 测试结束时编码器收到 SIGINT，FFmpeg 可能以255退出；解码器读完管道后正常退出。请结合 `stop_reason`、完整帧计数和错误日志判断本次结果。
- 进程RSS不包含全部MPP/DMA硬件缓冲；评估8GB内存是否够用时，还需结合板端最小可用内存。

结果用于所记录时长的本地实时编解码性能评估。显示刷新、镜头到屏幕时延和AI推理需要各自的实测记录。

## 板端 HDMI 实时显示

`live_hdmi.py` 使用 IMX415 4K60采集、FFmpeg MPP H.265编码、GStreamer MPP完整4K解码，再由GL渲染到HDMI当前分辨率。它会在HDMI屏幕上全屏显示相机画面，按 Escape 可结束预览。

显示版本采用HEVC裸流内存管道，显示端关闭按时间戳等待和上游QoS丢帧；解码后设置最多2帧的队列，拥塞时淘汰较旧的已解码画面，保持预览更新。性能基准程序 `live_mpp_bench.py` 继续使用其记录的测试链路。

在板端 root 终端手动启动：

```bash
bash /userdata/av_dev/mpp_decode_bench/start_hdmi.sh
```

停止：

```bash
bash /userdata/av_dev/mpp_decode_bench/stop_hdmi.sh
```

查看实时状态：

```bash
cat /userdata/av_dev/mpp_decode_bench/hdmi_status.json
```

启动脚本建立名为 `imx415-hdmi-live.service` 的临时服务，在当前启动周期内运行。画面关闭后可以再次手动启动。退出后程序释放相机并恢复采集格式。

程序读取实际HDMI输出模式。当前连接的HDMI屏幕报告1920×1080@60Hz，显示窗口按此模式输出，采集、编码和硬件解码维持3840×2160。每次运行的日志、每秒遥测和一张HDMI画面截图保存在 `results/hdmi-日期时间/`。

`display_fps_average` 使用预热后的实际渲染计数和墙钟差分；`display_dropped_since_warmup`、`capture_gaps_since_warmup` 分别记录预热后的新增显示掉帧和采集序号缺口。首次启动的累计序号变化单独保留在原始字段中。

`decoder_fps_average` 直接统计MPP解码器输出帧。`display_queue_buffers`、`display_queue_time_ms` 表示显示队列当前深度，`display_queue_overruns_since_warmup` 表示预热后的队列拥塞事件。队列淘汰事件和显示sink报告的掉帧分别记录。完整镜头到屏幕延迟需要单独测量。
