"""Image stabilization uses selected-cut transactions without requiring Gyroflow."""
from __future__ import annotations

import copy
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from gyroflow_core import StabilizationSettings,StabilizationResult,GyroflowProbe,cache_signature
try:
    from . import test_effect_workspace as fixture
except ImportError:
    import test_effect_workspace as fixture


class ImageEffectWorkspaceTests(unittest.TestCase):
    setUp=fixture.EffectWorkspaceTests.setUp
    tearDown=fixture.EffectWorkspaceTests.tearDown
    fixture_file=fixture.EffectWorkspaceTests.fixture_file
    assert_clean=fixture.EffectWorkspaceTests.assert_clean
    context=fixture.EffectWorkspaceTests.context
    program_event=fixture.EffectWorkspaceTests.program_event
    cascade=staticmethod(fixture.EffectWorkspaceTests.cascade)

    def start_fixture(self,late_cancel=False):
        ws=self.workspace
        self.root.caps.ffprobe_path=str(self.fixture_file('ffprobe.exe'))
        self.root.caps.ffmpeg_path=str(self.fixture_file('ffmpeg.exe'))
        settings=StabilizationSettings(engine='image',mode='manual',image_smoothing=.8,
                                        image_model='translation',image_tripod=True)
        entered=threading.Event()
        calls=[]
        def render(source,first,last,settings,output,**kwargs):
            entered.set();calls.append((source,first,last,copy.deepcopy(settings),kwargs))
            if not self.release.wait(3):raise RuntimeError('test worker did not release')
            kwargs['progress'](35,'画面分析')
            path=Path(output);path.write_bytes(b'cache fixture only');self.generated.append(path)
            return StabilizationResult(str(path),cache_signature(source,first,last,settings),first,last,
                                       last-first,30.,1920,1080,GyroflowProbe(source_name='画面运动'))
        old=copy.deepcopy(ws.project)
        with patch.object(ws,'_gyroflow_executable',side_effect=AssertionError('Image engine asked for Gyroflow')),\
             patch('effect_workspace.run_stabilization',side_effect=render):
            self.assertTrue(ws.start_stabilization(settings));self.assertTrue(entered.wait(1))
            self.assertTrue(ws.busy);self.assertEqual(ws.project,old)
            self.release.set();ws._stabilization_worker.join(3)
            self.assertFalse(ws._stabilization_worker.is_alive())
            if late_cancel:self.root.cancel_event.set()
            ws._poll()
        self.assertFalse(ws.busy);self.assertFalse(self.root.working)
        return old,calls,settings

    def test_image_right_click_and_gyroflow_remain_independent(self):
        menu=self.context((3.,7.))
        labels={menu.entrycget(index,'label'):index for index in range(menu.index('end')+1)
                if menu.type(index)!='separator'}
        image=next(name for name in labels if '画面防抖' in name)
        self.assertTrue(any('Gyroflow' in name for name in labels))
        submenu=self.cascade(menu,labels[image])
        before=copy.deepcopy(self.workspace.project)
        with patch('multicam_ui.open_image_stabilization_dialog') as show:
            submenu.invoke(0);self.assertEqual(show.call_args.kwargs['mode'],'auto')
            submenu.invoke(1);self.assertEqual(show.call_args.kwargs['mode'],'manual')
        self.assertEqual(self.workspace.selected_program_range,(3.,7.))
        self.assertEqual(self.workspace.project,before)
        self.assert_clean()

    def test_image_backend_applies_only_cut_and_preview_export_share_cache(self):
        old,calls,settings=self.start_fixture()
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][1:3],(.5,9.5))
        self.assertEqual(calls[0][3],settings)
        self.assertEqual(calls[0][4]['gyroflow_path'],'')
        record=self.workspace.project.stabilizations[0]
        self.assertEqual(record.settings.engine,'image')
        self.assertEqual((record.start,record.end,record.camera),(3.,7.,1))
        picture=self.workspace.project.media_at(1,3.)
        segment=next(item for item in self.workspace.project.segments() if item.start==3.)
        self.assertEqual((picture[0],picture[1]),(segment.path,segment.source_start))
        self.assertEqual(picture[:2],(record.cache_path,2.5))
        self.assertEqual(self.workspace.project.media_at(1,1.)[0],old.clips[0].path)
        self.assertEqual(self.workspace.project.media_at(1,9.)[0],old.clips[0].path)
        self.assertEqual(len(self.workspace.undo_stack),1)
        self.workspace.undo();self.assertEqual(self.workspace.project,old)
        self.assert_clean()

    def test_late_cancel_discards_generated_cache_and_preserves_original_project(self):
        old,_,_=self.start_fixture(late_cancel=True)
        self.assertEqual(self.workspace.project,old)
        self.assertEqual(self.workspace.undo_stack,[])
        self.assertTrue(all(not path.exists() for path in self.generated))
        self.assertIn('已取消',self.workspace.status.get())
        self.assert_clean()


if __name__=='__main__':unittest.main()
