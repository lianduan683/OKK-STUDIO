"""Actual off-screen Tk interaction for selected-cut Gyroflow settings.

No backend subprocess, GPU, source mutation or user desktop input is used.
"""
from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gyroflow_core import StabilizationSettings, default_settings
from multicam_core import CameraClip, Cut, MulticamProject
from stabilization_panel import StabilizationDialog, open_stabilization_dialog


class StabilizationDialogTests(unittest.TestCase):
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
        self.temp = tempfile.TemporaryDirectory(prefix='gyroflow_ui_')
        self.folder = Path(self.temp.name)
        self.source = self.folder/'Camera A.mp4'
        self.source.write_bytes(b'fixture path only')
        self.ws = tk.Frame(self.root)
        self.ws.pack(fill='both', expand=True)
        self.ws.ui_scale = 1.0
        self.ws.selected_program_range = (3.,7.)
        self.ws.project = MulticamProject(clips=[CameraClip('a',1,str(self.source),0.,12.)],
                                         cuts=[Cut(0.,1)], edit_points=[3.,7.], fps=30.)
        self.ws.get_selected_stabilization = Mock(return_value=None)
        self.ws.start_stabilization = Mock(return_value=True)
        self.ws.remove_selected_stabilization = Mock(return_value=True)
        self.ws._available = Mock(return_value=True)
        self.dialog = None
        self.root.deiconify()
        self.root.update()
        self.error_patch = patch('stabilization_panel.messagebox.showerror')
        self.error_dialog = self.error_patch.start()

    def tearDown(self):
        if self.dialog is not None and not self.dialog._closed: self.dialog.close()
        for pending in self.root.tk.call('after', 'info'):
            self.root.tk.call('after', 'cancel', pending)
        self.root.destroy()
        self.error_patch.stop()
        self.temp.cleanup()

    def open(self, **kwargs):
        self.dialog = open_stabilization_dialog(self.ws, **kwargs)
        self.root.update()
        self.assertEqual(self.errors, [])
        return self.dialog

    def assert_visible(self, widget, ancestor):
        top = widget.winfo_rooty()
        self.assertTrue(widget.winfo_ismapped())
        self.assertGreaterEqual(top, ancestor.winfo_rooty())
        self.assertLessEqual(top+widget.winfo_height(), ancestor.winfo_rooty()+ancestor.winfo_height()+2)

    def test_default_auto_scope_is_selected_cut_and_never_claims_gyro_without_probe(self):
        dialog = self.open()
        self.assertIn('00:00:03.000',dialog.target_var.get())
        self.assertIn('00:00:07.000',dialog.target_var.get())
        self.assertIn('机位 1',dialog.target_var.get())
        self.assertIn('Camera A.mp4',dialog.source_var.get())
        self.assertEqual(dialog.read_settings(),default_settings())
        self.assertIn('待',dialog.gyro_status_var.get())
        self.assertNotIn('已检测到',dialog.gyro_status_var.get())
        self.assertTrue(all(str(widget.cget('state'))=='disabled' for widget,_ in dialog._manual_widgets))
        self.assert_visible(dialog.start_button,dialog)
        self.assert_visible(dialog.close_button,dialog)
        self.ws.start_stabilization.assert_not_called()

    def test_manual_parameters_submit_once_without_mutating_other_cuts(self):
        before=copy.deepcopy(self.ws.project)
        dialog=self.open(mode='manual')
        dialog.variables['smoothness'].set('0.8')
        dialog.variables['fov'].set('1.1')
        dialog.variables['zoom_speed'].set('5')
        dialog.variables['horizon_lock'].set('60')
        dialog.variables['horizon_roll'].set('-2')
        dialog.variables['rolling_shutter_ms'].set('12.5')
        dialog.variables['sync_offset'].set('-0.2')
        dialog.zoom_var.set('固定缩放')
        dialog.auto_sync_var.set(False)
        dialog.start()
        self.ws.start_stabilization.assert_called_once()
        settings=self.ws.start_stabilization.call_args.args[0]
        self.assertIsInstance(settings,StabilizationSettings)
        self.assertEqual((settings.mode,settings.smoothness,settings.fov,settings.zoom_mode),('manual',.8,1.1,'static'))
        self.assertEqual((settings.horizon_lock,settings.horizon_roll,settings.rolling_shutter_ms),(60.,-2.,12.5))
        self.assertEqual(settings.sync_offset,-.2)
        self.assertFalse(settings.auto_sync)
        self.assertEqual(self.ws.project,before)
        self.assertTrue(dialog._closed)
        self.error_dialog.assert_not_called()

    def test_invalid_number_and_missing_path_never_start_backend(self):
        dialog=self.open(mode='manual')
        dialog.variables['smoothness'].set('nan')
        dialog.start()
        self.error_dialog.assert_called_once()
        self.ws.start_stabilization.assert_not_called()
        self.assertFalse(dialog._closed)
        dialog.variables['smoothness'].set('0.5')
        dialog.paths['gyro_path'].set(str(self.folder/'missing.gcsv'))
        dialog.start()
        self.assertEqual(self.error_dialog.call_count,2)
        self.ws.start_stabilization.assert_not_called()

    def test_auto_configure_keeps_explicit_paths_and_uses_real_probe_result(self):
        gyro=self.folder/'motion.gcsv';gyro.write_text('GYROFLOW IMU LOG\n',encoding='utf-8')
        received=[]
        def probe(callback,settings=None):
            received.append(settings)
            callback({'has_gyro':True,'has_lens':False,'message':'镜头需要指定'})
        self.ws.probe_selected_stabilization=probe
        dialog=self.open(mode='manual')
        dialog.paths['gyro_path'].set(str(gyro))
        dialog.variables['smoothness'].set('0.9')
        dialog.auto_configure()
        self.assertEqual(dialog.mode_var.get(),'auto')
        self.assertEqual(dialog.variables['smoothness'].get(),'0.5')
        self.assertEqual(dialog.paths['gyro_path'].get(),str(gyro))
        self.assertEqual(len(received),1)
        self.assertEqual(received[0].gyro_path,str(gyro))
        self.assertIn('已检测到',dialog.gyro_status_var.get())
        self.assertIn('未检测到',dialog.lens_status_var.get())
        self.ws.start_stabilization.assert_not_called()

    def test_probe_failure_is_visible_and_late_result_after_close_is_ignored(self):
        callbacks=[]
        self.ws.probe_selected_stabilization=lambda callback,settings=None: callbacks.append(callback)
        dialog=self.open()
        dialog.request_probe()
        self.assertTrue(dialog._probe_pending)
        self.assertEqual(str(dialog.probe_button.cget('state')),'disabled')
        callbacks[0]({'error':'没有陀螺仪数据'})
        self.assertIn('没有陀螺仪数据',dialog.status_var.get())
        self.assertNotIn('已检测到',dialog.gyro_status_var.get())
        dialog.close()
        callbacks[0]({'has_gyro':True,'has_lens':True})
        self.assertEqual(self.errors,[])

    def test_probe_result_for_old_input_is_not_reported_as_current_success(self):
        callbacks=[]
        self.ws.probe_selected_stabilization=lambda callback,settings=None: callbacks.append(callback)
        dialog=self.open()
        dialog.request_probe()
        dialog.paths['gyro_path'].set(str(self.folder/'different.gcsv'))
        callbacks[0]({'has_gyro':True,'has_lens':True})
        self.assertNotIn('已检测到',dialog.gyro_status_var.get())
        self.assertIn('过期',dialog.status_var.get())

    def test_selection_change_does_not_apply_or_remove_from_another_cut(self):
        dialog=self.open()
        self.ws.selected_program_range=(7.,12.)
        dialog.start();dialog.remove()
        self.ws.start_stabilization.assert_not_called()
        self.ws.remove_selected_stabilization.assert_not_called()
        self.assertEqual(self.error_dialog.call_count,2)
        self.assertFalse(dialog._closed)

    def test_existing_settings_are_restored_and_dialog_reuses_only_one_window(self):
        expected=replace(default_settings(mode='manual'),smoothness=.7,zoom_mode='none',auto_sync=False)
        self.ws.get_selected_stabilization.return_value={'settings':expected}
        dialog=self.open()
        same=open_stabilization_dialog(self.ws)
        self.assertIs(same,dialog)
        self.assertEqual(dialog.read_settings(),expected)
        dialog.remove()
        self.ws.remove_selected_stabilization.assert_called_once_with()
        self.assertTrue(dialog._closed)
        self.assertIsNone(self.ws._stabilization_dialog)

    def test_high_dpi_body_scrolls_but_action_buttons_remain_visible(self):
        self.root.tk.call('tk','scaling',240/72)
        self.ws.ui_scale=2.5
        self.root.geometry('3840x2400+10000+10000')
        dialog=self.open(mode='manual')
        buttons=[dialog.start_button,dialog.remove_button,dialog.close_button]
        old_positions=[widget.winfo_rooty() for widget in buttons]
        for widget in buttons:self.assert_visible(widget,dialog)
        dialog.canvas.yview_moveto(1.)
        self.root.update()
        self.assertEqual([widget.winfo_rooty() for widget in buttons],old_positions)
        for left,right in zip(sorted(buttons,key=lambda item:item.winfo_rootx()),sorted(buttons,key=lambda item:item.winfo_rootx())[1:]):
            self.assertLessEqual(left.winfo_rootx()+left.winfo_width(),right.winfo_rootx()+2)
        self.assertEqual(self.errors,[])


if __name__=='__main__':unittest.main()
