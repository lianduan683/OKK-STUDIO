"""Atomic publication from export caches, plus legacy parallel-route coverage."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import errno
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cache_publish as module
from cache_paths import cache_dir
from cache_publish import publish_completed
from editor_core import JoinExportPlan, JoinVideoSpan
from exporter_core import Capabilities, ExportCancelled, ExportSettings, MediaInfo
from multicam_core import CameraClip, Cut, MulticamProject
from proxy_cache import register_proxy
from rtx5090_exporter import ExporterApp


class CachePublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='atomic_publish_', dir=cache_dir('tests'))
        self.folder = Path(self.temp.name)
        self.source = self.folder/'completed.mp4'
        self.destination = self.folder/'deliveries'/'programme.mp4'
        self.destination.parent.mkdir()
        self.payload = b'complete encoded movie' * 100
        self.source.write_bytes(self.payload)
        self.destination.write_bytes(b'previous completed movie')
        self.original_replace = module.os.replace

    def tearDown(self):
        self.temp.cleanup()

    def cross_volume(self, source, destination):
        if Path(source).resolve() == self.source.resolve():
            raise OSError(errno.EXDEV, 'simulated cross-volume move')
        return self.original_replace(source, destination)

    def assert_previous(self):
        self.assertEqual(self.destination.read_bytes(), b'previous completed movie')
        self.assertEqual(self.source.read_bytes(), self.payload)
        self.assertEqual(list(self.destination.parent.glob('*.pending')), [])

    def test_same_volume_uses_one_atomic_replace_without_pending_copy(self):
        with patch.object(module.os, 'replace', wraps=self.original_replace) as replace, \
                patch.object(module.tempfile, 'mkstemp') as temporary:
            result = publish_completed(self.source, self.destination)
        replace.assert_called_once_with(self.source.resolve(), self.destination.resolve())
        temporary.assert_not_called()
        self.assertEqual(result, str(self.destination.resolve()))
        self.assertEqual(self.destination.read_bytes(), self.payload)
        self.assertFalse(self.source.exists())

    def test_exdev_copies_completed_file_to_unique_sibling_before_atomic_commit(self):
        progress = []
        pending_paths = []
        def replace(source, destination):
            if Path(source) == self.source: return self.cross_volume(source, destination)
            pending = Path(source)
            pending_paths.append(pending)
            self.assertEqual(pending.parent, self.destination.parent)
            self.assertEqual(pending.suffix, '.pending')
            self.assertEqual(pending.read_bytes(), self.payload)
            self.assertEqual(self.destination.read_bytes(), b'previous completed movie')
            return self.original_replace(source, destination)
        def report(copied, total):
            self.assertEqual(self.destination.read_bytes(), b'previous completed movie')
            progress.append((copied, total))
        with patch.object(module.os, 'replace', side_effect=replace):
            publish_completed(self.source, self.destination, progress=report)
        self.assertEqual(self.destination.read_bytes(), self.payload)
        self.assertEqual(self.source.read_bytes(), self.payload)
        self.assertEqual(progress[0], (0, len(self.payload)))
        self.assertEqual(progress[-1], (len(self.payload), len(self.payload)))
        self.assertEqual(len(pending_paths), 1)
        self.assertFalse(pending_paths[0].exists())

    def test_windows_not_same_device_error_also_uses_safe_copy(self):
        failure = OSError(errno.EACCES, 'not same device')
        failure.winerror = 17
        def replace(source, destination):
            if Path(source) == self.source: raise failure
            return self.original_replace(source, destination)
        with patch.object(module.os, 'replace', side_effect=replace):
            publish_completed(self.source, self.destination)
        self.assertEqual(self.destination.read_bytes(), self.payload)

    def test_precancelled_publication_preserves_both_files_without_attempting_replace(self):
        cancel = threading.Event(); cancel.set()
        with patch.object(module.os, 'replace') as replace:
            with self.assertRaises(ExportCancelled):
                publish_completed(self.source, self.destination, cancel_event=cancel)
        replace.assert_not_called()
        self.assert_previous()

    def test_cancel_during_cross_volume_copy_removes_pending_and_preserves_old_output(self):
        cancel = threading.Event()
        def report(copied, total):
            if copied: cancel.set()
        with patch.object(module.os, 'replace', side_effect=self.cross_volume), \
                patch.object(module, 'COPY_BLOCK_SIZE', 32):
            with self.assertRaises(ExportCancelled):
                publish_completed(self.source, self.destination, cancel_event=cancel, progress=report)
        self.assert_previous()

    def test_copy_failure_preserves_old_output_and_cleans_pending(self):
        with patch.object(module.os, 'replace', side_effect=self.cross_volume), \
                patch.object(module.os, 'fsync', side_effect=OSError(errno.ENOSPC, 'disk full')):
            with self.assertRaises(OSError): publish_completed(self.source, self.destination)
        self.assert_previous()

    def test_final_replace_failure_preserves_old_output_and_cleans_pending(self):
        def replace(source, destination):
            if Path(source) == self.source: return self.cross_volume(source, destination)
            raise PermissionError(errno.EACCES, 'output locked')
        with patch.object(module.os, 'replace', side_effect=replace):
            with self.assertRaises(PermissionError): publish_completed(self.source, self.destination)
        self.assert_previous()

    def test_unrelated_replace_error_does_not_fall_back_to_copy(self):
        with patch.object(module.os, 'replace', side_effect=PermissionError(errno.EACCES, 'locked')), \
                patch.object(module.tempfile, 'mkstemp') as temporary:
            with self.assertRaises(PermissionError): publish_completed(self.source, self.destination)
        temporary.assert_not_called()
        self.assert_previous()

    def test_missing_empty_or_same_file_is_never_published(self):
        empty = self.folder/'empty.mp4'; empty.touch()
        for source, destination in ((self.folder/'missing.mp4', self.destination),
                                    (empty, self.destination), (self.source, self.source)):
            with self.subTest(source=source), self.assertRaises(ValueError):
                publish_completed(source, destination)
        self.assert_previous()

    def test_concurrent_completed_exports_use_distinct_pending_files(self):
        second = self.folder/'second.mp4'; second.write_bytes(b'second complete movie'*100)
        barrier = threading.Barrier(2)
        observed = []
        lock = threading.Lock()
        def replace(source, destination):
            if Path(source) in (self.source, second): raise OSError(errno.EXDEV, 'different drive')
            with lock: observed.append(Path(source))
            return self.original_replace(source, destination)
        def report(copied, total):
            if not copied: barrier.wait(timeout=3)
        with patch.object(module.os, 'replace', side_effect=replace), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(publish_completed, source, self.destination, progress=report)
                       for source in (self.source, second)]
            for future in futures: future.result(timeout=5)
        self.assertEqual(len(set(observed)), 2)
        self.assertIn(self.destination.read_bytes(), (self.payload, second.read_bytes()))
        self.assertEqual(list(self.destination.parent.glob('*.pending')), [])


class LegacyParallelCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='legacy_export_cache_', dir=cache_dir('tests'))
        self.folder = Path(self.temp.name)
        self.cache = self.folder/'exports'; self.cache.mkdir()
        self.source = self.folder/'original.mp4'; self.source.write_bytes(b'original recording')
        self.output = self.folder/'delivery.mov'; self.output.write_bytes(b'previous finished output')
        self.app = ExporterApp.__new__(ExporterApp)
        self.app.events = queue.Queue()
        self.app.cancel_event = threading.Event()
        self.app.caps = Capabilities(ffmpeg_path='ffmpeg')
        self.app.process_lock = threading.Lock()
        self.app.processes = []
        self.app._terminate = Mock()
        self.app._editor_progress_reporter = Mock(return_value=lambda progress: None)
        self.media = MediaInfo(str(self.source), 4., 160, 90, 30., 'h264', 'yuv420p')
        self.settings = ExportSettings(codec='libx264', preset='veryfast', decode_mode='cpu')
        self.commands = []
        self.cache_patch = patch('rtx5090_exporter.cache_dir', return_value=self.cache)
        self.cache_lookup = self.cache_patch.start()

    def tearDown(self):
        self.cache_patch.stop()
        self.temp.cleanup()

    def writer(self, command, *args, **kwargs):
        self.commands.append(command)
        Path(command[-1]).write_bytes(b'completed generated movie')
        return 0, ''

    def assert_routed(self):
        self.assertTrue(self.commands)
        self.assertTrue(all(Path(command[-1]).is_relative_to(self.cache) for command in self.commands))
        self.assertEqual(list(self.cache.iterdir()), [])
        self.assertEqual(self.source.read_bytes(), b'original recording')

    def test_legacy_parallel_encode_chunks_merge_and_completed_mux_all_use_cache(self):
        self.app._run_ffmpeg = self.writer
        self.app._run_simple = self.writer
        self.app._parallel_export(str(self.source), str(self.output), self.settings, self.media, 2)
        self.assertEqual(self.output.read_bytes(), b'completed generated movie')
        self.assert_routed()

    def test_parallel_cancel_after_completed_mux_keeps_previous_output(self):
        def encode(command, *args, **kwargs):
            self.writer(command)
            if Path(command[-1]).stem == 'completed': self.app.cancel_event.set()
        self.app._run_ffmpeg = encode
        self.app._run_simple = self.writer
        with self.assertRaises(ExportCancelled):
            self.app._parallel_export(str(self.source), str(self.output), self.settings, self.media, 2)
        self.assertEqual(self.output.read_bytes(), b'previous finished output')
        self.assert_routed()

    def test_parallel_join_completed_mux_publishes_only_after_cached_jobs_succeed(self):
        self.app._run_editor_process = self.writer
        plan = JoinExportPlan((JoinVideoSpan(0, 0., 0., 2., 60), JoinVideoSpan(1, 0., 2., 2., 60)),
                              ((0., 2.), (0., 2.)), 4., 30.)
        code, _ = self.app._run_parallel_join_export([str(self.source)]*2, '', [self.media]*2,
            str(self.output), self.settings, plan, 2, '导出', audio_bitrate='192k', audio_codec='aac', audio_sample_rate=48000)
        self.assertEqual(code, 0)
        self.assertEqual(self.output.read_bytes(), b'completed generated movie')
        self.assert_routed()

    def test_parallel_join_mux_failure_keeps_previous_output(self):
        def encode(command, *args, **kwargs):
            self.writer(command)
            return (1, 'mux failed') if Path(command[-1]).stem == 'completed' else (0, '')
        self.app._run_editor_process = encode
        plan = JoinExportPlan((JoinVideoSpan(0, 0., 0., 2., 60), JoinVideoSpan(1, 0., 2., 2., 60)),
                              ((0., 2.), (0., 2.)), 4., 30.)
        code, _ = self.app._run_parallel_join_export([str(self.source)]*2, '', [self.media]*2,
            str(self.output), self.settings, plan, 2, '导出', audio_bitrate='192k', audio_codec='aac', audio_sample_rate=48000)
        self.assertEqual(code, 1)
        self.assertEqual(self.output.read_bytes(), b'previous finished output')
        self.assert_routed()

    def test_proxy_default_uses_cache_but_explicit_directory_is_retained(self):
        variable = SimpleNamespace(value='', get=lambda: variable.value,
                                   set=lambda value: setattr(variable, 'value', value))
        self.app.editor_proxy_files = []
        self.app.editor_proxy_dir_var = variable
        self.app._refresh_proxy_list = Mock()
        self.app._add_proxy_paths([str(self.source)])
        self.assertEqual(variable.value, str(self.cache))
        self.cache_lookup.assert_called_with('proxies')
        variable.value = str(self.folder/'chosen proxies')
        self.app._add_proxy_paths([str(self.folder/'other.mp4')])
        self.assertEqual(variable.value, str(self.folder/'chosen proxies'))

    def test_registered_proxy_precedes_legacy_but_explicit_proxy_and_original_export_win(self):
        legacy = self.source.parent/'ProxyMedia'; legacy.mkdir()
        legacy = legacy/self.source.name; legacy.write_bytes(b'legacy proxy')
        indexed = self.cache/'bound proxy.mp4'; indexed.write_bytes(b'indexed proxy')
        explicit = self.folder/'chosen proxy.mp4'; explicit.write_bytes(b'explicit proxy')
        index = self.folder/'index'; index.mkdir()
        project = MulticamProject([CameraClip('a', 1, str(self.source), 0., 4.)], [Cut(0., 1)])
        with patch('proxy_cache._index_directory', return_value=index):
            self.assertTrue(register_proxy(self.source, indexed))
            self.assertEqual(Path(project.media_at(1, 1., True)[0]), indexed)
            project.clips[0].proxy_path = str(explicit)
            self.assertEqual(Path(project.media_at(1, 1., True)[0]), explicit)
            project.clips[0].proxy_path = str(self.folder/'explicit missing proxy.mp4')
            self.assertEqual(Path(project.media_at(1, 1., True)[0]), self.source)
            self.assertEqual(Path(project.media_at(1, 1., False)[0]), self.source)
            self.assertEqual(Path(project.segments()[0].path), self.source)
            project.clips[0].proxy_path = ''
            indexed.write_bytes(b'proxy replaced by another recording')
            self.assertEqual(Path(project.media_at(1, 1., True)[0]), legacy)

    def test_successful_generated_proxy_registers_and_index_failure_is_nonfatal(self):
        self.prepare_proxy_worker()
        with patch('rtx5090_exporter.probe_media', return_value=self.media), \
                patch('proxy_media.validate_proxy_media', return_value=self.media) as validate, \
                patch('rtx5090_exporter.register_proxy', return_value=False) as register, \
                patch('rtx5090_exporter.messagebox.showerror') as error:
            notifications = self.run_proxy_worker()
        output = self.cache/self.source.name
        register.assert_called_once_with(str(self.source), str(output))
        validate.assert_called_once()
        pending = Path(validate.call_args.args[2])
        self.assertNotEqual(pending, output)
        self.assertFalse(pending.exists(), 'completed pending file was not atomically published')
        error.assert_not_called()
        self.assertEqual(notifications[-1][0], 'editor_proxy_batch_done')
        self.assertEqual(notifications[-1][1][0], [str(output)])
        self.assertEqual(notifications[-1][1][1], [])
        self.assertTrue(any(kind == 'log' and '索引未登记' in text for kind, text in notifications))
        self.assertEqual(output.read_bytes(), b'completed generated movie')
        self.assertEqual(self.source.read_bytes(), b'original recording')

    def prepare_proxy_worker(self):
        def variable(value=''):
            item = SimpleNamespace(value=value)
            item.get = lambda: item.value
            item.set = lambda value: setattr(item, 'value', value)
            return item
        self.app.working = False
        self.app.editor_proxy_files = [str(self.source)]
        self.app.editor_proxy_dir_var = variable(str(self.cache))
        self.app.editor_proxy_format_var = variable('h264_nvenc')
        self.app.progress_var = variable(0.)
        self.app.progress_text_var = variable()
        self.app.editor_status_var = variable()
        self.app._set_working = Mock()
        self.app._proxy_parallel_count = lambda: 1
        self.app.caps.ffprobe_path = 'ffprobe'
        self.app._run_editor_process = self.writer

    def run_proxy_worker(self):
        self.app._create_1080p_proxy()
        notifications = []
        deadline = time.monotonic()+3.
        while time.monotonic() < deadline:
            try: item = self.app.events.get(timeout=.05)
            except queue.Empty: continue
            notifications.append(item)
            if item[0] in {'editor_proxy_batch_done', 'cancelled'}:
                return notifications
        self.fail('proxy worker did not finish')

    def assert_failed_proxy_preserves_files(self, output, register):
        register.assert_not_called()
        self.assertEqual(output.read_bytes(), b'previous valid proxy')
        self.assertEqual(self.source.read_bytes(), b'original recording')
        self.assertEqual(list(self.cache.glob('.*.okk-*')), [])

    def test_failed_encode_or_validation_never_replaces_or_registers_partial_proxy(self):
        for failure in ('encode', 'validate'):
            with self.subTest(failure=failure):
                self.prepare_proxy_worker()
                output = self.cache/self.source.name
                output.write_bytes(b'previous valid proxy')
                def writer(command, *args, **kwargs):
                    self.writer(command)
                    return (1, 'encoder failed after partial output') if failure == 'encode' else (0, '')
                self.app._run_editor_process = writer
                with patch('rtx5090_exporter.probe_media', return_value=self.media), \
                        patch('rtx5090_exporter.messagebox.askyesno', return_value=True), \
                        patch('proxy_media.validate_proxy_media', side_effect=ValueError('proxy duration mismatch')) as validate, \
                        patch('rtx5090_exporter.register_proxy') as register:
                    notifications = self.run_proxy_worker()
                self.assert_failed_proxy_preserves_files(output, register)
                self.assertEqual(notifications[-1][0], 'editor_proxy_batch_done')
                self.assertEqual(notifications[-1][1][0], [])
                self.assertEqual(len(notifications[-1][1][1]), 1)
                self.assertEqual(validate.call_count, int(failure == 'validate'))

    def test_cancel_during_encode_or_before_publication_preserves_old_proxy_and_index(self):
        for moment in ('encode', 'publication'):
            with self.subTest(moment=moment):
                self.prepare_proxy_worker()
                output = self.cache/self.source.name
                output.write_bytes(b'previous valid proxy')
                def writer(command, *args, **kwargs):
                    self.writer(command)
                    if moment == 'encode': self.app.cancel_event.set()
                    return 0, ''
                def validate(*args, **kwargs):
                    self.app.cancel_event.set()
                    return self.media
                self.app._run_editor_process = writer
                with patch('rtx5090_exporter.probe_media', return_value=self.media), \
                        patch('rtx5090_exporter.messagebox.askyesno', return_value=True), \
                        patch('proxy_media.validate_proxy_media', side_effect=validate) as validation, \
                        patch('rtx5090_exporter.register_proxy') as register:
                    notifications = self.run_proxy_worker()
                self.assert_failed_proxy_preserves_files(output, register)
                self.assertEqual(notifications[-1][0], 'cancelled')
                self.assertEqual(validation.call_count, int(moment == 'publication'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
