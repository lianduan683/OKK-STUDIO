"""Deterministic transport timing and isolated Tk keyboard/seek regressions.

No real MPV, native keyboard injection or user application is used here.
"""
from __future__ import annotations

import heapq
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from multicam_core import CameraClip, Cut, MulticamProject
from transport_controls import TransportControlsMixin


class RecordingPlayer:
    def __init__(self):
        self.commands = []
        self.closed = False

    def submit(self, action, **values):
        self.commands.append((action, values))

    def close(self):
        self.closed = True

    @property
    def scrubs(self):
        return [values for action, values in self.commands if action == 'scrub']


class TransportHarness(TransportControlsMixin):
    def __init__(self, fps=30.):
        self.now = 10.
        self.pending = []
        self.cancelled = set()
        self.sequence = 0
        self.current = 5.
        self.playing = True
        self.alive = True
        self.busy = False
        self.app = SimpleNamespace(working=False)
        self.project = MulticamProject(clips=[CameraClip('one', 1, 'one.mp4', 0., 20.)],
                                       cuts=[Cut(0., 1)], fps=fps)
        self.player = RecordingPlayer()
        self._show_time = Mock()
        self.draw_playhead = Mock()
        self.status = Mock()
        self._init_transport_controls()

    def after(self, delay, callback, *args):
        self.sequence += 1
        token = f'after-{self.sequence}'
        heapq.heappush(self.pending, (self.now + delay / 1000., self.sequence, token, callback, args))
        return token

    def after_idle(self, callback, *args):
        return self.after(0, callback, *args)

    def after_cancel(self, token):
        self.cancelled.add(token)

    def advance(self, seconds):
        end = self.now + seconds
        count = 0
        while self.pending and self.pending[0][0] <= end + 1e-9:
            due, _, token, callback, args = heapq.heappop(self.pending)
            self.now = due
            if token not in self.cancelled:
                callback(*args)
                count += 1
                if count > 1000:
                    raise AssertionError('transport timer is scheduling without advancing time')
        self.now = end

    def _available(self):
        return not self.busy and not self.app.working

    def winfo_ismapped(self):
        return True

    def focus_get(self):
        return None

    def pause(self):
        self.player.submit('pause')
        self.playing = False

    def seek(self, value):
        return self._request_transport_seek(value)


