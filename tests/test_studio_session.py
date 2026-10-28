"""Whole-session save/recovery through the real, withdrawn desktop app."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import json
import subprocess
import tempfile
import time
import tkinter as tk
from tkinter import messagebox
import unittest
from unittest.mock import patch

import rtx5090_exporter as app_module
import studio_project
from project_file import (ProjectFileError, apply_state, build_project_payload,
                          collect_state, read_project_file, state_digest, write_project_file)
from multicam_core import CameraClip, Cut, MulticamProject
from creative_core import ColorGrade, Transition
from gyroflow_core import StabilizationSettings, StabilizedRange
from lossless_core import CutSegment, SegmentModel
from lossless_ffmpeg import ExternalTrack
from program_edits import set_grade_on_range
from subtitle_core import SubtitleCue, SubtitleTrack
from exporter_core import Capabilities, CREATE_NO_WINDOW


class StudioSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_dir = Path(__file__).resolve().parents[1]
        cls.temporary = tempfile.TemporaryDirectory(prefix='session_qa_', dir=cls.app_dir/'work')
        cls.root = Path(cls.temporary.name)
        cls.media = cls.root/'original.mp4'
        cls.ffmpeg, cls.ffprobe = cls.app_dir/'bin/ffmpeg.exe', cls.app_dir/'bin/ffprobe.exe'
        if not cls.ffmpeg.is_file():
            cls.temporary.cleanup()
            raise unittest.SkipTest('bundled FFmpeg required')
        result = subprocess.run([str(cls.ffmpeg), '-v', 'error', '-y', '-f', 'lavfi', '-i',
            'testsrc2=size=96x54:rate=30:duration=8', '-f', 'lavfi', '-i',
            'sine=frequency=440:duration=8', '-c:v', 'libx264', '-preset', 'ultrafast',
            '-c:a', 'aac', '-shortest', str(cls.media)], capture_output=True, timeout=20,
            creationflags=CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError(result.stderr.decode('utf-8', 'replace'))

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.directory = self.root/self._testMethodName
        self.directory.mkdir()
        self.patches = [
            patch.object(studio_project, 'app_directory', return_value=self.directory),
            patch.object(app_module, 'initialize_cache_policy', return_value=self.directory),
            patch.object(app_module.ExporterApp, '_detect_async'),
            patch.object(app_module.ExporterApp, '_offer_session_recovery'),
            patch.object(app_module.ExporterApp, 'state'),
            patch.object(app_module.MulticamWorkspace, '_schedule_camera_metadata'),
            patch.object(app_module.MulticamWorkspace, '_schedule_transition_previews'),
        ]
        self.dialogs = {}
        for name in ('showerror', 'showwarning', 'showinfo', 'askyesno', 'askyesnocancel'):
            item = patch.object(messagebox, name, return_value=False)
            self.patches.append(item)
        for item in self.patches:
            item.start()
        self.app = app_module.ExporterApp()
        self.app.withdraw()
        self.app.mpv_path = ''
        self.app.caps = Capabilities(ffmpeg_path=str(self.ffmpeg), ffprobe_path=str(self.ffprobe))
        self.errors = []
        self.app.report_callback_exception = lambda *args: self.errors.append(args)

    def tearDown(self):
        if hasattr(self, 'app'):
            self.app._closing = True
            self.app.lossless_workspace.close()
            self.app.media_pool_workspace.close()
            self.app.multicam_workspace.close()
            for pending in self.app.tk.call('after', 'info'):
                self.app.tk.call('after', 'cancel', pending)
            self.app.destroy()
        for item in reversed(self.patches):
            item.stop()

    def populate(self):
        ws = self.app.multicam_workspace
        proxy = self.directory/'proxy.mov'; proxy.write_bytes(self.media.read_bytes())
        ws.project = MulticamProject(
            clips=[CameraClip('a', 1, str(self.media), 0., 8., str(proxy)),
                   CameraClip('b', 2, str(self.media), 0., 8.)],
            cuts=[Cut(0., 1), Cut(4., 2)], master_audio=str(self.media), master_duration=8.,
            global_grade=ColorGrade(exposure=.5, lut_path=str(self.directory/'missing.cube')),
            camera_grades={1: ColorGrade(saturation=.8)}, transitions=[Transition(4., 'fade', .5)],
            subtitles=SubtitleTrack(cues=[SubtitleCue('s', 1., 2., '测试字幕')],
                                    source_path=str(self.directory/'captions.srt')),
            alignment_reference_camera=1, color_reference_camera=2)
        set_grade_on_range(ws.project, 1., 2., ColorGrade(contrast=1.1))
        ws.project.stabilizations = [StabilizedRange(2., 3., 1, 'a',
            StabilizationSettings(engine='image'), str(self.directory/'stable.mp4'), 'b'*64, 2., 3.)]
        ws.start_var.set('0'); ws.end_var.set('8'); ws.fps_var.set('30')
        ws.proxy_var.set(False); ws.export_mode.set('CPU 兼容')
        ws.output_var.set(str(self.directory/'final.mov'))
        ws.delivery_auto_parallel_var.set(False)
        ws._refresh()
        ll = self.app.lossless_workspace
        ll.state.source = str(self.media)
        ll.state.model = SegmentModel([CutSegment(id='keep', start=.5, end=2., name='保留', selected=True),
                                      CutSegment(id='mark', start=3., end=None, name='标记')], media_duration=8.)
        ll.state.active_id = 'keep'
        ll.state.track_selection = {0: True, 1: False}
        ll.state.external_tracks = [ExternalTrack(str(self.directory/'captions.srt'), kind='subtitle')]
        ll._sync_options_to_vars()
        self.app.editor_proxy_files = [str(self.media)]
        self.app._refresh_proxy_list()
        return ws

    def settle_lossless(self):
        deadline = time.monotonic() + 8
        while self.app.lossless_workspace.state.probe is None and time.monotonic() < deadline:
            self.app._drain_events()
            self.app.update()
            time.sleep(.01)
        self.assertIsNotNone(self.app.lossless_workspace.state.probe)
        self.assertEqual(self.errors, [])

    def test_roundtrip_all_edit_layers_and_probe_preserves_cuts_disabled_tracks(self):
        ws = self.populate()
        ws.project.video_stack_enabled = True
        ws.project.video_stack_order = [2,1]
        ws.project.video_stack_hidden = [1]
        ws.project.clips[1].has_alpha = True
        ws._refresh()
        before = collect_state(self.app)
        path = self.directory/'complete.rtxproj'
        self.assertTrue(self.app._project_write(path))
        self.assertFalse(self.app._project_is_dirty())
        expected = asdict(MulticamProject.from_payload(before['okk_multicam']['project'],
                                                       before['okk_multicam']['base_directory']))
        ws.project.clips.clear()
        self.app.lossless_workspace.state.model = SegmentModel()
        self.app._project_open(path)
        self.settle_lossless()
        self.assertEqual(asdict(ws.project), expected)
        restored = collect_state(self.app)
        self.assertEqual(restored['lossless']['segments'], before['lossless']['segments'])
        self.assertEqual(restored['lossless']['track_selection'], {'0': True, '1': False})
        self.assertEqual(restored['lossless']['external_tracks'], before['lossless']['external_tracks'])
        self.assertEqual(restored['okk_multicam'], before['okk_multicam'])
        self.assertFalse(self.app._project_is_dirty())

    def test_bad_engine_and_widget_values_leave_current_session_untouched(self):
        self.populate()
        before = collect_state(self.app)
        for change in ('clips', 'settings', 'lossless'):
            bad = build_project_payload(self.app)
            if change == 'clips': bad['state']['okk_multicam']['project']['clips'][0]['duration'] = -1
            elif change == 'settings': bad['state']['okk_multicam']['settings']['proxy_var'] = {'bad': True}
            else: bad['state']['lossless']['segments'][0]['end'] = -1
            path = self.directory/(change+'.rtxproj')
            write_project_file(path, bad)
            self.app._project_open(path)
            self.assertEqual(collect_state(self.app), before)
        self.assertEqual(self.errors, [])

    def test_failed_atomic_save_keeps_previous_file_and_unsaved_state(self):
        ws = self.populate()
        path = self.directory/'complete.rtxproj'
        self.assertTrue(self.app._project_write(path))
        saved = path.read_bytes(); digest = self.app._project_saved_digest
        ws.project.global_grade.exposure = .75
        with patch('project_file.os.replace', side_effect=PermissionError('locked output')):
            self.assertFalse(self.app._project_write(path))
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(self.app._project_saved_digest, digest)
        self.assertTrue(self.app._project_is_dirty())
        self.assertEqual(list(self.directory.glob('*.tmp')), [])
        ws.dirty = False
        ws.fps_var.set('invalid')
        self.assertTrue(self.app._project_is_dirty())
        self.assertFalse(self.app._project_write(path))
        self.assertEqual(path.read_bytes(), saved)


if __name__ == '__main__': unittest.main(verbosity=2)
