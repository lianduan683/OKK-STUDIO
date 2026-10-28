"""Vendor settings through real isolated Tk widgets, without hardware claims."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from delivery_panel import make_delivery_settings
from exporter_core import Capabilities, MediaInfo
from hardware_encoders import available_codecs, codec_label
from tests import test_delivery_ui as fixture


class VendorUiTests(fixture.DeliveryTkTests):
    # This class intentionally exercises only the vendor scenarios below.
    def setUp(self):
        super().setUp()
        self.app.withdraw()

    def use_caps(self, **values):
        self.app.caps = Capabilities(ffmpeg_path='test-ffmpeg',ffprobe_path='test-ffprobe',**values)
        self.app._apply_gpu_profile()
        self._settle()
        return self.app.caps

    def widget(self, variable):
        return self._bound_widgets(variable)[0]

    def test_amd_only_choices_settings_and_nvenc_controls(self):
        self.use_caps(gpu_name='AMD Radeon 890M',amf_codecs={'h264_amf','hevc_amf','av1_amf'})
        self.assertEqual(self.app._settings().codec,'hevc_amf')
        self.assertIn('AMD AMF',self.app.gpu_status_var.get())
        choices=self.app.codec_combo.cget('values')
        self.assertIn(codec_label('hevc_amf'),choices)
        self.assertFalse(any('NVENC' in value for value in choices))
        self.workspace.delivery_gpuindex_var.set('stale-invalid-nvenc-value')
        self.workspace.delivery_tune_var.set('stale-invalid-nvenc-tune')
        settings,_=make_delivery_settings(self.workspace)
        self.assertEqual((settings.codec,settings.decode_mode,settings.split_encode,settings.multipass,settings.gpu_index,settings.bframes),
                         ('hevc_amf','cpu','disabled','disabled',0,0))
        for variable in (self.app.decode_var,self.app.split_var,self.workspace.delivery_tune_var,
                         self.workspace.delivery_multipass_var,self.workspace.delivery_gpuindex_var,
                         self.workspace.delivery_bframes_var):
            self.assertEqual(str(self.widget(variable).cget('state')),'disabled')

    def test_intel_only_all_families_and_compatible_decode(self):
        self.use_caps(gpu_name='Intel Arc A770',qsv_codecs={'h264_qsv','hevc_qsv','av1_qsv'})
        for codec in ('h264_qsv','hevc_qsv','av1_qsv'):
            self.app.codec_var.set(codec_label(codec))
            self.app.depth_var.set('8-bit' if codec=='h264_qsv' else '10-bit')
            settings,_=make_delivery_settings(self.workspace)
            self.assertEqual(settings.codec,codec)
            self.assertEqual(settings.decode_mode,'cpu')
            self.assertEqual(settings.split_encode,'disabled')
            self.assertFalse(settings.auto_parallel)
        self.assertIn('Intel QSV',self.app.gpu_status_var.get())
        self.app.editor_join_video_output_var.set('MOV / HEVC H.265（自动适配）')
        self.assertEqual(self.app._join_video_output_profile(),('.mov','hevc_qsv'))

    def test_4050_laptop_single_engine_does_not_offer_forced_split(self):
        caps=self.use_caps(gpu_name='NVIDIA GeForce RTX 4050 Laptop GPU',
            nvenc_codecs={'h264_nvenc','hevc_nvenc','av1_nvenc'},cuda_decode=True,
            split_encode={'hevc_nvenc','av1_nvenc'})
        self.assertTrue(caps.gpu_profile.in_supported_range)
        self.assertEqual(caps.gpu_profile.nvenc_engines,1)
        self.assertEqual(self.app.parallel_var.get(),1)
        self.assertFalse(any('强制' in value for value in self.app.split_combo.cget('values')))
        self.assertEqual(str(self.app.split_combo.cget('state')),'disabled')
        settings,_=make_delivery_settings(self.workspace)
        self.assertEqual(settings.codec,'hevc_nvenc')
        self.assertEqual(settings.decode_mode,'cuda')

    def test_cpu_only_is_usable_and_unsupported_old_encoder_is_rejected(self):
        self.use_caps(gpu_name='GPU not detected')
        settings,_=make_delivery_settings(self.workspace)
        self.assertIn(settings.codec,{'libx264','libx265'})
        self.assertEqual(settings.decode_mode,'cpu')
        self.assertNotIn('NVIDIA',self.app.gpu_status_var.get())
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source.mp4';source.write_bytes(b'source')
            self.app.input_var.set(str(source));self.app.output_var.set(str(Path(folder)/'out.mp4'))
            self.app.codec_var.set(codec_label('hevc_nvenc'))
            with patch('rtx5090_exporter.messagebox.showerror') as error:
                self.assertIsNone(self.app._validate())
            self.assertIn('hevc_nvenc',error.call_args.args[1])
            self.assertIn('已检测可用',error.call_args.args[1])

    def test_h264_hardware_10bit_and_unverified_10bit_fail_before_dispatch(self):
        self.use_caps(gpu_name='AMD Radeon',amf_codecs={'h264_amf','hevc_amf'},
                      hardware_probe_complete=True,ten_bit_codecs=set())
        self.app.depth_var.set('10-bit')
        for codec in ('h264_amf','hevc_amf'):
            self.app.codec_var.set(codec_label(codec))
            with self.assertRaisesRegex(ValueError,'10-bit'):
                make_delivery_settings(self.workspace)

    def test_multiple_vendors_are_available_without_nvidia_only_selection(self):
        caps=self.use_caps(gpu_name='NVIDIA GeForce RTX 5090 Laptop GPU',
            gpu_names=['NVIDIA GeForce RTX 5090 Laptop GPU','AMD Radeon 890M','Intel Arc'],
            nvenc_codecs={'hevc_nvenc'},amf_codecs={'hevc_amf'},qsv_codecs={'h264_qsv'},cuda_decode=True)
        values=self.app.codec_combo.cget('values')
        for codec in available_codecs(caps):
            self.assertIn(codec_label(codec),values)
        self.app.codec_var.set(codec_label('hevc_amf'))
        self.assertIn('890M',self.app.gpu_status_var.get())
        self.assertNotIn('5090',self.app.gpu_status_var.get())
        self.assertEqual(self.app._settings().decode_mode,'cpu')
        self.assertEqual(str(self.app.decode_combo.cget('state')),'disabled')

    def test_join_format_keeps_container_setup_independent_of_encoder_detection(self):
        self.use_caps(gpu_name='GPU not detected',software_codecs={'libx264'})
        self.app.editor_join_video_output_var.set('MOV / HEVC H.265（自动适配）')
        self.assertEqual(self.app._join_output_suffix(),'.mov')
        with self.assertRaisesRegex(ValueError,'HEVC'):
            self.app._join_video_output_profile()
        self.app.editor_join_video_output_var.set('MP4 / H.264（自动适配）')
        self.assertEqual(self.app._join_video_output_profile(),('.mp4','libx264'))


# Inherited delivery tests are already run by their own suite; avoid duplication.
for _name in tuple(name for name in vars(fixture.DeliveryTkTests) if name.startswith('test_')):
    setattr(VendorUiTests,_name,None)

if __name__=='__main__':unittest.main()
