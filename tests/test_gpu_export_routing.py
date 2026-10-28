"""Export orchestrator routing/atomicity, without launching GPU executables.

Real programme/transition planning and mux argument builders run unchanged.
Only probing, GPU filter plans and the external process runner are fixtures.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
from fractions import Fraction
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cache_paths import cache_dir
from creative_core import ColorGrade, Transition, grade_filter
from creative_render import TransitionSegment
from exporter_core import Capabilities, ExportCancelled, ExportSettings, MediaInfo
import gpu_export
import multicam_core as module
from multicam_core import CameraClip, Cut, MulticamProject
from program_edits import set_grade_on_range
from subtitle_core import SubtitleCue


class GpuExportRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gpu_route_', dir=cache_dir('tests'))
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder/'camera-a.mp4'; self.source.write_bytes(b'original A')
        self.other = self.folder/'camera-b.mp4'; self.other.write_bytes(b'original B')
        self.master = self.folder/'master.wav'; self.master.write_bytes(b'original master')
        self.ffmpeg = self.folder/'original-ffmpeg.exe'; self.ffmpeg.write_bytes(b'fixture, never executed')
        self.gpu_ffmpeg = self.folder/'gpu-ffmpeg.exe'; self.gpu_ffmpeg.write_bytes(b'fixture, never executed')
        self.destination = self.folder/'delivery.mp4'; self.destination.write_bytes(b'existing delivery')
        self.plans = []; self.commands = []; self.metrics = []; self.phases = []
        self.settings = ExportSettings(codec='hevc_nvenc', preset='p4', quality=17, gpu_index=2,
            gop=73, bframes=3, bit_depth='10', audio='aac', audio_bitrate='192k',
            decode_mode='cuda', extra_args=['-rc-lookahead', '9'])
        self.caps = Capabilities(nvenc_codecs={'hevc_nvenc'}, cuda_decode=True, scale_cuda=True)
        self.media = MediaInfo(str(self.source), duration=4.504, width=1280, height=720,
            fps=30000/1001, video_codec='hevc', pixel_format='yuv420p10le', bit_depth=10,
            color_primaries='bt709', color_transfer='bt709', color_space='bt709', color_range='tv')
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(module, 'probe_media', side_effect=lambda _, path: replace(self.media, path=path)))
        self.stack.enter_context(patch.object(module, '_audio_stream_info', return_value=(
            {'codec_name': 'pcm_s16le', 'sample_rate': '48000', 'channels': 2, 'channel_layout': 'stereo'}, 4.504)))
        self.stack.enter_context(patch.object(module, 'probe_partition_rates', return_value={
            str(self.source): Fraction(30000, 1001), str(self.other): Fraction(30000, 1001)}))
        self.finder = self.stack.enter_context(patch.object(module, 'find_gpu_export_ffmpeg', return_value=str(self.gpu_ffmpeg)))
        self.stack.enter_context(patch.object(module, 'build_gpu_video_plan', side_effect=self.plan))
        self.stack.enter_context(patch.object(gpu_export, 'build_gpu_video_plan', side_effect=self.plan))
        self.stack.enter_context(patch.object(gpu_export, 'detect_gpu_export', return_value=SimpleNamespace(
            ffmpeg_path=str(self.gpu_ffmpeg), xfade_vulkan=True,
            transition_kinds=('fade', 'dissolve', 'wipeleft', 'wiperight', 'slideleft', 'slideright', 'circleopen'))))

    def plan(self, grades, **parameters):
        self.plans.append((tuple(grades), dict(parameters)))
        mode = parameters['decode_mode']
        return SimpleNamespace(global_options=('-init_hw_device', 'vulkan=fixture'),
            input_options=('-hwaccel', mode), filter_chain=f'gpu_fixture_{mode}',
            color_options=('-color_primaries', 'bt709', '-color_trc', 'bt709'),
            download_format='p010le')

    def project(self, *, transition=False, local=False):
        project = MulticamProject(clips=[CameraClip('a', 1, str(self.source), -.5, 4.504)],
            cuts=[Cut(0., 1)], master_audio=str(self.master), master_duration=3.003,
            fps=29.97003, range_end=1.001,
            camera_grades={1: ColorGrade(saturation=.8)}, global_grade=ColorGrade(exposure=.25))
        if transition:
            project.clips.append(CameraClip('b', 2, str(self.other), -.25, 4.504))
            project.cuts.append(Cut(1.5015, 2))
            project.camera_grades[2] = ColorGrade(temperature=-.1)
            project.transitions = [Transition(1.5015, 'fade', 1.001)]
            project.range_start, project.range_end = 1.2012, 1.8018
            set_grade_on_range(project, 0., 1.5015, ColorGrade(exposure=-.15))
            set_grade_on_range(project, 1.5015, 3.003, ColorGrade(exposure=.45))
        elif local:
            set_grade_on_range(project, 0., 1.001, ColorGrade(exposure=-.15))
        return project

    @staticmethod
    def option(command, key): return command[command.index(key)+1]

    def route(self, command):
        if '-f' in command and self.option(command, '-f') == 'concat': return 'mux'
        if command[0] == str(self.gpu_ffmpeg): return self.option(command, '-hwaccel')
        return 'cpu'

    def runner(self, command, callback):
        command = list(command); self.commands.append(command)
        route = self.route(command)
        if callback is not None:
            callback({'frame': '2', 'out_time_us': '66733'})
        if route == 'mux':
            manifest = Path(self.option(command, '-i'))
            self.manifest = manifest.read_text(encoding='utf-8')
            Path(command[-1]).write_bytes(b'completed delivery')
        else:
            Path(command[-1]).write_bytes(('completed '+route).encode())
        return 0, ''

    def export(self, project=None, *, runner=None, cancel_event=None, settings=None):
        return module.render_multicam(project or self.project(), self.destination, str(self.ffmpeg), 'fixture-ffprobe',
            settings or self.settings, self.caps, runner or self.runner,
            progress=lambda _, __, phase: self.phases.append(phase), cancel_event=cancel_event,
            metrics=self.metrics.append)

    def assert_delivery_settings(self, command):
        for key, value in (('-c:v', 'hevc_nvenc'), ('-preset', 'p4'), ('-cq', '17'),
                           ('-g', '73'), ('-bf', '3'), ('-gpu', '2'), ('-fps_mode', 'cfr'),
                           ('-rc-lookahead', '9'), ('-pix_fmt', 'yuv420p10le'), ('-r', '30000/1001'),
                           ('-video_track_timescale', '30000')):
            self.assertEqual(self.option(command, key), value, key)

    def assert_originals_unchanged(self):
        self.assertEqual(self.source.read_bytes(), b'original A')
        self.assertEqual(self.other.read_bytes(), b'original B')
        self.assertEqual(self.master.read_bytes(), b'original master')

    def test_normal_gpu_route_keeps_grade_order_delivery_settings_and_continuous_master(self):
        project = self.project(local=True)
        self.assertEqual(Path(self.export(project)), self.destination)
        self.assertEqual([self.route(command) for command in self.commands], ['cuda', 'mux'])
        grade, parameters = self.plans[0]
        self.assertEqual(grade[0], project.camera_grades[1])
        self.assertEqual(grade[1].exposure, -.15)
        self.assertEqual(grade[2], project.global_grade)
        self.assertEqual(parameters['rate_text'], '30000/1001')
        self.assertEqual(parameters['frames'], 30)
        self.assertEqual((parameters['width'], parameters['height']), (1280, 720))
        self.assertEqual(parameters['pixel_format'], 'yuv420p10le')
        command, mux = self.commands
        self.assert_delivery_settings(command)
        self.assertEqual(self.option(command, '-frames:v'), '30')
        self.assertEqual(self.option(command, '-ss'), '0.500000000')
        self.assertIn('gpu_fixture_cuda', self.option(command, '-vf'))
        self.assertEqual(self.option(mux, '-c:v'), 'copy')
        self.assertEqual(self.option(mux, '-c:a'), 'aac')
        self.assertEqual(self.option(mux, '-b:a'), '192k')
        self.assertIn(str(self.master), mux)
        self.assertNotIn('-ss', mux)
        self.assertEqual(self.option(mux, '-t'), '1.001000000')
        self.assertEqual(self.metrics[-1]['encoded_frames'], 30)
        self.assertEqual(self.metrics[-1]['stage'], 'done')
        self.assertEqual(self.destination.read_bytes(), b'completed delivery')
        self.assert_originals_unchanged()

    def test_unverified_transition_keeps_cpu_grade_phase_subtitle_order_and_audio_seek(self):
        project = self.project(transition=True)
        project.subtitles.cues = [SubtitleCue('caption', 1.3, 1.7, '转场字幕')]
        self.export(project)
        command, mux = self.commands
        self.assertEqual(self.route(command), 'cpu')
        self.assert_delivery_settings(command)
        self.assertEqual(self.option(command, '-frames:v'), '18')
        graph = self.option(command, '-filter_complex')
        self.assertIn('xfade=transition=fade:duration=1.001000000:offset=0', graph)
        self.assertIn('trim=start_frame=6:end_frame=24', graph)
        self.assertEqual(graph.count('subtitles=filename='), 1)
        self.assertGreater(graph.index('subtitles=filename='), graph.index('xfade='))
        for camera, local in ((1, project.clip_grades[0].grade), (2, project.clip_grades[1].grade)):
            expected = ','.join(grade_filter(grade) for grade in
                (project.camera_grades[camera], local, project.global_grade))
            self.assertIn(expected, graph)
            self.assertLess(graph.index(expected), graph.index('xfade='))
        self.assertEqual(self.plans, [], 'unverified GPU transition must not bypass the runtime stability gate')
        self.assertAlmostEqual(float(self.option(mux, '-ss')), 1.2012)
        self.assertEqual(self.option(mux, '-t'), '0.600600000')
        self.assertEqual(self.metrics[-1]['encoded_frames'], 18)
        self.assert_originals_unchanged()

    def test_runtime_cuda_failure_uses_original_effects_before_publishing(self):
        def run(command, callback):
            if self.route(command) == 'cuda':
                self.commands.append(list(command)); Path(command[-1]).write_bytes(b'partial GPU file')
                return 1, 'unsupported CUDA camera profile'
            return self.runner(command, callback)
        self.export(runner=run)
        self.assertEqual([self.route(command) for command in self.commands], ['cuda', 'cpu', 'mux'])
        self.assert_delivery_settings(self.commands[1])
        self.assertEqual(self.destination.read_bytes(), b'completed delivery')
        self.assertTrue(any('CPU 特效回退' in phase for phase in self.phases))
        self.assertTrue(all(parameters['decode_mode'] == 'cuda' for _, parameters in self.plans))

    def test_gpu_failure_keeps_complete_original_grade_and_subtitle_chain(self):
        project = self.project(local=True)
        project.subtitles.cues = [SubtitleCue('caption', 0., 1., '保留字幕')]
        def run(command, callback):
            if self.route(command) == 'cuda':
                self.commands.append(list(command)); return 1, 'GPU unavailable'
            return self.runner(command, callback)
        self.export(project, runner=run)
        self.assertEqual([self.route(command) for command in self.commands], ['cuda', 'cpu', 'mux'])
        command = self.commands[1]
        self.assert_delivery_settings(command)
        chain = self.option(command, '-vf')
        self.assertNotIn('gpu_fixture_', chain)
        self.assertIn('lutrgb=', chain)
        self.assertIn('subtitles=filename=', chain)
        self.assertLess(chain.index('lutrgb='), chain.index('subtitles=filename='))
        self.assertEqual(self.option(command, '-frames:v'), '30')
        self.assertEqual(self.destination.read_bytes(), b'completed delivery')
        self.assertTrue(any('CPU 特效回退' in phase for phase in self.phases))
        self.assert_originals_unchanged()

    def test_unsupported_gpu_effect_uses_original_transition_without_substitution(self):
        project = self.project(transition=True)
        project.transitions[0].kind = 'fadeblack'
        self.export(project)
        command, _ = self.commands
        self.assertEqual(self.route(command), 'cpu')
        self.assertIn('xfade=transition=fadeblack:', self.option(command, '-filter_complex'))
        self.assertEqual(self.option(command, '-frames:v'), '18')
        self.assert_delivery_settings(command)
        self.assertEqual(self.plans, [])

    def test_cancelled_gpu_failure_never_retries_or_replaces_existing_destination(self):
        event = threading.Event()
        def run(command, callback):
            self.commands.append(list(command)); Path(command[-1]).write_bytes(b'partial')
            event.set(); return 1, 'cancelled hardware command'
        with self.assertRaises(ExportCancelled): self.export(runner=run, cancel_event=event)
        self.assertEqual([self.route(command) for command in self.commands], ['cuda'])
        self.assertEqual(self.destination.read_bytes(), b'existing delivery')
        self.assertFalse(any(snapshot['stage'] == 'done' for snapshot in self.metrics))
        self.assert_originals_unchanged()

    def test_runner_cancellation_exception_is_not_converted_to_fallback(self):
        def run(command, callback):
            self.commands.append(list(command)); raise ExportCancelled('user cancelled')
        with self.assertRaises(ExportCancelled): self.export(runner=run)
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.destination.read_bytes(), b'existing delivery')

    def test_all_routes_failing_preserves_existing_delivery(self):
        def run(command, callback):
            self.commands.append(list(command)); Path(command[-1]).write_bytes(b'partial')
            return 1, 'fixture encoder failure'
        with self.assertRaisesRegex(RuntimeError, '失败'): self.export(runner=run)
        self.assertEqual([self.route(command) for command in self.commands], ['cuda', 'cpu'])
        self.assertEqual(self.destination.read_bytes(), b'existing delivery')
        self.assert_originals_unchanged()

    def test_software_delivery_does_not_probe_or_invoke_gpu_runtime(self):
        settings = replace(self.settings, codec='libx265', preset='veryfast', decode_mode='cpu')
        self.export(settings=settings)
        self.finder.assert_not_called()
        self.assertEqual([self.route(command) for command in self.commands], ['cpu', 'mux'])
        self.assertEqual(self.plans, [])

    def diagnostics(self):
        paths = {snapshot['diagnostic_path'] for snapshot in self.metrics}
        self.assertEqual(len(paths), 1)
        path = Path(paths.pop())
        self.assertEqual(path.suffix, '.jsonl')
        self.assertTrue(path.is_file())
        self.assertTrue(path.is_relative_to(cache_dir('logs/exports')))
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]

    def three_jobs(self):
        project = self.project()
        project.clips.append(CameraClip('b', 2, str(self.other), -.25, 4.504))
        project.cuts = [Cut(0., 1), Cut(1.001, 2), Cut(2.002, 1)]
        project.range_end = 3.003
        return project

    def test_ordinary_source_failure_skips_same_source_but_allows_other_camera_gpu(self):
        def run(command, callback):
            if self.route(command) == 'cuda' and self.option(command, '-i') == str(self.source):
                self.commands.append(list(command)); return 1, 'unsupported camera color profile'
            return self.runner(command, callback)
        self.export(self.three_jobs(), runner=run)
        self.assertEqual([self.route(command) for command in self.commands], ['cuda', 'cpu', 'cuda', 'cpu', 'mux'])
        gpu_sources = [self.option(command, '-i') for command in self.commands if self.route(command) == 'cuda']
        self.assertEqual(gpu_sources, [str(self.source), str(self.other)])
        disabled = [item for item in self.diagnostics() if item['event'] == 'gpu_route_disabled']
        self.assertEqual([(item['scope'], item['source']) for item in disabled], [('source', str(self.source))])
        self.assertEqual(self.metrics[-1]['encoded_frames'], 90)
        self.assert_originals_unchanged()

    def test_two_different_grades_on_same_source_do_not_repeat_failed_gpu_route(self):
        project = self.project()
        project.range_end = 2.002
        set_grade_on_range(project, 0., 1.001, ColorGrade(exposure=-.15))
        set_grade_on_range(project, 1.001, 2.002, ColorGrade(exposure=.45))
        def run(command, callback):
            if self.route(command) == 'cuda':
                self.commands.append(list(command)); return 1, 'unsupported camera filter input'
            return self.runner(command, callback)
        self.export(project, runner=run)
        self.assertEqual([self.route(command) for command in self.commands], ['cuda', 'cpu', 'cpu', 'mux'])
        self.assertEqual(len(self.plans), 1)
        self.assertEqual(self.metrics[-1]['encoded_frames'], 60)

    def test_vulkan_device_or_submit_failure_disables_gpu_for_other_sources_too(self):
        for error in ('Unable to submit command: VK_ERROR_DEVICE_LOST',
                      'vkQueueSubmit failed: VK_ERROR_OUT_OF_HOST_MEMORY',
                      'Failed creating Vulkan device: initialization failed'):
            with self.subTest(error=error):
                self.commands.clear(); self.plans.clear(); self.metrics.clear()
                def run(command, callback):
                    if self.route(command) == 'cuda':
                        self.commands.append(list(command)); return 1, error
                    return self.runner(command, callback)
                self.export(self.three_jobs(), runner=run)
                self.assertEqual([self.route(command) for command in self.commands], ['cuda', 'cpu', 'cpu', 'cpu', 'mux'])
                self.assertEqual(len(self.plans), 1)
                disabled = [item for item in self.diagnostics() if item['event'] == 'gpu_route_disabled']
                self.assertEqual([item['scope'] for item in disabled], ['export'])

    def test_parallel_prepared_job_rechecks_device_failure_before_launch(self):
        prepared = threading.Event(); failure_reported = threading.Event()
        class Metrics(list):
            def append(values, snapshot):
                super().append(snapshot)
                if 'CPU 特效回退' in snapshot['phase']: failure_reported.set()
        self.metrics = Metrics()
        def plan(grades, **parameters):
            result = self.plan(grades, **parameters)
            if parameters['media'].path == str(self.other):
                prepared.set()
                self.assertTrue(failure_reported.wait(2.), 'device failure did not reach the second prepared worker')
            return result
        def run(command, callback):
            if self.route(command) == 'cuda':
                self.assertTrue(prepared.wait(2.), 'second worker did not prepare its GPU job')
                self.commands.append(list(command)); return 1, 'Unable to submit command: VK_ERROR_DEVICE_LOST'
            return self.runner(command, callback)
        with patch.object(module, 'build_gpu_video_plan', side_effect=plan):
            self.export(self.three_jobs(), runner=run, settings=replace(self.settings, parallel_workers=2))
        self.assertEqual(sum(self.route(command) == 'cuda' for command in self.commands), 1)
        self.assertEqual(sum(self.route(command) == 'cpu' for command in self.commands), 3)
        self.assertEqual(self.route(self.commands[-1]), 'mux')
        self.assertEqual(self.metrics[-1]['encoded_frames'], 90)

    def test_trace_retains_full_gpu_diagnostics_attempt_times_and_metrics_path(self):
        error = 'FIRST DIAGNOSTIC\n'+('full stack/context line\n'*600)+'LAST DIAGNOSTIC\nUnable to submit command'
        def run(command, callback):
            if self.route(command) == 'cuda':
                self.commands.append(list(command)); return 1, error
            return self.runner(command, callback)
        self.export(runner=run)
        events = self.diagnostics()
        finishes = [item for item in events if item['event'] == 'attempt_finish']
        self.assertEqual([item['backend'] for item in finishes], ['gpu_effects', 'cpu_effects_fallback'])
        self.assertEqual(finishes[0]['details'], error)
        self.assertEqual(finishes[0]['returncode'], 1)
        self.assertTrue(all(isinstance(item['seconds'], (float, int)) and item['seconds'] >= 0 for item in finishes))
        self.assertEqual([item['reason'] for item in events if item['event'] == 'gpu_route_disabled'], [error])
        self.assertEqual([item['event'] for item in events].count('export_start'), 1)
        self.assertEqual([item['event'] for item in events].count('export_finish'), 1)
        self.assertTrue(all(snapshot['average_fps'] >= 0 for snapshot in self.metrics))


if __name__ == '__main__': unittest.main(verbosity=2)
