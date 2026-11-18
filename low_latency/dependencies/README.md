# 板端已有模块快照

`camera_control/` 是2026-11-17从板端 `/userdata/av_dev/ffmpeg_pipeline/camera_control/` 保存的依赖快照。当前运行仍导入板端原路径；本目录用于审阅和复现实验，不自动替换原模块。

本程序复用其中的 RAW 调度、摄像头占用检查及基础配置。私有 MPP SDK 仍位于 `/userdata/av_dev/ffmpeg_pipeline/runtime/`，未重新打包。每轮 `manifest.json` 记录实时程序和 MPP 库摘要；依赖快照另用 `SHA256SUMS.json` 记录摘要。