class TransportTimingTests(unittest.TestCase):
    def setUp(self):
        self.ws = TransportHarness()
        self.clock = patch('transport_controls.time.monotonic', side_effect=lambda: self.ws.now)
        self.clock.start()

    def tearDown(self):
        self.ws._stop_transport_hold()
        self.clock.stop()

    def test_first_press_pauses_and_steps_one_frame_in_one_atomic_request(self):
        for direction in (-1, 1):
            with self.subTest(direction=direction):
                self.ws._reset_transport_controls()
                self.ws.current = 5.
                self.ws.playing = True
                self.ws.player.commands.clear()
                self.ws._transport_key_press(direction)
                self.assertAlmostEqual(self.ws.current, 5. + direction / 30.)
                self.assertFalse(self.ws.playing)
                self.assertEqual(len(self.ws.player.commands), 1)
                action, values = self.ws.player.commands[-1]
                self.assertEqual(action, 'scrub')
                self.assertTrue(values['pause'])
                self.assertAlmostEqual(values['time'], self.ws.current)
                self.assertIsInstance(values['request_id'], int)
                self.ws._stop_transport_hold()

    def test_frame_step_is_precise_at_integer_and_fractional_rates(self):
        for fps in (30., 30000 / 1001., 60.):
            with self.subTest(fps=fps):
                self.ws.project.fps = fps
                self.ws.current = 90 / fps
                self.ws.step_frame(-1)
                self.assertAlmostEqual(self.ws.current, 89 / fps, places=10)
                self.ws.step_frame(1)
                self.assertAlmostEqual(self.ws.current, 90 / fps, places=10)
                self.assertFalse(self.ws.playing)

    def test_system_repeat_is_ignored_and_short_tap_does_not_start_jog(self):
        self.ws._transport_key_press(1)
        first = self.ws.current
        for _ in range(12):
            self.ws._transport_key_press(1)
        self.ws.advance(.249)
        self.assertEqual(self.ws.current, first)
        self.assertEqual(len(self.ws.player.scrubs), 1)
        self.ws._transport_key_release(SimpleNamespace(keysym='Right'))
        self.ws.advance(2.)
        self.assertEqual(self.ws.current, first)
        self.assertFalse(self.ws.playing)

    def test_both_long_hold_directions_follow_wall_clock_and_release_stops(self):
        for direction, key in ((-1, 'Left'), (1, 'Right')):
            with self.subTest(direction=direction):
                self.ws._reset_transport_controls()
                self.ws.current = 5.
                self.ws._transport_key_press(direction)
                self.ws.advance(1.25)
                earlier = self.ws.current
                self.ws.advance(1.)
                later = self.ws.current
                self.assertAlmostEqual((later - earlier) * direction, 1., delta=2 / self.ws.project.fps)
                self.assertAlmostEqual(later * self.ws.project.fps, round(later * self.ws.project.fps))
                self.ws._transport_key_release(SimpleNamespace(keysym=key))
                stopped = self.ws.current
                request_count = len(self.ws.player.scrubs)
                self.ws.advance(3.)
                self.assertEqual(self.ws.current, stopped)
                self.assertEqual(len(self.ws.player.scrubs), request_count)
                self.assertFalse(self.ws.playing)
                self.assertTrue(all(values['pause'] for values in self.ws.player.scrubs))

    def test_releasing_previous_direction_does_not_cancel_new_direction(self):
        self.ws._transport_key_press(-1)
        self.ws._transport_key_press(1)
        start = self.ws.current
        self.ws._transport_key_release(SimpleNamespace(keysym='Left'))
        self.ws.advance(.8)
        self.assertGreater(self.ws.current, start)
        self.ws._transport_key_release(SimpleNamespace(keysym='Right'))
        stopped = self.ws.current
        self.ws.advance(1.)
        self.assertEqual(self.ws.current, stopped)

    def test_steps_and_hold_clamp_at_project_boundaries(self):
        self.ws.current = 0.
        self.ws._transport_key_press(-1)
        self.ws.advance(1.)
        self.assertEqual(self.ws.current, 0.)
        self.ws._reset_transport_controls()
        self.ws.current = self.ws.project.duration
        self.ws._transport_key_press(1)
        self.ws.advance(1.)
        self.assertLessEqual(self.ws.current, self.ws.project.duration)
        self.assertGreaterEqual(self.ws.current, self.ws.project.duration - 1 / self.ws.project.fps)
        self.assertTrue(all(0. <= value['time'] <= self.ws.project.duration for value in self.ws.player.scrubs))

    def test_latest_seek_ack_filters_stale_and_unversioned_state(self):
        self.ws._request_transport_seek(2.)
        first = self.ws.player.scrubs[-1]['request_id']
        self.ws._request_transport_seek(7.)
        second = self.ws.player.scrubs[-1]['request_id']
        self.assertGreater(second, first)
        self.assertFalse(self.ws._accept_transport_state({'time': 2., 'playing': True, 'request_id': first}))
        self.assertFalse(self.ws._accept_transport_state({'time': 1., 'playing': True}))
        self.assertEqual(self.ws.current, 7.)
        self.assertFalse(self.ws.playing)
        self.assertTrue(self.ws._accept_transport_state({'time': 7., 'playing': False, 'request_id': second}))
        self.assertFalse(self.ws._accept_transport_state({'time': 2., 'playing': False, 'request_id': first}))

    def test_reset_and_close_cancel_pending_hold_callbacks(self):
        self.ws._transport_key_press(1)
        self.ws._reset_transport_controls()
        self.ws.current = 0.
        count = len(self.ws.player.scrubs)
        self.ws.advance(2.)
        self.assertEqual(self.ws.current, 0.)
        self.assertEqual(len(self.ws.player.scrubs), count)
        self.ws._transport_key_press(1)
        self.ws.alive = False
        self.ws._stop_transport_hold()
        stopped = self.ws.current
        count = len(self.ws.player.scrubs)
        self.ws.advance(2.)
        self.assertEqual(self.ws.current, stopped)
        self.assertEqual(len(self.ws.player.scrubs), count)


