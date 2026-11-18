# 第三轮低延迟与出帧稳定性优化

发布状态：published

## 采集端定位

使用独立ftrace实例和mono时钟，不修改全局追踪设置。此板的V4L2扩展事件没有输出，改用通用vb2事件；按应用线程、队列owner及完全相同的驱动时间戳匹配，避免把RAW和ISP共享时间戳误当成同一输出队列。追踪开销属于诊断条件，性能对照不开追踪。

| 诊断轮次 | 有效匹配帧 | 驱动戳→完成 P95 ms | 完成→内核取帧 P95 ms | 内核→应用返回 P95 ms |
|---|---:|---:|---:|---:|
| v3-trace-baseline-01（无有效事件，不能用于结论） | 0 | — | — | — |
| v3-trace-baseline-02 | 901 | 32.058 | 0.016 | 0.002 |
| v3-trace-baseline-03 | 902 | 32.064 | 0.015 | 0.002 |
| v3-trace-baseline-04 | 901 | 32.068 | 0.015 | 0.002 |
| v3-trace-fast-01 | 902 | 32.079 | 0.015 | 0.002 |
| v3-trace-fast-02 | 904 | 32.056 | 0.016 | 0.002 |
| v3-trace-fast-03 | 901 | 32.052 | 0.014 | 0.001 |

已观察到的约32ms主要发生在驱动交出成品缓冲之前，完成后的应用取帧等待仅为微秒级。板端proc显示RAW回读frame1、传感器读出约15ms和ISP工作约14ms，与该结果相符。不能据此宣称已消除全部上游延迟或确认49ms启动差异的根因。20ms启动调度检查实验未证明有额外收益，因此不自动启用。

## 实现

- HUD独立生产线程只写未扫描的ARGB缓冲。ready/pending/scanout所有权以原子状态交接，翻页完成才允许复用；显示线程只接收已准备的文字。退出先停止并等待HUD线程，再禁用显示和释放缓冲。
- 提交提前量做参数扫描，以真实显示间隔、延迟和帧率选取，不通过移动刷新目标掩盖漏刷。
- 解码可选poll或wait：poll维持300us重试；wait使用MPP有界20ms等待，帧就绪时返回，超时单独统计。20ms是最长等待时间，不能当作每帧固定增加20ms。
- 首次wait短测因私有MPP的超时返回差异退出，失败数据保留在v3-wait-smoke。核对a9380ef源码后，仅对SHA256匹配的该库兼容等待达到18ms以上的MPP_NOK；立即返回的错误及其他错误仍失败，10秒无显示进展仍退出。没有替换MPP库。
- 编解码仍保持4K60、20Mbps、GOP60和无B帧；HDMI按当前屏幕能力显示1080p60，透明文字保留。

连续NV12路径严格校验单平面、stride、UV偏移和有效载荷大小，再把相机DMA-BUF导入MPP。原始相机帧保留到该编码帧最后一个输出包释放后才QBUF，不会提前让相机覆盖输入。copy路径保留供对照。这里省去的是采集到编码输入的全帧CPU拷贝，不宣称所有码流和HUD操作零拷贝。

## 性能对照

