"""Real Tk integration for atomic batch imports and paged camera monitoring.

Windows are transparent and off screen. Source bytes, media metadata and the
decoder are fixtures; no user input or GPU playback is performed.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from camera_import import batch_import
from exporter_core import Capabilities, MediaInfo
from multicam_core import CameraClip, Cut, MulticamProject
import multicam_player as player_module
from multicam_player import MulticamPlayer
from multicam_ui import MulticamWorkspace
try:
    from .test_multicam_player import _Trace, _controller_type
except ImportError:
    from test_multicam_player import _Trace, _controller_type


class BatchCameraWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='batch_camera_ui_')
        self.folder = Path(self.temp.name)
        self.paths = []
        for index in range(20):
            path = self.folder / f'camera_{index + 1:02}.mp4'
            path.write_bytes(b'original source fixture ' + bytes([index]))
            self.paths.append(str(path))
        self.patches = [patch('multicam_ui.messagebox.showerror'),
                        patch('multicam_ui.messagebox.showwarning'),
                        patch('multicam_ui.messagebox.showinfo'),
                        patch('multicam_ui.messagebox.askyesnocancel', return_value=False)]
        self.dialogs = [item.start() for item in self.patches]
        self._create_workspace()

    def _create_workspace(self, dpi=96, width=1280, height=820):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f'Tk display unavailable: {error}')
        self.root.withdraw()
        self.root.tk.call('tk', 'scaling', dpi / 72)
        self.root.attributes('-alpha', 0)
        self.root.overrideredirect(True)
        self.root.geometry(f'{width}x{height}+10000+10000')
        self.root.working = False
        self.root.mpv_path = ''
        self.root.mpv = None
        self.root.cancel_event = threading.Event()
        self.root.caps = Capabilities(ffmpeg_path='fixture-ffmpeg.exe', ffprobe_path='fixture-ffprobe.exe')
        self.root.progress_var = tk.DoubleVar(self.root)
        self.root.progress_text_var = tk.StringVar(self.root)
        self.root._log = Mock()
        self.root._run_editor_process = Mock()
        self.root._begin_shared_progress = Mock()
        self.root._set_working = lambda active: setattr(self.root, 'working', bool(active))
        self.callback_errors = []
        self.root.report_callback_exception = lambda *details: self.callback_errors.append(details)
        self.workspace = MulticamWorkspace(self.root, self.root)
        self.workspace.pack(fill='both', expand=True)
        self.root.deiconify()
        self.root.update()

    def _destroy_workspace(self):
        if getattr(self, 'workspace', None) is not None:
            self.workspace.close()
            self.workspace = None
        if getattr(self, 'root', None) is not None:
            for pending in self.root.tk.call('after', 'info'):
                self.root.tk.call('after', 'cancel', pending)
            self.root.destroy()
            self.root = None

    def tearDown(self):
        self._destroy_workspace()
        for item in getattr(self, 'patches', []):
            item.stop()
        self.temp.cleanup()

    @staticmethod
    def media(_executable, path):
        return MediaInfo(path=str(path), duration=12., width=1920, height=1080,
                         fps=30., has_audio=True, audio_codec='aac')

    def wait_for(self, predicate, timeout=4.):
        deadline = time.monotonic() + timeout
        while not predicate():
            self.root.update()
            if time.monotonic() >= deadline:
                self.fail(f'workspace did not reach expected state: {self.workspace.status.get()}')
            time.sleep(.005)
        self.root.update()
        self.assertEqual(self.callback_errors, [])

    def populate(self, count=20):
        self.workspace.camera_page = 0
        self.workspace.project = MulticamProject(
            clips=[CameraClip(f'clip_{index + 1}', index + 1, path, 0., 12.)
                   for index, path in enumerate(self.paths[:count])],
            cuts=[Cut(0., 1)], fps=30.)
        self.workspace._refresh()
        self.root.update()

    def key(self, digit, widget=None):
        return self.workspace._key(SimpleNamespace(keysym=digit, char=digit, state=0,
                                                    widget=widget or self.workspace.timeline))

    def start_import(self, paths):
        with patch('multicam_ui.filedialog.askopenfilenames', return_value=tuple(paths)) as dialog:
            self.workspace.add_camera()
        dialog.assert_called_once()

    def test_select_twenty_files_adds_twenty_cameras_at_zero_atomically(self):
        original = [hashlib.sha256(Path(path).read_bytes()).digest() for path in self.paths]
        with patch('camera_import.probe_media', side_effect=self.media) as probe:
            self.start_import(self.paths)
            self.wait_for(lambda: not self.workspace.busy)
        project = self.workspace.project
        self.assertEqual(project.cameras, list(range(1, 21)))
        self.assertEqual([(clip.camera, clip.path, clip.start, clip.duration) for clip in project.clips],
                         [(index + 1, path, 0., 12.) for index, path in enumerate(self.paths)])
        self.assertEqual(probe.call_count, 20)
        self.assertEqual(len(self.workspace.undo_stack), 1)
        self.assertEqual(project.cuts, [Cut(0., 1)])
        self.assertEqual(project.master_audio, self.paths[0])
        self.assertEqual(project.master_duration, 12.)
        self.assertEqual(self.root.progress_var.get(), 100.)
        self.root._begin_shared_progress.assert_called_once()
        self.assertFalse(self.root.working)
        self.dialogs[0].assert_not_called()
        self.assertEqual(original, [hashlib.sha256(Path(path).read_bytes()).digest() for path in self.paths])

    def test_invalid_file_keeps_existing_project_and_undo_unchanged(self):
        self.populate(2)
        before = copy.deepcopy(self.workspace.project)
        def probe(executable, path):
            return MediaInfo(path=path, duration=12.) if path == self.paths[4] else self.media(executable, path)
        with patch('camera_import.probe_media', side_effect=probe):
            self.start_import(self.paths[2:8])
            self.wait_for(lambda: not self.workspace.busy)
        self.assertEqual(self.workspace.project, before)
        self.assertEqual(self.workspace.undo_stack, [])
        self.assertFalse(self.workspace.dirty)
        self.assertFalse(self.root.working)
        self.dialogs[0].assert_called_once()

    def test_cancel_mid_probe_keeps_project_and_undo_unchanged(self):
        self.populate(2)
        before = copy.deepcopy(self.workspace.project)
        entered, release = threading.Event(), threading.Event()
        def probe(executable, path):
            if path == self.paths[3]:
                entered.set()
                if not release.wait(3.):
                    raise RuntimeError('test did not release probe')
            return self.media(executable, path)
        try:
            with patch('camera_import.probe_media', side_effect=probe):
                self.start_import(self.paths[2:8])
                self.wait_for(entered.is_set)
                self.root.cancel_event.set()
                release.set()
                self.wait_for(lambda: not self.workspace.busy)
        finally:
            release.set()
        self.assertEqual(self.workspace.project, before)
        self.assertEqual(self.workspace.undo_stack, [])
        self.assertFalse(self.workspace.dirty)
        self.assertFalse(self.root.working)
        self.assertIn('已取消', self.workspace.status.get())
        self.dialogs[0].assert_not_called()

    def test_cancel_after_probe_before_ready_commit_still_keeps_project(self):
        self.populate(2)
        before = copy.deepcopy(self.workspace.project)
        def finished_import(*args, **kwargs):
            result = batch_import(*args, **kwargs)
            kwargs['cancel_event'].set()
            return result
        with patch('camera_import.probe_media', side_effect=self.media), \
                patch('multicam_ui.batch_import', side_effect=finished_import):
            self.start_import(self.paths[2:8])
            self.wait_for(lambda: not self.workspace.busy)
        self.assertEqual(self.workspace.project, before)
        self.assertEqual(self.workspace.undo_stack, [])
        self.assertFalse(self.root.working)
        self.dialogs[0].assert_not_called()

    def test_append_uses_same_camera_and_preserves_sequential_source_ranges(self):
        self.populate(2)
        self.workspace.media_tree.selection_set('camera_2')
        with patch('multicam_ui.filedialog.askopenfilenames', return_value=tuple(self.paths[2:4])), \
                patch('multicam_ui.probe_media', side_effect=self.media):
            self.workspace.append_clips()
            self.wait_for(lambda: not self.workspace.busy)
        self.assertEqual(self.workspace.project.cameras, [1, 2])
        self.assertEqual([(clip.path, clip.start) for clip in self.workspace.project.clips if clip.camera == 2],
                         [(self.paths[1], 0.), (self.paths[2], 12.), (self.paths[3], 24.)])
        self.dialogs[0].assert_not_called()

    def test_page_number_keys_change_only_selected_cut_range_and_ignore_entry(self):
        self.populate()
        workspace = self.workspace
        workspace.project.edit_points = [2., 4.]
        workspace.selected_program_range = (2., 4.)
        workspace.current = 3.
        workspace._refresh()
        self.assertEqual(workspace._visible_camera_ids, tuple(range(1, 10)))
        workspace._change_camera_page(1)
        self.assertEqual(workspace._visible_camera_ids, tuple(range(10, 19)))
        self.assertEqual(self.key('1'), 'break')
        self.assertEqual(workspace.selected_program_range, (2., 4.))
        self.assertEqual(workspace.project.camera_at(3.), 10)
        for position in (0., 1., 4., 11.):
            self.assertEqual(workspace.project.camera_at(position), 1)
        workspace._change_camera_page(1)
        self.assertEqual(workspace._visible_camera_ids, (19, 20))
        self.assertEqual(self.key('1'), 'break')
        self.assertEqual(workspace.project.camera_at(3.), 19)
        before = copy.deepcopy(workspace.project)
        self.assertEqual(self.key('9'), 'break')
        self.assertEqual(workspace.project, before)
        entry = ttk.Entry(workspace)
        self.assertIsNone(self.key('2', entry))
        self.assertEqual(workspace.project, before)
        entry.destroy()
        for position in (0., 1., 4., 11.):
            self.assertEqual(workspace.project.camera_at(position), 1)
        self.assertEqual(workspace.camera_page_var.get(), '3/3')
        self.assertEqual(str(workspace.camera_next.cget('state')), 'disabled')

    def test_page_swap_keeps_program_audio_and_old_frames_until_matching_release(self):
        self.populate()
        workspace = self.workspace
        player = Mock()
        workspace.player = player
        protected = [workspace.program_surface, *workspace.program_views,
                     *workspace.comparison_views, workspace.audio_surface]
        protected_windows = [widget.winfo_id() for widget in protected]
        original = {workspace.camera_surfaces[camera].winfo_id(): frame
                    for camera, frame in workspace.camera_frames.items()}
        workspace.playing = True
        workspace._change_camera_page(1)
        middle = {workspace.camera_surfaces[camera].winfo_id(): frame
                  for camera, frame in workspace.camera_frames.items()}
        self.assertEqual(player.submit.call_args_list, [call('camera_windows', windows={
            camera: surface.winfo_id() for camera, surface in workspace.camera_surfaces.items()})])
        self.assertEqual(set(workspace._retired_camera_frames), set(original))
        self.assertTrue(all(frame.winfo_exists() and not frame.winfo_ismapped() for frame in original.values()))
        workspace._change_camera_page(1)
        self.assertEqual(set(workspace._retired_camera_frames), set(original) | set(middle))
        self.assertTrue(all(frame.winfo_exists() for frame in middle.values()))
        workspace.player_queue.put(('camera_windows_released', list(original)))
        workspace._poll()
        self.assertTrue(all(not frame.winfo_exists() for frame in original.values()))
        self.assertTrue(all(frame.winfo_exists() for frame in middle.values()))
        self.assertEqual(set(workspace._retired_camera_frames), set(middle))
        workspace.player_queue.put(('camera_windows_released', list(middle)))
        workspace._poll()
        self.assertEqual(workspace._retired_camera_frames, {})
        self.assertTrue(all(not frame.winfo_exists() for frame in middle.values()))
        workspace._release_camera_frames(list(original))  # late duplicate release is harmless
        self.assertTrue(all(frame.winfo_exists() for frame in workspace.camera_frames.values()))
        self.assertEqual(protected_windows, [widget.winfo_id() for widget in protected])
        self.assertTrue(all(widget.winfo_exists() for widget in protected))
        self.assertTrue(workspace.playing)
        self.assertIs(workspace.player, player)
        player.close.assert_not_called()
        self.assertEqual([args.args[0] for args in player.submit.call_args_list], ['camera_windows', 'camera_windows'])

    def test_all_twenty_tracks_context_choices_and_project_roundtrip(self):
        self.populate()
        workspace = self.workspace
        # Offscreen tracks keep their geometry while Canvas items are created
        # only for the current viewport.
        self.assertEqual(set(workspace._timeline_track_bounds), set(range(1, 21)))
        self.assertTrue(set(workspace.timeline_clip_items.values()) <= {clip.id for clip in workspace.project.clips})
        track_names = {workspace.timeline.itemcget(item, 'text') for item in workspace.timeline.find_all()
                       if workspace.timeline.type(item) == 'text'}
        visible_top = workspace.timeline.canvasy(0)
        visible_bottom = workspace.timeline.canvasy(workspace.timeline.winfo_height())
        visible_names = {f'V{camera}' for camera, (top, bottom) in workspace._timeline_track_bounds.items()
                         if bottom >= visible_top and top <= visible_bottom}
        self.assertTrue(visible_names <= track_names)
        scrollregion = [float(value) for value in workspace.timeline.cget('scrollregion').split()]
        self.assertGreater(scrollregion[3], workspace.timeline.winfo_height())
        self.assertTrue(workspace.timeline_vscroll.winfo_ismapped())
        workspace.timeline.yview_moveto(1.)
        self.wait_for(lambda: 'clip_20' in workspace.timeline_clip_items.values())
        last_clip = next(item for item, identifier in workspace.timeline_clip_items.items()
                         if identifier == 'clip_20' and workspace.timeline.type(item) == 'rectangle')
        last_coords = workspace.timeline.coords(last_clip)
        visible_y = (last_coords[1]+last_coords[3])/2-workspace.timeline.canvasy(0)
        self.assertGreaterEqual(visible_y, 0.)
        self.assertLessEqual(visible_y, workspace.timeline.winfo_height())
        workspace.timeline.yview_moveto(0.)
        self.wait_for(lambda: bool(workspace.timeline_program_items))
        item = next(item for item in workspace.timeline_program_items if workspace.timeline.type(item) == 'rectangle')
        left, top, right, bottom = workspace.timeline.coords(item)
        event = SimpleNamespace(x=(left+right)/2-workspace.timeline.canvasx(0),
                                y=(top+bottom)/2-workspace.timeline.canvasy(0), x_root=10000, y_root=10000)
        with patch('multicam_ui.tk.Menu.tk_popup') as popup:
            workspace.timeline_context(event)
        popup.assert_called_once()
        menu = workspace.timeline_context_menu
        camera_menu = menu.nametowidget(menu.entrycget(0, 'menu'))
        self.assertEqual([camera_menu.entrycget(index, 'label') for index in range(camera_menu.index('end')+1)],
                         [f'机位 {camera}' for camera in range(1, 21)])
        camera_menu.invoke(19)
        self.assertEqual(workspace.project.camera_at(6.), 20)
        path = self.folder / 'twenty_cameras.multicam.json'
        workspace.project.save(path)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['version'], 8)
        restored = MulticamProject.load(path)
        self.assertEqual(restored, workspace.project)
        self.assertEqual(restored.cameras, list(range(1, 21)))

    def test_worker_coalesced_pages_release_every_retired_tk_frame(self):
        self.populate()
        workspace = self.workspace
        workspace.project.master_audio = self.paths[0]
        workspace.project.master_duration = 12.
        trace = _Trace()
        fake = _controller_type(trace)
        with patch.object(player_module, 'MpvController', fake), \
                patch.object(player_module, '_AudioController', fake), \
                patch.object(MulticamPlayer, 'TICK', .01):
            player = MulticamPlayer('fixture-mpv.exe',
                {camera: surface.winfo_id() for camera, surface in workspace.camera_surfaces.items()},
                workspace.program_views[0].winfo_id(), workspace.audio_surface.winfo_id(), workspace.player_queue)
            workspace.player = player
            try:
                player.submit('configure', project=workspace.project)
                self.wait_for(lambda: player._program.active and player._audio.active
                              and all(slot.active for slot in player._slots.values()))
                player.submit('play')
                self.wait_for(lambda: player._playing and player._position > .03)
                protected = {player._program.window, player._audio.window}
                before = len(trace.snapshot())
                retired = dict(workspace.camera_frames)
                trace.cleanup_started.clear()
                trace.stop_gate.clear()
                workspace._change_camera_page(1)
                self.wait_for(trace.cleanup_started.is_set)
                # The worker is disposing page 0. These queued pages are
                # coalesced before they can create any MPV controllers.
                obsolete_windows = set()
                for delta in (1, -1):
                    workspace._change_camera_page(delta)
                    retired.update({f'{delta}_{camera}': frame for camera, frame in workspace.camera_frames.items()})
                    obsolete_windows.update(surface.winfo_id() for surface in workspace.camera_surfaces.values())
                retired.update({f'pending_{window}': frame for window, frame in workspace._retired_camera_frames.items()})
                workspace._change_camera_page(-1)
                retired.update({f'last_{window}': frame for window, frame in workspace._retired_camera_frames.items()})
                current = {camera: surface.winfo_id() for camera, surface in workspace.camera_surfaces.items()}
                self.assertTrue(workspace._retired_camera_frames)
                self.assertTrue(all(frame.winfo_exists() for frame in workspace._retired_camera_frames.values()))
                trace.stop_gate.set()
                self.wait_for(lambda: player.camera_windows == current and not workspace._retired_camera_frames)
                self.assertTrue(all(not frame.winfo_exists() for frame in retired.values()))
                self.assertTrue(all(frame.winfo_exists() for frame in workspace.camera_frames.values()))
                created = {entry[1] for entry in trace.snapshot() if entry[0] == 'create'}
                self.assertTrue(obsolete_windows.isdisjoint(created), 'obsolete page started a decoder')
                protected_calls = [entry for entry in trace.snapshot()[before:] if entry[1] in protected]
                self.assertFalse([entry for entry in protected_calls if entry[0] == 'load' or
                    (entry[0] == 'command' and (entry[2][0] in ('loadfile', 'seek') or
                     entry[2][:2] == ('set_property', 'pause')))])
                self.assertTrue(player._playing)
            finally:
                trace.stop_gate.set()
                player.close()
                self.assertTrue(player.wait_closed(3.), 'page worker did not close')
                workspace.player = None

    def test_pager_titles_and_buttons_fit_supported_sizes_for_14_and_20_cameras(self):
        # ExporterApp scales its 1280x820 minimum with DPI. At 240 DPI the
        # minimum is 3200x2050; a 1280-physical-pixel fixture is unreachable.
        for dpi, width, height in ((96, 1280, 820), (240, 3200, 2050), (240, 3840, 2160)):
            self._destroy_workspace()
            self._create_workspace(dpi=dpi, width=width, height=height)
            for count in (14, 20):
                with self.subTest(dpi=dpi, cameras=count):
                    self.populate(count)
                    workspace = self.workspace
                    for clip in workspace.project.clips:
                        if clip.camera % 3 == 1:
                            clip.camera_make, clip.camera_model = 'Sony', 'ILCE-7M4'
                        elif clip.camera % 3 == 2:
                            clip.camera_make, clip.camera_model = 'Nikon', 'NIKON Z8'
                    workspace._refresh_camera_labels()
                    self.root.update()
                    title = workspace.camera_pager.master
                    title_widgets = [widget for widget in title.winfo_children() if widget.winfo_ismapped()]
                    for widget in title_widgets:
                        self.assertGreaterEqual(widget.winfo_x(), 0)
                        self.assertLessEqual(widget.winfo_x()+widget.winfo_width(), title.winfo_width())
                        self.assertGreaterEqual(widget.winfo_width(), widget.winfo_reqwidth(),
                                                f'{dpi} DPI title control was clipped: {widget}')
                    for index, first in enumerate(title_widgets):
                        for second in title_widgets[index+1:]:
                            self.assertFalse(first.winfo_x() < second.winfo_x()+second.winfo_width()
                                             and second.winfo_x() < first.winfo_x()+first.winfo_width(),
                                             f'{dpi} DPI title controls overlap')
                    self.assertTrue(workspace.camera_pager.winfo_ismapped())
                    for widget in workspace.camera_pager.winfo_children():
                        self.assertTrue(widget.winfo_ismapped())
                        self.assertGreaterEqual(widget.winfo_width(), widget.winfo_reqwidth())
                    pages = (count+8)//9
                    self.assertEqual(workspace.camera_page_var.get(), f'1/{pages}')
                    self.assert_camera_captions_fit(dpi)
                    for page in range(1, pages):
                        workspace._change_camera_page(1)
                        self.root.update()
                        self.assertEqual(workspace.camera_page_var.get(), f'{page+1}/{pages}')
                        self.assertEqual(len(workspace.camera_frames), min(9, count-9*page))
                        self.assert_camera_captions_fit(dpi)
                    self.assertEqual(self.callback_errors, [])

    def assert_camera_captions_fit(self, dpi):
        for camera, button in self.workspace.camera_buttons.items():
            font = tkfont.Font(button, font=button.cget('font'))
            self.assertTrue(button.winfo_ismapped())
            text_width = max(font.measure(line) for line in button.cget('text').splitlines())
            self.assertLessEqual(text_width + 6, button.winfo_width(),
                                 f'{dpi} DPI camera {camera} caption clipped: {button.cget("text")!r}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
