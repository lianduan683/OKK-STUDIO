"""Real player worker with an in-process two-texture renderer fixture."""
from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from cache_paths import cache_dir
from creative_core import ColorGrade, Transition, TRANSITION_KINDS
from gpu_live_controller import TextureController, grade_layout
from gpu_live_player import GpuLivePlayerMixin, LiveSide
from multicam_core import CameraClip, Cut, MulticamProject
import multicam_player as module
from multicam_player import MulticamPlayer
from program_edits import set_grade_on_range
from subtitle_core import SubtitleCue
try:
    from .test_multicam_player import _Trace, _controller_type
    from .test_gpu_live_controller import capabilities
except ImportError:
    from test_multicam_player import _Trace, _controller_type
    from test_gpu_live_controller import capabilities


class MovieBackend:
    def __init__(self, trace, controller_type, host, runtime=None):
        self.host, self.runtime = host, runtime
        self.capabilities = capabilities()
        self.controllers = {slot: controller_type('inprocess', window)
                            for slot, window in (('a', 200), ('b', 201))}
        self.generations = {slot: {'file_generation': 0, 'render_generation': 0} for slot in ('a', 'b')}
        self.grades = {}
        self.calls = []
        self.closed = 0
    def start(self): pass
    def close(self): self.closed += 1
    def request(self, payload, timeout=3.):
        op = payload['op']
        self.calls.append(copy.deepcopy(payload))
        if op == 'capabilities': return self.capabilities
        if op == 'frame_state':
            slot = payload['slot']; controller = self.controllers[slot]
            return {**self.generations[slot], 'path': str(Path(controller.path).resolve()) if controller.path else '',
                'time_pos': controller._position(), 'seeking': False, 'width': 1920, 'height': 1080, 'hwdec': 'd3d11va'}
        if op == 'command':
            slot, command = payload['slot'], payload['command']
            controller = self.controllers[slot]
            if command[0] == 'loadfile':
                controller.load(command[1]); self.generations[slot]['file_generation'] += 1
                self.generations[slot]['render_generation'] += 1
                return None
            value = controller.command(command, timeout=timeout)
            if command[0] == 'seek': self.generations[slot]['render_generation'] += 1
            return value
        if op == 'grade': self.grades[payload['slot']] = payload['values']
        if op == 'compose':
            payload['_grades'] = copy.deepcopy(self.grades)
            payload['_native_times'] = {slot: controller._position() for slot, controller in self.controllers.items()}
            self.calls[-1] = copy.deepcopy(payload)
        return None


class GpuNavigationUnitTests(unittest.TestCase):
    def fixture(self):
        class Controller:
            def start(self): pass
            def command(self,*args): pass
            def set_grades(self,grades): pass
        class Player(GpuLivePlayerMixin):
            def __init__(self):
                self._closing=threading.Event();self._position=2.;self._playing=True
                self.pending=False;self.targets=[];self.failures=[]
            def _navigation_pending(self): return self.pending
            def _clock(self): return .25  # Audio has not yet completed its seek.
            def _gpu_pause(self,*args,**kwargs): pass
            def _set_picture_speed(self,*args): pass
            def _catch_up_picture(self,*args): pass
            def _gpu_failover(self,*args): self.failures.append(args)
            def _prepare_picture_frame(self,slot,target,**kwargs):
                self.targets.append(target);return target
        slot=SimpleNamespace(key=('source',),failed_key=None,active=True,controller=Controller())
        side=LiveSide(1,'source','source.mp4',('source',),.5,2.)
        return Player(),slot,side

    def test_force_seek_keeps_requested_time_despite_old_audio_clock(self):
        player,slot,side=self.fixture()
        self.assertTrue(player._gpu_ensure_side(slot,side,2.,force=True,grades=()))
        self.assertEqual(player.targets,[1.5])

    def test_superseded_wait_does_not_mark_gpu_or_source_failed(self):
        player,slot,side=self.fixture()
        def superseded(*args,**kwargs):
            player.pending=True
            raise RuntimeError('newer seek is waiting')
        player._prepare_picture_frame=superseded
        self.assertFalse(player._gpu_ensure_side(slot,side,2.,force=True,grades=()))
        self.assertIsNone(slot.failed_key)
        self.assertEqual(player.failures,[])

    def test_failed_gpu_stop_keeps_session_owner_for_shutdown_retry(self):
        player,slot,side=self.fixture();player._init_gpu_live()
        calls=[]
        class Controller:
            release_only=True
            def stop(self):calls.append('texture');raise RuntimeError('texture release failed')
        class Session:
            failed=True
            def close(self):
                calls.append('session')
                if self.failed:raise RuntimeError('render still closing')
        session=Session();player._gpu_session=session
        player._program=SimpleNamespace(controller=Controller())
        player._program_standby=SimpleNamespace(controller=Controller())
        with self.assertRaisesRegex(RuntimeError,'render still closing'):player._gpu_stop()
        self.assertEqual(calls,['texture','texture','session'])
        self.assertIs(player._gpu_session,session)
        # Failover is allowed to replace both slots; the native owner survives.
        player._program=player._program_standby=None;session.failed=False
        player._gpu_stop()
        self.assertIsNone(player._gpu_session);self.assertEqual(calls[-1],'session')


