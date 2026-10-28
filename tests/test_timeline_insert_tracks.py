"""Offscreen regression for arbitrary insertion, vertical moves and event pump."""
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from multicam_core import MulticamProject, CameraClip, Cut
from tests import test_timeline_drag_merge as fixture


class TrackModelTests(unittest.TestCase):
    def test_diagonal_move_validates_destination_track_not_old_track(self):
        from timeline_edits import source_move_delta
        p = MulticamProject(clips=[CameraClip('a', 1, 'a.mp4', 0, 1),
                                   CameraClip('b', 1, 'b.mp4', 5, 1)])
        p.library_overlays = [SimpleNamespace(camera=1, source_id='b', start=5., end=6.)]
        self.assertEqual(source_move_delta(p, ['a'], 5., target_cameras={'a': 2}), 5.)
        with self.assertRaises(ValueError): source_move_delta(p, ['a'], 5.)
        p.library_overlays[0].source_id = 'a'
        with self.assertRaises(ValueError): source_move_delta(p, ['a'], 0., target_cameras={'a': 2})

    def test_empty_tracks_roundtrip_without_becoming_monitor_cameras(self):
        p = MulticamProject(clips=[CameraClip('a', 1, 'a.mp4', 0, 10)], cuts=[Cut(0, 1)],
            timeline_empty_tracks=[3, 2], video_stack_order=[3, 1, 2])
        with tempfile.TemporaryDirectory() as folder:
            p.save(Path(folder)/'project.json')
            q = MulticamProject.load(Path(folder)/'project.json')
        self.assertEqual(q.cameras, [1])
        self.assertEqual(q.stacked_cameras(), [3, 1, 2])
        self.assertEqual(q.timeline_empty_tracks, [3, 2])
        for value in ([True], [2, 2], [-1], 'bad'):
            q.timeline_empty_tracks = value
            with self.assertRaises(ValueError): q.validate()


