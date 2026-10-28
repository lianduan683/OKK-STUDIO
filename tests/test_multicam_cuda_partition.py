"""Parallel render planning shares the encoder's exact NTSC clock."""
from fractions import Fraction
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from exporter_core import CREATE_NO_WINDOW, Capabilities, ExportSettings, MediaInfo, detect_capabilities, find_binary
from multicam_core import CameraClip, MulticamProject, render_multicam


class ParallelClockTests(unittest.TestCase):
    def test_rounded_ntsc_camera_splits_on_source_frame_phase(self):
        with tempfile.TemporaryDirectory(prefix='cuda_partition_plan_') as folder:
            root = Path(folder)
            source = root / 'camera.mov'
            source.write_bytes(b'camera')
            project = MulticamProject([CameraClip('a', 1, str(source), 0, 72)],
                                      fps=59.94006, range_end=71)
            info = MediaInfo(str(source), 72, 3840, 2160, 30, 'hevc', 'yuv420p10le', 10)
            commands, snapshots = [], []

            def run(command, callback):
                commands.append(command)
                Path(command[-1]).write_bytes(b'encoded')
                return 0, ''

            with patch('multicam_core.probe_media', return_value=info), \
                    patch('multicam_core.probe_partition_rates', return_value={str(source): Fraction(30)}):
                render_multicam(project, root / 'out.mp4', 'ffmpeg', 'ffprobe',
                                ExportSettings(codec='hevc_nvenc', audio='none', parallel_workers=3),
                                Capabilities(cuda_decode=True, scale_cuda=True), run, metrics=snapshots.append)
            chunks = sorted((c for c in commands if '-frames:v' in c), key=lambda c: c[-1])
            self.assertEqual([int(c[c.index('-frames:v')+1]) for c in chunks], [2000, 2000, 256])
            self.assertEqual([c[c.index('-ss')+1] for c in chunks], ['0.000000000', '33.366666667', '66.733333333'])
            self.assertTrue(all(c[c.index('-r')+1] == '60000/1001' for c in chunks))
            self.assertTrue(all('-hwaccel_output_format' in c for c in chunks))
            self.assertEqual(snapshots[-1]['encoded_frames'], 4256)
            self.assertEqual(snapshots[-1]['completed_segments'], 3)
            self.assertEqual(snapshots[-1]['total_segments'], 3)
            self.assertEqual(snapshots[-1]['stage'], 'done')
            self.assertEqual(project.fps, 59.94006, 'renderer changed the user project')


@unittest.skipUnless(os.environ.get('OKK_TEST_NVENC') == '1', 'short GPU integration opt-in')
class ActualParallelCudaTests(unittest.TestCase):
    def test_cuda_resize_and_depth_conversion_keep_ten_bit_output(self):
        app = Path(__file__).resolve().parents[1]
        ffmpeg, ffprobe = find_binary('ffmpeg', app), find_binary('ffprobe', app)
        caps = detect_capabilities(app)
        if not ffmpeg or not ffprobe or 'hevc_nvenc' not in caps.nvenc_codecs or not caps.scale_cuda:
            self.skipTest('CUDA scaler unavailable')

        def run(command, callback=None):
            result = subprocess.run(command, capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW)
            return result.returncode, result.stderr.decode('utf-8', 'replace')

        with tempfile.TemporaryDirectory(prefix='cuda_conversion_') as folder:
            source, target = Path(folder) / 'eight_bit.mp4', Path(folder) / 'ten_bit.mp4'
            code, error = run([ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i',
                               'color=red:s=320x180:r=30:d=0.5', '-c:v', 'libx264',
                               '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', str(source)])
            self.assertEqual(code, 0, error)
            render_multicam(MulticamProject([CameraClip('a', 1, str(source), 0, .5)], fps=30),
                            target, ffmpeg, ffprobe,
                            ExportSettings(codec='hevc_nvenc', audio='none', resolution='1080p', bit_depth='10-bit'),
                            caps, run)
            result = subprocess.run([ffprobe, '-v', 'error', '-select_streams', 'v:0', '-count_frames',
                                      '-show_entries', 'stream=width,height,pix_fmt,nb_read_frames', '-of', 'json', str(target)],
                                     capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr)
            stream = json.loads(result.stdout)['streams'][0]
            self.assertEqual((stream['width'], stream['height']), (1920, 1080))
            self.assertEqual(stream['pix_fmt'], 'yuv420p10le')
            self.assertEqual(stream['nb_read_frames'], '15')

    def test_one_long_camera_becomes_three_ten_bit_gpu_tasks(self):
        app = Path(__file__).resolve().parents[1]
        ffmpeg, ffprobe = find_binary('ffmpeg', app), find_binary('ffprobe', app)
        caps = detect_capabilities(app)
        if not ffmpeg or not ffprobe or 'hevc_nvenc' not in caps.nvenc_codecs:
            self.skipTest('HEVC NVENC unavailable')

        def process(command, callback=None):
            result = subprocess.run(command, capture_output=True, timeout=40, creationflags=CREATE_NO_WINDOW)
            return result.returncode, result.stderr.decode('utf-8', 'replace')

        with tempfile.TemporaryDirectory(prefix='cuda_partition_real_') as folder:
            root = Path(folder)
            source = root / 'camera.mp4'
            code, error = process([ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i',
                                   'testsrc2=s=320x180:r=30:d=72', '-pix_fmt', 'p010le',
                                   '-c:v', 'hevc_nvenc', '-preset', 'p2', '-bf', '0', str(source)])
            self.assertEqual(code, 0, error)
            commands, snapshots = [], []

            def runner(command, callback):
                commands.append(command)
                return process(command, callback)

            target = root / 'program.mp4'
            render_multicam(MulticamProject([CameraClip('a', 1, str(source), 0, 72)],
                                            fps=59.94006, range_end=71),
                            target, ffmpeg, ffprobe,
                            ExportSettings(codec='hevc_nvenc', audio='none', parallel_workers=3),
                            caps, runner, metrics=snapshots.append)
            chunks = [c for c in commands if '-frames:v' in c]
            self.assertEqual(len(chunks), 3)
            self.assertTrue(all('-hwaccel_output_format' in c for c in chunks))
            self.assertEqual(max(s['active_workers'] for s in snapshots), 3)
            result = subprocess.run([ffprobe, '-v', 'error', '-select_streams', 'v:0',
                                      '-count_frames', '-show_entries', 'stream=nb_read_frames,pix_fmt,r_frame_rate',
                                      '-of', 'json', str(target)], capture_output=True, timeout=30,
                                     creationflags=CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr)
            stream = json.loads(result.stdout)['streams'][0]
            self.assertEqual(stream['nb_read_frames'], '4256')
            self.assertEqual(stream['pix_fmt'], 'yuv420p10le')
            self.assertEqual(stream['r_frame_rate'], '60000/1001')


if __name__ == '__main__':
    unittest.main()