| 轮次 | 编码 | 采集/输入 | HUD | 解码输出 | 提前量 ms | 显示fps | 内部P95 ms | 内部P99 ms | 驱动戳P95 ms | >25ms间隔/分钟 | CPU单核% |
|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| v3-codec-comparison-20261117：发现连续NV12输入能力后主动中止，保留数据，不纳入通过判定 | | | | | | | | | | | |
| hud-inline-1 | hevc | nv12m/copy | inline | poll | 3.5 | 59.885 | 41.7 | 42.2 | 73.7 | 7.29 | 20.92 |
| hud-worker-1 | hevc | nv12m/copy | worker | poll | 3.5 | 59.966 | 41.9 | 42.5 | 90.7 | 1.99 | 20.49 |
| hud-worker-2 | hevc | nv12m/copy | worker | poll | 3.5 | 59.666 | 35.6 | 46.0 | 84.3 | 19.90 | 20.61 |
| hud-inline-2 | hevc | nv12m/copy | inline | poll | 3.5 | 59.679 | 42.1 | 43.6 | 74.1 | 17.94 | 20.67 |
| worker-margin1000 | hevc | nv12m/copy | worker | poll | 1.0 | 59.703 | 33.1 | 41.2 | 65.2 | 18.58 | 20.81 |
| worker-margin5000 | hevc | nv12m/copy | worker | poll | 5.0 | 59.990 | 39.4 | 39.8 | 71.4 | 0.66 | 20.65 |
| worker-margin2000 | hevc | nv12m/copy | worker | poll | 2.0 | 59.895 | 37.6 | 38.0 | 69.6 | 6.64 | 20.67 |
| worker-margin3500 | hevc | nv12m/copy | worker | poll | 3.5 | 59.830 | 37.4 | 37.8 | 69.4 | 9.96 | 20.67 |
| nv12m-copy-poll-1 | hevc | nv12m/copy | worker | poll | 3.5 | 59.928 | 39.1 | 39.9 | 71.1 | 4.32 | 20.55 |
| nv12-copy-wait | hevc | nv12/copy | worker | wait | 3.5 | 59.692 | 41.4 | 44.2 | 73.5 | 18.62 | 19.72 |
| hevc-dma-wait-1 | hevc | nv12/dmabuf | worker | wait | 3.5 | 59.985 | 37.7 | 38.4 | 69.6 | 1.00 | 5.71 |
| nv12m-copy-poll-2 | hevc | nv12m/copy | worker | poll | 3.5 | 59.831 | 41.9 | 42.7 | 73.9 | 10.31 | 20.70 |
| h264-dma-wait-1 | h264 | nv12/dmabuf | worker | wait | 3.5 | 59.574 | 40.1 | 41.1 | 72.1 | 24.94 | 5.94 |
| h264-dma-wait-2 | h264 | nv12/dmabuf | worker | wait | 3.5 | 59.745 | 40.2 | 41.0 | 72.2 | 15.30 | 5.91 |
| hevc-dma-wait-2 | hevc | nv12/dmabuf | worker | wait | 3.5 | 59.887 | 38.4 | 39.2 | 70.4 | 7.32 | 5.70 |
| hevc-dma-margin5000 | hevc | nv12/dmabuf | worker | wait | 5.0 | 59.951 | 39.7 | 40.5 | 71.7 | 2.99 | 5.50 |
| hud-delay300ms | hevc | nv12m/copy | worker | wait | 3.5 | 59.361 | 31.0 | 45.4 | 63.0 | 38.42 | 19.93 |
| processing-stall250ms | hevc | nv12m/copy | worker | wait | 3.5 | 59.690 | 30.0 | 30.3 | 62.0 | 5.97 | 20.07 |
| dma-hud-delay300ms | hevc | nv12/dmabuf | worker | wait | 3.5 | 60.012 | 32.0 | 32.3 | 64.0 | 0.00 | 5.75 |
| dma-processing-stall250ms | hevc | nv12/dmabuf | worker | wait | 3.5 | 59.745 | 34.8 | 35.1 | 83.4 | 1.99 | 5.93 |
| nv12-direct-diagnostic | direct | nv12/dmabuf | worker | wait | 3.5 | 59.955 | 15.7 | 16.2 | 47.6 | 1.98 | 1.49 |
| final-60min | hevc | nv12/dmabuf | worker | wait | 3.5 | 59.960 | 37.1 | 38.6 | 85.8 | 2.57 | 5.55 |

短测用于筛选，不单凭某一轮的P95或帧率归因；不同启动轮次的取帧前32/49ms偏移和刷新相位要单独考虑。各轮P95均值不是所有帧合并后的P95。

## 配置选择