class TrackInsertionTests(unittest.TestCase):
    setUp = fixture.TimelineDropTests.setUp
    tearDown = fixture.TimelineDropTests.tearDown
    timeline_event = fixture.TimelineDropTests.timeline_event

    def test_insert_above_below_and_undo_keeps_programme(self):
        w = self.workspace
        self.assertEqual(w.insert_video_track(('above', 2)), 3)
        self.assertEqual(w._timeline_camera_order(), [1, 3, 2])
        self.assertFalse(w.project.video_stack_enabled)
        self.assertIn(3, w._timeline_track_bounds)
        w.insert_video_track(('below', 3))
        self.assertEqual(w._timeline_camera_order(), [1, 3, 4, 2])
        w.undo()
        self.assertEqual(w._timeline_camera_order(), [1, 3, 2])
        w.redo()
        self.assertEqual(w._timeline_camera_order(), [1, 3, 4, 2])

    def test_material_insertion_uses_clicked_time_and_top_layer(self):
        w = self.workspace
        info = SimpleNamespace(path='new.mov', duration=2., fps=30., has_alpha=True, has_audio=False)
        w._finish_timeline_import((w.project, 3.123, None, [info], None, ('top', None)))
        clip = w.project.clips[-1]
        self.assertAlmostEqual(clip.start, 94/30)
        self.assertEqual(w.project.stacked_cameras()[0], clip.camera)
        self.assertTrue(w.project.video_stack_enabled)
        self.assertEqual(len(w.undo_stack), 1)

    def test_empty_canvas_context_and_source_context_have_insert_commands(self):
        w = self.workspace
        events = [SimpleNamespace(x=2, y=2, x_root=0, y_root=0), self.timeline_event('one')]
        for event in events:
            event.x_root = event.y_root = 0
            with patch('tkinter.Menu.tk_popup'):
                w.timeline_context(event)
            menu = w.timeline_context_menu
            labels = [menu.entrycget(i, 'label') for i in range(menu.index('end')+1)
                      if menu.type(i) != 'separator']
            self.assertIn('插入视频轨道', labels)
            self.assertTrue(any(label.startswith('在此位置插入素材') for label in labels))

    def test_vertical_drag_keeps_time_and_undo_restores_camera(self):
        w = self.workspace
        new_camera = w.insert_video_track(('top', None))
        count = len(w.undo_stack)
        press = self.timeline_event('one')
        top, bottom = w._timeline_track_bounds[new_camera]
        motion = SimpleNamespace(x=press.x, y=(top+bottom)/2-w.timeline.canvasy(0))
        w.timeline_press(press); w.timeline_motion(motion); w.timeline_release(motion)
        clip = next(c for c in w.project.clips if c.id == 'one')
        self.assertEqual((clip.camera, clip.start), (new_camera, 0.))
        self.assertTrue(w.project.video_stack_enabled)
        self.assertEqual(len(w.undo_stack), count+1)
        self.assertEqual(w.project.stacked_cameras()[0], new_camera)
        w.undo()
        self.assertEqual(next(c.camera for c in w.project.clips if c.id == 'one'), 1)

    def test_escape_vertical_drag_restores_without_new_history(self):
        w = self.workspace
        press = self.timeline_event('one')
        top, bottom = w._timeline_track_bounds[2]
        w.timeline_press(press)
        w.timeline_motion(SimpleNamespace(x=press.x, y=(top+bottom)/2-w.timeline.canvasy(0)))
        w._finish_selection_gesture(cancel=True)
        self.assertEqual(w.project.clips[0].camera, 1)
        self.assertFalse(w.project.video_stack_enabled)
        self.assertEqual(w.undo_stack, [])

    def test_player_pump_coalesces_backlog_without_delaying_swap(self):
        w = self.workspace
        w.player = Mock()
        w._show_program_view = Mock(return_value=True)
        w._show_time = Mock()
        w._sync_comparison_state = Mock()
        for i in range(500):
            w.player_queue.put(('state', dict(time=i/100, playing=True, camera=1, request_id=0)))
        w.player_queue.put(('program_swap', 123))
        began = time.perf_counter()
        w._poll_player()
        elapsed = time.perf_counter()-began
        w.player.submit.assert_called_with('program_swap_ack', window=123)
        w._show_time.assert_called_once()
        self.assertEqual(w.current, 4.99)
        self.assertLess(elapsed, .08)
        print(f'500-state backlog + swap ACK: {elapsed*1000:.2f} ms')

    def test_vertical_group_move_retains_track_spacing_and_escape(self):
        w = self.workspace
        w.insert_video_track(('top', None))
        w.insert_video_track(('top', None))
        before = len(w.undo_stack)
        w._set_timeline_selection({('clip', 'one'), ('clip', 'two')})
        press = self.timeline_event('one')
        target = w._timeline_camera_order()[0]
        top, bottom = w._timeline_track_bounds[target]
        w.timeline_press(press)
        w.timeline_motion(SimpleNamespace(x=press.x, y=(top+bottom)/2-w.timeline.canvasy(0)))
        self.assertEqual([c.camera for c in w.project.clips], [4, 3])
        w._finish_selection_gesture(cancel=True)
        self.assertEqual([c.camera for c in w.project.clips], [1, 2])
        self.assertEqual(len(w.undo_stack), before)

    def test_middle_drag_pans_timeline_vertically(self):
        w = self.workspace
        w.project.clips = [CameraClip(str(i), i, f'{i}.mp4', 0., 100.) for i in range(1, 37)]
        w._refresh()
        initial = w.timeline.yview()
        w._timeline_pan_press(SimpleNamespace(x=200, y=200))
        w._timeline_pan_motion(SimpleNamespace(x=200, y=40))
        self.assertGreater(w.timeline.yview()[0], initial[0])

    def test_merge_worker_publishes_one_edit_and_undo_restores_gaps(self):
        import threading
        from timeline_merge import SourceMergeResult
        w = self.workspace
        w.project.clips.append(CameraClip('three', 1, 'three.mp4', 13., 5.))
        w._refresh()
        w._set_timeline_selection({('clip', 'one'), ('clip', 'three')})
        caller = threading.get_ident()
        workers = []
        def render(plan, *_args, **kwargs):
            workers.append(threading.get_ident())
            kwargs['progress'](50., '测试合并')
            return SourceMergeResult('merged-fixture.mp4', 15., (10., 5.))
        with patch('timeline_merge.render_source_merge', side_effect=render):
            self.assertTrue(w._merge_timeline_selection())
            deadline = time.monotonic()+5
            while w.busy and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.01)
        self.assertFalse(w.busy)
        self.assertNotEqual(workers, [caller])
        self.assertEqual(len(w.project.clips), 2)
        merged = next(c for c in w.project.clips if c.id == 'one')
        self.assertEqual((merged.start, merged.duration), (0., 15.))
        self.assertEqual(len(w.undo_stack), 1)
        w.undo()
        self.assertEqual(next(c.start for c in w.project.clips if c.id == 'three'), 13.)
        self.assertEqual(self.errors, [])


if __name__ == '__main__': unittest.main()