class GpuLiveWorkerTests(unittest.TestCase):
    def setUp(self):
        self.trace = _Trace()
        base = _controller_type(self.trace)
        class Controller(base):
            def __init__(self, executable, window, *, allow_software_decode=False):
                self.allow_software_decode = allow_software_decode
                super().__init__(executable, window)
            def wait_for_frame(self, target, **kwargs): return self._position()
        self.controller_type = Controller
        self.backends = []
        def factory(host, runtime=None):
            value = MovieBackend(self.trace, Controller, host, runtime)
            self.backends.append(value)
            return value
        self.patches = [patch.object(module, 'MpvController', Controller),
                        patch.object(module, '_AudioController', Controller),
                        patch.object(MulticamPlayer, 'TICK', .01)]
        for item in self.patches: item.start()
        self.events = queue.Queue(); self.received = []
        self.player = MulticamPlayer('mpv', {1: 101, 2: 102}, 200, 300, self.events,
            program_standby_window=201, comparison_window=400, comparison_standby_window=401,
            gpu_live=True, gpu_session_factory=factory, gpu_output_window=555)
        self.project = MulticamProject(
            clips=[CameraClip('a', 1, 'camera-a.mp4', -1., 4.),
                   CameraClip('b', 2, 'camera-b.mp4', -.2, 3.2)],
            cuts=[Cut(0., 1), Cut(.7, 2)], fps=30., master_audio='master.wav', master_duration=3.,
            transitions=[Transition(.7, 'fade', .6)])
        self.player.submit('configure', project=self.project)
        self.wait(lambda: self.player._gpu_live_enabled and self.player._gpu_context is not None
                  and all(slot.active for slot in (self.player._program, self.player._program_standby)))

    def tearDown(self):
        self.player.close()
        stopped = self.player.wait_closed(3.)
        for item in reversed(self.patches): item.stop()
        self.assertTrue(stopped, 'GPU player worker leaked')
        self.assertTrue(all(backend.closed == 1 for backend in self.backends))

    def drain(self):
        while True:
            try:
                kind, value = self.events.get_nowait()
                self.received.append((kind, value))
                if kind == 'program_swap': self.player.submit('program_swap_ack', window=value)
            except queue.Empty: break

    def wait(self, predicate, timeout=3.):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            self.drain()
            if predicate(): return
            time.sleep(.003)
        self.fail('GPU player did not reach expected state: '+str(self.received[-5:]))

    def test_normal_transition_uses_two_running_sources_and_exit_has_no_load_or_seek(self):
        backend = self.backends[0]
        self.assertEqual(backend.host, 555)
        self.assertIsInstance(self.player._program.controller, TextureController)
        self.assertNotIsInstance(self.player._slots[1].controller, TextureController)
        self.player.submit('play')
        self.wait(lambda: any(kind == 'live_transition' and value.get('phase') == 'mixing'
                             for kind, value in self.received))
        before = len(backend.calls); trace_before = len(self.trace.snapshot())
        self.assertFalse(backend.controllers['a'].paused)
        self.assertFalse(backend.controllers['b'].paused)
        self.wait(lambda: self.player._position >= 1.15 and self.player._gpu_context is None)
        self.assertEqual(self.player._program.controller.slot, 'b')
        self.assertEqual(self.player._program.controller.command(['get_property', 'time-pos']) > 1.2, True)
        self.assertFalse(any(call['op'] == 'command' and call['command'][0] in ('seek', 'loadfile')
                             for call in backend.calls[before:]))
        audio = [value for kind, window, value, _ in self.trace.snapshot()[trace_before:] if kind == 'command' and window == 300]
        self.assertFalse(any(value[0] in ('seek', 'loadfile', 'stop') or value == ('set_property', 'pause', True) for value in audio))
        self.assertFalse(any(kind == 'program_swap' for kind, _ in self.received))

    def test_armed_compositor_runs_independently_from_clock_anchor(self):
        self.player.submit('play')
        backend = self.backends[0]
        self.wait(lambda: any(call['op'] == 'compose' and call.get('active') and
                             call.get('start_delay', 0.) > 0. for call in backend.calls))
        prepared = next(call for call in reversed(backend.calls) if call['op'] == 'compose' and call.get('start_delay', 0.) > 0.)
        self.assertAlmostEqual(prepared['progress_rate'], 1/.6)
        self.assertEqual(prepared['progress'], 0.)

    def test_plain_cut_reuses_preloaded_frame_and_restarts_its_clock(self):
        self.project.transitions = []
        self.player.submit('configure', project=self.project)
        self.wait(lambda: self.player._gpu_context is None and
                  self.player._program_standby.prewarm_target is not None)
        self.assertFalse(self.player._program_standby.active)
        backend = self.backends[0]
        before = len(backend.calls)
        self.player.submit('play')
        self.wait(lambda: self.player._position >= .9 and self.player._program.controller.slot == 'b')
        self.assertTrue(self.player._program.active)
        self.assertFalse(backend.controllers['b'].paused)
        self.assertGreater(backend.controllers['b']._position(), 1.)
        self.assertFalse(any(call['op'] == 'command' and call['slot'] == 'b' and
                             call['command'][0] in ('loadfile', 'seek') for call in backend.calls[before:]))

    def test_force_seek_before_unarmed_window_seeks_each_source_once(self):
        self.project.clips[0].duration = 12.
        self.project.clips[1].duration = 12.
        self.project.master_duration = 10.
        self.project.cuts = [Cut(0., 1), Cut(7., 2)]
        self.project.transitions = [Transition(7., 'fade', .6)]
        self.player.submit('configure', project=self.project)
        self.wait(lambda: self.player._gpu_context is None and self.player._project.master_duration == 10.)
        backend = self.backends[0]; before = len(backend.calls)
        self.player.submit('seek', time=4.)
        self.wait(lambda: any(call['op'] == 'compose' and call.get('active') and
                             call.get('progress') == 0. for call in backend.calls[before:]))
        seeks = [call['slot'] for call in backend.calls[before:]
                 if call['op'] == 'command' and call['command'][0] == 'seek']
        self.assertEqual(sorted(seeks), ['a', 'b'])

    def test_future_incoming_source_holds_zero_until_its_actual_start(self):
        self.project.clips[1].start = .25
        self.player.submit('configure', project=self.project)
        backend = self.backends[0]
        self.wait(lambda: self.player._gpu_context is not None and
                  self.player._gpu_context['plan'].incoming.source_epoch == .25)
        self.player.submit('play')
        self.wait(lambda: self.player._playing and self.player._position >= .08)
        self.assertTrue(backend.controllers['b'].paused)
        self.assertAlmostEqual(backend.controllers['b']._position(), 0.)
        self.wait(lambda: self.player._position >= .32)
        self.assertFalse(backend.controllers['b'].paused)
        self.assertGreater(backend.controllers['b']._position(), .04)
        self.assertTrue(any(call['op'] == 'compose' and call.get('active') and
                            0. < call.get('start_delay', 0.) < .16 for call in backend.calls))

    def test_deliberate_programme_gap_requests_black_instead_of_last_texture(self):
        self.project.transitions = []
        self.project.clips[1].start = 1.2
        self.player.submit('configure', project=self.project)
        backend = self.backends[0]; before = len(backend.calls)
        self.player.submit('seek', time=1.)
        self.wait(lambda: any(call['op'] == 'compose' and call.get('blank') for call in backend.calls[before:]))
        self.assertFalse(self.player._program.active)
        last = next(call for call in reversed(backend.calls) if call['op'] == 'compose')
        self.assertTrue(last['blank'])
        self.assertFalse(last['active'])

    def test_pause_and_frame_step_inside_mix_seek_both_sources_exactly_once(self):
        backend = self.backends[0]
        before = len(backend.calls)
        self.player.submit('seek', time=.7)
        self.wait(lambda: any(call['op'] == 'compose' and call.get('active') and call.get('progress') == .5
                             for call in backend.calls[before:]))
        seeks = [(call['slot'], call['command'][1]) for call in backend.calls[before:]
                 if call['op'] == 'command' and call['command'][0] == 'seek']
        self.assertEqual(len(seeks), 2)
        self.assertEqual(set(slot for slot, _ in seeks), {'a', 'b'})
        for slot, target in seeks:
            self.assertAlmostEqual(target, 1.7 if slot == 'a' else .9)
        self.assertFalse(self.player._playing)
        self.assertTrue(all(controller.paused for controller in backend.controllers.values()))
        self.assertTrue(all(slot.playback_speed == 1. for slot in (self.player._program, self.player._program_standby)))

    def test_same_camera_cut_keeps_side_grades_and_selection_draft_independent(self):
        self.project.cuts = [Cut(0., 1)]
        self.project.edit_points = [.7]
        set_grade_on_range(self.project, 0., .7, ColorGrade(exposure=-.3))
        set_grade_on_range(self.project, .7, 3., ColorGrade(exposure=.6))
        self.player.submit('configure', project=self.project)
        self.player.submit('seek', time=.6)
        backend = self.backends[0]; index = grade_layout()[0].index('g1_exposure')
        self.wait(lambda: backend.grades.get('a', [0.]*612)[index] == -.3 and
                  backend.grades.get('b', [0.]*612)[index] == .6 and
                  any(call['op'] == 'compose' and call.get('active') and
                      abs(call.get('progress', 0.)-1/3) < 1e-6 for call in backend.calls))
        before = len(backend.calls)
        self.player.submit('grade_preview', target=('selection', 0., .7), grade=ColorGrade(exposure=1.2))
        self.wait(lambda: backend.grades['a'][index] == 1.2)
        self.assertEqual(backend.grades['b'][index], .6)
        self.assertFalse(any(call['op'] == 'command' and call['command'][0] in ('seek', 'loadfile')
                             for call in backend.calls[before:]))

    def test_comparison_uses_original_mpv_controller_and_bypasses_live_mix(self):
        self.player.submit('seek', time=.7)
        self.wait(lambda: self.player._gpu_context is not None)
        self.player.submit('stabilization_compare', enabled=True)
        self.wait(lambda: self.player._comparison_enabled and self.player._comparison.active
                  and self.player._gpu_context is None)
        self.assertNotIsInstance(self.player._comparison.controller, TextureController)
        self.assertIsInstance(self.player._program.controller, TextureController)

    def test_leaving_rendered_excerpt_restores_live_transition(self):
        with tempfile.TemporaryDirectory(dir=cache_dir('tests')) as folder:
            path = Path(folder)/'excerpt.mp4'; path.write_bytes(b'excerpt')
            self.player.submit('render_preview', path=str(path), start=0., end=.2, play=False)
            self.wait(lambda: self.player._gpu_context is None and self.player._program.key[0] == 'render-preview')
            self.player.submit('seek', time=.7)
            self.wait(lambda: self.player._gpu_context is not None and
                      self.player._gpu_context['plan'].contains(self.player._position) and
                      not self.player._is_baked_key(self.player._program.key))
            self.assertIsNotNone(self.player._render_preview)
            self.assertFalse(self.player._is_baked_key(self.player._program.key))

    def test_missing_subtitle_capability_falls_back_explicitly_without_dropping_track(self):
        self.project.subtitles.cues = [SubtitleCue('s', 0., 2., '字幕')]
        self.player.submit('configure', project=self.project)
        self.wait(lambda: not self.player._gpu_live_enabled and self.player._program.active)
        self.assertFalse(isinstance(self.player._program.controller, TextureController))
        self.assertTrue(any(kind == 'gpu_live_state' and not value['enabled'] and '字幕' in value['reason']
                            for kind, value in self.received))
        self.assertEqual(self.player._project.subtitles.cues[0].text, '字幕')

    def test_draft_lut_uses_explicit_fallback_and_keeps_the_draft(self):
        with tempfile.TemporaryDirectory(dir=cache_dir('tests')) as folder:
            path = Path(folder)/'identity.cube'
            path.write_text('LUT_3D_SIZE 2\n0 0 0\n1 0 0\n0 1 0\n1 1 0\n0 0 1\n1 0 1\n0 1 1\n1 1 1\n', encoding='utf-8')
            self.player.submit('grade_preview', target=('global',), grade=ColorGrade(lut_path=str(path)))
            self.wait(lambda: not self.player._gpu_live_enabled and self.player._program.active)
            self.assertEqual(self.player._grade_draft[1].lut_path, str(path))
            self.assertTrue(any(kind == 'gpu_live_state' and not value['enabled'] and
                                'LUT 调色预览' in value['reason'] for kind, value in self.received))

    def test_runtime_decoder_failure_switches_to_compatible_player_without_touching_master(self):
        self.player.submit('play')
        self.wait(lambda: self.player._playing and self.player._position > .08)
        backend = self.backends[0]
        before = len(self.trace.snapshot())
        original_load = TextureController.load
        def load(controller, path, timeout=8.):
            if path == 'unsupported-c.mp4': raise RuntimeError('unsupported D3D11VA camera profile')
            return original_load(controller, path, timeout)
        with patch.object(TextureController, 'load', new=load):
            self.project.clips[1].path = 'unsupported-c.mp4'
            self.player.submit('configure', project=self.project, gpu_live=True)
            self.wait(lambda: not self.player._gpu_live_enabled and self.player._program.active and
                      self.player._program.controller.allow_software_decode)
            self.assertTrue(self.player._playing)
            self.assertGreater(self.player._position, .08)
            self.wait(lambda: self.player._position > .85 and self.player._program.controller.path == 'unsupported-c.mp4')
            self.assertEqual(backend.closed, 1)
            self.assertTrue(self.player._gpu_failure_reason)
            self.assertTrue(any(kind == 'gpu_live_state' and not value['enabled'] and
                                'D3D11VA camera profile' in value['reason'] for kind, value in self.received))
            self.player.submit('configure', project=self.project, gpu_live=True)
            self.player.submit('configure', project=self.project, gpu_live=True)
            self.wait(lambda: self.player._position > 1.)
            self.assertEqual(len(self.backends), 1, 'ordinary edits must not recreate the failed native renderer')
        audio = [value for kind, window, value, _ in self.trace.snapshot()[before:] if kind == 'command' and window == 300]
        self.assertFalse(any(value[0] in ('seek', 'loadfile', 'stop') or value == ('set_property', 'pause', True) for value in audio))
        self.assertFalse(self.player._slots[1].controller.allow_software_decode)

    def test_frame_timeout_keeps_pause_and_exact_position_and_explicit_toggle_can_retry(self):
        original = TextureController.wait_for_frame
        def wait_for_frame(controller, target, **kwargs):
            if controller.slot == 'b': raise RuntimeError('fresh render frame timeout')
            return original(controller, target, **kwargs)
        with patch.object(TextureController, 'wait_for_frame', new=wait_for_frame):
            self.project.clips[1].path = 'new-camera-b.mp4'
            self.player.submit('configure', project=self.project, gpu_live=True)
            self.wait(lambda: not self.player._gpu_live_enabled and self.player._program.active)
        self.assertFalse(self.player._playing)
        self.assertEqual(self.player._position, 0.)
        self.assertTrue(self.player._program.controller.allow_software_decode)
        self.player.submit('configure', project=self.project, gpu_live=False)
        self.player.submit('configure', project=self.project, gpu_live=True)
        self.wait(lambda: self.player._gpu_live_enabled and self.player._gpu_context is not None and
                  self.player._program_standby.active)
        self.assertEqual(len(self.backends), 2)
        self.assertEqual(self.player._gpu_failure_reason, '')
        self.assertFalse(self.player._gpu_software_fallback)

    def test_plain_cut_background_load_failure_also_disables_native_path(self):
        original_load = TextureController.load
        def load(controller, path, timeout=8.):
            if path == 'unsupported-c.mp4': raise RuntimeError('background decoder failed')
            return original_load(controller, path, timeout)
        with patch.object(TextureController, 'load', new=load):
            self.project.transitions = []
            self.project.clips[1].path = 'unsupported-c.mp4'
            self.player.submit('configure', project=self.project, gpu_live=True)
            self.wait(lambda: not self.player._gpu_live_enabled and self.player._program.active)
        self.assertIn('background decoder failed', self.player._gpu_failure_reason)
        self.assertTrue(self.player._program.controller.allow_software_decode)
        self.assertFalse(self.player._playing)


if __name__ == '__main__': unittest.main(verbosity=2)
