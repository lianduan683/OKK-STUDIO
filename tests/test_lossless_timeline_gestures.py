"""Off-screen Tk checks for selection and source-range timeline gestures."""
from __future__ import annotations

from pathlib import Path
import tempfile
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from lossless_core import CutSegment, SegmentModel
from lossless_workspace import LosslessWorkspace


class LosslessTimelineGestureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.source = Path(self.temp.name)/'original.mp4'
        self.source.write_bytes(b'original media remains untouched')
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.attributes('-alpha', 0)
        self.root.overrideredirect(True)
        self.root.geometry('1200x700+10000+10000')
        self.root.working = False
        self.root.mpv = None
        self.root._mpv_command_async = Mock()
        self.root._log = Mock()
        self.errors = []
        self.root.report_callback_exception = lambda *error: self.errors.append(error)
        self.ws = LosslessWorkspace(self.root)
        self.page = tk.Frame(self.root)
        self.page.pack(fill='both', expand=True)
        self.ws.view = self.page
        self.ws._build_edit_page(self.page)
        self.ws.state.source = str(self.source)
        self.ws.state.auto_save = False
        self.ws.state.model = SegmentModel([
            CutSegment(id='a', start=10., end=20., name='First', selected=True),
            CutSegment(id='b', start=30., end=40., name='Second', selected=False),
            CutSegment(id='m', start=60., end=None, name='Marker'),
        ], media_duration=100.)
        self.ws.state.active_id = 'a'
        self.ws.refresh()
        self.root.deiconify()
        self.root.update()
        self.ws._cancel_timeline_draw()
        self.ws._draw_timeline()

    def tearDown(self):
        self.ws.close()
        for pending in self.root.tk.call('after', 'info'):
            self.root.tk.call('after', 'cancel', pending)
        self.root.destroy()
        self.temp.cleanup()

    def event(self, seconds, *, y=45, state=0):
        canvas = self.ws.timeline
        return SimpleNamespace(x=10.+seconds*self.ws._timeline_pps()-canvas.canvasx(0),
            y=y, state=state, x_root=10000, y_root=10000)

    def click(self, seconds, *, state=0):
        event = self.event(seconds, state=state)
        self.ws._timeline_click(event)
        self.ws._timeline_release(event)

    def drag(self, first, last, *, y=45, state=0):
        press = self.event(first, y=y, state=state)
        release = self.event(last, y=y, state=state)
        self.ws._timeline_click(press)
        self.ws._timeline_motion(release)
        self.ws._timeline_release(release)

    def test_click_modifier_selection_and_text_hit_preserve_export_checks(self):
        before = {segment.id: segment.selected for segment in self.ws.state.model.segments}
        self.click(15.)
        self.click(35., state=4)
        self.assertEqual(self.ws._timeline_selection, {'a', 'b'})
        self.assertEqual(set(self.ws.segment_tree.selection()), {'a', 'b'})
        self.click(15., state=1)
        self.assertEqual(self.ws._timeline_selection, {'b'})
        self.assertEqual({segment.id: segment.selected for segment in self.ws.state.model.segments}, before)
        self.assertFalse(self.ws.state.model.history.can_undo)
        self.assertEqual(self.errors, [])

    def test_marquee_selects_intersections_in_both_directions_and_marker(self):
        # Only the right tip of a is inside; whole containment is not required.
        press, end = self.event(19., y=103), self.event(31., y=40)
        self.ws._timeline_click(press)
        self.ws._timeline_motion(end)
        self.ws._timeline_release(end)
        self.assertEqual(self.ws._timeline_selection, {'a', 'b'})
        press, end = self.event(61., y=103), self.event(59., y=40)
        self.ws._timeline_click(press)
        self.ws._timeline_motion(end)
        self.ws._timeline_release(end)
        self.assertEqual(self.ws._timeline_selection, {'m'})
        self.assertFalse(self.ws.state.model.history.can_undo)

    def test_group_drag_preserves_lengths_gap_flags_and_is_one_undo(self):
        original = self.ws.state.model.segments
        self.ws._timeline_select({'a', 'b'})
        self.ws._draw_timeline()
        self.drag(15., 22.)
        moved = self.ws.state.model.get('a').start
        self.assertAlmostEqual(moved, 17., delta=1/self.ws._timeline_pps())
        self.assertAlmostEqual(self.ws.state.model.get('b').start-moved, 20.)
        self.assertEqual((self.ws.state.model.get('a').duration, self.ws.state.model.get('b').duration), (10., 10.))
        self.assertEqual(len(self.ws.state.model.history), 2)
        self.assertTrue(self.ws.state.model.get('a').selected)
        self.assertFalse(self.ws.state.model.get('b').selected)
        self.ws.undo()
        self.assertEqual(self.ws.state.model.segments, original)
        self.ws.redo()
        self.assertEqual(self.ws.state.model.get('a').start, moved)
        self.assertEqual(self.errors, [])

    def test_group_clamps_together_to_source_bounds(self):
        self.ws._timeline_select({'a', 'b'})
        self.ws._draw_timeline()
        self.drag(15., 0.)
        self.assertEqual((self.ws.state.model.get('a').start, self.ws.state.model.get('b').start), (0., 20.))
        self.ws.undo()
        self.ws._draw_timeline()
        self.drag(15., 100.)
        self.assertEqual((self.ws.state.model.get('a').end, self.ws.state.model.get('b').end), (80., 100.))

    def test_motion_is_preview_only_cancel_preserves_redo_and_source(self):
        self.ws.state.model.move_by('a', 2.)
        self.ws.state.model.undo()
        before = self.ws.state.model.segments
        self.ws._draw_timeline()
        self.ws._timeline_click(self.event(15.))
        for seconds in (16., 17., 18., 20.):
            self.ws._timeline_motion(self.event(seconds))
        self.assertEqual(self.ws.state.model.segments, before)
        self.assertIsNotNone(self.ws._timeline_preview)
        self.assertEqual(len(self.ws.state.model.history), 2)
        self.ws._timeline_key(SimpleNamespace(keysym='Escape', state=0))
        self.assertIsNone(self.ws._timeline_preview)
        self.assertTrue(self.ws.state.model.history.can_redo)
        self.assertEqual(self.source.read_bytes(), b'original media remains untouched')

    def test_delete_selected_and_context_delete_never_remove_media(self):
        before = self.ws.state.model.segments
        self.ws._timeline_select({'a', 'b'})
        self.ws._timeline_key(SimpleNamespace(keysym='Delete', state=0))
        self.assertEqual([item.id for item in self.ws.state.model.segments], ['m'])
        self.assertEqual(len(self.ws.state.model.history), 2)
        self.ws.undo()
        self.assertEqual(self.ws.state.model.segments, before)
        self.ws._draw_timeline()
        event = self.event(35.)
        with patch('lossless_workspace.tk.Menu.tk_popup'):
            self.ws._timeline_context(event)
        menu = self.ws.timeline_context_menu
        self.assertEqual(menu.entrycget(0, 'label'), '删除所选片段')
        menu.invoke(0)
        self.assertNotIn('b', {item.id for item in self.ws.state.model.segments})
        self.assertTrue(self.source.is_file())
        self.assertEqual(self.errors, [])

    def test_real_canvas_mouse_and_keyboard_events_operate_locally(self):
        canvas = self.ws.timeline
        start, finish = self.event(15.), self.event(18.)
        canvas.event_generate('<ButtonPress-1>', x=round(start.x), y=45)
        canvas.event_generate('<B1-Motion>', x=round(finish.x), y=45)
        canvas.event_generate('<ButtonRelease-1>', x=round(finish.x), y=45)
        self.assertAlmostEqual(self.ws.state.model.get('a').start, 13., delta=.15)
        canvas.focus_force()
        self.root.update()
        canvas.event_generate('<Control-a>')
        self.root.update()
        self.assertEqual(self.ws._timeline_selection, {'a', 'b', 'm'})
        canvas.event_generate('<Delete>')
        self.root.update()
        self.assertEqual(self.ws.state.model.segments, ())
        # Text widgets have no timeline Delete binding.
        text = tk.Entry(self.page)
        text.insert(0, 'abc')
        self.assertEqual(text.bind('<Delete>'), '')
        self.assertEqual(self.errors, [])

    def test_cursor_anchored_zoom_scrolled_drag_and_culling(self):
        canvas = self.ws.timeline
        pointer = self.event(35.)
        expected = self.ws._timeline_time(pointer.x)
        self.ws._timeline_zoom_wheel(SimpleNamespace(x=pointer.x, delta=120))
        self.ws._flush_timeline_draw()
        self.assertEqual(self.ws._timeline_zoom, 2.)
        self.assertAlmostEqual(self.ws._timeline_time(pointer.x), expected, delta=.2)
        self.drag(35., 38.)
        self.assertAlmostEqual(self.ws.state.model.get('b').start, 33., delta=1/self.ws._timeline_pps())
        for _ in range(5):
            self.ws._timeline_zoom_wheel(SimpleNamespace(x=600, delta=120))
        self.ws._timeline_scroll('moveto', .7)
        self.ws._flush_timeline_draw()
        self.assertLess(len(self.ws._timeline_items), 6)

    def test_motion_draws_are_coalesced_and_playback_only_updates_head(self):
        self.ws._timeline_click(self.event(15.))
        self.ws._cancel_timeline_draw()
        with patch.object(self.ws, '_draw_timeline', wraps=self.ws._draw_timeline) as redraw:
            for index in range(200):
                self.ws._timeline_motion(self.event(15.+index/100.))
            self.assertEqual(redraw.call_count, 0)
            pending = self.ws._timeline_draw_after
            self.assertIsNotNone(pending)
            self.ws._cancel_timeline_draw()
            self.ws._timeline_cancel()
            self.ws._cancel_timeline_draw()
            self.ws.on_player_time(8.)
            self.ws._redraw_playhead()
            self.assertEqual(redraw.call_count, 0)
        self.assertFalse(self.ws.state.model.history.can_undo)

    def test_stale_project_change_and_busy_state_block_commit(self):
        self.ws._timeline_click(self.event(15.))
        self.ws._timeline_motion(self.event(18.))
        replacement = SegmentModel([CutSegment(id='new', start=5., end=9.)], media_duration=100.)
        self.ws.state.model = replacement
        self.ws._timeline_release(self.event(20.))
        self.assertFalse(replacement.history.can_undo)
        self.assertEqual(replacement.get('new').start, 5.)
        self.ws.state.active_id = 'new'
        self.ws.refresh()
        self.ws._timeline_select({'new'})
        self.root.working = True
        self.ws._timeline_key(SimpleNamespace(keysym='Delete', state=0))
        self.assertEqual(len(replacement.segments), 1)

    def test_automatic_sidecar_save_occurs_once_after_committed_drag(self):
        self.ws.state.auto_save = True
        with patch.object(self.ws, 'save_llc') as save:
            self.ws._timeline_click(self.event(15.))
            for seconds in (16., 18., 20.):
                self.ws._timeline_motion(self.event(seconds))
            save.assert_not_called()
            self.ws._timeline_release(self.event(20.))
            save.assert_called_once_with(automatic=True)
        self.assertEqual(len(self.ws.state.model.history), 2)

    def test_fractional_video_rate_snaps_group_delta_to_frames(self):
        fps = 30000/1001
        self.ws.state.probe = SimpleNamespace(duration=100., primary_video=SimpleNamespace(fps=fps))
        self.ws._timeline_select({'a', 'b'})
        self.ws._draw_timeline()
        self.drag(15., 16.123)
        delta = self.ws.state.model.get('a').start-10.
        self.assertAlmostEqual(delta*fps, round(delta*fps), places=7)
        self.assertAlmostEqual(self.ws.state.model.get('b').start-30., delta)
        self.assertAlmostEqual(self.ws.state.model.get('a').duration, 10.)

    def test_unmount_cancels_preview_and_callbacks_without_history_write(self):
        self.ws._timeline_click(self.event(15.))
        self.ws._timeline_motion(self.event(20.))
        self.assertIsNotNone(self.ws._timeline_draw_after)
        self.ws.unmount()
        self.assertIsNone(self.ws._timeline_gesture)
        self.assertIsNone(self.ws._timeline_preview)
        self.assertIsNone(self.ws._timeline_draw_after)
        self.assertFalse(self.ws.state.model.history.can_undo)


if __name__ == '__main__':
    unittest.main()
