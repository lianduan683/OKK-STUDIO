"""Gyroflow integration: configuration, genuine telemetry and frame mapping."""
from dataclasses import asdict, replace
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from exporter_core import ExportCancelled
from gyroflow_core import (StabilizationSettings, StabilizedRange, _json_arg, _run, _prepare_input,
    build_command, build_preset, cache_matches, cache_signature, default_settings,
    find_gyroflow, probe_source, range_from_dict, range_to_dict, run_stabilization,
    settings_from_dict, settings_to_dict, validate_range, validate_settings)


class GyroflowSettingsTests(unittest.TestCase):
    def test_defaults_and_manual_fields_have_real_project_mapping(self):
        settings = StabilizationSettings(mode='manual', smoothness=.8, fov=1.1,
            zoom_mode='static', horizon_lock=80, horizon_roll=2, rolling_shutter_ms=-15,
            auto_sync=False, sync_offset=.12)
        preset = build_preset(settings, 1.5, 3.5)
        self.assertEqual(preset['trim_ranges_ms'], [[1500,3500]])
        self.assertEqual(preset['stabilization']['method'], 'Default')
        self.assertEqual(preset['stabilization']['smoothing_params'][0], {'name':'smoothness','value':.8})
        self.assertEqual(preset['stabilization']['adaptive_zoom_window'], -1)
        self.assertEqual(preset['stabilization']['frame_readout_direction'], 'BottomToTop')
        self.assertEqual(preset['stabilization']['frame_readout_time'], 15)
        self.assertEqual(preset['offsets'], {'0':120})
        self.assertNotIn('frame_readout_time', build_preset(default_settings())['stabilization'])

    def test_validation_rejects_bad_fields_and_unknown_serialization(self):
        for kwargs in ({'smoothness':float('nan')},{'smoothness':True},{'fov':0},
                       {'zoom_mode':'made-up'},{'auto_sync':1},{'gyro_path':'x\nfile'},
                       {'rolling_shutter_ms':101}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                validate_settings(StabilizationSettings(**kwargs))
        with self.assertRaises(ValueError): settings_from_dict({'unknown':1})
        with self.assertRaises(ValueError): settings_from_dict({'mode':'invented'})

    def test_relative_settings_and_range_round_trip_preserves_source_clock(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            settings = StabilizationSettings(gyro_path=str(base/'a.gcsv'), project_path=str(base/'a.gyroflow'))
            record = StabilizedRange(8,10,2,'clip-b',settings,str(base/'cache.mp4'),'a'*64,1.5,6.5)
            relative = lambda value: Path(value).relative_to(base).as_posix()
            absolute = lambda value: str(base/value)
            encoded = range_to_dict(record, relative)
            self.assertEqual(encoded['settings']['gyro_path'],'a.gcsv')
            self.assertEqual(encoded['cache_path'],'cache.mp4')
            self.assertEqual(range_from_dict(encoded,absolute),record)
            self.assertEqual(settings_from_dict(settings_to_dict(settings)),settings)
            self.assertEqual(encoded['source_start'],1.5)
            with self.assertRaises(ValueError): range_from_dict(dict(encoded,unexpected=True))
            with self.assertRaises(ValueError): validate_range(replace(record,cache_path=''))

    def test_cache_invalidation_tracks_original_and_external_gyro_lens(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder)
            source=base/'source.mp4'; source.write_bytes(b'video')
            gyro=base/'gyro.gcsv'; gyro.write_bytes(b'log')
            cache=base/'cache.mp4'; cache.write_bytes(b'render')
            settings=StabilizationSettings(gyro_path=str(gyro))
            signature=cache_signature(source,0,2,settings)
            record=StabilizedRange(5,7,1,'source',settings,str(cache),signature,0,2)
            self.assertTrue(cache_matches(record,source))
            self.assertNotEqual(cache_signature(source,0,2,replace(settings,smoothness=1)),signature)
            gyro.write_bytes(b'different log')
            self.assertFalse(cache_matches(record,source))
            record.signature=cache_signature(source,0,2,settings)
            source.write_bytes(b'new video')
            self.assertFalse(cache_matches(record,source))
            source.unlink()
            self.assertFalse(cache_matches(record,source))

    def test_build_command_uses_argument_list_and_official_cli_fields(self):
        settings=StabilizationSettings(gyro_path="C:/John's videos/gyro.gcsv", lens_path='lens.json',auto_sync=False)
        command=build_command('Gyroflow.exe','video.mp4','cache.mp4',settings,1,2,width=1920,height=1080,bit_depth=10)
        self.assertIsInstance(command,list)
        self.assertIn('--stdout-progress',command)
        self.assertIn('-g',command)
        self.assertEqual(command[command.index('-g')+1],str(Path(settings.gyro_path).resolve()))
        output=json.loads(command[command.index('-p')+1])
        self.assertFalse(output['audio'])
        self.assertEqual(output['pixel_format'],'P010LE')
        self.assertEqual(json.loads(command[command.index('--preset')+1])['trim_ranges_ms'],[[1000,2000]])
        self.assertEqual(json.loads(_json_arg({'path':"John's"}).replace("'",'"'))['path'],"John's")
        with self.assertRaises(ValueError): build_command('Gyroflow.exe','video.mp4','video.mp4',settings)

    def test_runner_detects_failure_even_when_gyroflow_returns_zero(self):
        with self.assertRaisesRegex(RuntimeError,'处理失败'):
            _run([sys.executable,'-c',"print('Rendering failed: fixture')"])
        progress=[]
        _run([sys.executable,'-c',"print('Rendering progress: 25/100 frames (25.0%)')"],
             progress=lambda *args:progress.append(args))
        self.assertEqual(progress[0][0],25)

    def test_full_project_becomes_motion_preset_on_the_matching_video(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder); source=base/'source.mp4';source.write_bytes(b'fixture')
            project=base/'source.gyroflow'
            data={'version':3,'videofile':source.as_uri(),
                  'video_info':{'width':320,'height':180,'fps':30,'duration_ms':3000},
                  'gyro_source':{'filepath':'motion.gcsv'},'calibration_data':{'lens':'fixture'},
                  'offsets':{'0':4},'output':{'output_path':'never-write.mp4'}}
            project.write_text(json.dumps(data),encoding='utf-8')
            job=base/'job';job.mkdir()
            preset,original=_prepare_input(source,default_settings(),job)
            self.assertEqual(original,str(project))
            prepared=json.loads(Path(preset).read_text())
            self.assertFalse({'videofile','video_info','output'} & set(prepared))
            self.assertEqual(prepared['offsets'],{'0':4})
            command=build_command('Gyroflow.exe',preset,job/'render.mp4',default_settings(),source_path=source)
            self.assertEqual(command[1:3],[str(source),preset])
            data.pop('video_info');project.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError,'完整视频信息'):
                _prepare_input(source,default_settings(),job)

    def test_cancel_stops_our_child_process(self):
        cancel=threading.Event()
        timer=threading.Timer(.15,cancel.set);timer.start()
        began=time.monotonic()
        try:
            with self.assertRaises(ExportCancelled):
                _run([sys.executable,'-c','import time;time.sleep(30)'],cancel_event=cancel)
        finally: timer.cancel()
        self.assertLess(time.monotonic()-began,3)


@unittest.skipUnless(os.environ.get('OKK_TEST_GYROFLOW')=='1','set OKK_TEST_GYROFLOW=1 for official Gyroflow integration')
class ActualGyroflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='okk_gyroflow_actual_')
        self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)
        app=Path(__file__).resolve().parents[1]
        self.ffmpeg=str(app/'bin'/'ffmpeg.exe');self.ffprobe=str(app/'bin'/'ffprobe.exe')
        development=app.parent/'gyroflow_portable'/'Gyroflow.exe'
        self.exe=find_gyroflow(os.environ.get('OKK_GYROFLOW_EXE') or (str(development) if development.is_file() else ''))
        # Per-frame luminance identifies the exact source frame after a trim,
        # independent of stabilization geometry or encoder reconstruction.
        self.source=self.folder/'source.mp4'
        subprocess.run([self.ffmpeg,'-hide_banner','-loglevel','error','-y','-f','lavfi','-i',
            'nullsrc=size=320x180:rate=30:duration=3,geq=lum=30+N*2:cb=128:cr=128',
            '-c:v','libx264','-qp','0','-pix_fmt','yuv420p',str(self.source)],check=True,
            creationflags=0x08000000)
        gyro=['GYROFLOW IMU LOG','version,1.3','id,OKK TEST FIXTURE','orientation,XYZ',
              'tscale,0.001','gscale,0.017453292519943295','ascale,1.0','t,gx,gy,gz,ax,ay,az']
        for index in range(1001):
            t=index*3;gyro.append(f'{t},0,0,{20*math.sin(t/1000*math.tau*2):.9f},0,0,9.81')
        self.gyro=self.folder/'motion.gcsv';self.gyro.write_text('\n'.join(gyro),encoding='utf-8')
        lens={'name':'Synthetic pinhole 320x180','calibrated_by':'Engineering fixture',
            'camera_brand':'Synthetic','camera_model':'Fixture','calib_dimension':{'w':320,'h':180},
            'orig_dimension':{'w':320,'h':180},'fisheye_params':{'camera_matrix':[[250,0,160],[0,250,90],[0,0,1]],
            'distortion_coeffs':[0,0,0,0,0]},'distortion_model':'opencv_standard','calibrator_version':'1.6.3'}
        self.lens=self.folder/'lens.json';self.lens.write_text(json.dumps(lens),encoding='utf-8')
        self.settings=StabilizationSettings(mode='manual',gyro_path=str(self.gyro),lens_path=str(self.lens),auto_sync=False)

    def test_real_probe_rejects_video_without_motion_data(self):
        probe=probe_source(self.source,StabilizationSettings(),gyroflow_path=self.exe,ffprobe_path=self.ffprobe)
        self.assertFalse(probe.has_gyro)
        self.assertFalse(probe.has_lens)
        self.assertTrue(probe.problems)
        output=self.folder/'must_not_exist.mp4'
        with self.assertRaisesRegex(ValueError,'陀螺仪'):
            run_stabilization(self.source,.5,2.5,default_settings(),output,
                gyroflow_path=self.exe,ffprobe_path=self.ffprobe)
        self.assertFalse(output.exists())

    def test_real_selected_source_range_has_exact_frames_and_correct_source_start(self):
        output=self.folder/'stabilized.mp4';progress=[]
        result=run_stabilization(self.source,.5,2.5,self.settings,output,gyroflow_path=self.exe,
            ffprobe_path=self.ffprobe,progress=lambda *args:progress.append(args))
        self.assertEqual((result.source_start,result.source_end),(.5,2.5))
        self.assertAlmostEqual(result.duration,2,places=3)
        self.assertTrue(result.probe.has_gyro)
        self.assertTrue(result.probe.has_lens)
        data=subprocess.run([self.ffmpeg,'-hide_banner','-loglevel','error','-i',str(output),
            '-vf','scale=1:1','-pix_fmt','gray','-f','rawvideo','-'],capture_output=True,check=True,
            creationflags=0x08000000).stdout
        self.assertEqual(len(data),60)
        # Original source frame 15: Y=60. Limited-range to gray is ~51;
        # comparing source decoded through the same conversion avoids a
        # hard-coded colour-range assumption.
        reference=subprocess.run([self.ffmpeg,'-hide_banner','-loglevel','error','-ss','0.5','-i',str(self.source),
            '-frames:v','60','-vf','scale=1:1','-pix_fmt','gray','-f','rawvideo','-'],capture_output=True,check=True,
            creationflags=0x08000000).stdout
        self.assertEqual(len(reference),60)
        self.assertLessEqual(max(abs(a-b) for a,b in zip(data,reference)),3)
        self.assertEqual(progress[-1][0],100)


if __name__=='__main__': unittest.main()