```json
{
  "status": "published",
  "published_at": "2026-11-18T09:35:22.682656+08:00",
  "profile": {
    "encoder_input": "dmabuf",
    "validation_status": "passed",
    "display_margin_us": 3500,
    "cpu_affinity": "big",
    "decoder_output": "wait",
    "capture_layout": "nv12",
    "capture_startup_poll_ms": 0,
    "codec": "hevc",
    "display_scheduling": "deadline",
    "bitrate": 20000000,
    "buffers": 8,
    "capture_scheduling": "raw-rr",
    "overlay_preparation": "worker",
    "endurance_status": "passed",
    "continuous_test_seconds": 3600,
    "endurance_seconds": 3600,
    "validation_output": "/userdata/av_dev/mpp_decode_bench/low_latency/results/v3-dma-comparison-20261117",
    "endurance_output": "/userdata/av_dev/mpp_decode_bench/low_latency/results/v3-final-60min-20261117"
  },
  "baseline_mean_cpu_one_core_percent": 20.622893009587706,
  "dma_mean_cpu_one_core_percent": 5.707139279777742,
  "baseline_round_mean_p95_ms": 40.5,
  "dma_round_mean_p95_ms": 38.05,
  "h264_round_mean_p95_ms": 40.150000000000006,
  "final_rates": {
    "captured_fps": 59.99649660605308,
    "encoded_fps": 59.9962187441557,
    "decoded_fps": 59.99649660605308,
    "displayed_fps": 59.96009669749632,
    "actual_mbps": 20.050050105446644
  },
  "final_internal_latency": {
    "n": 215859,
    "p50": 29.7,
    "p95": 37.1,
    "p99": 38.6,
    "max": 50.993,
    "quantile_resolution_ms": 0.1
  },
  "final_driver_timestamp_latency": {
    "n": 215859,
    "p50": 78.3,
    "p95": 85.8,
    "p99": 87.2,
    "max": 99.613,
    "quantile_resolution_ms": 0.1
  },
  "final_cpu_one_core_percent": 5.547998996512083,
  "final_temperature_c": 43.461,
  "final_rss_mib": 106.55859375,
  "extra_acceptance": {
    "passed": true,
    "encoder_input": "dmabuf",
    "cadence": {
      "displayed_frames": 215859,
      "interval_p99_ms": 16.676,
      "interval_max_ms": 33.383,
      "interval_over25ms": 154,
      "interval_over41_7ms": 0,
      "misses": 154,
      "misses_with_decode_after_target": 120,
      "misses_with_commit_after_target": 140
    },
    "camera_imports": 8,
    "camera_buffers": 8,
    "held_camera_peak": 2,
    "input_shared_frames": 216275,
    "input_copied_frames": 0,
    "matching_source_quality": {
      "stream_probe": {
        "programs": [],
        "stream_groups": [],
        "streams": [
          {
            "codec_name": "hevc",
            "width": 3840,
            "height": 2160,
            "has_b_frames": 0,
            "color_range": "pc",
            "color_space": "bt709",
            "r_frame_rate": "60/1"
          }
        ]
      },
      "psnr": 36.502194,
      "ssim": 0.873934
    }
  },
  "final_state": {
    "boot_id": "c7e16e83-4bc8-4d23-906a-568e75be624f",
    "same_build_as_comparison": true,
    "camera_owners": [],
    "raw_workers": [
      {
        "pid": 664,
        "tid": 71618,
        "start": "7777516",
        "comm": "xc:isp_tx_poll",
        "exe": "/usr/bin/rkaiq_3A_server",
        "policy": 0,
        "priority": 0
      },
      {
        "pid": 664,
        "tid": 71619,
        "start": "7777516",
        "comm": "xc:isp_rx_poll",
        "exe": "/usr/bin/rkaiq_3A_server",
        "policy": 0,
        "priority": 0
      },
      {
        "pid": 664,
        "tid": 71620,
        "start": "7777516",
        "comm": "xc:RawProcThrea",
        "exe": "/usr/bin/rkaiq_3A_server",
        "policy": 0,
        "priority": 0
      }
    ],
    "raw_scheduler_journal_exists": false,
    "restored": true,
    "capture_scheduler_restored": true,
    "desktop_active": true,
    "desktop_was_active": true,
    "preview_active": false,
    "benchmark_active": false
  },
  "notes": [
    "Round P95 means are not merged-frame quantiles.",
    "No optical end-to-end claim.",
    "The earlier spontaneous reboot root cause remains unconfirmed."
  ]
}
```

