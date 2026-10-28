"""Configurable 1..6 by 1..6 monitoring, with original native surfaces retained."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import time
import tkinter as tk
from tkinter import font as tkfont
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from multicam_core import CameraClip, Cut, MulticamProject
from multicam_ui import MulticamWorkspace, PINK
from exporter_core import MediaInfo
try:
    from . import test_timeline_drag_merge as fixture
    from .test_transport_controls import RecordingPlayer
except ImportError:
    import test_timeline_drag_merge as fixture
    from test_transport_controls import RecordingPlayer


class CameraGridLayoutTests(unittest.TestCase):
    def setUp(self):
        fixture.TimelineDropTests.setUp(self)
        self.original_scaling = self.root.tk.call('tk', 'scaling')
        self.temp = tempfile.TemporaryDirectory(prefix='okk-grid-layout-')
        self.addCleanup(self.temp.cleanup)
        self.root._begin_shared_progress = Mock()

    def tearDown(self):
        self.root.tk.call('tk', 'scaling', self.original_scaling)
        fixture.TimelineDropTests.tearDown(self)

    def populate(self, count):
        ws = self.workspace
        ws.project = MulticamProject(clips=[CameraClip(str(camera), camera, f'{camera}.mp4', 0., 12.)
            for camera in range(1, count+1)], cuts=[Cut(0., 1)] if count else [])
        ws.camera_page = 0
        ws._displayed_camera = None
        ws._refresh()
        self.root.update_idletasks()

    def handles(self):
        return {camera: surface.winfo_id() for camera, surface in self.workspace.camera_surfaces.items()}

    def assert_cells_fit(self):
        ws = self.workspace
        self.root.update_idletasks()
        for index, camera in enumerate(ws._visible_camera_ids):
            frame, surface, button = ws.camera_frames[camera], ws.camera_surfaces[camera], ws.camera_buttons[camera]
            self.assertEqual((frame.grid_info()['row'], frame.grid_info()['column']),
                             divmod(index, ws.camera_columns))
            self.assertTrue(surface.winfo_ismapped())
            self.assertGreater(surface.winfo_width(), 1)
            self.assertGreater(surface.winfo_height(), 1)
            self.assertLessEqual(frame.winfo_x()+frame.winfo_width(), ws.camera_grid.winfo_width())
            self.assertLessEqual(frame.winfo_y()+frame.winfo_height(), ws.camera_grid.winfo_height())
            width = tkfont.Font(button, font=button.cget('font')).measure(button.cget('text'))
            self.assertLessEqual(width+4, button.winfo_width())

    def test_six_by_six_keeps_first_nine_hwnds_and_all_thirty_six_fit(self):
        self.populate(36)
        ws = self.workspace
        original, model = self.handles(), copy.deepcopy(ws.project)
        player = ws.player = RecordingPlayer()
        output = [view.winfo_id() for view in [ws.program_surface, *ws.program_views, ws.audio_surface]]
        ws.set_camera_grid(6, 6)
        self.assertEqual(ws._visible_camera_ids, tuple(range(1, 37)))
        self.assertEqual(len(self.handles()), 36)
        self.assertEqual({camera: self.handles()[camera] for camera in original}, original)
        self.assertEqual([action for action, _ in player.commands], ['camera_windows'])
        self.assertEqual(len(player.commands[-1][1]['windows']), 36)
        self.assertEqual(output, [view.winfo_id() for view in [ws.program_surface, *ws.program_views, ws.audio_surface]])
        self.assert_cells_fit()
        self.assertEqual(ws.project, model)
        self.assertEqual(ws.undo_stack, [])
        self.assertEqual(self.errors, [])

    def test_two_by_five_then_five_by_two_reflows_same_ten_windows_without_decoder_reload(self):
        self.populate(20)
        ws = self.workspace
        ws.set_camera_grid(2, 5)
        handles = self.handles()
        ws.player = RecordingPlayer()
        ws.set_camera_grid(5, 2)
        self.assertEqual(ws._visible_camera_ids, tuple(range(1, 11)))
        self.assertEqual(self.handles(), handles)
        self.assertEqual(ws.player.commands, [])
        self.assertEqual(ws.camera_page_var.get(), '1/2')
        self.assert_cells_fit()
        self.assertEqual(self.errors, [])

    def test_capacity_pagination_digit_keys_never_select_a_hidden_camera(self):
        self.populate(73)
        ws = self.workspace
        ws.set_camera_grid(6, 6)
        self.assertEqual(ws.camera_page_var.get(), '1/3')
        ws._change_camera_page(1)
        self.assertEqual(ws._visible_camera_ids, tuple(range(37, 73)))
        with patch.object(ws, 'switch') as switch:
            ws._key(SimpleNamespace(widget=ws.timeline, state=0, keysym='9', char='9'))
            switch.assert_called_once_with(45)
        ws._change_camera_page(1)
        self.assertEqual(ws._visible_camera_ids, (73,))
        self.assertEqual(ws.camera_page_var.get(), '3/3')
        with patch.object(ws, 'switch') as switch:
            ws._key(SimpleNamespace(widget=ws.timeline, state=0, keysym='9', char='9'))
            switch.assert_not_called()
            ws._key(SimpleNamespace(widget=ws.timeline, state=0, keysym='1', char='1'))
            switch.assert_called_once_with(73)
        ws.camera_page = 0
        ws.set_camera_grid(1, 1)
        self.assertEqual(len(ws._visible_camera_ids), 1)
        with patch.object(ws, 'switch') as switch:
            ws._key(SimpleNamespace(widget=ws.timeline, state=0, keysym='2', char='2'))
            switch.assert_not_called()
        self.assertEqual(str(ws.camera_next.cget('state')), 'disabled')
        self.assertEqual(self.errors, [])

    def test_six_by_one_and_one_by_six_keep_visible_video_below_captions(self):
        self.populate(36)
        ws = self.workspace
        for rows, columns in ((6, 1), (1, 6), (5, 2)):
            with self.subTest(grid=(rows, columns)):
                ws.set_camera_grid(rows, columns)
                self.root.update()
                self.assert_cells_fit()
                for surface in ws.camera_surfaces.values():
                    self.assertGreaterEqual(surface.winfo_height(), 5)
        self.assertEqual(self.errors, [])

    def test_camera_thirty_six_mouse_choice_highlight_and_cut_editor_are_correct(self):
        self.populate(40)
        ws = self.workspace
        ws.set_camera_grid(6, 6)
        ws.current = 2.
        ws.camera_buttons[36].invoke()
        self.assertEqual(ws.project.camera_at(2.), 36)
        self.assertEqual(ws.camera_frames[36].cget('bg'), PINK)
        self.assertEqual(ws.cut_camera_combo.cget('values'), tuple(str(camera) for camera in range(1, 41)))
        ws.set_camera_grid(2, 5)
        self.assertIn(36, ws._visible_camera_ids)
        self.assertEqual(ws.camera_frames[36].cget('bg'), PINK)
        ws.undo()
        self.assertEqual(ws.project.camera_at(2.), 1)
        self.assertEqual((ws.camera_rows, ws.camera_columns), (2, 5))
        self.assertEqual(self.errors, [])

    def test_smaller_grid_retires_only_removed_windows_until_player_acknowledges(self):
        self.populate(36)
        ws = self.workspace
        ws.set_camera_grid(6, 6)
        old = self.handles()
        ws.player = RecordingPlayer()
        ws.set_camera_grid(2, 5)
        removed = {old[camera] for camera in range(11, 37)}
        self.assertEqual(set(ws._retired_camera_frames), removed)
        self.assertEqual(self.handles(), {camera: old[camera] for camera in range(1, 11)})
        self.assertTrue(all(frame.winfo_exists() for frame in ws._retired_camera_frames.values()))
        ws.player_queue.put(('camera_windows_released', list(removed)))
        ws._poll()
        self.assertEqual(ws._retired_camera_frames, {})
        self.assertEqual(self.handles(), {camera: old[camera] for camera in range(1, 11)})
        self.assertEqual(self.errors, [])

    def test_grid_preference_roundtrip_validation_and_default_reset_do_not_edit_project(self):
        self.populate(36)
        ws = self.workspace
        ws._dock_layout_path = Path(self.temp.name) / 'workspace-layout.json'
        model = copy.deepcopy(ws.project)
        ws.set_camera_grid(2, 5)
        self.assertTrue(ws.save_workspace_layout())
        payload = json.loads(ws._dock_layout_path.read_text(encoding='utf-8'))
        self.assertEqual((payload['workspace_chrome']['camera_rows'], payload['workspace_chrome']['camera_columns']), (2, 5))
        ws.set_camera_grid(6, 6)
        self.assertTrue(ws._load_workspace_layout())
        self.assertEqual((ws.camera_rows, ws.camera_columns), (2, 5))
        for rows, columns in ((0, 3), (7, 3), (3, 7), (True, 2), (2.5, 3), ('bad', 2)):
            with self.subTest(values=(rows, columns)), self.assertRaises(ValueError):
                ws.set_camera_grid(rows, columns)
        self.assertEqual((ws.camera_rows, ws.camera_columns), (2, 5))
        ws.reset_workspace_layout()
        self.assertEqual((ws.camera_rows, ws.camera_columns), (3, 3))
        self.assertEqual(ws.project, model)
        self.assertEqual(ws.undo_stack, [])
        self.assertEqual(self.errors, [])

    def test_batch_import_forty_creates_thirty_six_visible_and_four_on_next_page(self):
        ws = self.workspace
        self.populate(0)
        ws.set_camera_grid(6, 6)
        paths = [Path(self.temp.name)/f'camera-{index}.mp4' for index in range(40)]
        for path in paths: path.write_bytes(b'fixture')
        def probe(_binary, path):
            return MediaInfo(path=path, duration=12., width=96, height=54, fps=30., has_audio=False)
        with patch('multicam_ui.filedialog.askopenfilenames', return_value=tuple(map(str, paths))), \
                patch('camera_import.probe_media', side_effect=probe):
            ws.add_camera()
            deadline = time.monotonic()+10
            while ws.busy and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.005)
        self.assertFalse(ws.busy)
        self.assertEqual(ws.project.cameras, list(range(1, 41)))
        self.assertEqual(ws._visible_camera_ids, tuple(range(1, 37)))
        ws._change_camera_page(1)
        self.assertEqual(ws._visible_camera_ids, (37, 38, 39, 40))
        self.assertEqual(len(ws.undo_stack), 1)
        ws.undo()
        self.assertEqual(ws.project.clips, [])
        self.assertEqual((ws.camera_rows, ws.camera_columns), (6, 6))
        self.assertEqual(self.errors, [])

    def test_six_by_six_surfaces_fit_high_dpi_and_resize_without_recreation(self):
        self.workspace.close(); self.workspace.destroy()
        for pending in self.root.tk.call('after', 'info'): self.root.tk.call('after', 'cancel', pending)
        self.root.tk.call('tk', 'scaling', 240/72)
        self.root.geometry('3200x2050+10000+10000')
        self.workspace = MulticamWorkspace(self.root, self.root)
        self.workspace.pack(fill='both', expand=True)
        self.populate(36)
        self.workspace.set_camera_grid(6, 6)
        self.root.update()
        handles = self.handles()
        self.assert_cells_fit()
        self.root.geometry('3840x2160+10000+10000')
        self.root.update()
        self.assert_cells_fit()
        self.assertEqual(self.handles(), handles)
        self.assertEqual(self.errors, [])


if __name__ == '__main__':
    unittest.main()
