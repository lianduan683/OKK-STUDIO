"""Off-screen native Tk interaction and bounded alpha presentation regressions."""
from __future__ import annotations

import copy
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from exporter_core import MediaInfo
from multicam_core import CameraClip, Cut
try:
    from . import test_timeline_drag_merge as fixture
    from .test_transport_controls import RecordingPlayer
except ImportError:
    import test_timeline_drag_merge as fixture
    from test_transport_controls import RecordingPlayer


class VideoStackUITests(unittest.TestCase):
    setUp = fixture.TimelineDropTests.setUp
    tearDown = fixture.TimelineDropTests.tearDown
    root_position = fixture.TimelineDropTests.root_position

    def stack(self):
        ws = self.workspace
        ws.project.clips[1].has_alpha = True
        ws.project.clips.append(CameraClip('three', 3, 'three.mp4', 0., 10., has_alpha=True))
        ws.project.cuts = [Cut(0., 1), Cut(4., 2)]
        ws.project.video_stack_enabled = True
        ws.project.video_stack_order = [2, 1, 3]
        ws._refresh()
        self.root.update_idletasks()

    def label_xy(self, camera):
        ws = self.workspace
        top, bottom = ws._timeline_track_bounds[camera]
        return dict(x=round(ws._px(10)), y=round((top+bottom)/2-ws.timeline.canvasy(0)))

    def drag(self, source, target, *, release=True):
        canvas = self.workspace.timeline
        canvas.event_generate('<ButtonPress-1>', **self.label_xy(source))
        canvas.event_generate('<B1-Motion>', **self.label_xy(target))
        if release: canvas.event_generate('<ButtonRelease-1>', **self.label_xy(target))

    def test_alpha_timeline_import_creates_new_top_track_and_preserves_programme_cuts(self):
        ws = self.workspace
        original = copy.deepcopy(ws.project)
        info = MediaInfo(path=Path('alpha.mov'), duration=3., width=1920, height=1080,
                         fps=30., has_audio=False, has_alpha=True)
        ws._finish_timeline_import((ws.project, 2., 1, [info]))
        self.assertTrue(ws.project.video_stack_enabled)
        self.assertEqual(ws.project.stacked_cameras(), [3, 1, 2])
        self.assertEqual(ws.project.cuts, original.cuts)
        self.assertEqual((ws.project.clips[-1].camera, ws.project.clips[-1].start), (3, 2.))
        self.assertTrue(ws.project.clips[-1].has_alpha)
        self.assertEqual(len(ws.undo_stack), 1)
        self.assertIn(ws.project.clips[-1].id, ws.timeline_clip_items.values())
        ws.undo(); self.assertEqual(ws.project, original)
        ws.redo(); self.assertEqual(ws.project.stacked_cameras(), [3, 1, 2])
        self.assertEqual(self.errors, [])

    def test_stack_media_drop_changes_only_source_time_and_layer_order(self):
        self.stack()
        ws = self.workspace
        cuts = copy.deepcopy(ws.project.cuts)
        ws._media_drag = dict(id='two', active=True, x=0, y=0)
        ws._media_drag_release(self.root_position(3., 2))
        self.assertEqual(ws.project.clips[1].start, 3.)
        self.assertEqual(ws.project.cuts, cuts)
        self.assertEqual(ws.project.stacked_cameras(), [2, 1, 3])
        self.assertEqual(len(ws.undo_stack), 1)
        self.assertEqual(self.errors, [])

    def test_real_label_drag_after_horizontal_scroll_is_one_undo_and_redo(self):
        self.stack()
        ws = self.workspace
        original = copy.deepcopy(ws.project)
        ws.zoom = 5; ws.draw_timeline(); ws._scroll_timeline('moveto', .45)
        self.assertGreater(ws.timeline.canvasx(0), 100)
        header = next(item for item in ws.timeline.find_withtag('stack-header')
                      if ws.timeline.type(item) == 'rectangle')
        self.assertAlmostEqual(ws.timeline.coords(header)[0], ws.timeline.canvasx(0), delta=1.)
        self.drag(2, 3)
        self.assertEqual(ws.project.video_stack_order, [1, 3, 2])
        self.assertEqual(ws.project.cuts, original.cuts)
        self.assertEqual(len(ws.undo_stack), 1)
        ws.undo(); self.assertEqual(ws.project, original)
        ws.redo(); self.assertEqual(ws.project.video_stack_order, [1, 3, 2])
        self.assertEqual(self.errors, [])

    def test_escape_and_project_replacement_cancel_uncommitted_order(self):
        self.stack()
        ws = self.workspace
        original = copy.deepcopy(ws.project)
        self.drag(2, 3, release=False)
        self.assertEqual(ws.project, original)
        ws._key(SimpleNamespace(widget=ws.timeline, state=0, keysym='Escape', char=''))
        self.assertIsNone(ws._video_stack_drag)
        self.assertEqual(ws.project, original)
        self.assertEqual(ws.undo_stack, [])
        self.drag(2, 3, release=False)
        ws.project = copy.deepcopy(original)
        ws.timeline.event_generate('<ButtonRelease-1>', **self.label_xy(3))
        self.assertEqual(ws.project, original)
        self.assertEqual(ws.undo_stack, [])
        self.assertIn('图层已变化', ws.status.get())
        self.assertEqual(self.errors, [])

    def test_context_order_hide_and_disable_preserve_original_edit(self):
        self.stack()
        ws = self.workspace
        cuts = copy.deepcopy(ws.project.cuts)
        point = self.label_xy(2)
        with patch('video_stack_ui.tk.Menu.tk_popup'):
            ws.timeline_context(SimpleNamespace(**point, x_root=10000, y_root=10000))
        menu = ws.video_stack_context_menu
        self.assertEqual(menu.entrycget(0, 'state'), 'disabled')
        menu.invoke(1)
        self.assertEqual(ws.project.video_stack_order, [1, 2, 3])
        ws.toggle_video_layer(2)
        self.assertEqual(ws.project.stacked_cameras(), [1, 3])
        self.assertEqual(ws._timeline_camera_order(), [1, 2, 3])
        ws.video_stack_button.invoke()
        self.assertFalse(ws.project.video_stack_enabled)
        self.assertEqual(ws.project.cuts, cuts)
        ws.undo()
        self.assertTrue(ws.project.video_stack_enabled)
        self.assertEqual(ws.project.video_stack_hidden, [2])
        ws.undo(); self.assertEqual(ws.project.video_stack_hidden, [])
        self.assertEqual(self.errors, [])

    def test_camera_click_and_digits_select_grade_layer_without_recording_or_audition(self):
        self.stack()
        ws = self.workspace
        original = copy.deepcopy(ws.project)
        ws.player = RecordingPlayer()
        ws.selected_program_range = (0., 4.)
        ws.camera_buttons[3].invoke()
        self.assertEqual(ws.grade_target, 3)
        self.assertEqual(ws._displayed_camera, 3)
        ws._key(SimpleNamespace(widget=ws.timeline, state=0, keysym='2', char='2'))
        self.assertEqual(ws.grade_target, 2)
        self.assertEqual(ws.project, original)
        self.assertEqual(ws.undo_stack, [])
        self.assertFalse(any(action == 'audition' and values.get('camera') is not None
                             for action, values in ws.player.commands))
        ws.player_queue.put(('state', dict(time=3., playing=False, camera=1, request_id=0)))
        ws._poll()
        self.assertEqual(ws._displayed_camera, 2)
        self.assertIn('图层合成', ws.program_var.get())
        self.assertEqual(self.errors, [])

    def test_preview_mailbox_freshness_pixels_mode_and_native_handles(self):
        self.stack()
        ws = self.workspace
        ws.player = RecordingPlayer()
        ws._transport_request_id = 7
        handles = [item.winfo_id() for item in [ws.program_surface, *ws.program_views, *ws.camera_surfaces.values()]]
        ws._handle_stack_preview_job('stack_mode', True)
        self.root.update_idletasks()
        self.assertTrue(ws.stack_preview_label.winfo_ismapped())
        self.assertTrue(all(not item.winfo_ismapped() for item in ws.program_views))
        self.assertTrue(all(item.winfo_ismapped() for item in ws.camera_surfaces.values()))
        frame = dict(request_id=7, time=2., width=2, height=2, rgba=bytes([255, 0, 0, 255])*4)
        with patch('stack_preview_ui.tk.PhotoImage', wraps=__import__('tkinter').PhotoImage) as image:
            self.assertFalse(ws._present_stack_frame(dict(frame, request_id=6)))
            self.assertFalse(ws._present_stack_frame(dict(frame, rgba=b'bad')))
            image.assert_not_called()
            self.assertTrue(ws._present_stack_frame(frame))
            self.assertEqual(ws._stack_preview_image.get(0, 0), (255, 0, 0))
            self.assertEqual(image.call_count, 1)
            current_image = ws._stack_preview_image
            self.assertTrue(ws._present_stack_frame(dict(frame, rgba=bytes([0, 0, 255, 255])*4)))
            self.assertIs(ws._stack_preview_image, current_image)
            self.assertEqual(ws._stack_preview_image.get(0, 0), (0, 0, 255))
            self.assertEqual(image.call_count, 1)
        old_size = ws._stack_preview_draw_size
        self.root.geometry('1600x950+10000+10000')
        deadline = time.monotonic()+.5
        while ws._stack_preview_draw_size == old_size and time.monotonic() < deadline:
            self.root.update(); time.sleep(.003)
        self.assertNotEqual(ws._stack_preview_draw_size, old_size)
        self.assertEqual(ws._stack_preview_image.get(0, 0), (0, 0, 255))
        self.assertIs(ws._stack_preview_image, current_image)
        ws._handle_stack_preview_job('stack_mode', False)
        self.root.update_idletasks()
        self.assertFalse(ws.stack_preview_label.winfo_ismapped())
        self.assertTrue(all(item.winfo_ismapped() for item in ws.program_views))
        self.assertIsNone(ws._stack_preview_after)
        self.assertEqual(handles, [item.winfo_id() for item in [ws.program_surface, *ws.program_views, *ws.camera_surfaces.values()]])
        self.assertEqual(self.errors, [])

    def test_preview_drains_latest_without_regular_poll_and_does_no_work_when_empty(self):
        self.stack()
        ws = self.workspace
        ws.player = RecordingPlayer()
        slot = [None]
        ws.player.take_stack_frame = lambda: slot.pop(0) if slot else None
        ws._handle_stack_preview_job('stack_mode', True)
        frame = dict(request_id=0, time=5., width=2, height=2, rgba=bytes([0, 255, 0, 255])*4)
        slot[0] = frame
        with patch.object(ws, '_present_stack_frame', wraps=ws._present_stack_frame) as present:
            deadline = time.monotonic()+.25
            while ws._stack_preview_last_time != 5. and time.monotonic() < deadline:
                self.root.update(); time.sleep(.002)
            self.assertEqual(ws._stack_preview_last_time, 5.)
            for _ in range(4): ws._drain_stack_preview()
            self.assertEqual(present.call_count, 1)
        ws.stop_monitors()
        self.assertFalse(ws._stack_preview_active)
        self.assertIsNone(ws._stack_preview_after)
        self.assertEqual(self.errors, [])


if __name__ == '__main__': unittest.main()
