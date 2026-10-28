import copy
from pathlib import Path
import tempfile
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from creative_core import ColorGrade, grade_filter
from effects_browser import (PRESETS, PRESET_BY_KEY, EffectPreset, EffectsBrowser,
                             apply_preset, filtered_presets, preset_grade, show_effects_browser)
from effects_library import OverlayAsset, TransitionMask
from multicam_core import Cut, MulticamProject
from program_edits import grade_at
import test_program_edit_ui as workspace_fixture


class EffectPresetTests(unittest.TestCase):
    def test_all_color_presets_validate_and_preserve_independent_fields(self):
        base=ColorGrade(lut_path='custom.cube',offset_r=.03,curve_b=((0.,0.),(.5,.6),(1.,1.)))
        for item in PRESETS:
            if item.kind!='grade':continue
            with self.subTest(preset=item.key):
                result=preset_grade(item,base,.5)
                self.assertEqual(result.lut_path,base.lut_path)
                self.assertEqual(result.curve_b,base.curve_b)
                self.assertEqual(result.offset_r,base.offset_r)
                self.assertEqual(preset_grade(item,base,0),base)
        self.assertEqual(base.saturation,1)

    def test_search_combines_words_and_category(self):
        self.assertEqual([item.key for item in filtered_presets('调色风格','经典 黑白')],['mono'])
        self.assertEqual(len(filtered_presets('转场')),9)
        self.assertEqual(filtered_presets('转场','人像'),())
        asset=EffectPreset('overlay:a','夜间光斑','叠加 / 光效','alpha',kind='overlay')
        self.assertEqual(filtered_presets('叠加','光斑',(asset,)),(asset,))

    def test_library_assets_forward_their_real_payload_and_controls(self):
        asset=OverlayAsset('glow','光斑','screen','光效','glow.mov',1920,1080,30,60,2,'test')
        mask=TransitionMask('m','画笔','画笔','mask.png')
        ws=SimpleNamespace(_available=lambda:True,selected_program_range=(2,4),clear_grade_preview=Mock(),
            apply_library_overlay=Mock(return_value=True),apply_library_mask=Mock(return_value=True))
        self.assertTrue(apply_preset(ws,EffectPreset('o','光斑','叠加','',kind='overlay',asset=asset),.42))
        ws.apply_library_overlay.assert_called_once_with(asset,opacity=.42)
        self.assertTrue(apply_preset(ws,EffectPreset('m','画笔','转场','',kind='mask',asset=mask),duration=.8))
        ws.apply_library_mask.assert_called_once_with(mask,duration=.8)


class ResponsiveLibraryGeometryTests(unittest.TestCase):
    def test_short_default_sidebar_keeps_list_and_primary_actions_visible(self):
        try:root=tk.Tk()
        except tk.TclError as error:self.skipTest(str(error))
        root.withdraw();root.attributes('-alpha',0)
        try:
            root.tk.call('tk','scaling',96/72)
            ttk.Style(root).configure('Multicam.Treeview',rowheight=25)
            ws=SimpleNamespace(_px=lambda value:value,selected_program_range=None,
                _available=lambda:True,clear_grade_preview=Mock(),
                apply_library_overlay=Mock(),apply_library_mask=Mock())
            with patch('effects_browser.EffectsBrowser._request_thumbnail'):
                panel=EffectsBrowser(root,ws);panel.pack(fill='both',expand=True)
                root.geometry('304x340+10000+10000');root.deiconify();root.update()
                for height in (340,350,400,450):
                    with self.subTest(height=height):
                        root.geometry(f'304x{height}+10000+10000');root.update()
                        visible=sum(bool(panel.list.bbox(key)) for key in panel.list.get_children())
                        bottom=panel.apply_button.winfo_rooty()+panel.apply_button.winfo_height()-panel.winfo_rooty()
                        self.assertGreaterEqual(visible,4)
                        self.assertLessEqual(bottom,height)
                        self.assertGreaterEqual(panel.detail_canvas.winfo_height(),48)
                        self.assertGreater(panel.detail_body.winfo_height(),panel.detail_canvas.winfo_height())
                panel.feedback.set('这是一段需要在详情区域内滚动的较长说明。'*20)
                root.update()
                self.assertGreaterEqual(sum(bool(panel.list.bbox(key)) for key in panel.list.get_children()),4)
                panel.detail_canvas.yview_moveto(1)
                self.assertGreater(panel.detail_canvas.yview()[0],0)
                panel.destroy()
        finally:root.destroy()


