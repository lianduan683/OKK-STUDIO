"""Real Tk layout/drag tests. All windows belong to this offscreen fixture."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import tempfile
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from docking import DockManager


class DockingTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk();self.root.geometry('1100x700+8000+8000')
        self.workspace=tk.Frame(self.root);self.workspace.pack(fill='both',expand=True)
        self.workspace.grid_rowconfigure(0,weight=1);self.workspace.grid_columnconfigure(0,weight=1)
        self.host=tk.PanedWindow(self.workspace,orient='horizontal');self.host.grid(row=0,column=0,sticky='nsew')
        left=tk.Frame(self.host,width=300);right=tk.Frame(self.host,width=800)
        self.host.add(left,minsize=200);self.host.add(right,minsize=400)
        right.grid_columnconfigure((0,1),weight=1);right.grid_rowconfigure(0,weight=1)
        self.notebook=ttk.Notebook(left);self.notebook.pack(fill='both',expand=True)
        self.widgets={};self.videos={};self.headers={}
        for key in ('media','cuts','source','program','timeline'):
            panel=tk.Frame(self.workspace,bg='#292b2e');self.widgets[key]=panel
            title=tk.Label(panel,text=key,bg='#292b2e',fg='white');title.pack(fill='x')
            self.headers[key]=title
            video=tk.Frame(panel,bg='#15384b',width=80,height=60);video.pack(fill='both',expand=True)
            self.videos[key]=video
        self.notebook.add(self.widgets['media'],text='素材');self.notebook.add(self.widgets['cuts'],text='切点')
        self.widgets['source'].grid(in_=right,row=0,column=0,sticky='nsew')
        self.widgets['program'].grid(in_=right,row=0,column=1,sticky='nsew')
        self.widgets['timeline'].grid(row=1,column=0,sticky='nsew')
        self.changed=0;self.focused=[]
        self.manager=DockManager(self.workspace,scale=1,on_change=self.change,on_focus=self.focused.append)
        for key,widget in self.widgets.items():
            self.manager.register(key,key,widget,header=self.headers[key] if key in ('source','program','timeline') else None,minsize=(180,100))
        self.root.update()
        self.ids={key:(widget.winfo_id(),self.videos[key].winfo_id()) for key,widget in self.widgets.items()}

    def change(self):self.changed+=1
    def tearDown(self):
        self.manager.close();self.root.destroy()
    def update(self):self.root.update()
    def stable_ids(self):
        self.assertEqual(self.ids,{key:(widget.winfo_id(),self.videos[key].winfo_id()) for key,widget in self.widgets.items()})

    def test_registration_does_not_change_original_layout_or_native_handles(self):
        self.assertEqual(tuple(map(str,self.notebook.tabs())),tuple(str(self.widgets[key]) for key in ('media','cuts')))
        self.assertEqual(self.widgets['source'].grid_info()['column'],0)
        self.assertTrue(all(self.manager.is_home(key) for key in self.widgets))
        self.assertFalse(self.manager.serialize()['active']);self.stable_ids()

    def test_cross_group_tab_merge_and_all_four_split_edges_preserve_handles(self):
        self.manager.dock('media','program');self.update()
        self.assertEqual(self.manager.focus_home('media'),'program')
        self.assertEqual(self.manager._find('program')[1]['tabs'],['program','media'])
        for side in ('left','right','top','bottom'):
            self.manager.dock('source','program',side);self.update();self.stable_ids()
            root=self.manager.serialize()['roots']['program']
            self.assertEqual(root['type'],'split')
            self.assertEqual(root['orient'],'horizontal' if side in ('left','right') else 'vertical')

    def test_visible_panel_content_is_above_dynamic_sibling_hosts(self):
        self.manager.dock('source','program');self.update()
        self.manager.dock('cuts','timeline','right');self.update()
        for key in ('media','source','program','cuts','timeline'):
            self.manager.focus(key);self.update()
            video=self.videos[key]
            x=video.winfo_rootx()+video.winfo_width()//2
            y=video.winfo_rooty()+video.winfo_height()//2
            self.assertIs(self.root.winfo_containing(x,y),video,key+' content covered by dock host')

    def test_dock_tabs_do_not_inherit_hidden_application_mode_tab_layout(self):
        style=ttk.Style(self.root)
        style.layout('Workspace.TNotebook.Tab',[])
        self.manager.dock('source','program');self.update()
        _,group=self.manager._find('source');notebook=self.manager._groups[group['id']][1]
        self.assertEqual(notebook.cget('style'),'Dock.TNotebook')
        self.assertNotEqual(style.layout('Dock.TNotebook.Tab'),[('null',{'sticky':'nswe'})])
        self.assertEqual(notebook.index('@12,10'),0)
        x,y=notebook.winfo_rootx()+12,notebook.winfo_rooty()+10
        self.assertIs(self.root.winfo_containing(x,y),notebook)

    def test_empty_original_monitor_column_collapses_and_default_restores_weight(self):
        container=self.widgets['source'].grid_info()['in']
        self.manager.dock('source','program')
        self.assertEqual(container.grid_columnconfigure(0)['weight'],0)
        self.assertEqual(container.grid_columnconfigure(1)['weight'],1)
        self.assertFalse(self.manager.home_has_content('source'))
        self.manager.restore_defaults()
        self.assertEqual(container.grid_columnconfigure(0)['weight'],1)
        self.assertTrue(self.manager.home_has_content('source'))

    def test_float_original_frame_then_dock_back_without_recreating_video_hwnd(self):
        self.manager.float_panel('media','420x300+7800+7800');self.update();self.stable_ids()
        self.assertEqual(self.widgets['media'].winfo_manager(),'wm')
        self.assertEqual(self.manager.panel_location('media'),'floating')
        self.assertIsNone(self.manager.focus_home('media'))
        self.assertIsNotNone(self.manager.panels['media'].grip)
        self.manager.dock('media','program','bottom');self.update();self.stable_ids()
        self.assertEqual(self.widgets['media'].winfo_manager(),'notebook')
        self.assertIsNone(self.manager.panels['media'].grip)

    def test_restore_defaults_recovers_original_tabs_grid_and_pack_order(self):
        self.manager.float_panel('cuts');self.manager.dock('source','program','right')
        self.manager.hide('media');self.manager.restore_defaults();self.update()
        self.assertEqual(self.notebook.tabs(),tuple(str(self.widgets[key]) for key in ('media','cuts')))
        self.assertEqual(self.widgets['source'].grid_info()['column'],0)
        self.assertEqual(self.widgets['media'].pack_slaves(),[self.headers['media'],self.videos['media']])
        self.assertTrue(all(self.manager.is_home(key) for key in self.widgets));self.stable_ids()

    def test_serialized_layout_preserves_split_tabs_float_geometry_hidden_and_selection(self):
        self.manager.dock('media','program','top');self.manager.dock('cuts','media')
        self.manager.float_panel('source','440x330+7800+7700');self.manager.hide('timeline');self.update()
        self.manager.focus('cuts');state=self.manager.serialize()
        self.manager.restore_defaults()
        with patch.object(self.manager,'_screen_bounds',return_value=(0,0,20000,20000)):
            self.manager.restore(json.loads(json.dumps(state)))
        self.update()
        restored=self.manager.serialize()
        self.assertEqual(self.manager.focus_home('cuts'),'program')
        self.assertEqual(self.manager.panel_location('source'),'floating')
        self.assertEqual(self.manager.panel_location('timeline'),'hidden')
        self.assertEqual(restored['floating']['source'],state['floating']['source'])
        self.assertEqual(self.manager._find('cuts')[1]['selected'],'cuts');self.stable_ids()

    def test_hide_show_keeps_group_and_float_location(self):
        self.manager.dock('media','program');self.manager.hide('media');self.manager.show('media')
        self.assertEqual(self.manager.focus_home('media'),'program')
        self.manager.float_panel('cuts','400x300+7900+7800');self.update()
        self.manager.hide('cuts');self.manager.show('cuts');self.update()
        self.assertEqual(self.manager.panel_location('cuts'),'floating');self.stable_ids()

    def test_actual_notebook_mouse_events_merge_then_header_drag_floats(self):
        notebook=self.notebook
        # Generate native Tk events through the bound tab/header entry points.
        x,y=12,10
        target=self.widgets['program'];rx=target.winfo_rootx()+target.winfo_width()//2;ry=target.winfo_rooty()+target.winfo_height()//2
        notebook.event_generate('<ButtonPress-1>',x=x,y=y,rootx=notebook.winfo_rootx()+x,rooty=notebook.winfo_rooty()+y)
        notebook.event_generate('<B1-Motion>',x=x+30,y=y+30,rootx=rx,rooty=ry)
        notebook.event_generate('<ButtonRelease-1>',x=x+30,y=y+30,rootx=rx,rooty=ry)
        self.update();self.assertEqual(self.manager.focus_home('media'),'program')
        header=self.headers['source']
        header.event_generate('<ButtonPress-1>',x=10,y=5,rootx=header.winfo_rootx()+10,rooty=header.winfo_rooty()+5)
        header.event_generate('<B1-Motion>',x=10,y=5,rootx=7000,rooty=7000)
        header.event_generate('<ButtonRelease-1>',x=10,y=5,rootx=7000,rooty=7000)
        self.update();self.assertEqual(self.manager.panel_location('source'),'floating');self.stable_ids()

    def test_escape_cancels_drag_without_changing_layout(self):
        before=self.manager.serialize();header=self.headers['source']
        self.manager._press(SimpleNamespace(x_root=10,y_root=10,widget=header),'source')
        self.manager._motion(SimpleNamespace(x_root=100,y_root=100));self.manager._cancel_drag()
        self.assertEqual(before,self.manager.serialize());self.assertIsNone(self.manager._overlay)

    def test_first_tab_can_be_split_from_its_own_group_by_dragging_to_edge(self):
        notebook=self.notebook
        x=notebook.winfo_rootx()+2;y=notebook.winfo_rooty()+notebook.winfo_height()//2
        target=self.manager._drag_target(x,y,'media')
        self.assertEqual(target[:2],('cuts','left'))
        self.manager._press(SimpleNamespace(x_root=x+30,y_root=y,widget=notebook),'media')
        self.manager._motion(SimpleNamespace(x_root=x,y_root=y))
        self.manager._release(SimpleNamespace(x_root=x,y_root=y));self.update()
        node=self.manager.serialize()['roots']['media']
        self.assertEqual(node['type'],'split')
        self.assertEqual(node['children'][0]['tabs'],['media'])
        self.assertEqual(node['children'][1]['tabs'],['cuts']);self.stable_ids()

    def test_restored_geometry_scales_once_and_recovers_missing_monitor(self):
        with patch.object(self.manager,'_screen_bounds',return_value=(0,0,1920,1080)):
            self.assertEqual(self.manager._restore_geometry('source','800x600+9000+9000',2.),'400x300+40+40')

    def test_default_hidden_panel_shows_and_reset_hides_it_again(self):
        panel=tk.Frame(self.workspace)
        self.manager.register('grading','调色',panel,home={'manager':'paned','container':self.host,'index':1,'options':{}},default_hidden=True)
        self.assertEqual(self.manager.panel_location('grading'),'hidden')
        self.manager.show('grading');self.update();self.assertTrue(self.manager.is_visible('grading'))
        self.assertFalse(self.manager.serialize()['active'])
        self.assertEqual(self.notebook.tabs(),tuple(str(self.widgets[key]) for key in ('media','cuts')))
        self.manager.restore_defaults();self.update();self.assertEqual(panel.winfo_manager(),'')
        self.assertEqual(self.manager.panel_location('grading'),'hidden')

    def test_invalid_layout_rejected_without_mutating_current_layout(self):
        before=self.manager.serialize();invalid=copy.deepcopy(before)
        invalid['roots']['source']['tabs']=['media']
        with self.assertRaises(ValueError):self.manager.restore(invalid)
        self.assertEqual(before,self.manager.serialize())

    def test_save_load_roundtrip_and_callable_dpi_scaling(self):
        self.manager.dock('source','program','left')
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'layout.json';self.manager.save(path)
            self.manager.restore_defaults();self.manager.load(path)
            self.assertEqual(self.manager.focus_home('source'),'program')
            self.assertFalse(path.with_name('layout.json.pending').exists())
        other=tk.Frame(self.root)
        manager=DockManager(other,scale=lambda value:round(value*1.5))
        self.assertEqual(manager._px(240),360);manager.close();other.destroy()

    def test_workspace_hidden_temporarily_withdraws_floating_panel(self):
        self.manager.float_panel('media');self.update()
        self.workspace.pack_forget();self.update()
        self.assertEqual(self.root.tk.call('wm','state',self.widgets['media']),'withdrawn')
        self.workspace.pack(fill='both',expand=True);self.update()
        self.assertEqual(self.root.tk.call('wm','state',self.widgets['media']),'normal')


if __name__=='__main__':unittest.main(verbosity=2)