## 详细稳定性与故障注入

## 上游等待仍未消除

最终长测的驱动戳到取帧P50/P95为48.645/48.698ms，属于约49ms档。与V2约49ms档的20分钟测试相比，驱动戳到翻页P95从90.0降至85.8ms。V2另一次60分钟测试处于约32ms档，驱动戳到翻页P95为73.6ms，低于本次的85.8ms。因此不能跨这两次不同上游档位的长测宣称端到端更快；本轮确认的是应用内部耗时和CPU拷贝减少，32/49ms启动差异的根因尚未解决。

### hud-delay300ms

```json
{
  "rates": {
    "captured_fps": 59.996966088315894,
    "encoded_fps": 59.996966088315894,
    "decoded_fps": 59.996966088315894,
    "displayed_fps": 59.36111633416266,
    "actual_mbps": 20.080586144470512
  },
  "cadence": {
    "displayed_frames": 2681,
    "interval_p99_ms": 33.328,
    "interval_max_ms": 33.335,
    "interval_over25ms": 29,
    "interval_over41_7ms": 0,
    "misses": 29,
    "misses_with_decode_after_target": 28,
    "misses_with_commit_after_target": 28
  },
  "driver_gaps": 0,
  "errors": 0,
  "overlay_errors": 0,
  "overlay_updates": 46,
  "capture_layout": null,
  "encoder_input": null,
  "camera_dma_imports": null,
  "held_camera_peak": null,
  "input_copied_frames": null,
  "input_shared_frames": null,
  "max_display_overlay_accept_ms": 0.001,
  "max_hud_worker_ms": 300.633,
  "decoder_output_calls": 3041,
  "decoder_empty_returns": 0,
  "decoder_output_timeouts": 40,
  "temperature_max_c": 47.153,
  "decoder_legacy_wait_timeouts": 40,
  "rss_max_mib": 104.7265625,
  "frame_audit": {
    "unique_frame_ids": 3005,
    "duplicate_ids": 0,
    "nonmonotonic_display_timelines": 0,
    "outcomes": {
      "invalid_driver_timestamp": 4,
      "displayed": 2966,
      "display_replaced": 33,
      "stopped_display": 1,
      "stopped_decoder": 1
    },
    "all_capture_ids_accounted": true
  },
  "restored": true,
  "internal_targets_passed": true,
  "stall_recovery": null
}
```

### processing-stall250ms

```json
{
  "rates": {
    "captured_fps": 59.99458473942996,
    "encoded_fps": 59.75751892335544,
    "decoded_fps": 59.75751892335544,
    "displayed_fps": 59.68978583304843,
    "actual_mbps": 20.03440475228908
  },
  "cadence": {
    "displayed_frames": 3592,
    "interval_p99_ms": 16.669,
    "interval_max_ms": 249.989,
    "interval_over25ms": 6,
    "interval_over41_7ms": 1,
    "misses": 6,
    "misses_with_decode_after_target": 5,
    "misses_with_commit_after_target": 5
  },
  "driver_gaps": 0,
  "errors": 0,
  "overlay_errors": 0,
  "overlay_updates": 58,
  "capture_layout": null,
  "encoder_input": null,
  "camera_dma_imports": null,
  "held_camera_peak": null,
  "input_copied_frames": null,
  "input_shared_frames": null,
  "max_display_overlay_accept_ms": 0.001,
  "max_hud_worker_ms": 0.871,
  "decoder_output_calls": 3910,
  "decoder_empty_returns": 0,
  "decoder_output_timeouts": 24,
  "temperature_max_c": 47.153,
  "decoder_legacy_wait_timeouts": 24,
  "rss_max_mib": 105.1171875,
  "frame_audit": {
    "unique_frame_ids": 3904,
    "duplicate_ids": 0,
    "nonmonotonic_display_timelines": 0,
    "outcomes": {
      "invalid_driver_timestamp": 4,
      "displayed": 3877,
      "display_replaced": 8,
      "raw_replaced": 14,
      "stopped_decoder": 1
    },
    "all_capture_ids_accounted": true
  },
  "restored": true,
  "internal_targets_passed": true,
  "stall_recovery": {
    "injected_stall_ms": 250,
    "observed_submission_gap_ms": 252.606,
    "first_fresh_frame_id": 908,
    "first_fresh_frame_raw_wait_ms": 3.142,
    "first_fresh_frame_dq_to_flip_ms": 29.257,
    "first_flip_after_submission_resumes_ms": 23.587,
    "stall_observed": true
  }
}
```

