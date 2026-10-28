"""Selected-cut integration for real Tk workspace and effect transactions.

Workers are held behind an event to verify the UI returns before processing.
The produced fixture files are only model paths; no executable or GPU starts.
"""
from __future__ import annotations

import copy
from dataclasses import replace
import json
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

try:
    from . import test_program_edit_ui as program_fixture
except ImportError:
    import test_program_edit_ui as program_fixture

from creative_core import Transition
from gyroflow_core import (GyroflowProbe, StabilizationResult, StabilizationSettings,
                           StabilizedRange, cache_signature)
from multicam_core import CameraClip, Cut, MulticamProject
from transition_preview import transition_signature


class EffectWorkspaceTests(unittest.TestCase):
    def setUp(self):
        program_fixture.ProgramEditWorkspaceTests.setUp(self)
        self.workspace.project.edit_points=[3.,7.]
        self.workspace._refresh()
        self.workspace.select_program_range(3.,7.)
        self.workspace.player=Mock()
        self.release=threading.Event()
        self.generated=[]

    def tearDown(self):
        self.release.set()
        for name in ('_stabilization_worker','_transition_preview_worker'):
            worker=getattr(self.workspace,name,None)
            if worker:worker.join(timeout=3)
        program_fixture.ProgramEditWorkspaceTests.tearDown(self)
        for path in self.generated:
            if path.is_file():path.unlink()

    program_event=program_fixture.ProgramEditWorkspaceTests.program_event
    context=program_fixture.ProgramEditWorkspaceTests.context
    cascade=staticmethod(program_fixture.ProgramEditWorkspaceTests.cascade)

    def fixture_file(self,name):
        path=self.folder/name
        path.write_bytes(b'fixture path only')
        return path

    def record(self,start=3.,end=7.,source_start=.5,source_end=9.5):
        cache=self.fixture_file('stabilized.mp4')
        settings=StabilizationSettings()
        signature=cache_signature(self.folder/'a.mp4',source_start,source_end,settings)
        return StabilizedRange(start,end,1,'a',settings,str(cache),signature,source_start,source_end)

    def assert_clean(self):
        self.assertEqual(self.callback_errors,[])
        self.dialogs[0].assert_not_called()
        self.root._run_editor_process.assert_not_called()

    def test_context_menu_gyroflow_auto_manual_and_remove_target_selected_cut(self):
        workspace=self.workspace
        menu=self.context((3.,7.))
        entries={menu.entrycget(index,'label'):index for index in range(menu.index('end')+1)
                 if menu.type(index)!='separator'}
        gyro_name=next(name for name in entries if 'Gyroflow' in name)
        submenu=self.cascade(menu,entries[gyro_name])
        labels={submenu.entrycget(index,'label'):index for index in range(submenu.index('end')+1)
                if submenu.type(index)!='separator'}
        auto=next(name for name in labels if '自动' in name)
        manual=next(name for name in labels if '手动' in name)
        remove=next(name for name in labels if '移除' in name)
        before=copy.deepcopy(workspace.project)
        with patch('multicam_ui.open_stabilization_dialog') as show:
            submenu.invoke(labels[auto])
            self.assertEqual(workspace.selected_program_range,(3.,7.))
            self.assertEqual(show.call_args.kwargs.get('mode'),'auto')
            submenu.invoke(labels[manual])
            self.assertEqual(show.call_args.kwargs.get('mode'),'manual')
        self.assertEqual(workspace.project,before)
        workspace.project.stabilizations=[self.record()]
        workspace._refresh()
        menu=self.context((3.,7.))
        gyro=next(index for index in range(menu.index('end')+1)
                  if menu.type(index)!='separator' and 'Gyroflow' in menu.entrycget(index,'label'))
        submenu=self.cascade(menu,gyro)
        remove=next(index for index in range(submenu.index('end')+1)
                    if submenu.type(index)!='separator' and '移除' in submenu.entrycget(index,'label'))
        submenu.invoke(remove)
        self.assertEqual(workspace.project.stabilizations,[])
        self.assert_clean()

    def test_async_stabilization_applies_only_cut_and_preview_matches_export_path_and_offset(self):
        workspace=self.workspace
        ffprobe=self.fixture_file('ffprobe.exe')
        executable=self.fixture_file('Gyroflow.exe')
        self.root.caps.ffprobe_path=str(ffprobe)
        entered=threading.Event()
        calls=[]
        def run(source,first,last,settings,output,**kwargs):
            calls.append((source,first,last,settings))
            entered.set()
            if not self.release.wait(3):raise RuntimeError('test worker did not release')
            kwargs['progress'](50.)
            path=Path(output);path.write_bytes(b'valid fixture path only')
            self.generated.append(path)
            signature=cache_signature(source,first,last,settings)
            return StabilizationResult(str(path),signature,first,last,last-first,30.,1920,1080,
                                        GyroflowProbe(has_gyro=True,has_lens=True,motion_samples=100))
        old=copy.deepcopy(workspace.project)
        with patch.object(workspace,'_gyroflow_executable',return_value=str(executable)), \
             patch('effect_workspace.run_stabilization',side_effect=run):
            self.assertTrue(workspace.start_stabilization(StabilizationSettings()))
            self.assertTrue(entered.wait(1))
            self.assertTrue(workspace.busy)
            self.assertEqual(workspace.project,old)
            self.release.set();workspace._stabilization_worker.join(timeout=3)
            self.assertFalse(workspace._stabilization_worker.is_alive())
            workspace._poll()
        self.assertFalse(workspace.busy)
        self.assertFalse(self.root.working)
        self.assertEqual(self.root.progress_var.get(),100.)
        self.assertEqual(len(workspace.project.stabilizations),1)
        record=workspace.project.stabilizations[0]
        self.assertEqual((record.start,record.end,record.camera,record.source_id),(3.,7.,1,'a'))
        self.assertEqual((calls[0][1],calls[0][2]),(.5,9.5))
        picture=workspace.project.media_at(1,3.,use_proxies=True)
        segment=next(item for item in workspace.project.segments() if item.start==3.)
        self.assertEqual((picture[0],picture[1]),(segment.path,segment.source_start))
        self.assertEqual((segment.path,segment.source_start),(record.cache_path,2.5))
        self.assertEqual(workspace.project.media_at(1,1.)[0],old.clips[0].path)
        self.assertEqual(workspace.project.media_at(1,9.)[0],old.clips[0].path)
        self.assertEqual(len(workspace.undo_stack),1)
        self.assert_clean()

    def test_removing_middle_only_keeps_outer_cache_ranges_and_undo_restores(self):
        workspace=self.workspace
        whole=self.record(0.,12.,0.,12.)
        workspace.project.stabilizations=[whole]
        workspace._refresh();workspace.select_program_range(3.,7.)
        original=copy.deepcopy(workspace.project)
        self.assertTrue(workspace.remove_selected_stabilization())
        records=workspace.project.stabilizations
        self.assertEqual([(item.start,item.end) for item in records],[(0.,3.),(7.,12.)])
        self.assertTrue(all(item.cache_path==whole.cache_path for item in records))
        self.assertEqual(workspace.project.media_at(1,4.)[0],workspace.project.clips[0].path)
        self.assertEqual(workspace.project.media_at(1,2.)[0],whole.cache_path)
        self.assertEqual(workspace.project.media_at(1,9.)[0],whole.cache_path)
        workspace.undo()
        self.assertEqual(workspace.project,original)
        self.assert_clean()

    def test_transition_cache_runs_in_background_then_publishes_normal_player_entries(self):
        workspace=self.workspace
        workspace.gpu_live_var.set(False)  # Explicit legacy/unsupported-effect path.
        self.root.caps.ffmpeg_path=str(self.fixture_file('ffmpeg.exe'))
        self.root.caps.ffprobe_path=str(self.fixture_file('ffprobe.exe'))
        workspace.project.transitions=[Transition(3.,'fade',.5)]
        entered=threading.Event()
        cache=self.fixture_file('transition.mp4')
        items=[{'path':str(cache),'start':2.75,'end':3.25}]
        def render(*args,**kwargs):
            entered.set()
            if not self.release.wait(3):raise RuntimeError('test worker did not release')
            return items
        workspace.player.reset_mock()
        with patch('effect_workspace.render_transition_previews',side_effect=render) as renderer:
            workspace._refresh()
            self.assertIsNotNone(workspace._transition_preview_after)
            workspace.after_cancel(workspace._transition_preview_after)
            workspace._transition_preview_after=None
            workspace._prepare_transition_previews()
            self.assertTrue(entered.wait(1))
            self.assertFalse(workspace.busy)
            self.assertFalse(self.root.working)
            self.release.set();workspace._transition_preview_worker.join(timeout=3)
            workspace._poll()
            renderer.assert_called_once()
        calls=[call for call in workspace.player.submit.call_args_list if call.args[0]=='transition_previews']
        self.assertTrue(calls)
        self.assertEqual(calls[-1].kwargs['items'],items)
        self.assertEqual(calls[-1].kwargs['signature'],transition_signature(workspace.project))
        self.assertFalse(workspace.busy)
        self.assert_clean()


