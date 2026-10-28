"""Real, off-screen Tk checks for viewport rendering and pointer navigation."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from multicam_core import CameraClip, Cut, MulticamProject
from gyroflow_core import StabilizedRange
from . import test_timeline_drag_merge as fixture


class TimelineViewportTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.TimelineDropTests()
        self.fixture.setUp()
        self.ws = self.fixture.workspace
        self.root = self.fixture.root

    def tearDown(self):
        self.fixture.tearDown()

    def dense(self):
        self.ws.project = MulticamProject(
            clips=[CameraClip(str(i), i, f'camera-{i}.mp4', 0., 3600.)
                   for i in range(1, 37)],
            cuts=[Cut(float(i), 1+i % 36) for i in range(2000)])
        self.ws.zoom = 50
        self.ws.draw_timeline()

    def flush(self):
        self.root.after(35, self.root.quit)
        self.root.mainloop()

    def test_dense_project_only_creates_visible_items_but_keeps_all_geometry(self):
        self.dense()
        ws = self.ws
        self.assertEqual(len(ws._timeline_track_bounds), 36)
        self.assertEqual(len(ws._timeline_boundary_times), 1999)
        self.assertLess(len(ws.timeline_program_items), 160)
        self.assertLess(len(ws.timeline.find_all()), 350)
        left, right = ws.timeline.canvasx(0), ws.timeline.canvasx(ws.timeline.winfo_width())
        for item in ws.timeline_program_items:
            if ws.timeline.type(item) == 'rectangle':
                x, _, x2, _ = ws.timeline.coords(item)
                self.assertGreaterEqual(x2, left-ws._px(3))
                self.assertLessEqual(x, right+ws._px(3))
        self.assertEqual(self.fixture.errors, [])

    def test_vertical_navigation_reveals_bottom_tracks_and_retains_hit_geometry(self):
        self.dense()
        ws = self.ws
        self.assertNotIn('36', ws.timeline_clip_items.values())
        ws._timeline_vertical_scroll('moveto', 1.)
        self.flush()
        self.assertIn('36', ws.timeline_clip_items.values())
        self.assertNotIn('1', ws.timeline_clip_items.values())
        self.assertEqual(len(ws._timeline_track_bounds), 36)
        # Direct canvas scrolling, used by auto-pan, follows the same callback.
        ws.timeline.yview_moveto(0.)
        self.flush()
        self.assertIn('1', ws.timeline_clip_items.values())

    def test_scroll_events_merge_into_one_pending_redraw(self):
        self.dense()
        ws = self.ws
        self.flush()
        with patch.object(ws, 'draw_timeline', wraps=ws.draw_timeline) as draw:
            for _ in range(25):
                ws._scroll_timeline('scroll', 1, 'units')
            self.assertIsNotNone(ws._timeline_redraw_after)
            self.assertEqual(draw.call_count, 0)
            self.flush()
            self.assertLessEqual(draw.call_count, 2)
            self.assertIsNone(ws._timeline_redraw_after)

    def test_ctrl_wheel_keeps_time_under_pointer_and_does_not_vertical_scroll(self):
        self.dense()
        ws = self.ws
        ws.timeline.xview_moveto(.3)
        self.flush()
        pointer = 210
        def time_at_pointer():
            return ws._timeline_origin()+(ws.timeline.canvasx(pointer)-ws._timeline_left)/ws._timeline_scale()
        original = time_at_pointer()
        vertical = ws.timeline.yview()
        ws.timeline.event_generate('<Control-MouseWheel>', x=pointer, y=45, delta=120)
        self.assertEqual(ws.zoom, 100)
        self.assertLess(abs(time_at_pointer()-original)*ws._timeline_scale(), 1.1)
        self.assertEqual(ws.timeline.yview(), vertical)

    def test_zoom_is_frozen_during_an_edit_gesture(self):
        self.dense()
        ws = self.ws
        ws._selection_gesture = {'active': True}
        original = ws.zoom
        ws.change_zoom(2, anchor_x=100)
        ws.fit_timeline()
        self.assertEqual(ws.zoom, original)
        ws._selection_gesture = None
        ws._capture_timeline_geometry()
        ws.change_zoom(.5, anchor_x=100)
        self.assertEqual(ws.zoom, original)
        ws._timeline_drag_geometry = None

    def test_boundary_hover_uses_one_geometry_lookup_and_nearest_edges(self):
        self.dense()
        ws = self.ws
        boundary = 30.
        top, bottom = ws._timeline_program_bounds
        event = SimpleNamespace(x=ws._timeline_x(boundary)-ws.timeline.canvasx(0),
                                y=(top+bottom)/2-ws.timeline.canvasy(0))
        with patch.object(ws, '_timeline_scale', wraps=ws._timeline_scale) as scale:
            self.assertEqual(ws._timeline_boundary_at(event), boundary)
            self.assertEqual(scale.call_count, 1)

    def test_text_width_uses_binary_search_and_cached_repeated_results(self):
        ws = self.ws
        font = ws._timeline_label_font
        title = '长标题-camera-'*100
        with patch.object(font, 'measure', wraps=font.measure) as measure:
            fitted = ws._timeline_fit_label(font, title, 120)
            self.assertLessEqual(measure.call_count, 13)
            count = measure.call_count
            self.assertEqual(ws._timeline_fit_label(font, title, 120), fitted)
            self.assertEqual(measure.call_count, count)
        self.assertLessEqual(font.measure(fitted), 120)

    def test_stabilization_cache_file_is_not_checked_for_every_span_or_redraw(self):
        ws = self.ws
        ws.project = MulticamProject(clips=[CameraClip('one', 1, 'one.mp4', 0., 10.)],
            cuts=[Cut(float(i), 1) for i in range(10)],
            stabilizations=[StabilizedRange(0., 10., 1, 'one', cache_path='cache.mp4')])
        ws._timeline_cache_file_states = {}
        with patch.object(Path, 'is_file', return_value=True) as exists:
            ws.draw_timeline()
            ws.draw_timeline()
        self.assertEqual(exists.call_count, 1)

    def test_audio_hit_map_resets_after_each_draw(self):
        ws = self.ws
        ws.project.master_audio, ws.project.master_duration = 'master.wav', 10.
        ws.draw_timeline()
        first = set(ws.timeline_audio_items)
        self.assertEqual(len(first), 2)
        ws.draw_timeline()
        self.assertEqual(len(ws.timeline_audio_items), 2)
        self.assertFalse(first & ws.timeline_audio_items)


if __name__ == '__main__':
    unittest.main()
