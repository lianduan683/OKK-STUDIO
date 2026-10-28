"""Native comparison layouts and camera-reference alignment orchestration."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import queue
import threading
import time
import unittest
from unittest.mock import Mock,patch

try:
    from . import test_program_edit_ui as fixture
except ImportError:
    import test_program_edit_ui as fixture

from editor_core import AlignmentResult
from gyroflow_core import StabilizationSettings,StabilizedRange,cache_signature
from multicam_core import CameraClip,Cut,MulticamProject


class CompareReferenceUiTests(unittest.TestCase):
    def setUp(self):
        fixture.ProgramEditWorkspaceTests.setUp(self)
        self.workspace.project.clips[0].camera_make='Sony'
        self.workspace.project.clips[0].camera_model='ILCE-7M4'
        self.workspace.project.clips[1].camera_make='Nikon'
        self.workspace.project.clips[1].camera_model='NIKON Z8'
        self.workspace._refresh();self.root.update()

    tearDown=fixture.ProgramEditWorkspaceTests.tearDown

    def record(self):
        source=self.workspace.project.clips[0]
        cache=self.folder/'stabilized.mp4';cache.write_bytes(b'cache fixture')
        settings=StabilizationSettings(auto_sync=False)
        return StabilizedRange(3.,7.,1,source.id,settings,str(cache),
            cache_signature(source.path,.5,9.5,settings),.5,9.5)

    def comparison_state(self,enabled,active=True):
        self.workspace.player_queue.put(('state',dict(time=4.,playing=False,camera=1,
            comparison_enabled=enabled,comparison_ready=enabled,stabilization_active=active)))
        self.workspace._poll();self.root.update_idletasks()

    def assert_clean(self):
        self.assertEqual(self.callback_errors,[])
        self.dialogs[0].assert_not_called()

    def test_checkbox_uses_upper_right_split_views_and_engine_state_can_close_it(self):
        workspace=self.workspace
        workspace.project.stabilizations=[self.record()];workspace._refresh()
        player=workspace.player=Mock()
        workspace.compare_button.invoke();self.root.update()
        self.assertTrue(workspace.stabilization_compare_var.get())
        self.assertTrue(workspace._comparison_layout_enabled)
        self.assertTrue(workspace.comparison_original_label.winfo_ismapped())
        self.assertTrue(workspace.comparison_stabilized_label.winfo_ismapped())
        for view in workspace.comparison_views:
            self.assertEqual(float(view.place_info()['relx']),0.)
            self.assertEqual(float(view.place_info()['relwidth']),.5)
            self.assertIs(view.master,workspace.program_surface)
        for view in workspace.program_views:
            self.assertEqual(float(view.place_info()['relx']),.5)
            self.assertEqual(float(view.place_info()['relwidth']),.5)
        player.submit.assert_any_call('stabilization_compare',enabled=True)
        self.comparison_state(False)  # A stale disabled state cannot undo the just-requested toggle.
        self.assertTrue(workspace.stabilization_compare_var.get())
        self.comparison_state(True,False)
        self.assertIn('未防抖',workspace.comparison_stabilized_label.cget('text'))
        self.comparison_state(True,True)
        self.assertIn('含裁切',workspace.comparison_stabilized_label.cget('text'))
        self.comparison_state(False)
        self.assertFalse(workspace.stabilization_compare_var.get())
        self.assertFalse(workspace.comparison_original_label.winfo_ismapped())
        for view in workspace.program_views:self.assertEqual(float(view.place_info()['relwidth']),1.)
        workspace.compare_button.invoke();self.comparison_state(True)
        workspace.compare_button.invoke()
        player.submit.assert_any_call('stabilization_compare',enabled=False)
        self.assert_clean()

    def test_comparison_swap_lifts_known_surface_then_acknowledges_worker(self):
        workspace=self.workspace
        workspace.project.stabilizations=[self.record()];workspace._refresh()
        workspace.player=Mock();workspace.stabilization_compare_var.set(True)
        workspace.toggle_stabilization_compare()
        handles=[view.winfo_id() for view in workspace.comparison_views]
        camera_handles={key:view.winfo_id() for key,view in workspace.camera_surfaces.items()}
        before=copy.deepcopy(workspace.project)
        workspace.player_queue.put(('comparison_swap',handles[1]))
        workspace._poll()
        self.assertEqual(workspace._comparison_front_window,handles[1])
        workspace.player.submit.assert_any_call('comparison_swap_ack',window=handles[1])
        workspace.player.submit.reset_mock()
        workspace.player_queue.put(('comparison_swap',-1));workspace._poll()
        self.assertFalse(any(call.args[0]=='comparison_swap_ack' for call in workspace.player.submit.call_args_list))
        self.assertEqual([view.winfo_id() for view in workspace.comparison_views],handles)
        self.assertEqual({key:view.winfo_id() for key,view in workspace.camera_surfaces.items()},camera_handles)
        self.assertEqual(workspace.project,before)
        self.assert_clean()

    def test_precise_preview_closes_unacknowledged_comparison_before_rendering(self):
        workspace=self.workspace
        workspace.project.stabilizations=[self.record()];workspace._refresh()
        workspace.player=Mock();workspace.compare_button.invoke()
        self.assertIs(workspace._comparison_requested,True)
        self.root.mpv_path='fixture-mpv.exe'
        entered=threading.Event();release=threading.Event()
        def render(*args,**kwargs):entered.set();release.wait(3)
        try:
            with patch('multicam_ui.render_multicam',side_effect=render):
                workspace.preview_effects()
                self.assertTrue(entered.wait(2))
                self.assertFalse(workspace.stabilization_compare_var.get())
                self.assertFalse(workspace._comparison_layout_enabled)
                self.assertIsNone(workspace._comparison_requested)
                self.assertIsNone(workspace.player)
        finally:
            release.set()
            if workspace._creative_preview_worker:workspace._creative_preview_worker.join(3)
            while not workspace.jobs.empty():workspace.jobs.get_nowait()
        self.assert_clean()

    def test_both_reference_selectors_share_persistent_camera_choice(self):
        workspace=self.workspace
        self.assertEqual(len(workspace._sync_reference_combos),2)
        label=workspace._label_camera(1)
        for combo in workspace._sync_reference_combos:self.assertIn(label,combo.cget('values'))
        workspace.sync_reference_var.set(label);workspace._sync_reference_changed()
        self.assertEqual(workspace.project.alignment_reference_camera,1)
        self.assertTrue(all(combo.get()==label for combo in workspace._sync_reference_combos))
        self.assertEqual(workspace.project.master_audio,'')
        path=self.folder/'reference.multicam.json';workspace.project.save(path)
        payload=json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(payload['version'],8)
        self.assertEqual(MulticamProject.load(path).alignment_reference_camera,1)
        payload['version']=4;payload.pop('alignment_reference_camera')
        path.write_text(json.dumps(payload),encoding='utf-8')
        self.assertIsNone(MulticamProject.load(path).alignment_reference_camera)
        workspace.sync_reference_var.set('主音频');workspace._sync_reference_changed()
        self.assertIsNone(workspace.project.alignment_reference_camera)
        self.assert_clean()

    def test_camera_reference_aligns_only_other_cameras_without_replacing_master(self):
        workspace=self.workspace
        path=self.folder/'c.mp4';path.write_bytes(b'fixture')
        workspace.project.clips.append(CameraClip('c',3,str(path),2,12))
        workspace.project.clips[0].start=10
        workspace.project.alignment_reference_camera=1
        workspace.project.master_audio=''
        workspace._refresh()
        reference_before=copy.deepcopy(workspace.project.clips[0])
        original_starts={clip.id:clip.start for clip in workspace.project.clips}
        results=[AlignmentResult(-15.,.95,50,'strong camera match'),AlignmentResult(-100.,.05,50,'weak match')]
        with (patch('multicam_ui.align_to_camera',return_value=results) as match,
              patch('multicam_ui.align_audio_segments') as master_match):
            workspace.align()
            self.assertTrue(workspace.sync_running)
            deadline=time.monotonic()+3
            terminal=None
            while time.monotonic()<deadline:
                item=workspace.jobs.get(timeout=.5)
                if item[0] in ('aligned','align_error','align_cancelled'):
                    terminal=item;break
            self.assertIsNotNone(terminal)
            self.assertEqual(terminal[0],'aligned',terminal)
            args=match.call_args.args
            self.assertEqual([clip.id for clip in args[1]],['b','c'])
            self.assertEqual([clip.id for clip in args[2]],['a'])
            self.assertEqual(args[2][0].start,10)
            master_match.assert_not_called()
            workspace._finish_alignment(*terminal[1])
        self.assertEqual(workspace.project.clips[0],reference_before)
        self.assertEqual(next(clip for clip in workspace.project.clips if clip.id=='b').start,15.)
        self.assertEqual(next(clip for clip in workspace.project.clips if clip.id=='c').start,original_starts['c'])
        self.assertEqual(workspace.project.master_audio,'')
        self.assertEqual(workspace.project.alignment_reference_camera,1)
        self.assertFalse(workspace.sync_running)
        self.assert_clean()

    def test_moving_source_keeps_stabilized_range_bound_to_its_source_frames(self):
        workspace=self.workspace
        record=self.record();workspace.project.stabilizations=[record]
        before=workspace.project.media_at(1,4.)
        workspace._move_clip_start(workspace.project.clips[0],10.)
        after=workspace.project.media_at(1,14.)
        self.assertEqual((record.start,record.end),(13.,17.))
        self.assertEqual(before[0],after[0]);self.assertEqual(before[1],after[1])
        workspace.project.validate()
        self.assert_clean()


if __name__=='__main__':unittest.main()