class StabilizationProjectTests(unittest.TestCase):
    def test_version_four_cache_roundtrip_and_version_three_defaults(self):
        with tempfile.TemporaryDirectory(prefix='stabilized_project_') as folder:
            root=Path(folder)
            for name in ('source.mp4','cache.mp4','lens.json'):(root/name).write_bytes(b'fixture')
            settings=StabilizationSettings(mode='manual',smoothness=.8,lens_path=str(root/'lens.json'))
            signature=cache_signature(root/'source.mp4',.5,9.5,settings)
            record=StabilizedRange(3.,7.,1,'a',settings,str(root/'cache.mp4'),signature,.5,9.5)
            project=MulticamProject(clips=[CameraClip('a',1,str(root/'source.mp4'),0.,12.)],cuts=[Cut(0.,1)],
                                    edit_points=[3.,7.],stabilizations=[record])
            path=root/'project.multicam.json';project.save(path)
            payload=json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(payload['version'],8)
            self.assertEqual(payload['stabilizations'][0]['cache_path'],'cache.mp4')
            loaded=MulticamProject.load(path)
            self.assertEqual(loaded,project)
            self.assertEqual(loaded.media_at(1,4.)[0],str(root/'cache.mp4'))
            payload['version']=3;payload.pop('stabilizations')
            path.write_text(json.dumps(payload),encoding='utf-8')
            old=MulticamProject.load(path)
            self.assertEqual(old.stabilizations,[])
            self.assertEqual(old.edit_points,[3.,7.])

    def test_missing_stabilized_cache_is_clear_export_error(self):
        with tempfile.TemporaryDirectory(prefix='missing_stabilized_') as folder:
            root=Path(folder);source=root/'source.mp4';source.write_bytes(b'fixture')
            record=StabilizedRange(3.,7.,1,'a',StabilizationSettings(),str(root/'missing.mp4'),'a'*64,.5,9.5)
            project=MulticamProject(clips=[CameraClip('a',1,str(source),0.,12.)],cuts=[Cut(0.,1)],stabilizations=[record])
            project.validate()
            self.assertEqual(project.media_at(1,4.)[0],str(source))
            with self.assertRaisesRegex(ValueError,'防抖缓存缺失'):
                project.segments()

    def test_stale_source_and_uncovered_cache_range_cannot_silently_change_export(self):
        with tempfile.TemporaryDirectory(prefix='stale_stabilized_') as folder:
            root=Path(folder);source=root/'source.mp4';source.write_bytes(b'original fixture')
            cache=root/'cache.mp4';cache.write_bytes(b'cache fixture')
            settings=StabilizationSettings()
            signature=cache_signature(source,.5,9.5,settings)
            record=StabilizedRange(3.,7.,1,'a',settings,str(cache),signature,.5,9.5)
            project=MulticamProject(clips=[CameraClip('a',1,str(source),0.,12.)],cuts=[Cut(0.,1)],stabilizations=[record])
            self.assertEqual(project.media_at(1,4.)[0],str(cache))
            source.write_bytes(b'replaced source fixture')
            self.assertEqual(project.media_at(1,4.)[0],str(source))
            with self.assertRaisesRegex(ValueError,'原素材或防抖设置已变化'):
                project.segments()
            project.stabilizations=[replace(record,source_start=4.)]
            with self.assertRaisesRegex(ValueError,'没有覆盖所选片段'):
                project.validate()


if __name__=='__main__':unittest.main()
