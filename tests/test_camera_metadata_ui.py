"""Camera make/model labels in native multicamera editing controls.

Metadata is a deterministic fixture. No source probing, GPU or user desktop
input is used; the actual workspace keyboard and drawing callbacks are used.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

try:
    from . import test_program_edit_ui as program_fixture
except ImportError:
    import test_program_edit_ui as program_fixture

from multicam_core import CameraClip, Cut, MulticamProject


class CameraMetadataWorkspaceTests(unittest.TestCase):
    def setUp(self):
        program_fixture.ProgramEditWorkspaceTests.setUp(self)
        clips=[]
        for identifier,camera,make,model in (
            ('a',1,'Sony','ILCE-7M4'),
            ('b',2,'Sony','ILCE-7M5'),
            ('c',3,'Nikon','NIKON Z8'),
            ('d',4,'Nikon','NIKON Z9'),
        ):
            path=self.folder/(identifier+'.mp4')
            if not path.is_file():path.write_bytes(b'fixture path only')
            clips.append(CameraClip(identifier,camera,str(path),0.,12.,camera_make=make,camera_model=model))
        self.workspace.project=MulticamProject(clips=clips,cuts=[Cut(0.,1),Cut(4.,2),Cut(8.,3)],fps=30.)
        self.workspace.current=5.
        self.workspace._refresh()
        self.root.update()

    tearDown=program_fixture.ProgramEditWorkspaceTests.tearDown

    def canvas_text(self):
        canvas=self.workspace.timeline
        return '\n'.join(canvas.itemcget(item,'text') for item in canvas.find_all() if canvas.type(item)=='text')

    def test_source_brand_and_model_are_visible_without_changing_programme(self):
        workspace=self.workspace
        before=copy.deepcopy(workspace.project)
        timeline_text=self.canvas_text()
        names={1:'A7M4',2:'A7M5',3:'Z8',4:'Z9'}
        for clip in workspace.project.clips:
            tree_text=' '.join(workspace.media_tree.item(item,'text') for item in (clip.id,f'camera_{clip.camera}'))
            button_text=workspace.camera_buttons[clip.camera].cget('text')
            value=names[clip.camera]
            self.assertIn(value,tree_text)
            self.assertIn(value,button_text)
            self.assertIn(value,timeline_text)
        self.assertEqual(workspace.project,before)
        self.assertEqual((workspace.project.clips[0].camera_make,workspace.project.clips[0].camera_model),('Sony','ILCE-7M4'))
        path=self.folder/'metadata.multicam.json'
        workspace.project.save(path)
        restored=MulticamProject.load(path)
        self.assertEqual((restored.clips[0].camera_make,restored.clips[0].camera_model),('Sony','ILCE-7M4'))
        self.assertEqual((restored.clips[3].camera_make,restored.clips[3].camera_model),('Nikon','NIKON Z9'))
        self.assertEqual(self.callback_errors,[])
        self.dialogs[0].assert_not_called()

    def test_number_switch_updates_program_title_and_only_selected_cut(self):
        workspace=self.workspace
        workspace.player=Mock()
        workspace.select_program_range(4.,8.,seek=False)
        before=copy.deepcopy(workspace.project)
        result=workspace._key(SimpleNamespace(widget=workspace.timeline,keysym='3',char='3',state=0))
        self.assertEqual(result,'break')
        self.assertEqual(workspace.selected_program_range,(4.,8.))
        self.assertEqual(workspace.project.camera_at(5.),3)
        self.assertEqual(workspace.project.camera_at(1.),before.camera_at(1.))
        self.assertEqual(workspace.project.camera_at(9.),before.camera_at(9.))
        workspace.player_queue.put(('state',{'time':5.,'camera':3,'playing':False,'preview':False}))
        workspace._poll()
        self.assertIn('Z8',workspace.program_var.get())
        self.assertEqual(len(workspace.undo_stack),1)
        workspace.undo()
        self.assertEqual(workspace.project.cuts,before.cuts)
        self.assertEqual(self.callback_errors,[])
        self.dialogs[0].assert_not_called()


if __name__=='__main__':unittest.main()
