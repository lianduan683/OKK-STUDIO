"""Cross-vendor proxy arguments and adapter selection must stay isolated."""
from pathlib import Path
import ctypes as C
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from exporter_core import MediaInfo
from gpu_d3d11 import _adapter_matches, _adapter_priority
import gpu_d3d11 as d3d
from mpv_controller import MPV_GPU_PLAYBACK_OPTIONS, MpvController
import quick_proxy as proxy
import gpu_export


def capabilities(vendor='unknown', *, nvenc=(), amf=(), qsv=(), cuda=False):
    return SimpleNamespace(gpu_vendor=vendor,nvenc_codecs=set(nvenc),amf_codecs=set(amf),
        qsv_codecs=set(qsv),software_codecs={'libx264'},cuda_decode=cuda,scale_cuda=cuda)


class VendorPreviewTests(unittest.TestCase):
    def setUp(self):
        self.media=MediaInfo('camera.mov',duration=4,width=3840,height=2160,fps=60,bit_depth=10)

    def command(self,mode):
        return proxy._build_command('ffmpeg',self.media.path,'out.mp4',self.media,mode)

    def test_hardware_selection_respects_primary_vendor_and_keeps_bounded_fallbacks(self):
        caps=capabilities('amd',nvenc=['h264_nvenc'],amf=['h264_amf'],qsv=['h264_qsv'],cuda=True)
        self.assertEqual(proxy._proxy_modes(caps),['cpu_amf','cuda_nvenc','cpu_nvenc','cpu_qsv','cpu'])
        self.assertEqual(proxy._proxy_modes(capabilities('intel',qsv=['h264_qsv'])),['cpu_qsv','cpu'])

    def test_compiled_but_unusable_hardware_is_not_selected(self):
        caps=capabilities('amd')
        caps.raw_encoder_help={'h264_amf':'Encoder h264_amf'}
        caps.encoder_probe_errors={'h264_amf':'driver initialization failed'}
        self.assertEqual(proxy._proxy_modes(caps),['cpu'])

    def test_amf_proxy_uses_only_amf_controls_and_no_bframes(self):
        command=self.command('cpu_amf')
        self.assertEqual(command[command.index('-c:v')+1],'h264_amf')
        self.assertEqual(command[command.index('-quality')+1],'speed')
        self.assertEqual(command[command.index('-rc')+1],'cqp')
        self.assertEqual(command[command.index('-qp_i')+1],'27')
        self.assertEqual(command[command.index('-bf')+1],'0')
        self.assertEqual(command[command.index('-async_depth')+1],'1')
        for unsupported in ('-hwaccel','-gpu','-cq','-tune','-multipass','-global_quality','-split_encode_mode'):
            self.assertNotIn(unsupported,command)

    def test_qsv_proxy_uses_nv12_and_no_nvenc_or_amf_controls(self):
        command=self.command('cpu_qsv')
        self.assertEqual(command[command.index('-c:v')+1],'h264_qsv')
        self.assertEqual(command[command.index('-preset')+1],'veryfast')
        self.assertIn('format=nv12',command[command.index('-vf')+1])
        self.assertEqual(command[command.index('-global_quality')+1],'27')
        self.assertEqual(command[command.index('-bf')+1],'0')
        for unsupported in ('-hwaccel','-gpu','-rc','-cq','-tune','-multipass','-usage','-qp_i'):
            self.assertNotIn(unsupported,command)

    def test_every_proxy_preserves_complete_source_clock_and_monitoring_limits(self):
        for mode in ('cuda_nvenc','cpu_nvenc','cpu_amf','cpu_qsv','cpu'):
            with self.subTest(mode=mode):
                command=self.command(mode);graph=command[command.index('-vf')+1]
                self.assertIn('fps=fps=30/1:start_time=0',graph)
                self.assertIn('min(iw,1920)',graph);self.assertIn('min(ih,1080)',graph)
                self.assertEqual(command[command.index('-t')+1],'4.000000000')
                self.assertEqual(command[command.index('-g')+1],'30')
                self.assertEqual(command[command.index('-bf')+1],'0')
        with self.assertRaises(ValueError):self.command('unrecognized')

    def test_parameter_recipe_changes_invalidate_old_proxy_cache(self):
        stamp={'path':'camera.mov','size':10,'mtime_ns':1}
        first=proxy._cache_key(stamp)
        with patch.dict(proxy.PARAMETERS['encoders'],amf='different-quality'):
            self.assertNotEqual(first,proxy._cache_key(stamp))

    def test_d3d11_does_not_blacklist_amd_intel_or_force_nvidia(self):
        self.assertTrue(_adapter_matches(0x1002,'Radeon 890M','amd'))
        self.assertTrue(_adapter_matches(0x8086,'Intel Arc','intel'))
        self.assertFalse(_adapter_matches(0x10de,'RTX 5090','amd'))
        self.assertTrue(_adapter_matches(0x1002,'Radeon 890M','890M'))
        self.assertTrue(_adapter_matches(0x8086,'Intel Arc',None))
        # Memory/OS order is the automatic preference, never vendor identity.
        self.assertGreater(_adapter_priority(16<<30,1),_adapter_priority(6<<30,0))
        self.assertFalse(any(option.startswith('--d3d11-adapter=') for option in MPV_GPU_PLAYBACK_OPTIONS))

    def test_adapter_enumeration_handles_hybrid_single_vendor_and_unknown_once(self):
        cases=[([(0x1002,'AMD Radeon 890M',512<<20,0),(0x10de,'NVIDIA RTX 4050 Laptop',6<<30,0)],'NVIDIA RTX 4050 Laptop'),
               ([(0x1002,'AMD Radeon 890M',512<<20,0)],'AMD Radeon 890M'),
               ([(0x8086,'Intel Arc',16<<30,0)],'Intel Arc'),
               ([(123,'Other D3D11 hardware',8<<30,0)],'Other D3D11 hardware'),
               ([(0x1414,'Microsoft software',0,2)],'')]
        for rows,expected in cases:
            with self.subTest(expected=expected):
                d3d.preferred_adapter_name.cache_clear();enumerations=[]
                def native(pointer,index,result,argtypes,*args):
                    if index==12:
                        number=args[0];enumerations.append(number)
                        if number>=len(rows):return -1
                        C.cast(args[1],C.POINTER(d3d.P))[0]=d3d.P(1000+number);return 0
                    if index==10:
                        number=int(pointer.value)-1000
                        vendor,name,memory,flags=rows[number]
                        desc=C.cast(args[0],C.POINTER(d3d.AdapterDesc)).contents
                        desc.vendor=vendor;desc.description=name;desc.dedicated=memory;desc.flags=flags
                        return 0
                    if index==2:return 0
                    raise AssertionError(index)
                try:
                    with patch.object(d3d.os,'name','nt'),patch.object(d3d,'call',side_effect=native), \
                         patch.object(d3d,'_create_factory',return_value=d3d.Com(d3d.P(900))):
                        self.assertEqual(d3d.preferred_adapter_name(),expected)
                        self.assertEqual(d3d.preferred_adapter_name(),expected)
                        self.assertEqual(enumerations,list(range(len(rows)+1)))
                finally:d3d.preferred_adapter_name.cache_clear()

    def test_mpv_adapter_helper_failure_keeps_automatic_selection(self):
        for name in ('AMD Radeon 890M','Intel Arc','NVIDIA RTX 4050 Laptop',None):
            controller=MpvController('mpv.exe',123)
            process=Mock();process.poll.return_value=None
            reader=Mock(return_value=name) if name else Mock(side_effect=OSError('DXGI unavailable'))
            with patch.object(d3d,'preferred_adapter_name',reader), \
                 patch('mpv_controller.subprocess.Popen',return_value=process) as spawn, \
                 patch.object(controller,'command',return_value='ready'):
                controller.start()
            options=[x for x in spawn.call_args.args[0] if x.startswith('--d3d11-adapter=')]
            self.assertEqual(options,['--d3d11-adapter='+name] if name else [])

    def test_only_cuda_export_requires_nvidia_vulkan_adapter(self):
        with tempfile.TemporaryDirectory() as directory:
            caps=gpu_export.GpuExportCapabilities('ffmpeg',True,True,False)
            with patch.object(gpu_export,'detect_gpu_export',return_value=caps):
                args=dict(ffmpeg_path='ffmpeg',width=128,height=72,pixel_format='yuv420p',
                    rate_text='30',frames=30,duration=1,shader_dir=directory)
                for mode in ('cpu','vulkan'):
                    plan=gpu_export.build_gpu_video_plan((),decode_mode=mode,**args)
                    self.assertEqual(plan.global_options[1],'vulkan=okk_vk')
                plan=gpu_export.build_gpu_video_plan((),decode_mode='cuda',**args)
                self.assertEqual(plan.global_options[1],'vulkan=okk_vk:NVIDIA')


if __name__=='__main__':unittest.main()
