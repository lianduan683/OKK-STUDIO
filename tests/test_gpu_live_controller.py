"""In-process renderer bridge and independent texture-resource leases."""
from __future__ import annotations

from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from creative_core import ColorGrade, TRANSITION_KINDS
from gpu_live_controller import GpuLiveSession, TextureController, grade_layout, validate_capabilities
from realtime_grade import grade_uniforms


def capabilities(**changes):
    names, digest = grade_layout()
    return {'protocol': 1, 'slots': ['a', 'b'], 'transitions': list(TRANSITION_KINDS),
            'grade_uniforms': True, 'grade_count': len(names), 'grade_layout_hash': digest,
            'lut': False, 'subtitles': False, **changes}


class FakeBackend:
    def __init__(self):
        self.capabilities = capabilities()
        self.calls = []
        self.close_count = 0
        self.states = {slot: {'path': '', 'file_generation': 0, 'render_generation': 0,
                              'time_pos': 0., 'seeking': False, 'width': 1920, 'height': 1080,
                              'hwdec': 'd3d11va'} for slot in ('a', 'b')}
    def start(self): pass
    def close(self): self.close_count += 1
    def request(self, payload, *, timeout=3.):
        op = payload['op']
        parameters = {key: value for key, value in payload.items() if key not in ('op', 'request_id')}
        self.calls.append((op, parameters))
        if op == 'capabilities': return self.capabilities
        if op == 'frame_state': return dict(self.states[parameters['slot']])
        if op == 'command':
            state = self.states[parameters['slot']]
            command = parameters['command']
            if command[0] == 'loadfile':
                state.update(path=str(Path(command[1]).resolve()), time_pos=0.)
                state['file_generation'] += 1; state['render_generation'] += 1
            elif command[0] == 'seek':
                state['time_pos'] = command[1]; state['render_generation'] += 1
            elif command[:2] == ['get_property', 'time-pos']: return state['time_pos']
        return None


class FakeSession(GpuLiveSession):
    def __init__(self):
        self.backend = FakeBackend()
        super().__init__(123, session_factory=lambda *args, **kwargs: self.backend)
        self.calls = self.backend.calls
        self.states = self.backend.states


class GpuProtocolTests(unittest.TestCase):
    def test_visibility_is_forwarded_without_releasing_decoders(self):
        session=FakeSession();session.start();backend=session._backend
        session.set_visible(False);session.set_visible(True)
        self.assertEqual(session.calls[-2:],[('set_visible',{'visible':False}),('set_visible',{'visible':True})])
        self.assertIs(session._backend,backend);self.assertEqual(backend.close_count,0)
        with self.assertRaises(ValueError):session.set_visible(1)
        session.close()

    def test_capability_layout_checks_count_names_hash_and_version(self):
        self.assertEqual(validate_capabilities(capabilities())['protocol'], 1)
        self.assertEqual(len(grade_layout()[0]), 612)
        for changes in ({'protocol': 2}, {'slots': ['a']}, {'grade_count': 611},
                        {'grade_layout_hash': 'incorrect'}, {'grade_fields': list(reversed(grade_layout()[0]))}):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                validate_capabilities(capabilities(**changes))

    def test_request_uses_inprocess_backend_and_unique_ids_without_process_launch(self):
        session = FakeSession()
        with patch('subprocess.Popen') as launch:
            session.start()
            session.request('command', slot='a', command=['get_property', 'time-pos'])
            session.request('command', slot='b', command=['get_property', 'time-pos'])
            session.close()
        launch.assert_not_called()
        self.assertIsNone(session.process)
        self.assertEqual(session._request_id, 3)
        self.assertEqual([op for op, _ in session.calls], ['capabilities', 'command', 'command'])

    def test_core_future_timeout_never_resends_a_seek(self):
        session = FakeSession(); session.start()
        with patch.object(session.backend, 'request', side_effect=TimeoutError('deadline')) as request:
            with self.assertRaisesRegex(RuntimeError, '未重复'):
                session.request('command', slot='b', command=['seek', 4., 'absolute+exact'], timeout=.01)
        request.assert_called_once()
        self.assertEqual(request.call_args.kwargs, {'timeout': .01})

    def test_native_errors_are_returned_as_errors_not_successful_data(self):
        session = FakeSession(); session.start()
        with patch.object(session.backend, 'request', side_effect=RuntimeError('unsupported LUT')):
            with self.assertRaisesRegex(RuntimeError, 'unsupported LUT'):
                session.request('grade', slot='a', values=[])

    def test_failed_close_keeps_backend_available_for_retry(self):
        session=FakeSession();session.start()
        with patch.object(session.backend,'close',side_effect=RuntimeError('close pending')):
            with self.assertRaisesRegex(RuntimeError,'close pending'):session.close()
        self.assertIs(session._backend,session.backend)
        session.close()
        self.assertIsNone(session._backend)


