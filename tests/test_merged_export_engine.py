"""Regressions for the integrated engine, independent of either UI layout."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from exporter_core import (
    Capabilities, ExportSettings, MediaInfo, build_encode_command,
    build_mux_command, probe_media,
    _run_text,
)


class IntegratedEngineTests(unittest.TestCase):
    def test_av1_uses_defined_level_for_frame_rate_and_requested_bitrate(self):
        caps = Capabilities(raw_encoder_help={'av1_nvenc':'\n -level <int>\n'})
        for width, height, fps, bitrate, expected in (
            (3840,2160,60,'','5.1'), (7680,4320,30,'','6.0'),
            (7680,4320,60,'','6.1'), (7680,4320,120,'','6.2'),
            (3840,2160,30,'300M','6.1')):
            with self.subTest(size=(width,height), fps=fps, bitrate=bitrate):
                media = MediaInfo('camera.mov', width=width,height=height,fps=fps,bit_depth=10)
                settings = ExportSettings(codec='av1_nvenc',video_bitrate=bitrate)
                command = build_encode_command('ffmpeg',media.path,'out.mp4',settings,media,caps)
                self.assertEqual(command[command.index('-level:v')+1],expected)
                self.assertEqual(command[command.index('-tier')+1],'1')

    def test_explicit_av1_level_is_preserved(self):
        caps = Capabilities(raw_encoder_help={'av1_nvenc':'\n -level <int>\n'})
        media = MediaInfo('camera.mov',width=3840,height=2160,fps=30)
        settings = ExportSettings(codec='av1_nvenc',extra_args=['-level:v','6.3'])
        command = build_encode_command('ffmpeg',media.path,'out.mp4',settings,media,caps)
        self.assertEqual(command.count('-level:v'),1)
        self.assertEqual(command[command.index('-level:v')+1],'6.3')

    def test_successful_structured_probe_keeps_diagnostics_out_of_json(self):
        result=SimpleNamespace(returncode=0,stdout='{"streams": []}',stderr='decoder warning')
        with patch('exporter_core.subprocess.run',return_value=result):
            code, output = _run_text(['ffprobe'],structured=True)
        self.assertEqual(code,0)
        self.assertEqual(json.loads(output),{'streams':[]})

    def test_av1_ten_bit_does_not_request_hevc_profile(self):
        media = MediaInfo('camera.mov', width=3840, height=2160,
                          pixel_format='yuv420p10le', bit_depth=10)
        command = build_encode_command('ffmpeg', media.path, 'out.mp4',
            ExportSettings(codec='av1_nvenc'), media, Capabilities(scale_cuda=True))
        self.assertNotIn('main10', command)
        self.assertNotIn('-vf', command)  # Original-size CUDA frames remain on GPU.

    def test_cuda_download_matches_source_before_bit_depth_conversion(self):
        for source_depth, target_depth, source_format, target_format in (
            (10, '8-bit', 'p010le', 'nv12'), (8, '10-bit', 'nv12', 'p010le')):
            with self.subTest(source_depth=source_depth):
                media = MediaInfo('camera.mov', width=1920, height=1080, bit_depth=source_depth)
                command = build_encode_command('ffmpeg', media.path, 'out.mp4',
                    ExportSettings(bit_depth=target_depth), media, Capabilities(scale_cuda=False))
                graph = command[command.index('-vf') + 1]
                self.assertEqual(graph,
                    f'hwdownload,format={source_format},format={target_format},hwupload_cuda')

    def test_audio_cover_art_is_not_imported_as_a_camera_video(self):
        payload = {'streams': [
            {'codec_type':'video', 'codec_name':'mjpeg', 'disposition':{'attached_pic':1}},
            {'codec_type':'audio', 'codec_name':'flac', 'duration':'9.5'}],
            'format': {'duration':'9.5'}}
        with patch('exporter_core._run_text', return_value=(0, json.dumps(payload))):
            with self.assertRaisesRegex(RuntimeError, 'No video'):
                probe_media('ffprobe', 'master.flac')
            media = probe_media('ffprobe', 'master.flac', allow_audio_only=True)
        self.assertFalse(media.has_video)
        self.assertTrue(media.has_audio)
        self.assertEqual(media.duration, 9.5)

    def test_picture_duration_wins_over_trailing_audio(self):
        payload = {'streams': [
            {'codec_type':'video', 'codec_name':'h264', 'duration':'8.0'},
            {'codec_type':'audio', 'codec_name':'aac', 'duration':'8.2'}],
            'format': {'duration':'8.2'}}
        with patch('exporter_core._run_text', return_value=(0, json.dumps(payload))):
            self.assertEqual(probe_media('ffprobe', 'camera.mp4').duration, 8.0)

    def test_requested_audio_sample_rate_is_applied_to_encode_and_mux(self):
        settings = ExportSettings(audio_sample_rate=44100)
        media = MediaInfo('camera.mov')
        for command in (
            build_encode_command('ffmpeg', media.path, 'out.mp4', settings, media, Capabilities()),
            build_mux_command('ffmpeg', 'video.mp4', media.path, 'out.mp4', settings)):
            self.assertEqual(command[command.index('-ar') + 1], '44100')

    def test_audio_copy_and_silent_jobs_do_not_request_resampling(self):
        media = MediaInfo('camera.mov')
        for audio in ('copy', 'none'):
            settings = ExportSettings(audio=audio)
            self.assertNotIn('-ar', build_encode_command('ffmpeg', media.path, 'out.mp4',
                settings, media, Capabilities()))
            self.assertNotIn('-ar', build_mux_command('ffmpeg', 'video.mp4', media.path,
                'out.mp4', settings))


if __name__ == '__main__':
    unittest.main()
