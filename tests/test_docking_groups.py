"""Actual Windows floating groups: native content, tab events and restoration."""
from __future__ import annotations
import copy
import os
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_docking import DockingTests


@unittest.skipUnless(os.name=='nt','Native wrapper embedding requires Windows')
class DockingGroupTests(unittest.TestCase):
    setUp=DockingTests.setUp
    tearDown=DockingTests.tearDown
    update=DockingTests.update
    stable_ids=DockingTests.stable_ids
    change=DockingTests.change

    def float(self,key='media'):
        self.errors=[]
        self.root.report_callback_exception=lambda *args:self.errors.append(args)
        self.manager.float_group(key,'500x360+7800+7800');self.update()
        self.assertEqual(self.errors,[])
        return self.manager.floating_group_window(key)

    def assert_content_visible(self,key):
        self.manager.focus(key);self.update()
        video=self.videos[key]
        x=video.winfo_rootx()+video.winfo_width()//2;y=video.winfo_rooty()+video.winfo_height()//2
        self.assertIs(self.root.winfo_containing(x,y),video)

    def test_entire_default_tab_group_floats_in_one_shell_and_tab_clicks_show_real_content(self):
        window=self.float()
        self.assertEqual(self.manager.group_members('media'),('media','cuts'))
        self.assertIs(window,self.manager.floating_group_window('cuts'))
        self.assertEqual(list(self.manager.iter_floating_windows()),[window])
        for key in ('cuts','media','cuts'):self.assert_content_visible(key)
        group=self.manager._floating_group('media');host=self.manager._float_hosts[group['id']]
        # A real press on the first tab reaches the notebook, not a blank host.
        host.notebook.event_generate('<ButtonPress-1>',x=12,y=10)
        host.notebook.event_generate('<ButtonRelease-1>',x=12,y=10)
        self.update();self.assertEqual(group['selected'],'media')
        self.assertEqual(self.errors,[]);self.stable_ids()

    def test_shell_moves_resizes_and_minimizes_all_native_content_as_one_window(self):
        window=self.float()
        for geometry in ('650x420+7900+7700','420x280+8100+7800'):
            window.geometry(geometry);self.update();self.assert_content_visible('cuts')
            video=self.videos['cuts']
            self.assertGreaterEqual(video.winfo_rootx(),window.winfo_rootx())
            self.assertLess(video.winfo_rootx()+video.winfo_width(),window.winfo_rootx()+window.winfo_width()+2)
        window.iconify();self.update();self.assertFalse(self.manager.is_visible('cuts'))
        window.deiconify();self.update();self.assert_content_visible('media');self.stable_ids()

    def test_child_configure_and_unchanged_shell_do_not_repaint_native_panels(self):
        self.float();group=self.manager._floating_group('media')
        shell=self.manager._float_hosts[group['id']]
        selected=shell.members[group['selected']]
        other=shell.members[next(key for key in group['tabs'] if key!=group['selected'])]
        with (patch.object(selected,'show',wraps=selected.show) as show,
              patch.object(selected,'resize',wraps=selected.resize) as resize,
              patch.object(other,'hide',wraps=other.hide) as hide):
            # Real Tk Toplevel bindtags forward each descendant's event.
            for _ in range(100):
                shell._configure(SimpleNamespace(widget=self.videos['media']))
            self.assertIsNone(shell._after)
            # A move event, or repeated Configure with unchanged client size,
            # must also leave MPV/GPU native viewports untouched.
            for _ in range(12):
                shell._configure(SimpleNamespace(widget=shell.window));self.update()
            self.assertEqual((show.call_count,resize.call_count,hide.call_count),(0,0,0))
        self.assert_content_visible('cuts');self.assert_content_visible('media')
        self.assertEqual(self.errors,[]);self.stable_ids()

    def test_first_tab_can_leave_then_remaining_group_header_drags_correct_members(self):
        self.float();self.manager.dock('media','source');self.update()
        group=self.manager._floating_group('cuts');host=self.manager._float_hosts[group['id']]
        host.header.event_generate('<ButtonPress-1>',x=8,y=6)
        self.assertEqual(self.manager._drag['key'],'cuts')
        target=self.widgets['program'];x=target.winfo_rootx()+target.winfo_width()//2;y=target.winfo_rooty()+target.winfo_height()//2
        host.header.event_generate('<B1-Motion>',x=8,y=6,rootx=x,rooty=y)
        host.header.event_generate('<ButtonRelease-1>',x=8,y=6,rootx=x,rooty=y)
        self.update()
        self.assertEqual(self.manager.group_members('cuts'),('program','cuts'))
        self.assertEqual(self.manager.group_members('media'),('source','media'));self.stable_ids()

    def test_single_tab_and_whole_group_can_merge_into_other_floating_group(self):
        self.float();self.manager.float_group('source','440x320+8400+7800');self.update()
        self.manager.dock('cuts','source');self.update()
        self.assertEqual(self.manager.group_members('source'),('source','cuts'))
        self.manager.dock_group('source','media');self.update()
        self.assertEqual(self.manager.group_members('media'),('media','source','cuts'))
        self.assertEqual(len(list(self.manager.iter_floating_windows())),1)
        for key in ('source','cuts','media'):self.assert_content_visible(key)
        self.assertEqual(self.errors,[]);self.stable_ids()

    def test_float_close_returns_complete_group_and_edge_dock_preserves_order(self):
        window=self.float();command=window.protocol('WM_DELETE_WINDOW')
        window.tk.call(command);self.update()
        self.assertIsNone(self.manager.floating_group_window('media'))
        self.assertEqual(self.manager.group_members('media'),('media','cuts'))
        self.manager.float_group('media','500x360+7800+7800');self.update()
        self.manager.dock_group('media','program','bottom');self.update()
        node=self.manager.serialize()['roots']['program']
        self.assertEqual(node['orient'],'vertical')
        self.assertEqual(node['children'][1]['tabs'],['media','cuts']);self.stable_ids()

    def test_save_restore_group_order_selection_geometry_and_v1_single_windows(self):
        self.float();self.manager.focus('cuts');self.update()
        saved=self.manager.serialize()
        self.manager.restore_defaults()
        with patch.object(self.manager,'_screen_bounds',return_value=(0,0,20000,20000)):
            self.manager.restore(saved)
        self.update();self.assertEqual(self.manager.group_members('cuts'),('media','cuts'))
        self.assertEqual(self.manager._floating_group('cuts')['selected'],'cuts')
        self.assertEqual(self.manager.floating_group_window('cuts').geometry(),saved['floating_groups'][0]['geometry'])
        self.manager.restore_defaults();self.manager.float_panel('source','430x310+7800+7800');self.update()
        old=self.manager.serialize();old['version']=1;old.pop('floating_groups')
        self.manager.restore_defaults()
        with patch.object(self.manager,'_screen_bounds',return_value=(0,0,20000,20000)):self.manager.restore(old)
        self.update();self.assertEqual(self.manager.panel_location('source'),'floating')
        self.assertIsNone(self.manager.floating_group_window('source'));self.stable_ids()

    def test_hide_one_member_and_show_rejoins_same_float_shell(self):
        window=self.float();self.manager.hide('cuts');self.update()
        self.assertEqual(self.manager.panel_location('cuts'),'hidden')
        self.manager.show('cuts');self.update()
        self.assertIs(self.manager.floating_group_window('cuts'),window)
        self.assert_content_visible('cuts');self.stable_ids()

    def test_hidden_tab_rejoins_restored_group_after_layout_save_and_reload(self):
        self.float();self.manager.hide('cuts');self.update()
        saved=self.manager.serialize();unchanged=copy.deepcopy(saved)
        old_identity=saved['hidden']['cuts']['group']
        self.manager.restore_defaults()
        with patch.object(self.manager,'_screen_bounds',return_value=(0,0,20000,20000)):
            self.manager.restore(saved)
        self.update();window=self.manager.floating_group_window('media')
        self.assertNotEqual(self.manager._hidden['cuts']['group'],old_identity)
        self.manager.show('cuts');self.update()
        self.assertIs(self.manager.floating_group_window('cuts'),window)
        self.assertEqual(self.manager.group_members('cuts'),('media','cuts'))
        self.assertEqual(len(list(self.manager.iter_floating_windows())),1)
        self.assertEqual(saved,unchanged)
        self.assert_content_visible('cuts');self.stable_ids()

    def test_fully_hidden_group_retains_shared_identity_without_colliding_with_live_group(self):
        self.float();self.manager.hide('media');self.manager.hide('cuts');self.update()
        self.manager.dock('source','program');self.manager.float_group('source','500x360+8300+7800');self.update()
        saved=self.manager.serialize()
        self.manager.restore_defaults()
        with patch.object(self.manager,'_screen_bounds',return_value=(0,0,20000,20000)):
            self.manager.restore(saved)
        self.update();other=self.manager.floating_group_window('source')
        self.assertEqual(self.manager._hidden['media']['group'],self.manager._hidden['cuts']['group'])
        self.assertNotEqual(self.manager._hidden['media']['group'],self.manager._floating_group('source')['id'])
        self.manager.show('cuts');self.update();window=self.manager.floating_group_window('cuts')
        self.manager.show('media');self.update()
        self.assertIs(self.manager.floating_group_window('media'),window)
        self.assertIsNot(window,other)
        self.assertEqual(set(self.manager.group_members('media')),{'media','cuts'})
        self.assertEqual(len(list(self.manager.iter_floating_windows())),2)
        self.assert_content_visible('media');self.stable_ids()

    def test_detach_failure_keeps_shell_and_owner_reachable_until_retry(self):
        window=self.float();group=self.manager._floating_group('media')
        host=self.manager._float_hosts[group['id']];native=host.members['media']
        with patch.object(native,'detach',side_effect=RuntimeError('retry native detach')):
            with self.assertRaisesRegex(RuntimeError,'retry native'):self.manager.restore_defaults()
        self.assertTrue(window.winfo_exists());self.assertIs(host.members['media'],native)
        self.manager.restore_defaults();self.update();self.stable_ids()
        self.assertEqual(list(self.manager.iter_floating_windows()),[])


if __name__=='__main__':unittest.main(verbosity=2)
