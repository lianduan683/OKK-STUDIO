"""Engine selection preserves old Gyroflow projects and independent caches."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from gyroflow_core import (PATH_FIELDS,StabilizationSettings,StabilizedRange,
    cache_matches,cache_signature,range_from_dict,range_to_dict,run_stabilization,
    settings_from_dict,settings_to_dict,validate_settings)

LEGACY_FIELDS=('mode','smoothness','fov','zoom_mode','zoom_speed','horizon_lock',
               'horizon_roll','rolling_shutter_ms','gyro_path','lens_path','project_path',
               'gyroflow_path','auto_sync','sync_search_size','sync_offset')


class StabilizationEngineTests(unittest.TestCase):
    def test_old_settings_and_range_load_with_gyroflow_defaults(self):
        legacy={'mode':'manual','smoothness':.8,'gyro_path':'motion.gcsv','auto_sync':False}
        settings=settings_from_dict(legacy)
        self.assertEqual(settings.engine,'gyroflow')
        self.assertEqual((settings.image_smoothing,settings.image_shakiness,settings.image_zoom,
                          settings.image_tripod,settings.image_model),(.5,5,True,False,'similarity'))
        record=range_from_dict({'start':4,'end':6,'camera':2,'source_id':'c2',
            'source_start':1,'source_end':3,'settings':legacy,'cache_path':'cache.mp4','signature':'a'*64})
        self.assertEqual(record.settings,settings)
        self.assertEqual(range_from_dict(range_to_dict(record)),record)

    def test_image_settings_and_paths_roundtrip_without_gyro_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary)
            settings=StabilizationSettings(engine='image',image_smoothing=1.2,image_shakiness=8,
                image_zoom=False,image_tripod=True,image_model='translation',
                gyro_path=str(base/'missing.gcsv'),lens_path=str(base/'missing.other'),
                project_path=str(base/'missing.project'),gyroflow_path=str(base/'missing.exe'))
            validate_settings(settings,check_files=True)
            encoded=settings_to_dict(settings,lambda value:Path(value).relative_to(base).as_posix())
            self.assertEqual(encoded['lens_path'],'missing.other')
            self.assertEqual(settings_from_dict(encoded,lambda value:str(base/value)),settings)
            record=StabilizedRange(4,6,2,'c2',settings,str(base/'cache.mp4'),'b'*64,1,3)
            encoded_range=range_to_dict(record,lambda value:Path(value).relative_to(base).as_posix())
            self.assertEqual(range_from_dict(encoded_range,lambda value:str(base/value)),record)

    def test_image_options_reject_invalid_types_ranges_and_unknown_models(self):
        for changes in ({'engine':'resolve'},{'engine':True},{'image_smoothing':-.1},
                        {'image_smoothing':5.1},{'image_smoothing':float('nan')},
                        {'image_smoothing':True},{'image_shakiness':0},{'image_shakiness':11},
                        {'image_shakiness':5.0},{'image_shakiness':True},
                        {'image_zoom':1},{'image_tripod':0},{'image_model':'perspective'}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                validate_settings(replace(StabilizationSettings(engine='image'),**changes))
        for smoothing,shakiness in ((0,1),(5,10)):
            validate_settings(StabilizationSettings(engine='image',image_smoothing=smoothing,
                                                    image_shakiness=shakiness))
        with self.assertRaises(ValueError):settings_from_dict({'image_nonexistent':1})

    def test_image_ignores_gyro_file_existence_but_preserves_path_type_checks(self):
        settings=StabilizationSettings(engine='image',gyro_path='missing.gcsv',
                                       lens_path='missing.wrong',project_path='missing.wrong')
        validate_settings(settings,check_files=True)
        with self.assertRaises(ValueError):validate_settings(replace(settings,engine='gyroflow'),check_files=True)
        for name in PATH_FIELDS:
            for invalid in (None,[], 'invalid\npath', 'invalid\x00path'):
                with self.subTest(name=name,value=invalid),self.assertRaises(ValueError):
                    validate_settings(replace(settings,**{name:invalid}),check_files=True)

    def test_gyroflow_signature_exactly_matches_legacy_payload_and_ignores_image_options(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary)
            paths={'source':base/'source.mp4','gyro_path':base/'motion.gcsv',
                   'lens_path':base/'lens.json','project_path':base/'source.gyroflow',
                   'gyroflow_path':base/'Gyroflow.exe'}
            for name,path in paths.items():path.write_bytes(name.encode())
            settings=StabilizationSettings(mode='manual',smoothness=.8,
                                           **{name:str(paths[name]) for name in PATH_FIELDS})
            def stamp(path):
                path=Path(path).resolve();info=path.stat()
                return (str(path),info.st_size,info.st_mtime_ns)
            legacy={'algorithm':'Gyroflow CLI range cache v1','source':stamp(paths['source']),
                'range':[1.25,3.75],'settings':{name:getattr(settings,name) for name in LEGACY_FIELDS},
                'dependencies':{name:stamp(getattr(settings,name)) for name in PATH_FIELDS},
                'automatic_project':stamp(settings.project_path)}
            expected=hashlib.sha256(json.dumps(legacy,sort_keys=True,ensure_ascii=True).encode()).hexdigest()
            with patch.dict(sys.modules,{'image_stabilization':None}):
                self.assertEqual(cache_signature(paths['source'],1.25,3.75,settings),expected)
                changed=replace(settings,image_smoothing=4,image_shakiness=9,image_zoom=False,
                                image_tripod=True,image_model='translation')
                self.assertEqual(cache_signature(paths['source'],1.25,3.75,changed),expected)
            cache=base/'cache.mp4';cache.write_bytes(b'existing Gyroflow range cache')
            record=StabilizedRange(6,8.5,1,'c1',settings,str(cache),expected,1.25,3.75)
            self.assertTrue(cache_matches(record,paths['source']))

    def test_image_signature_lazily_dispatches_after_validating_range(self):
        backend=Mock(return_value='c'*64)
        settings=StabilizationSettings(engine='image')
        with patch.dict(sys.modules,{'image_stabilization':SimpleNamespace(image_cache_signature=backend)}):
            self.assertEqual(cache_signature('source.mp4',1,3,settings),'c'*64)
            backend.assert_called_once_with('source.mp4',1.,3.,settings)
            backend.reset_mock()
            for start,end in ((2,1),(1,1),(True,2),(0,float('inf'))):
                with self.subTest(start=start,end=end),self.assertRaises(ValueError):
                    cache_signature('source.mp4',start,end,settings)
            backend.assert_not_called()

    def test_image_render_dispatch_never_requires_gyro_executable_or_files(self):
        result=object();backend=Mock(return_value=result)
        cancel=threading.Event();progress=Mock();log=Mock()
        settings=StabilizationSettings(engine='image',gyro_path='missing.gcsv',
                                       lens_path='missing.json',project_path='missing.gyroflow')
        with patch.dict(sys.modules,{'image_stabilization':SimpleNamespace(run_image_stabilization=backend)}),\
                patch('gyroflow_core.find_gyroflow') as find_gyro:
            actual=run_stabilization('source.mp4',1,3,settings,'output.mp4',
                gyroflow_path='missing.exe',ffprobe_path='ffprobe.exe',ffmpeg_path='ffmpeg.exe',
                cancel_event=cancel,progress=progress,log=log)
        self.assertIs(actual,result)
        backend.assert_called_once_with('source.mp4',1,3,settings,'output.mp4',
            ffprobe_path='ffprobe.exe',ffmpeg_path='ffmpeg.exe',cancel_event=cancel,progress=progress,log=log)
        find_gyro.assert_not_called()

    def test_invalid_settings_are_rejected_before_image_backend_runs(self):
        backend=Mock()
        with patch.dict(sys.modules,{'image_stabilization':SimpleNamespace(run_image_stabilization=backend)}):
            with self.assertRaises(ValueError):
                run_stabilization('source.mp4',0,2,
                    StabilizationSettings(engine='image',image_shakiness=True),'output.mp4')
        backend.assert_not_called()


if __name__=='__main__':unittest.main()