class EffectsBrowserUITests(unittest.TestCase):
    # Reuse only the established transparent/offscreen workspace fixture.
    setUp=workspace_fixture.ProgramEditWorkspaceTests.setUp
    tearDown=workspace_fixture.ProgramEditWorkspaceTests.tearDown

    def browser(self):
        with patch('effects_browser.library_presets',return_value=PRESETS):
            result=show_effects_browser(self.workspace)
        self.root.update_idletasks()
        return result

    def test_native_library_stays_beside_the_programme_and_timeline(self):
        browser=self.browser()
        self.assertEqual(self.workspace.inspector_tabs.select(),str(self.workspace.library_tab))
        self.assertEqual(browser.master,self.workspace.library_tab)
        self.assertIn(str(self.workspace.timeline_section),self.workspace.workspace_split.panes())
        self.assertEqual(browser.list.get_children(),tuple(item.key for item in PRESETS))
        browser.category.set('转场');browser.query.set('向左')
        self.assertEqual(len(browser.list.get_children()),2)
        self.assertEqual(self.callback_errors,[])

    def test_range_grade_undo_redo_save_and_export_share_the_same_values(self):
        ws=self.workspace;ws.selected_program_range=(2,6)
        original=copy.deepcopy(ws.project)
        browser=self.browser();browser.list.selection_set('mono');browser._selection_changed()
        self.assertTrue(browser.apply())
        result=grade_at(ws.project,3)
        self.assertEqual(result.saturation,0)
        self.assertIsNone(grade_at(ws.project,1))
        self.assertIsNone(grade_at(ws.project,7))
        self.assertEqual(ws.project.global_grade,original.global_grade)
        self.assertEqual(len(ws.undo_stack),1)
        self.assertTrue(grade_filter(result))
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'library-test.okk'
            ws.project.save(path)
            restored=MulticamProject.load(path)
            self.assertEqual(grade_filter(grade_at(restored,3)),grade_filter(result))
        ws.undo();self.assertEqual(ws.project,original)
        ws.redo();self.assertEqual(grade_at(ws.project,3).saturation,0)
        self.assertEqual(self.callback_errors,[])

    def test_color_audition_does_not_mutate_or_create_undo_and_can_be_stopped(self):
        ws=self.workspace;ws.selected_program_range=(2,6);ws.player=Mock()
        before=copy.deepcopy(ws.project)
        browser=self.browser();browser.list.selection_set('warm');browser._selection_changed()
        self.assertTrue(browser.preview())
        ws._flush_grade_preview()
        calls=ws.player.submit.call_args_list
        self.assertTrue(any(call.args==('grade_preview',) and call.kwargs.get('target')==('selection',2,6) for call in calls))
        self.assertEqual(ws.project,before);self.assertEqual(ws.undo_stack,[])
        browser.stop_preview()
        ws.player.submit.assert_called_with('grade_preview',grade=None)

    def test_transition_uses_selected_end_and_existing_undo(self):
        ws=self.workspace;ws.project.cuts=[Cut(0,1),Cut(4,2)];ws.selected_program_range=(0,4);ws._refresh()
        browser=self.browser();browser.list.selection_set('transition_wipeleft');browser._selection_changed()
        self.assertTrue(browser.apply())
        self.assertEqual(len(ws.project.transitions),1)
        self.assertEqual((ws.project.transitions[0].time,ws.project.transitions[0].kind),(4,'wipeleft'))
        self.assertEqual(len(ws.undo_stack),1)
        ws.undo();self.assertEqual(ws.project.transitions,[])

    def test_no_selection_or_busy_cannot_modify_the_project(self):
        browser=self.browser();before=copy.deepcopy(self.workspace.project)
        self.assertFalse(browser.apply());self.assertIn('选择',browser.feedback.get())
        self.workspace.selected_program_range=(0,4);self.workspace.busy=True
        self.assertFalse(browser.apply());self.assertEqual(self.workspace.project,before)
        self.workspace.busy=False

    def test_hiding_library_clears_color_audition(self):
        ws=self.workspace;ws.selected_program_range=(1,3);ws.player=Mock()
        browser=self.browser();self.assertTrue(browser.preview());ws._flush_grade_preview()
        ws.inspector_tabs.select(ws.program_inspector_tab);self.root.update()
        self.assertFalse(browser._previewing)
        ws.player.submit.assert_called_with('grade_preview',grade=None)


if __name__=='__main__':unittest.main()
