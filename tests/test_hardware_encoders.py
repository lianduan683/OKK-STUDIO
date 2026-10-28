"""GPU vendors are selected from usable codecs, with isolated encoder options."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import exporter_core as core
from exporter_core import Capabilities, ExportSettings, MediaInfo, build_encode_command, build_video_args
from hardware_encoders import (HARDWARE_ENCODERS, available_codecs, codec_from_label, codec_label,
                               encoder_vendor, is_hardware_codec, preferred_codec)


class HardwareEncoderTests(unittest.TestCase):
    def setUp(self):
        self.media=MediaInfo('source.mp4',duration=2,width=1920,height=1080,fps=30,bit_depth=8)

    def test_labels_roundtrip_and_known_codecs_only(self):
        for codec in HARDWARE_ENCODERS|{'libx264','libx265'}:
            self.assertEqual(codec_from_label(codec_label(codec)),codec)
        with self.assertRaises(ValueError):codec_from_label('Hacked codec')
        self.assertFalse(is_hardware_codec('madeup_amf'))

    def test_legacy_caps_and_software_fallback_remain_usable(self):
        caps=Capabilities(nvenc_codecs={'h264_nvenc'})
        self.assertEqual(preferred_codec(caps,'hevc'),'h264_nvenc')
        self.assertEqual(preferred_codec(Capabilities(),'h264'),'libx264')
        self.assertEqual(available_codecs(Capabilities()),{'libx264','libx265'})

    def test_h264_proxy_does_not_switch_to_hevc_when_h264_hardware_is_absent(self):
        caps=Capabilities(amf_codecs={'hevc_amf'},gpu_vendor='amd')
        self.assertEqual(preferred_codec(caps,'h264'),'libx264')

    def test_vendor_preference_keeps_both_adapters_available(self):
        caps=Capabilities(nvenc_codecs={'hevc_nvenc'},amf_codecs={'hevc_amf'},qsv_codecs={'h264_qsv'},gpu_vendor='amd')
        self.assertEqual(preferred_codec(caps),'hevc_amf')
        self.assertEqual(preferred_codec(caps,vendor='nvidia'),'hevc_nvenc')
        self.assertEqual(caps.hardware_codecs,{'hevc_nvenc','hevc_amf','h264_qsv'})

    def test_amf_qsv_and_software_do_not_receive_nvenc_options_or_cuda(self):
        for codec in ['h264_amf','hevc_amf','av1_amf','h264_qsv','hevc_qsv','av1_qsv','libx264','libx265']:
            with self.subTest(codec=codec):
                settings=ExportSettings(codec=codec,preset='p2',decode_mode='cuda',bit_depth='8-bit',split_encode='3')
                command=build_encode_command('ffmpeg','in.mp4','out.mkv',settings,self.media,Capabilities(scale_cuda=True))
                joined=' '.join(command)
                for option in ['-hwaccel','hwupload_cuda','-gpu','-cq','-tune','-multipass','-split_encode_mode','-rc-lookahead']:
                    self.assertNotIn(option,command if option.startswith('-') else joined)
                self.assertIn(codec,command)
                self.assertEqual(command[command.index('-c:v')+1],codec)

    def test_amf_cqp_and_qsv_quality_and_bitrate_are_vendor_specific(self):
        amf=build_video_args(ExportSettings(codec='hevc_amf',decode_mode='cpu'),self.media,Capabilities())
        qsv=build_video_args(ExportSettings(codec='hevc_qsv',decode_mode='cpu'),self.media,Capabilities())
        self.assertEqual(amf[amf.index('-rc')+1],'cqp');self.assertIn('-qp_i',amf)
        self.assertIn('-global_quality',qsv);self.assertNotIn('-rc',qsv)
        for codec in ['hevc_amf','hevc_qsv','libx264']:
            args=build_video_args(ExportSettings(codec=codec,video_bitrate='12M',maxrate='16M'),self.media,Capabilities())
            self.assertEqual(args[args.index('-b:v')+1],'12M')
            self.assertEqual(args[args.index('-maxrate')+1],'16M')

    def test_h264_hardware_auto_uses_8bit_and_explicit_10bit_is_rejected(self):
        media=MediaInfo('10bit.mov',width=3840,height=2160,fps=60,bit_depth=10)
        for codec in ['h264_nvenc','h264_amf','h264_qsv']:
            self.assertEqual(core._target_depth(ExportSettings(codec=codec),media),8)
            with self.assertRaisesRegex(ValueError,'H.264'):
                build_video_args(ExportSettings(codec=codec,bit_depth='10-bit'),media,Capabilities())

    def test_probe_complete_enforces_real_10bit_support_but_manual_caps_are_compatible(self):
        settings=ExportSettings(codec='hevc_amf',bit_depth='10-bit')
        caps=Capabilities(amf_codecs={'hevc_amf'},hardware_probe_complete=True)
        with self.assertRaisesRegex(ValueError,'10-bit'):build_video_args(settings,self.media,caps)
        caps.ten_bit_codecs.add('hevc_amf')
        args=build_video_args(settings,self.media,caps)
        self.assertEqual(args[args.index('-profile:v')+1],'main10')
        self.assertIn('main10',build_video_args(ExportSettings(codec='hevc_nvenc',bit_depth='10-bit'),self.media,Capabilities()))

    def test_qsv_zero_quality_uses_highest_valid_icq_value(self):
        args=build_video_args(ExportSettings(codec='hevc_qsv',quality=0),self.media,Capabilities())
        self.assertEqual(args[args.index('-global_quality')+1],'1')

    def test_qsv_maxrate_without_target_bitrate_selects_constrained_vbr(self):
        args=build_video_args(ExportSettings(codec='hevc_qsv',maxrate='16M'),self.media,Capabilities())
        self.assertNotIn('-global_quality',args)
        self.assertEqual(args[args.index('-b:v')+1],'16M')

    def _fake_detect(self, successful):
        def run(args,timeout=15,**kwargs):
            if '-encoders' in args:return 0,' '.join(sorted(HARDWARE_ENCODERS))+' libx264 libx265'
            if '-hwaccels' in args:return 0,'cuda\nd3d11va\n'
            if '-filters' in args:return 0,'scale_cuda'
            if '-h' in args:return 0,'Supported pixel formats: nv12 p010le\n -split_encode_mode value\n'
            return 1,''
        def probe(ffmpeg,ffprobe,codec,depth):return (codec in successful and depth==8,'' if codec in successful else 'Driver refused')
        with patch.object(core,'find_binary',side_effect=lambda name,root=None:None if name=='nvidia-smi' else name), \
             patch.object(core,'_windows_adapters',return_value=[{'Name':'AMD Radeon RX 7600'},{'Name':'Intel Arc A380'}]), \
             patch.object(core,'_run_text',side_effect=run),patch.object(core,'_probe_hardware_encoder',side_effect=probe):
            return core.detect_capabilities()

    def test_compiled_encoder_without_driver_is_not_available(self):
        caps=self._fake_detect(set())
        self.assertEqual(caps.hardware_codecs,set())
        self.assertEqual(caps.compiled_hardware_codecs,HARDWARE_ENCODERS)
        self.assertEqual(preferred_codec(caps,'h264'),'libx264')
        self.assertEqual(len(caps.encoder_probe_errors),9)

    def test_probe_selects_amf_and_qsv_independently_and_does_not_guess_10bit(self):
        caps=self._fake_detect({'h264_amf','hevc_qsv'})
        self.assertEqual(caps.amf_codecs,{'h264_amf'})
        self.assertEqual(caps.qsv_codecs,{'hevc_qsv'})
        self.assertFalse(caps.ten_bit_codecs)
        self.assertTrue(caps.hardware_probe_complete)
        self.assertFalse(caps.cuda_decode)


if __name__=='__main__':unittest.main()
