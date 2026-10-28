"""Effect-boundary hot paths must not wait on redundant GPU commands."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from gpu_live_controller import TextureController, grade_layout


class ObservedSession:
    process = None
    executable = 'inprocess-libmpv'
    pipe_name = ''

    def __init__(self):
        self.states = {'a': {'time-pos': 12., 'seeking': False, 'pause': False},
                       'b': {'time-pos': 7., 'seeking': False, 'pause': True}}
        self.commands = []
        self.snapshots = []

    def acquire(self, slot, owner): pass
    def release(self, slot, owner): pass

    def frame_state(self, slot, **kwargs):
        self.snapshots.append(slot)
        return dict(self.states[slot])

    def request(self, op, **kwargs):
        self.commands.append((op, kwargs))
        return 'native reply'


class GpuEffectSyncTests(unittest.TestCase):
    def setUp(self):
        self.session = ObservedSession()
        self.a = TextureController(self.session, 'a', 100)
        self.b = TextureController(self.session, 'b', 101)

    def tearDown(self):
        self.a.stop()
        self.b.stop()

    def test_live_position_does_not_use_one_frame_old_observation(self):
        self.assertEqual(self.a.command(['get_property', 'time-pos']), 'native reply')
        self.assertEqual(self.session.snapshots, [])
        self.assertEqual(self.session.commands[0][1]['command'], ['get_property', 'time-pos'])

    def test_seeks_still_capture_a_render_floor_and_execute_once(self):
        self.session.states['a'].update(render_generation=41, seek_generation=8)
        self.a.command(['seek', 5., 'absolute+exact'])
        self.assertEqual(self.a._frame_floor, 41)
        self.assertEqual(self.a._seek_floor, 8)
        self.assertEqual(len(self.session.commands), 1)
        self.assertEqual(self.session.commands[0][1]['command'], ['seek', 5., 'absolute+exact'])

    def test_static_shader_layout_is_computed_once_across_effect_frames(self):
        grade_layout.cache_clear()
        try:
            with patch('gpu_live_controller.grade_uniforms', return_value=({'a': 0., 'b': 0.}, None)) as uniforms:
                first = grade_layout()
                for _ in range(120):
                    self.assertIs(grade_layout(), first)
                uniforms.assert_called_once_with(())
                self.assertEqual(first[0], ('a', 'b'))
        finally:
            grade_layout.cache_clear()


if __name__ == '__main__':
    unittest.main(verbosity=2)
