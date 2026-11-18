"""Offline regression checks; no camera, display or service operations."""
import csv
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import analyze
import control
import trace_capture
import evaluate_v3
import audit_v3


class TimingEvidenceContracts(unittest.TestCase):
    def test_dma_acceptance_rejects_copying_and_excess_camera_leases(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'timing.json').write_text(json.dumps({'start_monotonic_us':0,'warmup_seconds':0}))
            (root/'frames.csv').write_text('outcome,dq_us,dec_out_us,display_in_us,flip_event_us,display_target_us\n'
                'displayed,1000,2000,3000,16666,16666\n'
                'displayed,20000,22000,23000,33333,33333\n')
            result={'completed':True,'meets_internal_targets':True,'restored':True,
                'summary':{'codec':'hevc','encoder_input':'dmabuf','capture_layout':'nv12','encoded':100,
                    'input_shared_frames':100,'input_copied_frames':0,'camera_dma_imports':8,
                    'capture_buffers_actual':8,'held_camera_peak':2},
                'quality_matched_single_frame':{'psnr':35,'ssim':.9,'stream_probe':{'streams':[
                    {'width':3840,'height':2160,'has_b_frames':0}]}}}
            def save(): (root/'analysis.json').write_text(json.dumps(result))
            save();self.assertTrue(audit_v3.audit(root)['passed'])
            result['summary']['input_copied_frames']=1;save()
            with self.assertRaises(AssertionError): audit_v3.audit(root)
            result['summary']['input_copied_frames']=0;result['summary']['held_camera_peak']=3;save()
            with self.assertRaises(AssertionError): audit_v3.audit(root)

    def test_raw_and_isp_same_timestamp_are_not_conflated(self):
        with tempfile.TemporaryDirectory() as directory:
            frames = Path(directory)/'frames.csv'
            frames.write_text('id,driver_us,dq_us\n301,2000000,2032018\n')
            trace = '\n'.join([
                ' <idle>-0 [000] .... 2.016000: vb2_buf_done: owner = aaaa, index = 0, type = 9, timestamp = 2000000000',
                ' xc:isp_tx_poll-20 [004] .... 2.016010: vb2_dqbuf: owner = aaaa, index = 0, type = 9, timestamp = 2000000000',
                ' <idle>-0 [000] .... 2.032000: vb2_buf_done: owner = bbbb, index = 1, type = 9, timestamp = 2000000000',
                ' imx415_low_late-30 [004] .... 2.032016: vb2_dqbuf: owner = bbbb, index = 1, type = 9, timestamp = 2000000000'])
            result = trace_capture.analyze(trace,frames)['video44_by_exact_driver_timestamp']
            self.assertEqual(result['owners'], ['bbbb'])
            self.assertEqual(result['matched_frames'],1)
            self.assertEqual(result['driver_to_buf_done_ms']['p50'],32)
            self.assertEqual(result['buf_done_to_kernel_dq_ms']['p50'],.016)
            self.assertEqual(result['kernel_to_application_dq_ms']['p50'],.002)

    def test_cadence_retains_real_refresh_gaps(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'timing.json').write_text(json.dumps({'start_monotonic_us':0,'warmup_seconds':0}))
            (root/'frames.csv').write_text('outcome,dq_us,dec_out_us,display_in_us,flip_event_us,display_target_us\n'
                'displayed,1000,2000,3000,16666,16666\n'
                'displayed,20000,34000,35000,50000,33333\n')
            result=evaluate_v3.cadence(root)
            self.assertEqual(result['interval_over25ms'],1)
            self.assertEqual(result['misses'],1)
            self.assertEqual(result['misses_with_decode_after_target'],1)


