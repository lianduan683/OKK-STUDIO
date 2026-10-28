"""Off-screen real Tk settings for image-motion stabilization of one Cut."""
from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gyroflow_core import StabilizationSettings, StabilizedRange
from image_stabilization_panel import ImageStabilizationDialog, image_defaults, open_image_stabilization_dialog
from multicam_core import CameraClip, Cut, MulticamProject
from stabilization_panel import open_stabilization_dialog


class ImageStabilizationDialogTests(unittest.TestCase):
    def setUp(self):
        try: self.root = tk.Tk()
        except tk.TclError as error: self.skipTest(str(error))
        self.root.withdraw()
        self.root.tk.call('tk', 'scaling', 96/72)
        self.root.attributes('-alpha', 0)
        self.root.overrideredirect(True)
        self.root.geometry('1280x820+10000+10000')
        self.errors = []
        self.root.report_callback_exception = lambda *details: self.errors.append(details)
        self.temp = tempfile.TemporaryDirectory(prefix='image_stabilization_ui_')
        self.folder = Path(self.temp.name)
        self.source = self.folder/'Camera A.mp4'
        self.source.write_bytes(b'original video fixture')
        self.ws = tk.Frame(self.root)
        self.ws.pack(fill='both', expand=True)
        self.ws.ui_scale = 1.
        self.ws.selected_program_range = (3., 7.)
        self.ws.project = MulticamProject(
            clips=[CameraClip('a', 1, str(self.source), 0., 12.)],
            cuts=[Cut(0., 1)], edit_points=[3., 7.], fps=30.)
        self.ws.get_selected_stabilization = Mock(return_value=None)
        self.ws.start_stabilization = Mock(return_value=True)
        self.ws.remove_selected_stabilization = Mock(return_value=True)
        self.ws.probe_selected_stabilization = Mock()
        self.ws._available = Mock(return_value=True)
        self.dialog = None
        self.gyro_dialog = None
        self.root.deiconify()
        self.root.update()
        self.error_patch = patch('image_stabilization_panel.messagebox.showerror')
        self.error_dialog = self.error_patch.start()

    def tearDown(self):
        for dialog in (self.dialog, self.gyro_dialog):
            if dialog is not None and not dialog._closed: dialog.close()
        for pending in self.root.tk.call('after', 'info'):
            self.root.tk.call('after', 'cancel', pending)
        self.root.destroy()
        self.error_patch.stop()
        self.temp.cleanup()

    def open(self, **kwargs):
        self.dialog = open_image_stabilization_dialog(self.ws, **kwargs)
        self.root.update()
        self.assertEqual(self.errors, [])
        return self.dialog

    def assert_visible(self, widget, ancestor):
        self.assertTrue(widget.winfo_ismapped())
        self.assertGreaterEqual(widget.winfo_rootx(), ancestor.winfo_rootx())
        self.assertGreaterEqual(widget.winfo_rooty(), ancestor.winfo_rooty())
        self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(), ancestor.winfo_rootx()+ancestor.winfo_width()+2)
        self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(), ancestor.winfo_rooty()+ancestor.winfo_height()+2)

    def test_auto_recommends_only_image_settings_and_never_probes_gyro(self):
        original = copy.deepcopy(self.ws.project)
        dialog = self.open(mode='auto')
        self.assertIn('00:00:03.000', dialog.target_var.get())
        self.assertIn('00:00:07.000', dialog.target_var.get())
        self.assertIn('机位 1', dialog.target_var.get())
        self.assertIn('Camera A.mp4', dialog.source_var.get())
        self.assertEqual(dialog.read_settings(), image_defaults())
        self.assertTrue(all(str(widget.cget('state')) == 'disabled' for widget, _ in dialog._manual_widgets))
        self.assert_visible(dialog.start_button, dialog)
        dialog.start()
        self.ws.start_stabilization.assert_called_once_with(image_defaults())
        self.ws.probe_selected_stabilization.assert_not_called()
        self.assertEqual(self.ws.project, original)
        self.assertEqual(self.source.read_bytes(), b'original video fixture')
        self.assertTrue(dialog._closed)
        self.error_dialog.assert_not_called()

    def test_manual_image_controls_submit_exact_values_without_touching_gyro(self):
        original = copy.deepcopy(self.ws.project)
        dialog = self.open(mode='manual')
        dialog.variables['image_smoothing'].set('1.25')
        dialog.variables['image_shakiness'].set('8')
        dialog.model_var.set('仅平移')
        dialog.zoom_var.set(False)
        dialog.tripod_var.set(True)
        dialog.start()
        settings = self.ws.start_stabilization.call_args.args[0]
        self.assertEqual(settings, replace(image_defaults('manual'), image_smoothing=1.25, image_shakiness=8,
                                          image_model='translation', image_zoom=False, image_tripod=True))
        self.assertEqual((settings.gyro_path, settings.lens_path, settings.project_path, settings.gyroflow_path), ('', '', '', ''))
        self.assertEqual(settings.smoothness, StabilizationSettings().smoothness)
        self.assertEqual(self.ws.project, original)
        self.ws.probe_selected_stabilization.assert_not_called()
        self.error_dialog.assert_not_called()

    def test_invalid_numbers_ranges_integer_and_model_never_start(self):
        dialog = self.open(mode='manual')
        for field, value in (('image_smoothing', 'nan'), ('image_smoothing', '-.01'),
                             ('image_smoothing', '5.1'), ('image_shakiness', '1.5'),
                             ('image_shakiness', '0'), ('image_shakiness', '11'),
                             ('image_shakiness', 'inf'), ('image_shakiness', 'no')):
            with self.subTest(field=field, value=value):
                dialog.auto_configure()
                dialog.mode_var.set('manual')
                dialog._mode_changed()
                dialog.variables[field].set(value)
                dialog.start()
                self.ws.start_stabilization.assert_not_called()
                self.assertFalse(dialog._closed)
        dialog.auto_configure()
        dialog.model_var.set('invalid')
        dialog.start()
        self.ws.start_stabilization.assert_not_called()
        self.assertEqual(self.error_dialog.call_count, 9)

    def test_selection_or_same_range_camera_change_never_applies_or_removes(self):
        dialog = self.open()
        self.ws.selected_program_range = (7., 12.)
        dialog.start(); dialog.remove()
        self.ws.start_stabilization.assert_not_called()
        self.ws.remove_selected_stabilization.assert_not_called()
        self.ws.selected_program_range = (3., 7.)
        self.ws.project.clips.append(CameraClip('b', 2, str(self.source), 0., 12.))
        self.ws.project.set_cut(3., 2)
        dialog.start(); dialog.remove()
        self.ws.start_stabilization.assert_not_called()
        self.ws.remove_selected_stabilization.assert_not_called()
        self.assertEqual(self.error_dialog.call_count, 4)
        self.assertFalse(dialog._closed)

    def test_changed_source_with_same_camera_and_range_is_rejected(self):
        dialog = self.open()
        self.ws.project.clips[0].start = -1.
        dialog.start()
        self.ws.start_stabilization.assert_not_called()
        self.error_dialog.assert_called_once()

    def test_existing_image_settings_reload_after_project_save_and_single_window_reuse(self):
        expected = replace(image_defaults('manual'), image_smoothing=2.5, image_shakiness=9,
                           image_zoom=False, image_tripod=True, image_model='translation')
        cache = self.folder/'stabilized.mp4'
        cache.write_bytes(b'processed cache fixture')
        record = StabilizedRange(3., 7., 1, 'a', expected, str(cache), 'a'*64, 3., 7.)
        self.ws.project.stabilizations = [record]
        path = self.folder/'image_settings.multicam.json'
        self.ws.project.save(path)
        self.ws.project = MulticamProject.load(path)
        self.ws.get_selected_stabilization.return_value = self.ws.project.stabilizations[0]
        dialog = self.open()
        self.assertIs(open_image_stabilization_dialog(self.ws), dialog)
        self.assertEqual(dialog.read_settings(), expected)
        dialog.variables['image_smoothing'].set('1.5')
        self.assertEqual(self.ws.project.stabilizations[0].settings, expected)
        dialog.remove()
        self.ws.remove_selected_stabilization.assert_called_once_with()
        self.assertTrue(dialog._closed)
        self.assertIsNone(self.ws._image_stabilization_dialog)

    def test_gyro_settings_and_gyro_window_remain_independent(self):
        gyro = StabilizationSettings(mode='manual', smoothness=1.4, horizon_lock=35., fov=1.1,
                                     auto_sync=False, sync_offset=.25)
        self.ws.get_selected_stabilization.return_value = {'settings': gyro}
        self.gyro_dialog = open_stabilization_dialog(self.ws)
        self.root.update()
        original = copy.deepcopy(gyro)
        dialog = self.open(mode='manual')
        self.assertEqual(dialog.read_settings(), image_defaults('manual'))
        dialog.auto_configure()
        self.assertEqual(dialog.read_settings(), image_defaults())
        self.assertEqual(gyro, original)
        self.assertEqual(self.gyro_dialog.read_settings(), original)
        self.assertIs(self.ws._stabilization_dialog, self.gyro_dialog)
        dialog.close()
        self.assertTrue(self.gyro_dialog.winfo_exists())
        self.assertIs(self.ws._stabilization_dialog, self.gyro_dialog)

    def test_explicit_auto_uses_recommended_defaults_and_can_restore_manual_controls(self):
        expected = replace(image_defaults('manual'), image_smoothing=4., image_shakiness=10, image_zoom=False)
        self.ws.get_selected_stabilization.return_value = {'settings': expected}
        dialog = self.open(mode='auto')
        self.assertEqual(dialog.read_settings(), image_defaults())
        dialog.mode_var.set('manual')
        dialog._mode_changed()
        self.assertTrue(all(str(widget.cget('state')) == state for widget, state in dialog._manual_widgets))
        dialog.variables['image_smoothing'].set('2')
        dialog.auto_button.invoke()
        self.assertEqual(dialog.read_settings(), image_defaults())
        self.assertEqual(expected.image_smoothing, 4.)

    def test_invalid_target_and_busy_workspace_do_not_start_or_open(self):
        self.ws._available.return_value = False
        self.assertIsNone(open_image_stabilization_dialog(self.ws))
        self.ws._available.return_value = True
        self.ws.selected_program_range = None
        dialog = self.open()
        self.assertEqual(str(dialog.start_button.cget('state')), 'disabled')
        dialog.start(); dialog.remove()
        self.ws.start_stabilization.assert_not_called()
        self.ws.remove_selected_stabilization.assert_not_called()
        self.assertEqual(self.error_dialog.call_count, 2)

    def test_range_containing_multiple_camera_cuts_is_not_submitted(self):
        self.ws.project.clips.append(CameraClip('b', 2, str(self.source), 0., 12.))
        self.ws.project.set_cut(5., 2)
        dialog = self.open()
        self.assertEqual(str(dialog.start_button.cget('state')), 'disabled')
        self.assertIn('单个机位', dialog.source_var.get())
        dialog.start()
        self.ws.start_stabilization.assert_not_called()

    def test_backend_rejection_and_error_keep_window_available(self):
        dialog = self.open()
        self.ws.start_stabilization.return_value = False
        dialog.start()
        self.assertFalse(dialog._closed)
        self.ws.start_stabilization.side_effect = RuntimeError('无法分析所选片段')
        dialog.start()
        self.assertFalse(dialog._closed)
        self.error_dialog.assert_called_once()

    def test_dpi_scroll_preserves_visible_footer_and_precision_note(self):
        for dpi, scale in ((96, 1.), (240, 2.5)):
            with self.subTest(dpi=dpi):
                if self.dialog is not None and not self.dialog._closed: self.dialog.close()
                self.root.tk.call('tk', 'scaling', dpi/72)
                self.ws.ui_scale = scale
                self.root.geometry(f'{round(1280*scale)}x{round(820*scale)}+10000+10000')
                dialog = self.open(mode='manual')
                dialog.geometry(f'{round(500*scale)}x{round(390*scale)}+10000+10000')
                self.root.update()
                buttons = [dialog.start_button, dialog.remove_button, dialog.close_button]
                positions = [button.winfo_rooty() for button in buttons]
                for button in buttons: self.assert_visible(button, dialog)
                self.assertLess(dialog.canvas.yview()[1]-dialog.canvas.yview()[0], 1.)
                dialog._wheel(SimpleNamespace(delta=-120))
                dialog.canvas.yview_moveto(1.)
                self.root.update()
                self.assertGreater(dialog.canvas.yview()[0], 0.)
                self.assertEqual([button.winfo_rooty() for button in buttons], positions)
                for button in buttons: self.assert_visible(button, dialog)
                children = list(dialog.body.winfo_children())
                note = next(widget for widget in children if isinstance(widget, tk.Label) and '滤镜内部为8位' in widget.cget('text'))
                self.assertIn('10位/HDR', note.cget('text'))
                self.assert_visible(note, dialog)
                ordered = sorted(buttons, key=lambda widget: widget.winfo_rootx())
                for first, second in zip(ordered, ordered[1:]):
                    self.assertLessEqual(first.winfo_rootx()+first.winfo_width(), second.winfo_rootx())
                self.assertEqual(self.errors, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
