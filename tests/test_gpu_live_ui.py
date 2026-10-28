"""Tk output ownership and explicit unsupported-effect fallback."""
import unittest
from unittest.mock import Mock,patch
from creative_core import ColorGrade,Transition
from subtitle_core import SubtitleCue
try:
    from . import test_program_edit_ui as fixture
except ImportError:
    import test_program_edit_ui as fixture

class GpuLiveUiTests(unittest.TestCase):
    setUp=fixture.ProgramEditWorkspaceTests.setUp
    tearDown=fixture.ProgramEditWorkspaceTests.tearDown

    def test_one_surface_hides_old_video_windows_and_restores_on_fallback(self):
        ws=self.workspace
        self.assertTrue(ws.gpu_live_var.get())
        ws._set_gpu_live_state({'enabled':True})
        self.assertTrue(ws._gpu_live_active)
        self.assertTrue(all(not view.winfo_manager() for view in ws.program_views))
        self.assertFalse(ws._show_program_view(ws.program_views[1].winfo_id()))
        with patch.object(ws,'_schedule_transition_previews') as schedule:
            ws._set_gpu_live_state({'enabled':False,'reason':'字幕使用原效果路径'})
        self.assertFalse(ws._gpu_live_active)
        self.assertTrue(all(view.winfo_manager()=='place' for view in ws.program_views))
        self.assertIn('字幕',ws.status.get());schedule.assert_called_once()

    def test_supported_live_transition_skips_baking_but_subtitle_and_lut_keep_precise_path(self):
        ws=self.workspace;ws.project.transitions=[Transition(3.,'fade',.5)]
        self.assertTrue(ws._can_use_gpu_transitions())
        ws._schedule_transition_previews()
        self.assertIsNone(ws._transition_preview_after)
        self.assertTrue(ws._transition_preview_cancel.is_set())
        ws.project.subtitles.cues=[SubtitleCue('s',1.,2.,'字幕')]
        self.assertFalse(ws._can_use_gpu_transitions())
        ws.project.subtitles.cues=[]
        ws.project.global_grade=ColorGrade(lut_path='custom.cube')
        self.assertFalse(ws._can_use_gpu_transitions())

    def test_start_passes_single_output_host_and_current_preference_to_worker(self):
        ws=self.workspace;ws.app.mpv_path='test-mpv.exe'
        with patch('multicam_ui.MulticamPlayer') as player:
            ws.start_monitors()
        args=player.call_args.kwargs
        self.assertTrue(args['gpu_live'])
        self.assertEqual(args['gpu_output_window'],ws.program_surface.winfo_id())
        self.assertNotEqual(args['gpu_output_window'],ws.program_views[0].winfo_id())
        configurations=[call.kwargs for call in player.return_value.submit.call_args_list if call.args[0]=='configure']
        self.assertTrue(configurations[-1]['gpu_live'])
        ws.gpu_live_var.set(False);ws._toggle_gpu_live()
        configurations=[call.kwargs for call in player.return_value.submit.call_args_list if call.args[0]=='configure']
        self.assertFalse(configurations[-1]['gpu_live'])

if __name__=='__main__':unittest.main()