class AcceptanceContracts(unittest.TestCase):
    def fixture(self, root):
        (root / "summary.json").write_text(json.dumps({"codec": "hevc", "elapsed_s": 35.2,
            "interrupted": False, "errors": 0, "driver_gaps": 0,
            "latency": {"dq_to_flip_ms": {"p95": 45}}}))
        (root / "command.json").write_text(json.dumps(["native", "--warmup", "5", "--seconds", "30"]))
        (root / "rates.csv").write_text("elapsed_s,captured,encoded,decoded,displayed,bytes\n5,300,300,300,300,0\n35,2100,2100,2100,2100,75000000\n")
        (root / "frames.csv").write_text("outcome,flags,dq_us,flip_event_us\ndisplayed,8192,6000000,6045000\n")
        (root / "telemetry.jsonl").write_text("")
        (root / "restoration.json").write_text(json.dumps({"returncode": 0, "restored": True}))

    def test_nominal_60_does_not_hide_48fps(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            self.fixture(root)
            self.assertTrue(analyze.analyze(root)["meets_internal_targets"])
            (root / "rates.csv").write_text("elapsed_s,captured,encoded,decoded,displayed,bytes\n5,300,240,240,240,0\n35,2100,1680,1680,1680,60000000\n")
            result = analyze.analyze(root)
            self.assertEqual(result["rates"]["encoded_fps"], 48)
            self.assertFalse(result["meets_internal_targets"])

    def test_failed_hud_is_not_a_complete_preview_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            self.fixture(root)
            summary=json.loads((root/'summary.json').read_text())
            summary['overlay_errors']=1
            (root/'summary.json').write_text(json.dumps(summary))
            self.assertFalse(analyze.analyze(root)['meets_internal_targets'])

    def test_interruption_and_failed_restore_are_not_passes(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            self.fixture(root)
            (root / "restoration.json").write_text(json.dumps({"returncode": 0, "restored": False}))
            self.assertFalse(analyze.analyze(root)["meets_internal_targets"])
            (root / "restoration.json").write_text(json.dumps({"returncode": 0, "restored": True}))
            data = json.loads((root / "summary.json").read_text())
            data["interrupted"] = True
            (root / "summary.json").write_text(json.dumps(data))
            self.assertFalse(analyze.analyze(root)["completed"])

    def test_deadline_miss_is_verified_against_frame_timestamps(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            self.fixture(root)
            data = json.loads((root / "summary.json").read_text())
            data["missed_targets"] = 1
            (root / "summary.json").write_text(json.dumps(data))
            (root / "frames.csv").write_text("outcome,flags,dq_us,display_in_us,flip_event_us,display_target_us\n"
                "displayed,8192,6000000,6038000,6056667,6040000\n")
            audit = analyze.analyze(root)["deadline_audit"]
            self.assertEqual(audit["missed_targets"], 1)
            self.assertEqual(audit["commit_lead_p50_ms"], 2)
            self.assertTrue(audit["counter_matches_trace"])
            data["missed_targets"] = 0
            (root / "summary.json").write_text(json.dumps(data))
            self.assertFalse(analyze.analyze(root)["meets_internal_targets"])


class RestorationContracts(unittest.TestCase):
    def test_big_cpu_selection_uses_allowed_capacity_not_cpu_numbers(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            for cpu, capacity in [(0, 405), (1, 405), (2, 1024), (3, 1024)]:
                path = root / ("cpu" + str(cpu))
                path.mkdir()
                (path / "cpu_capacity").write_text(str(capacity))
            self.assertEqual(control.big_cpu_list(root, {0, 1, 2, 3}), [2, 3])
            with self.assertRaises(RuntimeError):
                control.big_cpu_list(root, {0, 1})

    def test_idle_desktop_is_not_started_and_restore_is_idempotent(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            state = root / "active.json"
            state.write_text(json.dumps({"output": str(root), "device": "/dev/video44",
                "camera_format": "width=1920,height=1080,pixelformat=NM12",
                "display_service": "gdm.service", "desktop_was_active": False, "restored": False}))
            calls = []
            def fake(args, **kwargs):
                calls.append(args)
                return SimpleNamespace(returncode=0, stdout="")
            with patch.object(control, "STATE", state), patch.object(control, "ROOT", root), patch.object(control, "command", fake):
                control.restore()
                control.restore()
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], "v4l2-ctl")
            self.assertTrue(json.loads(state.read_text())["restored"])


@unittest.skipUnless(importlib.util.find_spec("cairo"), "board Cairo is required")
class OverlayContracts(unittest.TestCase):
    def test_transparent_background_and_actual_counter_fps(self):
        import overlay
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            panel = overlay.Panel(root / "overlay.argb", root, "hevc", 100)
            with patch.object(overlay, "text_file", return_value=None):
                panel.update({"monotonic_s": 10}, {"elapsed_s": 5, "captured": 300, "encoded": 240, "decoded": 240, "displayed": 240})
                sample = panel.update({"monotonic_s": 11}, {"elapsed_s": 6, "captured": 360, "encoded": 288, "decoded": 288, "displayed": 288})
            self.assertEqual(sample["panel_fps"]["encoded"], 48)
            pixels = (root / "overlay.argb").read_bytes()[16:]
            self.assertEqual(pixels[:4], bytes(4))
            self.assertGreater(sum(v == 0 for v in pixels[3::4]), len(pixels[3::4]) * .7)
            self.assertTrue(any(v > 0 for v in pixels[3::4]))
            cr = overlay.cairo.Context(overlay.cairo.ImageSurface(overlay.cairo.FORMAT_ARGB32, overlay.WIDTH, overlay.HEIGHT))
            for text in sample["panel_lines"]:
                layout = overlay.PangoCairo.create_layout(cr)
                layout.set_font_description(overlay.Pango.FontDescription("Noto Sans CJK SC 16"))
                layout.set_text(text, -1)
                self.assertLessEqual(layout.get_pixel_size()[0], overlay.WIDTH - 36)


if __name__ == "__main__":
    unittest.main()