### dma-hud-delay300ms

```json
{
  "rates": {
    "captured_fps": 59.995328426996,
    "encoded_fps": 59.97839016374727,
    "decoded_fps": 59.995328426996,
    "displayed_fps": 60.01226669024473,
    "actual_mbps": 20.031896375770625
  },
  "cadence": {
    "displayed_frames": 3618,
    "interval_p99_ms": 16.671,
    "interval_max_ms": 16.716,
    "interval_over25ms": 0,
    "interval_over41_7ms": 0,
    "misses": 0,
    "misses_with_decode_after_target": 0,
    "misses_with_commit_after_target": 0
  },
  "driver_gaps": 0,
  "errors": 0,
  "overlay_errors": 0,
  "overlay_updates": 59,
  "capture_layout": "nv12",
  "encoder_input": "dmabuf",
  "camera_dma_imports": 8,
  "held_camera_peak": 2,
  "input_copied_frames": 0,
  "input_shared_frames": 3899,
  "max_display_overlay_accept_ms": 0.001,
  "max_hud_worker_ms": 300.473,
  "decoder_output_calls": 3905,
  "decoder_empty_returns": 0,
  "decoder_output_timeouts": 6,
  "temperature_max_c": 36.076,
  "decoder_legacy_wait_timeouts": 6,
  "rss_max_mib": 105.34765625,
  "frame_audit": {
    "unique_frame_ids": 3903,
    "duplicate_ids": 0,
    "nonmonotonic_display_timelines": 0,
    "outcomes": {
      "invalid_driver_timestamp": 4,
      "displayed": 3895,
      "display_replaced": 3,
      "stopped_decoder": 1
    },
    "all_capture_ids_accounted": true
  },
  "restored": true,
  "internal_targets_passed": true,
  "stall_recovery": null
}
```

### dma-processing-stall250ms

```json
{
  "rates": {
    "captured_fps": 59.99866895416884,
    "encoded_fps": 59.76158699950941,
    "decoded_fps": 59.76158699950941,
    "displayed_fps": 59.74465257417659,
    "actual_mbps": 19.95230561354185
  },
  "cadence": {
    "displayed_frames": 3601,
    "interval_p99_ms": 16.677,
    "interval_max_ms": 250.033,
    "interval_over25ms": 2,
    "interval_over41_7ms": 1,
    "misses": 2,
    "misses_with_decode_after_target": 1,
    "misses_with_commit_after_target": 1
  },
  "driver_gaps": 0,
  "errors": 0,
  "overlay_errors": 0,
  "overlay_updates": 59,
  "capture_layout": "nv12",
  "encoder_input": "dmabuf",
  "camera_dma_imports": 8,
  "held_camera_peak": 2,
  "input_copied_frames": 0,
  "input_shared_frames": 3883,
  "max_display_overlay_accept_ms": 0.001,
  "max_hud_worker_ms": 0.538,
  "decoder_output_calls": 3906,
  "decoder_empty_returns": 0,
  "decoder_output_timeouts": 23,
  "temperature_max_c": 37.0,
  "decoder_legacy_wait_timeouts": 23,
  "rss_max_mib": 105.4765625,
  "frame_audit": {
    "unique_frame_ids": 3902,
    "duplicate_ids": 0,
    "nonmonotonic_display_timelines": 0,
    "outcomes": {
      "invalid_driver_timestamp": 5,
      "displayed": 3879,
      "display_replaced": 3,
      "raw_replaced": 14,
      "stopped_decoder": 1
    },
    "all_capture_ids_accounted": true
  },
  "restored": true,
  "internal_targets_passed": true,
  "stall_recovery": {
    "injected_stall_ms": 250,
    "observed_submission_gap_ms": 250.071,
    "first_fresh_frame_id": 901,
    "first_fresh_frame_raw_wait_ms": 0.072,
    "first_fresh_frame_dq_to_flip_ms": 34.229,
    "first_flip_after_submission_resumes_ms": 34.155,
    "stall_observed": true
  }
}
```