class TransportTkTests(unittest.TestCase):
    def setUp(self):
        try:
            from . import test_timeline_drag_merge as fixture
        except ImportError:
            import test_timeline_drag_merge as fixture
        self.fixture = fixture
        fixture.TimelineDropTests.setUp(self)
        self.workspace.player = RecordingPlayer()

    def tearDown(self):
        self.fixture.TimelineDropTests.tearDown(self)

    def test_keyboard_release_is_bound_and_text_fields_keep_cursor_keys(self):
        import tkinter as tk
        from tkinter import ttk
        ws = self.workspace
        self.assertTrue(self.root.bind('<KeyRelease>'))
        ws.current = 2.
        for widget in (tk.Entry(ws), ttk.Combobox(ws), tk.Text(ws), tk.Spinbox(ws)):
            with self.subTest(widget=widget.winfo_class()):
                ws._key(SimpleNamespace(widget=widget, state=0, char='', keysym='Right'))
                self.assertEqual(ws.current, 2.)
                self.assertEqual(ws.player.scrubs, [])
                widget.destroy()
        ws._key(SimpleNamespace(widget=ws.timeline, state=0, char='', keysym='Left'))
        self.assertAlmostEqual(ws.current, 2. - 1 / ws.project.fps)
        self.assertFalse(ws.playing)
        ws._transport_key_release(SimpleNamespace(widget=ws.timeline, keysym='Left'))
        self.assertEqual(self.errors, [])

    def test_poll_does_not_overwrite_new_seek_with_old_playing_state(self):
        ws = self.workspace
        ws.seek(2.)
        older = ws.player.scrubs[-1]['request_id']
        ws.seek(7.)
        newest = ws.player.scrubs[-1]['request_id']
        ws.player_queue.put(('state', {'time': 2., 'playing': True, 'camera': 1, 'request_id': older}))
        ws._poll()
        self.assertEqual(ws.current, 7.)
        self.assertFalse(ws.playing)
        ws.player_queue.put(('state', {'time': 7., 'playing': False, 'camera': 1, 'request_id': newest}))
        ws._poll()
        self.assertEqual(ws.current, 7.)
        self.assertEqual(self.errors, [])

    def test_ruler_mouse_drag_scrubs_and_release_finishes_without_editing_sources(self):
        import copy
        ws = self.workspace
        canvas = ws.timeline
        source = copy.deepcopy(ws.project)
        original_undo = len(ws.undo_stack)
        y = max(1, round(ws._px(12) - canvas.canvasy(0)))
        positions = [2., 4., 6.]
        for index, position in enumerate(positions):
            x = round(ws._timeline_x(position) - canvas.canvasx(0))
            canvas.event_generate('<ButtonPress-1>' if index == 0 else '<B1-Motion>', x=x, y=y)
            self.root.update_idletasks()
        x = round(ws._timeline_x(positions[-1]) - canvas.canvasx(0))
        canvas.event_generate('<ButtonRelease-1>', x=x, y=y)
        self.root.update_idletasks()
        self.assertAlmostEqual(ws.current, positions[-1], delta=1 / ws.project.fps)
        self.assertFalse(ws.playing)
        self.assertEqual(ws.project, source)
        self.assertEqual(len(ws.undo_stack), original_undo)
        self.assertGreaterEqual(len(ws.player.scrubs), 2)
        self.assertEqual(self.errors, [])

    def test_new_project_and_close_cancel_holds(self):
        ws = self.workspace
        ws._transport_key_press(1)
        with patch.object(ws, '_reset_transport_controls', wraps=ws._reset_transport_controls) as reset:
            ws.dirty = False
            ws.new()
            reset.assert_called()
        self.assertEqual(ws.current, 0.)
        self.assertFalse(ws.playing)
        self.assertEqual(ws.project.clips, [])
        with patch.object(ws, '_stop_transport_hold', wraps=ws._stop_transport_hold) as stop:
            ws.close()
            stop.assert_called()
        self.assertEqual(self.errors, [])

    def test_real_focus_out_event_cancels_the_pending_hold_timer(self):
        ws = self.workspace
        ws._transport_key_press(1)
        self.assertIsNotNone(ws._transport_hold_after)
        position = ws.current
        ws.timeline.event_generate('<FocusOut>', when='now')
        self.assertIsNone(ws._transport_hold_after)
        self.assertEqual(ws._transport_held_direction, 0)
        self.assertEqual(ws.current, position)
        self.assertFalse(ws.playing)
        self.assertEqual(self.errors, [])

    def test_actual_treeview_key_binding_tag_stops_default_expansion(self):
        ws, tree = self.workspace, self.workspace.media_tree
        parent = tree.insert('', 'end', text='test parent', open=False)
        tree.insert(parent, 'end', text='test child')
        tree.selection_set(parent)
        tree.focus(parent)
        self.root.update_idletasks()
        tags = tree.bindtags()
        self.assertEqual(tags[0], ws._transport_tag)
        self.assertLess(tags.index(ws._transport_tag), tags.index('Treeview'))
        # Reuse the exact registered keyboard scripts through a virtual event.
        # This exercises Tk's binding order and break handling without taking
        # keyboard focus away from the user's application or injecting input.
        event = '<<TransportRightBindingTest>>'
        for tag in (ws._transport_tag, 'Treeview'):
            script = tree.bind_class(tag, '<KeyPress-Right>')
            self.assertTrue(script)
            script = script.replace('%K', 'Right').replace('%A', '{}').replace('%s', '0')
            tree.bind_class(tag, event, script)
        try:
            ws.current = 2.
            tree.event_generate(event, when='now')
            self.assertAlmostEqual(ws.current, 2. + 1 / ws.project.fps)
            self.assertFalse(tree.item(parent, 'open'))
            self.assertEqual(tree.selection(), (parent,))
            ws._stop_transport_hold()
            # Prove that the copied native Treeview binding actually would
            # expand this row if our transport tag did not consume the key.
            tree.bindtags(tuple(tag for tag in tags if tag != ws._transport_tag))
            tree.event_generate(event, when='now')
            self.assertTrue(tree.item(parent, 'open'))
        finally:
            tree.bindtags(tags)
            for tag in (ws._transport_tag, 'Treeview'):
                tree.unbind_class(tag, event)
        self.assertEqual(self.errors, [])


if __name__ == '__main__':
    unittest.main()
