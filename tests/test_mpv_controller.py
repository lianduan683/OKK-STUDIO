import io
import json
import unittest
from unittest.mock import patch

from mpv_controller import MPV_GPU_PLAYBACK_OPTIONS, MPV_SCREENSHOT_OPTIONS, MpvController


class _RunningProcess:
    def poll(self):
        return None


class _FakePipe(io.BytesIO):
    def __init__(self, responses):
        super().__init__()
        self.responses = iter(responses)
        self.writes = []

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def readline(self, size=-1):
        line=next(self.responses)
        value=json.loads(line)
        if 'error' in value and 'request_id' not in value:
            value['request_id']=json.loads(self.writes[-1])['request_id']
        return json.dumps(value).encode()+b'\n'


class MpvControllerTests(unittest.TestCase):
    def _controller(self):
        controller = MpvController("mpv.exe", 123)
        controller.process = _RunningProcess()
        return controller

    def test_reuses_one_named_pipe_for_multiple_commands(self):
        pipe = _FakePipe([
            b'{"error":"success","data":"0.41.0"}\n',
            b'{"error":"success","data":null}\n',
        ])
        controller = self._controller()

        with patch("builtins.open", return_value=pipe) as open_pipe:
            self.assertEqual(controller.command(["get_property", "mpv-version"]), "0.41.0")
            self.assertIsNone(controller.command(["set_property", "pause", True]))

        self.assertEqual(open_pipe.call_count, 1)
        self.assertEqual(len(pipe.writes), 2)

    def test_retries_temporary_access_denied_while_connecting(self):
        pipe = _FakePipe([b'{"error":"success","data":"ready"}\n'])
        controller = self._controller()

        with (
            patch("builtins.open", side_effect=[PermissionError(5, "Access denied"), pipe]) as open_pipe,
            patch("mpv_controller.time.sleep"),
        ):
            self.assertEqual(controller.command(["get_property", "mpv-version"]), "ready")

        self.assertEqual(open_pipe.call_count, 2)

    def test_screenshots_are_high_quality_srgb_jpeg(self):
        self.assertIn("--screenshot-format=jpg", MPV_SCREENSHOT_OPTIONS)
        self.assertIn("--screenshot-jpeg-quality=98", MPV_SCREENSHOT_OPTIONS)
        self.assertIn("--screenshot-tag-colorspace=no", MPV_SCREENSHOT_OPTIONS)
        self.assertIn("--screenshot-high-bit-depth=no", MPV_SCREENSHOT_OPTIONS)
        self.assertIn("--screenshot-sw=no", MPV_SCREENSHOT_OPTIONS)

    def test_preview_uses_vendor_neutral_d3d11_gpu_without_copy_back(self):
        self.assertIn("--hwdec=d3d11va,nvdec", MPV_GPU_PLAYBACK_OPTIONS)
        self.assertIn("--hwdec-software-fallback=no", MPV_GPU_PLAYBACK_OPTIONS)
        self.assertIn("--hwdec-codecs=all", MPV_GPU_PLAYBACK_OPTIONS)
        self.assertIn("--vo=gpu-next", MPV_GPU_PLAYBACK_OPTIONS)
        self.assertIn("--gpu-api=d3d11", MPV_GPU_PLAYBACK_OPTIONS)
        self.assertIn("--gpu-context=d3d11", MPV_GPU_PLAYBACK_OPTIONS)
        self.assertFalse(any(option.startswith('--d3d11-adapter=') for option in MPV_GPU_PLAYBACK_OPTIONS))
        self.assertFalse(any("-copy" in option for option in MPV_GPU_PLAYBACK_OPTIONS))

    def test_each_viewer_has_bounded_demux_cache_without_disk_spill(self):
        self.assertIn('--demuxer-max-bytes=32MiB', MPV_GPU_PLAYBACK_OPTIONS)
        self.assertIn('--demuxer-max-back-bytes=8MiB', MPV_GPU_PLAYBACK_OPTIONS)
        self.assertIn('--demuxer-readahead-secs=2', MPV_GPU_PLAYBACK_OPTIONS)
        self.assertIn('--cache-on-disk=no', MPV_GPU_PLAYBACK_OPTIONS)

    def test_gpu_status_rejects_cpu_fallback(self):
        controller = self._controller()
        with patch.object(controller, "command", return_value="no"):
            with self.assertRaisesRegex(RuntimeError, "回退到 CPU"):
                controller._wait_for_gpu_decoder(0.1)

    def test_gpu_status_reports_real_decoder(self):
        controller = self._controller()
        with patch.object(controller, "command", side_effect=["nvdec", "cuda", "cuda"]):
            status = controller._wait_for_gpu_decoder(0.1)
        self.assertEqual(status["hwdec"], "nvdec")
        self.assertEqual(status["interop"], "cuda")
        self.assertEqual(status["renderer"], "gpu-next/d3d11")

    def test_gpu_status_waits_for_asynchronous_decoder_after_initial_idle_no(self):
        controller = self._controller()
        with patch.object(controller, 'command', side_effect=['no', 'd3d11va', 'd3d11va', 'nv12']):
            status = controller._wait_for_gpu_decoder(.5)
        self.assertEqual(status['hwdec'], 'd3d11va')

    def test_aligned_preview_selects_master_audio_and_starts_playback(self):
        controller = self._controller()
        backend = {"hwdec": "nvdec", "renderer": "gpu-next/d3d11"}
        with (
            patch.object(controller, "load", return_value=backend) as load,
            patch.object(controller, "command") as command,
        ):
            result = controller.load_aligned_preview(
                "camera-2.mov", "concert.flac", -7505.125,
                start_seconds=3.0, end_seconds=12.5,
            )

        self.assertEqual(result, backend)
        load.assert_called_once_with("camera-2.mov", timeout=8.0)
        self.assertEqual(controller.preview_end_seconds, 12.5)
        self.assertEqual(command.call_args_list[0].args[0], ["audio-add", "concert.flac", "select", "已对齐主音频"])
        self.assertEqual(command.call_args_list[1].args[0], ["set_property", "audio-delay", -7505.125])
        self.assertEqual(command.call_args_list[2].args[0], ["seek", 3.0, "absolute+exact"])
        self.assertEqual(command.call_args_list[3].args[0], ["set_property", "pause", False])

    def test_aligned_preview_rejects_invalid_range_before_loading(self):
        controller = self._controller()
        with patch.object(controller, "load") as load:
            with self.assertRaisesRegex(ValueError, "结束时间必须晚于开始时间"):
                controller.load_aligned_preview(
                    "camera.mov", "concert.wav", 0.0,
                    start_seconds=10.0, end_seconds=10.0,
                )
        load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