### nv12-direct-diagnostic

```json
{
  "rates": {
    "captured_fps": 59.98907723292197,
    "encoded_fps": 0.0,
    "decoded_fps": 0.0,
    "displayed_fps": 59.95462055444241,
    "actual_mbps": 0.0
  },
  "cadence": {
    "displayed_frames": 1812,
    "interval_p99_ms": 16.674,
    "interval_max_ms": 33.34,
    "interval_over25ms": 1,
    "interval_over41_7ms": 0,
    "misses": 1,
    "misses_with_decode_after_target": 0,
    "misses_with_commit_after_target": 1
  },
  "driver_gaps": 0,
  "errors": 0,
  "overlay_errors": 0,
  "overlay_updates": 32,
  "capture_layout": "nv12",
  "encoder_input": "dmabuf",
  "camera_dma_imports": 0,
  "held_camera_peak": 0,
  "input_copied_frames": 0,
  "input_shared_frames": 0,
  "max_display_overlay_accept_ms": 0.001,
  "max_hud_worker_ms": 0.451,
  "decoder_output_calls": 0,
  "decoder_empty_returns": 0,
  "decoder_output_timeouts": 0,
  "temperature_max_c": 36.076,
  "decoder_legacy_wait_timeouts": 0,
  "rss_max_mib": 100.640625,
  "frame_audit": {
    "unique_frame_ids": 2102,
    "duplicate_ids": 0,
    "nonmonotonic_display_timelines": 0,
    "outcomes": {
      "invalid_driver_timestamp": 4,
      "displayed": 2096,
      "display_replaced": 1,
      "stopped_display": 1
    },
    "all_capture_ids_accounted": true
  },
  "restored": true,
  "internal_targets_passed": true,
  "stall_recovery": null
}
```

### final-60min

```json
{
  "rates": {
    "captured_fps": 59.99649660605308,
    "encoded_fps": 59.9962187441557,
    "decoded_fps": 59.99649660605308,
    "displayed_fps": 59.96009669749632,
    "actual_mbps": 20.050050105446644
  },
  "cadence": {
    "displayed_frames": 215859,
    "interval_p99_ms": 16.676,
    "interval_max_ms": 33.383,
    "interval_over25ms": 154,
    "interval_over41_7ms": 0,
    "misses": 154,
    "misses_with_decode_after_target": 120,
    "misses_with_commit_after_target": 140
  },
  "driver_gaps": 0,
  "errors": 0,
  "overlay_errors": 0,
  "overlay_updates": 3161,
  "capture_layout": "nv12",
  "encoder_input": "dmabuf",
  "camera_dma_imports": 8,
  "held_camera_peak": 2,
  "input_copied_frames": 0,
  "input_shared_frames": 216275,
  "max_display_overlay_accept_ms": 0.002,
  "max_hud_worker_ms": 0.681,
  "decoder_output_calls": 216304,
  "decoder_empty_returns": 0,
  "decoder_output_timeouts": 29,
  "temperature_max_c": 43.461,
  "decoder_legacy_wait_timeouts": 29,
  "rss_max_mib": 106.55859375,
  "frame_audit": {
    "unique_frame_ids": 216280,
    "duplicate_ids": 0,
    "nonmonotonic_display_timelines": 0,
    "outcomes": {
      "invalid_driver_timestamp": 5,
      "displayed": 216139,
      "display_replaced": 134,
      "stopped_display": 1,
      "stopped_decoder": 1
    },
    "all_capture_ids_accounted": true
  },
  "restored": true,
  "internal_targets_passed": true,
  "stall_recovery": null
}
```

