import tempfile
import unittest
from pathlib import Path

from editor_core import (AlignmentResult, JoinVideoSpan, build_gap_preserving_concat_encode_command,
                         build_join_video_chunk_command)
from effects_core import EffectSettings
from exporter_core import Capabilities, ExportSettings, MediaInfo


class TimelineEffectJoinTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.asset = Path(self.temp.name) / 'spark.mov'
        self.asset.touch()
        self.effect = EffectSettings(overlay='spark', overlay_path=str(self.asset),
                                     overlay_kind='screen', overlay_opacity=40, overlay_fps=25)
        self.media = [MediaInfo(p, 2, 160, 90, 30, 'h264', 'yuv420p', 8) for p in ('a.mov', 'b.mov')]
        self.settings = ExportSettings(decode_mode='cpu')
        self.caps = Capabilities()

    def test_chunk_overlay_is_local_to_span_and_keeps_source_offset(self):
        spans = [JoinVideoSpan(0, 0.5, 0.5, 1, 30), JoinVideoSpan(1, 0, 2, 1, 30)]
        cmd = build_join_video_chunk_command('ffmpeg', ['a.mov', 'b.mov'], spans, self.media,
                                             'out.mov', self.settings, self.caps,
                                             overlays={'a.mov': self.effect})
        graph = cmd[cmd.index('-filter_complex') + 1]
        self.assertIn('trim=start=0.500000', graph)
        self.assertIn('blend=all_mode=screen:all_opacity=0.4000', graph)
        self.assertEqual(graph.count('movie='), 1)
        self.assertIn('[effect_0_ov]', graph)
        self.assertIn('[vspan0][vspan1]concat=', graph)

    def test_gap_export_applies_overlay_and_keeps_audio(self):
        cmd = build_gap_preserving_concat_encode_command(
            'ffmpeg', ['a.mov', 'b.mov'], 'master.wav',
            [AlignmentResult(0, 1, 1), AlignmentResult(-3, 1, 1)], self.media,
            'out.mov', self.settings, self.caps, overlays={'b.mov': self.effect})
        graph = cmd[cmd.index('-filter_complex') + 1]
        self.assertIn('movie=', graph)
        self.assertIn('[aout]', graph)
        self.assertIn('color=c=black', graph)

    def test_absent_effect_does_not_change_command(self):
        spans = [JoinVideoSpan(0, 0, 0, 1, 30)]
        args = ('ffmpeg', ['a.mov', 'b.mov'], spans, self.media, 'out.mov', self.settings, self.caps)
        self.assertEqual(build_join_video_chunk_command(*args), build_join_video_chunk_command(*args, overlays={}))

    def test_missing_asset_fails(self):
        self.asset.unlink()
        with self.assertRaises(ValueError):
            build_join_video_chunk_command('ffmpeg', ['a.mov'], [JoinVideoSpan(0, 0, 0, 1, 30)],
                self.media[:1], 'out.mov', self.settings, self.caps, overlays={'a.mov': self.effect})

if __name__ == '__main__':
    unittest.main()

class TimelineEffectRealFfmpegTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import subprocess
        cls.subprocess = subprocess
        cls.ffmpeg = str(Path(__file__).resolve().parents[1] / 'bin' / 'ffmpeg.exe')
        if not Path(cls.ffmpeg).is_file():
            raise unittest.SkipTest('bundled FFmpeg unavailable')
        cls.temp = tempfile.TemporaryDirectory(prefix='joinfx_')
        cls.root = Path(cls.temp.name)
        cls.paths = [str(cls.root / 'a.mov'), str(cls.root / 'b.mov')]
        cls.assets = [str(cls.root / 'red.mov'), str(cls.root / 'blue.mov')]
        for path, color in zip(cls.paths + cls.assets, ['black', 'black', 'red', 'blue']):
            cls.run_ffmpeg([cls.ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i',
                f'color={color}:s=160x90:r=30:d=2', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                '-threads', '1', path])
        # The first asset changes colour at one second, so the output proves
        # that trimming a clip also advances the overlay's playback phase.
        cls.run_ffmpeg([cls.ffmpeg, '-v', 'error', '-y', '-filter_complex_threads', '1',
            '-f', 'lavfi', '-i', 'color=red:s=160x90:r=30:d=1',
            '-f', 'lavfi', '-i', 'color=green:s=160x90:r=30:d=1',
            '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0[v]', '-map', '[v]',
            '-c:v', 'libx264', '-threads', '1', cls.assets[0]])
        cls.media = [MediaInfo(p, 2, 160, 90, 30, 'h264', 'yuv420p', 8) for p in cls.paths]

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @classmethod
    def run_ffmpeg(cls, cmd):
        proc = cls.subprocess.run(cmd, stdout=cls.subprocess.PIPE, stderr=cls.subprocess.PIPE, timeout=45)
        if proc.returncode:
            raise AssertionError(proc.stderr.decode('utf-8', 'replace')[-5000:])
        return proc.stdout

    def test_two_screen_effects_in_trimmed_chunk(self):
        from unittest.mock import patch
        effects = {path: EffectSettings(overlay=color, overlay_path=asset, overlay_kind='screen',
                                        overlay_fps=30, overlay_opacity=100)
                   for path, asset, color in zip(self.paths, self.assets, ['red', 'blue'])}
        spans = [JoinVideoSpan(0, 0.5, 0.5, 1, 30), JoinVideoSpan(1, 0.75, 2.75, 1, 30)]
        output = str(self.root / 'chunk.mov')
        with patch('editor_core.build_video_args', return_value=['-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '12', '-threads', '1']):
            cmd = build_join_video_chunk_command(self.ffmpeg, self.paths, spans, self.media,
                                                  output, ExportSettings(decode_mode='cpu'),
                                                  Capabilities(), overlays=effects)
        cmd[1:1] = ['-filter_complex_threads', '1']
        self.run_ffmpeg(cmd)
        for timestamp, channel, minimum in [(0.2, 0, 200), (0.8, 1, 100), (1.4, 2, 200)]:
            raw = self.run_ffmpeg([self.ffmpeg, '-v', 'error', '-ss', str(timestamp), '-i', output,
                '-frames:v', '1', '-vf', 'scale=1:1', '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'])
            self.assertEqual(len(raw), 3)
            self.assertGreater(raw[channel], minimum, list(raw))
            self.assertLess(raw[(channel + 1) % 3], 25, list(raw))

    def test_gap_export_composites_screen_and_preserves_audio(self):
        from unittest.mock import patch
        import wave
        audio = str(self.root / 'master.wav')
        with wave.open(audio, 'wb') as stream:
            stream.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
            stream.writeframes((b'\x10\x27' * 48000) * 5)
        effects = {path: EffectSettings(overlay=color, overlay_path=asset, overlay_kind='screen',
                                        overlay_fps=30, overlay_opacity=100)
                   for path, asset, color in zip(self.paths, self.assets, ['red', 'blue'])}
        output = str(self.root / 'gap.mov')
        with patch('editor_core.build_video_args', return_value=['-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '12', '-threads', '1']):
            cmd = build_gap_preserving_concat_encode_command(
                self.ffmpeg, self.paths, audio,
                [AlignmentResult(0, 1, 1), AlignmentResult(-3, 1, 1)], self.media,
                output, ExportSettings(decode_mode='cpu'), Capabilities(),
                audio_codec='pcm_s16le', audio_sample_rate=48000, overlays=effects)
        cmd[1:1] = ['-filter_complex_threads', '1']
        self.run_ffmpeg(cmd)
        for timestamp, channel in [(0.4, 0), (3.4, 2)]:
            raw = self.run_ffmpeg([self.ffmpeg, '-v', 'error', '-ss', str(timestamp), '-i', output,
                '-frames:v', '1', '-vf', 'scale=1:1', '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'])
            self.assertEqual(len(raw), 3)
            self.assertGreater(raw[channel], 200, list(raw))
        pcm = self.run_ffmpeg([self.ffmpeg, '-v', 'error', '-i', output, '-map', '0:a:0',
                               '-f', 's16le', '-acodec', 'pcm_s16le', '-'])
        self.assertEqual(len(pcm), 5 * 48000 * 2)
        self.assertEqual(pcm, (b'\x10\x27' * 48000) * 5)
