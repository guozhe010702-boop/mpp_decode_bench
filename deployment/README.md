# RK3588 / IMX415 H.265 4K60 部署操作说明

本交付是完成60分钟验证的V3。实时程序运行在RK3588，Windows只保存源码及部署包。

## 程序位置

- 原Windows源码：`C:\Users\dvsense_docker\Desktop\work\RK3588\av_dev\mpp_decode_bench`。
- 交付副本：`C:\Users\dvsense_docker\Desktop\H.265-4K@60`。
- 板端核心：`/userdata/av_dev/mpp_decode_bench/low_latency`。
- 板端依赖：`/userdata/av_dev/ffmpeg_pipeline/runtime` 与同级`camera_control`。
- GitHub：https://github.com/guozhe010702-boop/mpp_decode_bench 。

交付目录包含展开的`RK3588_IMX415_H265_4K60_V3/`、Linux部署用同名`.tar.gz`和Windows查看用`.zip`。源码在`payload/userdata/av_dev/mpp_decode_bench/`；`low_latency/build/imx415_low_latency`是ARM64可执行文件。包含对应私有MPP、FFmpeg/ffprobe、SDK头文件和camera_control依赖；不包含内核、设备树、RKAIQ固件、IQ文件或根文件系统。完整旧实验CSV/摄像头图像仍在原工程，包内只带精简验收证据。

## 平台与边界

已验证当前EVM3588、RK3588、Ubuntu20.04.5、内核5.10.110、8GB、IMX415。需要BSP先支持`/dev/video44`连续NV12 3840×2160@60、传感器`/dev/v4l-subdev2`、MPP驱动、DRM/KMS和匹配的RKAIQ 3A服务。apt依赖安装不能替代这些驱动。

采集、编码、解码均为4K60，当前HDMI屏幕输出1080p60，由VOP缩放；尚未验证4K60 HDMI输出。左上透明中文HUD约1Hz更新。默认HEVC、CBR20Mbps、GOP60、无B帧、8个采集缓冲、NV12 DMA-BUF、worker HUD、解码有界等待和deadline提前3500us。

MPP版本a9380ef333102ac318628f83b5f7a460d377749e，库SHA256：`82b9ce889922cd2b399b1d54b7c2498c2ed812bffbe760e0ef0b661e03e1dd5b`。替换库后需重新验证兼容性与性能。

## 传输与安装

PowerShell执行（目标地址按实际修改）：

```powershell
scp "C:\Users\dvsense_docker\Desktop\H.265-4K@60\RK3588_IMX415_H265_4K60_V3.tar.gz" root@192.168.2.221:/userdata/
ssh root@192.168.2.221
```

密钥登录可加`-i C:\Users\dvsense_docker\.ssh\evm_rk3588`。板端执行：

```bash
cd /userdata
tar -xzf RK3588_IMX415_H265_4K60_V3.tar.gz
cd RK3588_IMX415_H265_4K60_V3
# 仅首次缺系统依赖时执行，会通过apt安装用户态软件
sudo bash install_system_deps.sh
sudo bash install.sh --check
sudo bash install.sh
sudo bash check_board.sh
```

安装器逐文件检查SHA256和软链接；已有不同文件时在复制前停止，不覆盖旧版本。请先备份和迁移冲突文件。预览运行时拒绝安装。安装路径固定`/userdata/av_dev/`，不启动预览，不启用开机服务。当前板已经部署V3，无需为了打包再安装。

Windows展开目录没有实际创建Linux软链接，Linux部署请用tar.gz，避免直接scp整个Windows展开目录。tar.gz/zip均携带链接元数据，但Windows解压器不一定保留链接。

## 日常手动操作（root）

```bash
cd /userdata/av_dev/mpp_decode_bench/low_latency
bash start_v3.sh
python3 control.py status
python3 monitor.py
journalctl -u imx415-low-latency.service -n 50 --no-pager
python3 control.py stop
```

start创建临时systemd单元，退出SSH后仍运行；stop才停止。运行时独占相机/DRM并临时停止原桌面，停止时恢复格式、RAW线程调度及原桌面布局。不安装开机自启。

60秒前台测试（输出目录必须为新目录）：

```bash
python3 control.py stop
python3 control.py run --codec hevc --seconds 60 --output "results/manual-$(date +%Y%m%d-%H%M%S)"
```

前台结束或Ctrl+C后自动恢复及分析。`--codec h264`为对照，`--codec direct`跳过编解码仅诊断。详细参数用`python3 control.py --help`。

修改C++后编译：

```bash
python3 control.py stop
mkdir -p binary-backup
cp -n build/imx415_low_latency binary-backup/
bash build_board.sh
```

包内只有预编译可执行文件，不带旧CMake缓存，首次构建会重新生成。构建默认链接固定私有SDK和系统libdrm。

## 结果与排查

结果在`low_latency/results/`：frames.csv逐帧时间、rates.csv计数、summary.json汇总、analysis.json统计、native.log硬件流水日志、telemetry.jsonl资源采样、manifest.json版本散列、restoration.json恢复状态。active.json/state是运行时文件，不随包交付。

60分钟实测采集/编码/解码约59.996fps，显示59.960fps，DQBUF到真实翻页P95=37.1ms/P99=38.6ms，原生C++进程平均占用单核5.55%，驱动缺帧/解码错误0。该CPU数不包含全部Python及系统负载。驱动时间戳到翻页P95=85.8ms，上游32/49ms档位问题未解决；尚无光学端到端延迟认证。

- 相机被占用：用fuser /dev/video44定位，停止已知旧采集程序。RKAIQ使用RAW节点正常，勿随意停3A。
- 无60fps/节点不同：先配置sensor/BSP/media拓扑与IQ；camera_control也绑定video44/subdev2，不是改一个device参数就完成移植。
- DRM失败：检查HDMI、桌面DRM master及NV12缩放/ARGB平面。现有Qt/Wayland与本程序不能同时独占KMS。
- 中文缺字：安装fonts-noto-cjk并用fc-match ':lang=zh'检查。
- 异常未恢复：确认原生进程停止后，执行python3 control.py restore并检查日志；勿对活跃采集强制恢复。

处理流程见`处理流程与集成说明.md`，每个程序的用途见`程序作用清单.md`。本次交付独立可执行程序及源码；接入现有产品软件的API还需按其实际接口适配。

GitHub保存源码、脚本和文档；ARM64二进制及运行库在本地交付压缩包，不进入源码仓库。只克隆GitHub不会获得payload/manifest.json，不能直接执行部署安装；可使用完整交付包，或自行准备匹配SDK并编译。
