#!/usr/bin/env python3
"""Generate evidence-driven V3 results without changing a running configuration."""
import argparse
import json
from pathlib import Path
from evaluate_v3 import cadence


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--root',type=Path,default=Path(__file__).resolve().parent)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    results=args.root/'results'
    release_path=args.root/'v3-release.json'
    release=json.loads(release_path.read_text()) if release_path.exists() else {}
    lines=['# 第三轮低延迟与出帧稳定性优化','','发布状态：'+release.get('status','尚未发布'),'','## 采集端定位','',
           '使用独立ftrace实例和mono时钟，不修改全局追踪设置。此板的V4L2扩展事件没有输出，改用通用vb2事件；按应用线程、队列owner及完全相同的驱动时间戳匹配，避免把RAW和ISP共享时间戳误当成同一输出队列。追踪开销属于诊断条件，性能对照不开追踪。','',
           '| 诊断轮次 | 有效匹配帧 | 驱动戳→完成 P95 ms | 完成→内核取帧 P95 ms | 内核→应用返回 P95 ms |',
           '|---|---:|---:|---:|---:|']
    for path in sorted(results.glob('v3-trace-*/trace-analysis.json')):
        trace=json.loads(path.read_text())
        row=trace.get('timing',{}).get('video44_by_exact_driver_timestamp',{})
        if not row.get('matched_frames'):
            lines.append(f'| {path.parent.name}（无有效事件，不能用于结论） | 0 | — | — | — |')
            continue
        vals=[row[key]['p95'] for key in ('driver_to_buf_done_ms','buf_done_to_kernel_dq_ms','kernel_to_application_dq_ms')]
        lines.append(f"| {path.parent.name} | {row['matched_frames']} | {vals[0]:.3f} | {vals[1]:.3f} | {vals[2]:.3f} |")
    lines += ['','已观察到的约32ms主要发生在驱动交出成品缓冲之前，完成后的应用取帧等待仅为微秒级。板端proc显示RAW回读frame1、传感器读出约15ms和ISP工作约14ms，与该结果相符。不能据此宣称已消除全部上游延迟或确认49ms启动差异的根因。20ms启动调度检查实验未证明有额外收益，因此不自动启用。','',
              '## 实现','',
              '- HUD独立生产线程只写未扫描的ARGB缓冲。ready/pending/scanout所有权以原子状态交接，翻页完成才允许复用；显示线程只接收已准备的文字。退出先停止并等待HUD线程，再禁用显示和释放缓冲。',
              '- 提交提前量做参数扫描，以真实显示间隔、延迟和帧率选取，不通过移动刷新目标掩盖漏刷。',
              '- 解码可选poll或wait：poll维持300us重试；wait使用MPP有界20ms等待，帧就绪时返回，超时单独统计。20ms是最长等待时间，不能当作每帧固定增加20ms。',
              '- 首次wait短测因私有MPP的超时返回差异退出，失败数据保留在v3-wait-smoke。核对a9380ef源码后，仅对SHA256匹配的该库兼容等待达到18ms以上的MPP_NOK；立即返回的错误及其他错误仍失败，10秒无显示进展仍退出。没有替换MPP库。',
              '- 编解码仍保持4K60、20Mbps、GOP60和无B帧；HDMI按当前屏幕能力显示1080p60，透明文字保留。','',
              '连续NV12路径严格校验单平面、stride、UV偏移和有效载荷大小，再把相机DMA-BUF导入MPP。原始相机帧保留到该编码帧最后一个输出包释放后才QBUF，不会提前让相机覆盖输入。copy路径保留供对照。这里省去的是采集到编码输入的全帧CPU拷贝，不宣称所有码流和HUD操作零拷贝。','',
              '## 性能对照','',
              '| 轮次 | 编码 | 采集/输入 | HUD | 解码输出 | 提前量 ms | 显示fps | 内部P95 ms | 内部P99 ms | 驱动戳P95 ms | >25ms间隔/分钟 | CPU单核% |',
              '|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|']
    runs=[]
    for directory in ('v3-initial-comparison-20261117','v3-codec-comparison-20261117','v3-dma-comparison-20261117','v3-confirmation-20261117','v3-stress-20261117','v3-dma-stress-20261117'):
        path=results/directory/'experiment.json'
        if path.exists():
            state=json.loads(path.read_text())
            for row in state['results']:
                runs.append(row)
            if not state.get('completed'):
                reason='发现连续NV12输入能力后主动中止，保留数据，不纳入通过判定' if directory=='v3-codec-comparison-20261117' and state.get('error')=='Experiment stopped' else '未全部完成，错误='+str(state.get('error'))
                lines += [f'| {directory}：{reason} | | | | | | | | | | | |']
    endurance=results/'v3-final-60min-20261117'
    if (endurance/'analysis.json').exists():
        row=json.loads((endurance/'analysis.json').read_text());row.update(name='final-60min',cadence=cadence(endurance),output=str(endurance));runs.append(row)
    for row in runs:
        s=row['summary'];lat=s['latency']['dq_to_flip_ms'];driver=s['latency'].get('driver_timestamp_to_flip_ms',{}).get('p95',0)
        minutes=max(s['elapsed_s']-5,1)/60
        gaps=row['cadence']['interval_over25ms']/minutes
        lines.append(f"| {row['name']} | {s['codec']} | {s.get('capture_layout','nv12m')}/{s.get('encoder_input','copy')} | {s.get('overlay_preparation','inline')} | {s.get('decoder_output','poll')} | {s['display_margin_us']/1000:.1f} | {row['rates']['displayed_fps']:.3f} | {lat['p95']:.1f} | {lat['p99']:.1f} | {driver:.1f} | {gaps:.2f} | {row['mean_cpu_one_core_percent']:.2f} |")
    lines += ['','短测用于筛选，不单凭某一轮的P95或帧率归因；不同启动轮次的取帧前32/49ms偏移和刷新相位要单独考虑。各轮P95均值不是所有帧合并后的P95。','',
              '## 配置选择','','```json',json.dumps(release,indent=2,ensure_ascii=False),'```','','## 详细稳定性与故障注入','']
    upstream=endurance/'upstream-delay-audit.json'
    if upstream.exists():
        info=json.loads(upstream.read_text())
        lines += ['## 上游等待仍未消除','',
            f"最终长测的驱动戳到取帧P50/P95为{info['driver_to_dq_p50_ms']:.3f}/{info['driver_to_dq_p95_ms']:.3f}ms，属于约49ms档。与V2约49ms档的20分钟测试相比，驱动戳到翻页P95从90.0降至85.8ms。V2另一次60分钟测试处于约32ms档，驱动戳到翻页P95为73.6ms，低于本次的85.8ms。因此不能跨这两次不同上游档位的长测宣称端到端更快；本轮确认的是应用内部耗时和CPU拷贝减少，32/49ms启动差异的根因尚未解决。",'']
    for row in runs:
        if row['name']=='final-60min' or 'stress' in row['output']:
            s=row['summary']
            details={'rates':row['rates'],'cadence':row['cadence'],'driver_gaps':s['driver_gaps'],'errors':s['errors'],
                     'overlay_errors':s.get('overlay_errors'),'overlay_updates':s.get('overlay_updates'),
                     'capture_layout':s.get('capture_layout'),'encoder_input':s.get('encoder_input'),
                     'camera_dma_imports':s.get('camera_dma_imports'),'held_camera_peak':s.get('held_camera_peak'),
                     'input_copied_frames':s.get('input_copied_frames'),'input_shared_frames':s.get('input_shared_frames'),
                     'max_display_overlay_accept_ms':s.get('max_overlay_prepare_ms'),'max_hud_worker_ms':s.get('max_overlay_worker_ms'),
                     'decoder_output_calls':s.get('decoder_output_calls'),'decoder_empty_returns':s.get('decoder_empty_returns'),
                     'decoder_output_timeouts':s.get('decoder_output_timeouts'),'temperature_max_c':row['max_temperature_c'],
                     'decoder_legacy_wait_timeouts':s.get('decoder_legacy_wait_timeouts'),
                     'rss_max_mib':row['max_rss_mib'],'frame_audit':row['frame_audit'],'restored':row['restored'],
                     'internal_targets_passed':row['meets_internal_targets'],'stall_recovery':row.get('stall_recovery')}
            lines += ['### '+row['name'],'','```json',json.dumps(details,indent=2,ensure_ascii=False),'```','']
    lifecycle=results/'v3-lifecycle-20261117/lifecycle.json'
    if lifecycle.exists():
        lines += ['## 退出恢复','','```json',lifecycle.read_text().strip(),'```','']
    lines += ['## 限制','',
              '以上为程序内时序及驱动时间戳，不是光学端到端延迟。CPU比例不是整板瓦数，RSS也不包含全部DMA内存。单帧PSNR/SSIM不等于运动画质评价。尚未完成物理HDMI拔插、光学测量以及此前意外重启的根因定位；本轮未复现不能证明旧问题已根治。原V2源码、推荐配置和ARM可执行文件保留在versions/v2_20261117。','',
              '参考：[V4L2时间戳](https://www.kernel.org/doc/html/latest/userspace-api/media/v4l/buffer.html)、[MPP等待接口](https://github.com/rockchip-linux/mpp/blob/develop/inc/rk_mpi_cmd.h)、[该私有版本的等待实现](https://github.com/rockchip-linux/mpp/blob/a9380ef333102ac318628f83b5f7a460d377749e/osal/mpp_list.c)、[外部DMA导入实现](https://github.com/rockchip-linux/mpp/blob/a9380ef333102ac318628f83b5f7a460d377749e/osal/allocator/allocator_ext_dma.c)。']
    args.output.write_text('\n'.join(lines)+'\n')


if __name__=='__main__':
    main()