class TextureControllerTests(unittest.TestCase):
    def setUp(self):
        self.session = FakeSession()
        self.a = TextureController(self.session, 'a', 123)
        self.b = TextureController(self.session, 'b', 124)
    def tearDown(self): self.a.stop(); self.b.stop(); self.session.close()

    def test_stopping_one_channel_keeps_other_and_last_lease_closes_renderer_once(self):
        self.a.start(); self.b.start()
        self.assertIsNone(self.a.process)
        self.a.stop()
        self.assertEqual(self.session.backend.close_count, 0)
        self.b.command(['get_property', 'time-pos'])
        self.b.stop(); self.b.stop()
        self.assertEqual(self.session.backend.close_count, 1)

    def test_new_path_generation_and_fresh_seek_frame_are_required(self):
        self.a.load('camera-a.mp4')
        self.a.command(['seek', 3.25, 'absolute+exact'])
        self.assertEqual(self.a.wait_for_frame(3.25), 3.25)
        self.assertEqual(self.session.states['b']['path'], '')
        self.a._frame_floor = self.session.states['a']['render_generation']
        with self.assertRaisesRegex(RuntimeError, '新画面超时'):
            self.a.wait_for_frame(3.25, timeout=.015)

    def test_software_decode_never_satisfies_hardware_load(self):
        self.session.states['a']['hwdec'] = 'no'
        with self.assertRaisesRegex(RuntimeError, '未使用 CPU'):
            self.a.load('camera-a.mp4', timeout=.015)

    def test_seek_wait_rejects_old_redraw_unfinished_seek_and_unpresented_frame(self):
        self.a.load('camera-a.mp4')
        state=self.session.states['a']
        state.update(seek_generation=3,restart_generation=3,seek_render_floor=0,
                     presented_render_generation=state['render_generation'])
        self.a.command(['seek', 3.25, 'absolute+exact'])
        # Fake command updated playback time and redraw count only. The old
        # implementation accepted this despite no completed decoder seek.
        with self.assertRaisesRegex(RuntimeError,'新画面超时'):
            self.a.wait_for_frame(3.25,timeout=.015)
        state.update(seek_generation=4,restart_generation=4,
                     seek_render_floor=self.a._frame_floor)
        with self.assertRaisesRegex(RuntimeError,'新画面超时'):
            self.a.wait_for_frame(3.25,timeout=.015)
        state['presented_render_generation']=state['render_generation']
        self.assertEqual(self.a.wait_for_frame(3.25),3.25)

    def test_frame_step_does_not_accept_three_frames_of_position_error(self):
        self.a.load('camera-a.mp4')
        self.a.command(['seek', 1., 'absolute+exact'])
        self.session.states['a'].update(time_pos=.9, **{'container-fps':30.})
        with self.assertRaisesRegex(RuntimeError,'新画面超时'):
            self.a.wait_for_frame(1.,timeout=.015)

    def test_superseded_seek_wait_cancels_promptly(self):
        self.a.load('camera-a.mp4')
        cancel=threading.Event();cancel.set()
        with self.assertRaisesRegex(RuntimeError,'关闭'):
            self.a.wait_for_frame(5.,timeout=4.,cancel_event=cancel)

    def test_superseded_load_does_not_wait_for_hardware_decode_timeout(self):
        self.a.start()
        self.a.operation_cancel_event=threading.Event()
        original=self.session.frame_state
        def state(*args,**kwargs):
            result=original(*args,**kwargs)
            if result['file_generation']:
                result['hwdec']='no'
                self.a.operation_cancel_event.set()
            return result
        with patch.object(self.session,'frame_state',side_effect=state):
            with self.assertRaisesRegex(RuntimeError,'加载已取消'):
                self.a.load('camera-a.mp4',timeout=8.)
        self.a.operation_cancel_event=None

    def test_grade_opts_are_validated_flattened_once_without_glsl_install(self):
        self.a.start()
        values, _ = grade_uniforms([ColorGrade(exposure=.5)])
        self.a.command(['set_property', 'glsl-shader-opts', values])
        self.a.command(['set_property', 'glsl-shader-opts', values])
        self.a.command(['change-list', 'glsl-shaders', 'append', 'not-loaded.glsl'])
        calls = [(op, data) for op, data in self.session.calls if op == 'grade']
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0][1]['values']), 612)
        self.assertEqual(calls[0][1]['layout_hash'], grade_layout()[1])
        with self.assertRaises(ValueError): self.session.grade('a', dict(reversed(list(values.items()))))

    def test_subtitle_and_lut_are_explicitly_rejected_when_native_lacks_support(self):
        self.a.start()
        with self.assertRaisesRegex(RuntimeError, '字幕'):
            self.session.compose(front_slot='a', other_slot='b', active=True,
                                 subtitle={'cues': [{'text': '字幕'}]})
        with self.assertRaisesRegex(RuntimeError, 'LUT'):
            self.a.set_grades([ColorGrade(lut_path='style.cube')])
        self.assertFalse(any(op in ('grade', 'compose') for op, _ in self.session.calls))

    def test_nine_kinds_share_sources_and_only_send_compose_parameters(self):
        self.a.load('camera-a.mp4'); self.b.load('camera-b.mp4')
        before = len(self.session.calls)
        for kind in TRANSITION_KINDS:
            self.session.compose(front_slot='a', other_slot='b', kind=kind, progress=.5, active=True)
        self.assertEqual([op for op, _ in self.session.calls[before:]], ['compose']*9)


if __name__ == '__main__': unittest.main(verbosity=2)