## 退出恢复

```json
[
  {
    "test": "display-error-injection",
    "passed": true,
    "physical_hotplug_tested": false,
    "output": "results/v3-lifecycle-20261117/display-error",
    "requested_profile": {
      "encoder_input": "dmabuf",
      "validation_status": "candidate_not_default",
      "display_margin_us": 3500,
      "cpu_affinity": "big",
      "decoder_output": "wait",
      "capture_layout": "nv12",
      "capture_startup_poll_ms": 0,
      "codec": "hevc",
      "display_scheduling": "deadline",
      "bitrate": 20000000,
      "buffers": 8,
      "capture_scheduling": "raw-rr",
      "overlay_preparation": "worker"
    }
  },
  {
    "test": "manual-stop",
    "passed": true,
    "output": "results/v3-lifecycle-20261117/manual-stop",
    "camera_format_restored": true,
    "desktop_restored": true,
    "display_layout_restored": true,
    "requested_profile": {
      "encoder_input": "dmabuf",
      "validation_status": "candidate_not_default",
      "display_margin_us": 3500,
      "cpu_affinity": "big",
      "decoder_output": "wait",
      "capture_layout": "nv12",
      "capture_startup_poll_ms": 0,
      "codec": "hevc",
      "display_scheduling": "deadline",
      "bitrate": 20000000,
      "buffers": 8,
      "capture_scheduling": "raw-rr",
      "overlay_preparation": "worker"
    }
  },
  {
    "test": "restart-stop",
    "passed": true,
    "output": "results/v3-lifecycle-20261117/restart-stop",
    "camera_format_restored": true,
    "desktop_restored": true,
    "display_layout_restored": true,
    "requested_profile": {
      "encoder_input": "dmabuf",
      "validation_status": "candidate_not_default",
      "display_margin_us": 3500,
      "cpu_affinity": "big",
      "decoder_output": "wait",
      "capture_layout": "nv12",
      "capture_startup_poll_ms": 0,
      "codec": "hevc",
      "display_scheduling": "deadline",
      "bitrate": 20000000,
      "buffers": 8,
      "capture_scheduling": "raw-rr",
      "overlay_preparation": "worker"
    }
  },
  {
    "test": "supervisor-sigkill",
    "passed": true,
    "output": "results/v3-lifecycle-20261117/supervisor-sigkill",
    "camera_format_restored": true,
    "desktop_restored": true,
    "display_layout_restored": true,
    "requested_profile": {
      "encoder_input": "dmabuf",
      "validation_status": "candidate_not_default",
      "display_margin_us": 3500,
      "cpu_affinity": "big",
      "decoder_output": "wait",
      "capture_layout": "nv12",
      "capture_startup_poll_ms": 0,
      "codec": "hevc",
      "display_scheduling": "deadline",
      "bitrate": 20000000,
      "buffers": 8,
      "capture_scheduling": "raw-rr",
      "overlay_preparation": "worker"
    }
  }
]
```

## 限制

以上为程序内时序及驱动时间戳，不是光学端到端延迟。CPU比例不是整板瓦数，RSS也不包含全部DMA内存。单帧PSNR/SSIM不等于运动画质评价。尚未完成物理HDMI拔插、光学测量以及此前意外重启的根因定位；本轮未复现不能证明旧问题已根治。原V2源码、推荐配置和ARM可执行文件保留在versions/v2_20261117。

参考：[V4L2时间戳](https://www.kernel.org/doc/html/latest/userspace-api/media/v4l/buffer.html)、[MPP等待接口](https://github.com/rockchip-linux/mpp/blob/develop/inc/rk_mpi_cmd.h)、[该私有版本的等待实现](https://github.com/rockchip-linux/mpp/blob/a9380ef333102ac318628f83b5f7a460d377749e/osal/mpp_list.c)、[外部DMA导入实现](https://github.com/rockchip-linux/mpp/blob/a9380ef333102ac318628f83b5f7a460d377749e/osal/allocator/allocator_ext_dma.c)。
