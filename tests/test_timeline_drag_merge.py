"""Real Tk drop/drag regressions; transparent off-screen test windows only."""
from __future__ import annotations

import copy
from pathlib import Path
import sys
import threading
import tempfile
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exporter_core import Capabilities, MediaInfo
from multicam_core import CameraClip, Cut, MulticamProject
from multicam_ui import MulticamWorkspace


class TimelineDropTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.attributes('-alpha', 0)
        self.root.overrideredirect(True)
        self.root.geometry('1280x820+10000+10000')
        self.root.working = False
        self.root.mpv_path = ''
        self.root.mpv = None
        self.root.cancel_event = threading.Event()
        self.root.caps = Capabilities(ffprobe_path='fixture-ffprobe.exe')
        self.root.progress_var = tk.DoubleVar(self.root)
        self.root.progress_text_var = tk.StringVar(self.root)
        self.root._log = Mock()
        self.root._set_working = lambda active: setattr(self.root, 'working', active)
        self.errors = []
        self.root.report_callback_exception = lambda *details: self.errors.append(details)
        self.patches = [patch.object(MulticamWorkspace, '_schedule_camera_metadata'),
                        patch.object(MulticamWorkspace, '_schedule_transition_previews'),
                        patch('multicam_ui.messagebox.showerror')]
        self.mocks = [item.start() for item in self.patches]
        self.workspace = MulticamWorkspace(self.root, self.root)
        self.workspace.pack(fill='both', expand=True)
        self.workspace.project = MulticamProject(clips=[
            CameraClip('one', 1, 'one.mp4', 0., 10.),
            CameraClip('two', 2, 'two.mp4', 0., 10.)], cuts=[Cut(0., 1)])
        self.workspace._refresh()
        self.root.deiconify()
        self.root.update()

    def tearDown(self):
        self.workspace.close()
        for pending in self.root.tk.call('after', 'info'):
            self.root.tk.call('after', 'cancel', pending)
        self.root.destroy()
        for item in self.patches:
            item.stop()

    def timeline_event(self, clip_id):
        canvas = self.workspace.timeline
        item = next(item for item, identifier in self.workspace.timeline_clip_items.items()
                    if identifier == clip_id and canvas.type(item) == 'rectangle')
        left, top, right, bottom = canvas.coords(item)
        return SimpleNamespace(x=(left+right)/2-canvas.canvasx(0),
                               y=(top+bottom)/2-canvas.canvasy(0))

    def root_position(self, position, camera=1):
        workspace = self.workspace
        canvas = workspace.timeline
        top, bottom = workspace._timeline_track_bounds[camera]
        return SimpleNamespace(x_root=canvas.winfo_rootx()+workspace._timeline_x(position)-canvas.canvasx(0),
                               y_root=canvas.winfo_rooty()+(top+bottom)/2-canvas.canvasy(0))

    def test_tree_name_column_drag_places_visible_clip_and_is_one_undo(self):
        workspace = self.workspace
        tree = workspace.media_tree
        tree.see('two')
        self.root.update_idletasks()
        x, y, width, height = tree.bbox('two', '#0')
        event = SimpleNamespace(x=x+25, y=y+height/2,
                                x_root=tree.winfo_rootx()+x+25, y_root=tree.winfo_rooty()+y+height/2)
        self.assertEqual(tree.identify_region(event.x, event.y), 'tree')
        original = copy.deepcopy(workspace.project)
        workspace._media_drag_press(event)
        drop = self.root_position(3., 2)
        self.assertEqual(workspace._media_drag_motion(drop), 'break')
        self.assertTrue(workspace.timeline.find_withtag('media-drop'))
        workspace._media_drag_release(drop)
        self.assertEqual(workspace.project.clips[1].start, 3.)
        self.assertEqual(workspace.project.camera_at(3.5), 2)
        self.assertIn('two', workspace.timeline_clip_items.values())
        self.assertEqual(len(workspace.undo_stack), 1)
        self.assertFalse(workspace.timeline.find_withtag('media-drop'))
        workspace.undo()
        self.assertEqual(workspace.project, original)
        workspace.redo()
        self.assertEqual(workspace.project.clips[1].start, 3.)
        self.assertEqual(self.errors, [])

    def test_drop_outside_or_escape_keeps_project_and_undo_unchanged(self):
        workspace = self.workspace
        original = copy.deepcopy(workspace.project)
        workspace._media_drag = dict(id='one', x=0, y=0, active=True)
        workspace._media_drag_release(SimpleNamespace(x_root=-500, y_root=-500))
        self.assertEqual(workspace.project, original)
        self.assertEqual(workspace.undo_stack, [])
        workspace._media_drag = dict(id='one', x=0, y=0, active=True)
        self.assertEqual(workspace._media_drag_cancel(), 'break')
        self.assertIsNone(workspace._media_drag)

    def test_repeated_drag_does_not_accelerate_when_duration_changes(self):
        workspace = self.workspace
        press = self.timeline_event('one')
        scale = workspace._timeline_scale()
        workspace.timeline_press(press)
        for position in (2., 4., 7.):
            workspace.timeline_motion(SimpleNamespace(x=press.x+position*scale, y=press.y))
            workspace.draw_timeline()
            self.assertEqual(workspace.project.clips[0].start, position)
            self.assertEqual(workspace._timeline_scale(), scale)
        workspace.timeline_release(press)
        self.assertIsNone(workspace._timeline_drag_geometry)
        self.assertEqual(len(workspace.undo_stack), 1)
        workspace.undo()
        self.assertEqual(workspace.project.clips[0].start, 0.)

    def test_negative_offset_does_not_shift_drag_origin_mid_gesture(self):
        workspace = self.workspace
        press = self.timeline_event('one')
        scale = workspace._timeline_scale()
        workspace.timeline_press(press)
        for position in (-2., -4.):
            workspace.timeline_motion(SimpleNamespace(x=press.x+position*scale, y=press.y))
            workspace.draw_timeline()
            self.assertEqual(workspace.project.clips[0].start, position)
            self.assertEqual(workspace._timeline_origin(), 0.)
        workspace.timeline_release(press)
        self.assertEqual(workspace._timeline_origin(), -4.)

    def test_scrolled_drop_uses_canvas_coordinates_for_time_and_track(self):
        workspace = self.workspace
        workspace.project.clips = [CameraClip(str(i), i, f'{i}.mp4', 0., 100.) for i in range(1, 21)]
        workspace._refresh()
        workspace.zoom = 4
        workspace.draw_timeline()
        workspace.timeline.xview_moveto(.4)
        workspace.timeline.yview_moveto(1.)
        self.root.update_idletasks()
        event = self.root_position(50., 20)
        self.assertEqual(workspace._timeline_drop_position(event.x_root, event.y_root), (50., 20))

    def test_external_batch_is_probed_off_thread_committed_once_and_visible(self):
        workspace = self.workspace
        original = copy.deepcopy(workspace.project)
        gate = threading.Event()
        started = threading.Event()
        infos = [MediaInfo(path=f'added-{i}.mp4', duration=2., width=1920, height=1080, fps=30.) for i in range(2)]
        main_thread = threading.get_ident()
        def probe(*args, **kwargs):
            self.assertNotEqual(threading.get_ident(), main_thread)
            started.set()
            if not gate.wait(5): raise RuntimeError('test worker timeout')
            return list(enumerate(infos, 1))
        with patch('timeline_interaction.batch_import', side_effect=probe):
            self.assertTrue(workspace.import_timeline_paths([info.path for info in infos], start=2.))
            self.assertTrue(started.wait(5))
            self.assertEqual(workspace.project, original)
            self.root.update()
            gate.set()
            kind, payload = workspace.jobs.get(timeout=5)
            self.assertEqual(kind, 'timeline_import_ready')
            workspace._finish_timeline_import(payload)
        self.assertEqual(len(workspace.project.clips), 4)
        self.assertEqual([clip.start for clip in workspace.project.clips[-2:]], [2., 4.])
        self.assertEqual(workspace.project.camera_at(2.5), 3)
        self.assertEqual(workspace.project.camera_at(6.5), 1)
        self.assertTrue({clip.id for clip in workspace.project.clips[-2:]} <= set(workspace.timeline_clip_items.values()))
        self.assertEqual(len(workspace.undo_stack), 1)
        workspace.undo()
        self.assertEqual(workspace.project, original)
        self.assertEqual(self.errors, [])

    def test_cancelled_or_stale_batch_never_partially_modifies_project(self):
        workspace = self.workspace
        original = copy.deepcopy(workspace.project)
        infos = [MediaInfo(path='new.mp4', duration=5., width=1920, height=1080, fps=30.)]
        self.root.cancel_event.set()
        workspace._finish_timeline_import((workspace.project, 1., None, infos))
        self.assertEqual(workspace.project, original)
        self.root.cancel_event.clear()
        workspace._finish_timeline_import((copy.deepcopy(workspace.project), 1., None, infos))
        self.assertEqual(workspace.project, original)
        self.assertEqual(workspace.undo_stack, [])

    def test_mixed_video_audio_drop_commits_as_one_edit(self):
        workspace = self.workspace
        original = copy.deepcopy(workspace.project)
        info = MediaInfo(path='new.mp4', duration=5., width=1920, height=1080, fps=30.)
        with patch('timeline_interaction.batch_import', return_value=[(1, info)]), \
             patch('timeline_interaction.probe_timeline_master', return_value=('master.wav', 20.)):
            event = self.root_position(3., 1)
            self.assertTrue(workspace.timeline_drop_at(['new.mp4'], event.x_root, event.y_root, master_audio='master.wav'))
            kind, payload = workspace.jobs.get(timeout=5)
            self.assertEqual(kind, 'timeline_import_ready')
            self.assertEqual(workspace.project, original)
            workspace._finish_timeline_import(payload)
        self.assertEqual(len(workspace.project.clips), 3)
        self.assertEqual(workspace.project.master_audio, 'master.wav')
        self.assertEqual(workspace.project.master_duration, 20.)
        self.assertEqual(len(workspace.undo_stack), 1)
        workspace.undo()
        self.assertEqual(workspace.project, original)

    def test_manual_proxy_validation_runs_off_thread_and_binding_can_be_undone(self):
        workspace = self.workspace
        workspace.media_tree.selection_set('one')
        main_thread = threading.get_ident()
        def validate(*args, **kwargs):
            self.assertNotEqual(threading.get_ident(), main_thread)
        with tempfile.TemporaryDirectory(prefix='okk-link-proxy-') as folder:
            source, proxy = Path(folder)/'original.mp4', Path(folder)/'proxy.mp4'
            source.write_bytes(b'original'); proxy.write_bytes(b'proxy')
            workspace.project.clips[0].path = str(source)
            self._check_manual_proxy_link(workspace, source, proxy, validate)
        workspace.undo()
        self.assertEqual(workspace.project.clips[0].proxy_path, '')

    def _check_manual_proxy_link(self, workspace, source, proxy, validate):
        with patch('multicam_proxy.filedialog.askopenfilename', return_value=str(proxy)), \
             patch('external_proxy_link.validate_existing_proxy', side_effect=validate) as probe, \
             patch('proxy_cache.register_proxy', return_value=False) as register:
            workspace.choose_proxy()
            kind, payload = workspace.jobs.get(timeout=5)
            self.assertEqual(kind, 'quick_proxy_link_done')
            self.assertFalse(workspace.busy)
            self.assertFalse(workspace.app.working)
            self.assertEqual(workspace.project.clips[0].proxy_path, '')
            workspace.jobs.put((kind, payload))
            workspace._poll()
            self.assertEqual(workspace.project.clips[0].proxy_path, str(proxy))
            self.assertEqual(len(workspace.undo_stack), 1)
            probe.assert_called_once_with('fixture-ffprobe.exe', str(source), str(proxy), workspace._proxy_link_cancel)
            register.assert_called_once_with(str(source), str(proxy))

    def test_bad_proxy_preserves_original_binding(self):
        workspace = self.workspace
        workspace.media_tree.selection_set('one')
        workspace.project.clips[0].proxy_path = 'good-proxy.mp4'
        with patch('multicam_proxy.filedialog.askopenfilename', return_value='bad-proxy.mp4'), \
             patch('external_proxy_link.validate_existing_proxy', side_effect=ValueError('duration mismatch')):
            workspace.choose_proxy()
            kind, payload = workspace.jobs.get(timeout=5)
            self.assertEqual(kind, 'quick_proxy_link_done')
            workspace.jobs.put((kind, payload))
            workspace._poll()
        self.assertEqual(workspace.project.clips[0].proxy_path, 'good-proxy.mp4')
        self.assertFalse(workspace.busy)
        self.assertEqual(workspace.undo_stack, [])


if __name__ == '__main__':
    unittest.main()
