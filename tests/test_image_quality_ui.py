"""Quality compatibility and real Tk controls for full-resolution stabilization."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import tkinter as tk
import unittest

from cache_paths import cache_dir
from gyroflow_core import (IMAGE_FIELDS, PATH_FIELDS, StabilizationSettings, StabilizedRange,
                           cache_signature, settings_from_dict, settings_to_dict, validate_settings)
from image_stabilization_panel import QUALITY_NAMES, image_defaults
try:
    from . import test_image_stabilization_ui as fixture
except ImportError:
    import test_image_stabilization_ui as fixture


class ImageQualityCompatibilityTests(unittest.TestCase):
    def test_legacy_dict_defaults_to_fine_and_quality_validation_is_strict(self):
        self.assertEqual(StabilizationSettings().image_quality, 'fine')
        self.assertEqual(settings_from_dict({'engine': 'image', 'mode': 'manual'}).image_quality, 'fine')
        for quality in ('fast', 'fine'):
            settings = StabilizationSettings(engine='image', image_quality=quality)
            validate_settings(settings)
            self.assertEqual(settings_from_dict(settings_to_dict(settings)), settings)
        for quality in ('FAST', '', None, True, ['fast'], {'quality': 'fast'}):
            with self.subTest(quality=quality), self.assertRaises(ValueError):
                validate_settings(StabilizationSettings(engine='image', image_quality=quality))

    def test_gyroflow_hash_remains_the_exact_legacy_hash_for_both_image_qualities(self):
        with tempfile.TemporaryDirectory(prefix='quality_hash_', dir=cache_dir('tests')) as directory:
            source = Path(directory)/'original.mp4'
            source.write_bytes(b'original recording fixture')
            settings = StabilizationSettings(mode='manual', smoothness=.8, sync_offset=.2)
            original_fields = ('mode', 'smoothness', 'fov', 'zoom_mode', 'zoom_speed', 'horizon_lock',
                'horizon_roll', 'rolling_shutter_ms', 'gyro_path', 'lens_path', 'project_path',
                'gyroflow_path', 'auto_sync', 'sync_search_size', 'sync_offset')
            info = source.stat()
            legacy = {'algorithm': 'Gyroflow CLI range cache v1',
                'source': (str(source.resolve()), info.st_size, info.st_mtime_ns),
                'range': [0., 2.], 'settings': {name: getattr(settings, name) for name in original_fields},
                'dependencies': {name: None for name in PATH_FIELDS}, 'automatic_project': None}
            expected = hashlib.sha256(json.dumps(legacy, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
            self.assertIn('image_quality', IMAGE_FIELDS)
            self.assertEqual(cache_signature(source, 0., 2., settings), expected)
            self.assertEqual(cache_signature(source, 0., 2., replace(settings, image_quality='fast')), expected)


class ImageQualityDialogTests(unittest.TestCase):
    setUp = fixture.ImageStabilizationDialogTests.setUp
    tearDown = fixture.ImageStabilizationDialogTests.tearDown
    open = fixture.ImageStabilizationDialogTests.open
    assert_visible = fixture.ImageStabilizationDialogTests.assert_visible

    def test_new_auto_and_new_manual_recommend_fast_without_changing_five_image_controls(self):
        for mode in ('auto', 'manual'):
            with self.subTest(mode=mode):
                if self.dialog is not None and not self.dialog._closed: self.dialog.close()
                dialog = self.open(mode=mode)
                settings = dialog.read_settings()
                self.assertEqual(settings.image_quality, 'fast')
                self.assertEqual(settings, image_defaults(mode))
                self.assertEqual((settings.image_smoothing, settings.image_shakiness, settings.image_zoom,
                                  settings.image_tripod, settings.image_model), (.5, 5, True, False, 'similarity'))
                self.assertEqual(dialog.quality_var.get(), '快速（推荐）')

    def test_existing_old_image_dict_fills_fine_in_manual_mode_and_submits_it(self):
        self.ws.get_selected_stabilization.return_value = {'settings': {
            'engine': 'image', 'mode': 'manual', 'image_smoothing': 1.2,
            'image_shakiness': 7, 'image_model': 'translation'}}
        dialog = self.open(mode='manual')
        self.assertEqual(dialog.quality_var.get(), '精细')
        self.assertEqual(dialog.read_settings().image_quality, 'fine')
        dialog.start()
        settings = self.ws.start_stabilization.call_args.args[0]
        self.assertEqual((settings.image_quality, settings.image_smoothing, settings.image_shakiness), ('fine', 1.2, 7))
        self.error_dialog.assert_not_called()

    def test_saved_fast_and_fine_roundtrip_and_manual_refill(self):
        cache = self.folder/'stabilized.mp4'; cache.write_bytes(b'generated range cache')
        for quality in ('fast', 'fine'):
            with self.subTest(quality=quality):
                if self.dialog is not None and not self.dialog._closed: self.dialog.close()
                expected = replace(image_defaults('manual'), image_quality=quality)
                self.ws.project.stabilizations = [StabilizedRange(3., 7., 1, 'a', expected, str(cache), 'a'*64, 3., 7.)]
                path = self.folder/f'{quality}.multicam.json'
                self.ws.project.save(path)
                self.ws.project = type(self.ws.project).load(path)
                self.ws.get_selected_stabilization.return_value = self.ws.project.stabilizations[0]
                dialog = self.open(mode='manual')
                self.assertEqual(dialog.read_settings(), expected)
                self.assertEqual(dialog.quality_var.get(), next(name for name, value in QUALITY_NAMES.items() if value == quality))

    def test_auto_quality_choice_is_honored_and_recommendation_restores_fast(self):
        self.ws.get_selected_stabilization.return_value = replace(image_defaults('manual'), image_quality='fine')
        dialog = self.open(mode='auto')
        self.assertEqual(dialog.read_settings().image_quality, 'fast')
        self.assertEqual(str(dialog.quality_combo.cget('state')), 'readonly')
        self.assertTrue(all(str(widget.cget('state')) == 'disabled' for widget, _ in dialog._manual_widgets))
        dialog.quality_var.set('精细')
        self.assertEqual(dialog.read_settings().image_quality, 'fine')
        dialog.auto_button.invoke()
        self.assertEqual(dialog.read_settings().image_quality, 'fast')
        dialog.quality_var.set('精细')
        dialog.start()
        self.ws.start_stabilization.assert_called_once_with(replace(image_defaults(), image_quality='fine'))

    def test_unknown_quality_does_not_start_stabilization(self):
        dialog = self.open(mode='manual')
        dialog.quality_var.set('invalid')
        dialog.start()
        self.ws.start_stabilization.assert_not_called()
        self.error_dialog.assert_called_once()
        self.assertFalse(dialog._closed)

    def test_quality_help_and_fixed_footer_are_accessible_at_96_and_240_dpi(self):
        for dpi, scale in ((96, 1.), (240, 2.5)):
            with self.subTest(dpi=dpi):
                if self.dialog is not None and not self.dialog._closed: self.dialog.close()
                self.root.tk.call('tk', 'scaling', dpi/72)
                self.ws.ui_scale = scale
                dialog = self.open(mode='manual')
                dialog.geometry(f'{round(500*scale)}x{round(420*scale)}+10000+10000')
                self.root.update()
                buttons = [dialog.start_button, dialog.remove_button, dialog.close_button]
                positions = [button.winfo_rooty() for button in buttons]
                for button in buttons: self.assert_visible(button, dialog)
                dialog.canvas.yview_moveto(0.)
                self.root.update()
                self.assertTrue(dialog.quality_combo.winfo_ismapped())
                self.assert_visible(dialog.quality_combo, dialog)
                note = next(child for child in dialog.body.winfo_children()
                            if isinstance(child, tk.Label) and '双线性插值' in child.cget('text'))
                self.assertIn('双三次插值', note.cget('text'))
                self.assertIn('最终分辨率', note.cget('text'))
                dialog.canvas.yview_moveto(1.)
                self.root.update()
                self.assertGreater(dialog.canvas.yview()[0], 0.)
                self.assertEqual([button.winfo_rooty() for button in buttons], positions)
                for button in buttons: self.assert_visible(button, dialog)
                ordered = sorted(buttons, key=lambda button: button.winfo_rootx())
                for first, second in zip(ordered, ordered[1:]):
                    self.assertLessEqual(first.winfo_rootx()+first.winfo_width(), second.winfo_rootx())
                self.assertEqual(self.errors, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
