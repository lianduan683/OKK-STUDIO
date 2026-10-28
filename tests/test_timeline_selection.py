"""Real Canvas selection/drag/delete regressions, using isolated offscreen Tk."""
from __future__ import annotations

import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from tests.test_timeline_drag_merge import TimelineDropTests
from multicam_core import CameraClip, Cut
from subtitle_core import SubtitleCue


class TimelineSelectionTests(unittest.TestCase):
    setUp = TimelineDropTests.setUp
    tearDown = TimelineDropTests.tearDown
    timeline_event = TimelineDropTests.timeline_event

    def event_at(self, seconds, y, state=0):
        w = self.workspace
        return SimpleNamespace(x=w._timeline_x(seconds)-w.timeline.canvasx(0),
                               y=y-w.timeline.canvasy(0), state=state)

    def select_sliver(self):
        w = self.workspace
        top = w._timeline_track_bounds[1][0]
        bottom = w._timeline_track_bounds[2][1]
        w.timeline_press(self.event_at(5., top-3))
        w.timeline_motion(self.event_at(5.1, bottom-2))
        w.timeline_release(self.event_at(5.1, bottom-2))

    def test_marquee_partial_overlap_selects_both_long_clips(self):
        self.select_sliver()
        w = self.workspace
        self.assertEqual(w._timeline_selection, {('clip', 'one'), ('clip', 'two')})
        self.assertEqual(set(w.media_tree.selection()), {'one', 'two'})
        self.assertEqual(w.undo_stack, [])
        self.assertEqual(self.errors, [])

    def test_ctrl_click_toggle_and_shift_add(self):
        w = self.workspace
        a, b = self.timeline_event('one'), self.timeline_event('two')
        w.timeline_press(a); w.timeline_release(a)
        b.state = 4
        w.timeline_press(b); w.timeline_release(b)
        self.assertEqual(len(w._timeline_selection), 2)
        a.state = 1
        w.timeline_press(a); w.timeline_release(a)
        self.assertEqual(w._timeline_selection, {('clip', 'two')})

    def test_group_drag_retains_relative_offset_and_is_one_undo(self):
        w = self.workspace
        w.project.clips[1].start = 2.
        w._refresh()
        w._set_timeline_selection({('clip', 'one'), ('clip', 'two')})
        press = self.timeline_event('one')
        scale = w._timeline_scale()
        w.timeline_press(press)
        for delta in (1., 2., 3.):
            w.timeline_motion(SimpleNamespace(x=press.x+delta*scale, y=press.y))
        w.timeline_release(press)
        self.assertEqual([c.start for c in w.project.clips], [3., 5.])
        self.assertEqual(len(w.undo_stack), 1)
        w.undo()
        self.assertEqual([c.start for c in w.project.clips], [0., 2.])
        w.redo()
        self.assertEqual([c.start for c in w.project.clips], [3., 5.])

    def test_escape_restores_move_redo_dirty_and_project(self):
        w = self.workspace
        original = copy.deepcopy(w.project)
        w.dirty = False
        w.redo_stack = [copy.deepcopy(original)]
        press = self.timeline_event('one')
        w.timeline_press(press)
        w.timeline_motion(SimpleNamespace(x=press.x+2*w._timeline_scale(), y=press.y))
        self.assertNotEqual(w.project, original)
        w._finish_selection_gesture(cancel=True)
        self.assertEqual(w.project, original)
        self.assertEqual(w.undo_stack, [])
        self.assertEqual(len(w.redo_stack), 1)
        self.assertFalse(w.dirty)

    def test_delete_group_and_restore_without_touching_master_audio(self):
        w = self.workspace
        w.project.master_audio, w.project.master_duration = 'master.wav', 10.
        w._refresh()
        original = copy.deepcopy(w.project)
        self.select_sliver()
        w._delete_timeline_selection()
        self.assertEqual(w.project.clips, [])
        self.assertEqual(w.project.master_audio, 'master.wav')
        self.assertEqual(len(w.undo_stack), 1)
        w.undo(); self.assertEqual(w.project, original)
        self.assertEqual(self.errors, [])

    def test_subtitle_and_source_move_together_without_changing_duration(self):
        w = self.workspace
        w.project.subtitles.cues = [SubtitleCue('cue', 2., 4., 'caption')]
        w._refresh()
        w._set_timeline_selection({('clip', 'one'), ('subtitle', 'cue')})
        press = self.timeline_event('one')
        w.timeline_press(press)
        w.timeline_motion(SimpleNamespace(x=press.x+w._timeline_scale(), y=press.y))
        w.timeline_release(press)
        self.assertEqual(w.project.clips[0].start, 1.)
        self.assertEqual((w.project.subtitles.cues[0].start,w.project.subtitles.cues[0].end), (3., 5.))
        self.assertEqual(len(w.undo_stack), 1)

    def test_clear_master_is_explicit_and_undoable(self):
        w = self.workspace
        w.project.master_audio, w.project.master_duration = 'master.wav', 10.
        w._refresh()
        w._set_timeline_selection({('audio', 'master')})
        w._delete_timeline_selection()
        self.assertEqual(w.project.master_audio, '')
        w.undo(); self.assertEqual(w.project.master_audio, 'master.wav')

    def test_delete_program_cut_merges_without_moving_sources(self):
        w = self.workspace
        w.project.cuts = [Cut(0., 1), Cut(4., 2), Cut(7., 1)]
        w._refresh()
        w._set_timeline_selection({('program', (4., 7.))})
        w._delete_timeline_selection()
        self.assertEqual(w.project.camera_at(5.), 1)
        self.assertEqual([c.start for c in w.project.clips], [0., 0.])
        w.undo(); self.assertEqual(w.project.camera_at(5.), 2)

    def test_redraw_preserves_selection_and_deleting_with_no_selection_is_noop(self):
        w = self.workspace
        w._set_timeline_selection({('clip', 'one')})
        w.draw_timeline()
        self.assertTrue(w.timeline.find_withtag('timeline-selected'))
        w._set_timeline_selection(set())
        w._delete_timeline_selection()
        self.assertEqual(len(w.project.clips), 2)
        self.assertEqual(w.undo_stack, [])

    def test_mixed_program_and_source_delete_is_atomic(self):
        w = self.workspace
        w.project.cuts = [Cut(0., 1), Cut(4., 2), Cut(7., 1)]
        w._refresh()
        w._set_timeline_selection({('program', (4., 7.)), ('clip', 'two')})
        w._delete_timeline_selection()
        self.assertEqual([c.id for c in w.project.clips], ['one'])
        w.project.validate()
        self.assertEqual(len(w.undo_stack), 1)
        w.undo(); self.assertEqual(len(w.project.clips), 2)

    def test_audio_removal_clamps_range_and_undo_restores_fields(self):
        w = self.workspace
        w.project.master_audio, w.project.master_duration = 'master.wav', 30.
        w.project.range_start, w.project.range_end = 12., 25.
        w.start_var.set('12'); w.end_var.set('25')
        w._refresh()
        w._set_timeline_selection({('audio', 'master')})
        w._delete_timeline_selection()
        w.project.validate()
        self.assertEqual(w._read_range()[:2], (0., 10.))
        w.undo(); self.assertEqual(w._read_range()[:2], (12., 25.))

    def test_non_frame_subtitle_group_clamp_uses_same_frame_delta(self):
        w = self.workspace
        w.project.subtitles.cues = [SubtitleCue('cue', .045, 1.045, 'caption')]
        w._refresh()
        w._set_timeline_selection({('clip', 'one'), ('subtitle', 'cue')})
        press = self.timeline_event('one')
        w.timeline_press(press)
        w.timeline_motion(SimpleNamespace(x=press.x-w._timeline_scale(), y=press.y))
        w.timeline_release(press)
        self.assertAlmostEqual(w.project.clips[0].start, -1/30)
        self.assertAlmostEqual(w.project.subtitles.cues[0].start-.045, -1/30)

    def test_moved_program_boundary_updates_delete_target(self):
        w = self.workspace
        w.project.cuts = [Cut(0., 1), Cut(4., 2), Cut(7., 1)]
        w._refresh()
        w._set_timeline_selection({('program', (4., 7.))})
        w._rekey_timeline_boundary({'original': 4., 'current': 5.}, False)
        self.assertEqual(w._timeline_selection, {('program', (5., 7.))})

    def test_marquee_retains_offscreen_tracks_with_viewport_culling(self):
        w = self.workspace
        w.project.clips = [CameraClip(str(i), i, f'{i}.mp4', 0., 10.) for i in range(1, 37)]
        w._refresh()
        w.timeline_press(self.event_at(5., w._timeline_track_bounds[1][0]-3))
        w.timeline_motion(self.event_at(5.1, w._timeline_track_bounds[36][1]-2))
        w.timeline_release(self.event_at(5.1, w._timeline_track_bounds[36][1]-2))
        self.assertEqual({key for key in w._timeline_selection if key[0]=='clip'},
                         {('clip', str(i)) for i in range(1, 37)})
        self.assertEqual(self.errors, [])

    def test_group_move_delete_and_save_reload_retains_timeline(self):
        from multicam_core import MulticamProject
        w = self.workspace
        w.project.master_audio, w.project.master_duration = 'master.wav', 20.
        w._refresh()
        w._set_timeline_selection({('clip', 'one'), ('clip', 'two')})
        press = self.timeline_event('one')
        w.timeline_press(press)
        w.timeline_motion(SimpleNamespace(x=press.x+2*w._timeline_scale(), y=press.y))
        w.timeline_release(press)
        w._set_timeline_selection({('clip', 'one')})
        w._delete_timeline_selection()
        with tempfile.TemporaryDirectory(prefix='okk-timeline-save-') as folder:
            path = Path(folder)/'edit.multicam.json'
            w.project.save(path)
            loaded = MulticamProject.load(path)
        self.assertEqual([(c.id,c.start) for c in loaded.clips], [('two',2.)])
        self.assertEqual(loaded.master_duration, 20.)
        loaded.validate()


if __name__ == '__main__':
    unittest.main()
