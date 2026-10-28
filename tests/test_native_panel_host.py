"""Real offscreen native group embedding and recoverable detach failures."""
from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W
import os
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

from native_panel_host import NativePanelHost


@unittest.skipUnless(os.name == 'nt', 'Windows native embedding')
class NativePanelHostTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw(); self.root.geometry('1000x700+8000+8000')
        self.errors = []
        self.root.report_callback_exception = lambda *values: self.errors.append(values)
        self.workspace = tk.Frame(self.root); self.workspace.pack(fill='both', expand=True)
        self.home = tk.Frame(self.workspace); self.home.pack(fill='both', expand=True)
        self.leaves, self.videos, self.buttons, self.hosts, self.clicks = {}, {}, {}, {}, []
        for key in ('source','program'):
            leaf = tk.Frame(self.workspace)
            leaf.pack(in_=self.home, fill='both', expand=True)
            button = tk.Button(leaf, text=key, command=lambda key=key: self.clicks.append(key))
            button.pack(fill='x')
            video = tk.Frame(leaf, bg='#23475f', width=320, height=150)
            video.pack(fill='both', expand=True)
            self.leaves[key], self.buttons[key], self.videos[key] = leaf, button, video
        self.shell = tk.Toplevel(self.root); self.shell.withdraw()
        self.shell.geometry('740x480+9000+9000')
        self.tabs = ttk.Notebook(self.shell); self.tabs.pack(fill='both', expand=True)
        self.pages = {}
        for key in self.leaves:
            self.pages[key] = tk.Frame(self.tabs)
            self.tabs.add(self.pages[key], text=key)
        self.root.deiconify(); self.shell.deiconify(); self.root.update()
        self.ids = {key: (self.leaves[key].winfo_id(), self.videos[key].winfo_id(), self.buttons[key].winfo_id())
                    for key in self.leaves}

    def tearDown(self):
        for host in self.hosts.values():
            if host.owns_window: host.detach()
        for pending in self.root.tk.call('after','info'):
            self.root.tk.call('after','cancel',pending)
        self.root.destroy()

    def attach(self, key):
        self.leaves[key].pack_forget()
        host = NativePanelHost(self.leaves[key], self.pages[key])
        self.hosts[key] = host
        host.attach()
        return host

    def select(self, key):
        self.tabs.select(self.pages[key])
        for name, host in self.hosts.items():
            host.show() if name == key else host.hide()
        for _ in range(3): self.root.update()

    def check_picture(self, key):
        video, leaf, button = self.videos[key], self.leaves[key], self.buttons[key]
        self.assertEqual(self.ids[key], (leaf.winfo_id(),video.winfo_id(),button.winfo_id()))
        api = self.hosts[key]._native.api
        api.GetWindowRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
        value = W.RECT(); self.assertTrue(api.GetWindowRect(video.winfo_id(), C.byref(value)))
        actual = [value.left,value.top,value.right,value.bottom]
        expected = [video.winfo_rootx(),video.winfo_rooty(),
                    video.winfo_rootx()+video.winfo_width(),video.winfo_rooty()+video.winfo_height()]
        self.assertEqual(actual, expected)
        self.assertIs(self.root.winfo_containing((value.left+value.right)//2,(value.top+value.bottom)//2), video)
        before = len(self.clicks)
        x,y = button.winfo_width()//2,button.winfo_height()//2
        for event in ('<Enter>','<ButtonPress-1>','<ButtonRelease-1>'):
            button.event_generate(event,x=x,y=y,when='now')
        self.root.update()
        self.assertEqual(self.clicks[before:], [key])

    def test_tabs_resize_idle_redock_and_shell_close_preserve_original_video_hwnds(self):
        for key in self.leaves: self.attach(key)
        for geometry in ('740x480+9000+9000','950x620+9200+9100','480x320+9100+9000'):
            self.shell.geometry(geometry)
            for key in ('source','program','source'):
                self.select(key)
                self.check_picture(key)
                host = self.hosts[key]
                self.assertEqual(host._native.parent(host._wrapper), self.pages[key].winfo_id())
        for key,host in self.hosts.items():
            host.detach()
            self.assertFalse(host.attached)
            self.assertFalse(host.owns_window)
            self.assertEqual(self.leaves[key].winfo_manager(), '')
            self.leaves[key].pack(in_=self.home,fill='both',expand=True)
        self.shell.destroy(); self.root.update()
        for key in self.leaves: self.check_picture(key)
        self.assertEqual(self.errors, [])

    def test_native_detach_failure_keeps_parent_and_owner_and_can_retry(self):
        host = self.attach('source'); self.select('source')
        wrapper = host._wrapper
        with patch.object(host._native,'set_parent',side_effect=OSError('injected SetParent failure')):
            with self.assertRaisesRegex(OSError,'SetParent'): host.detach()
        self.assertTrue(host.owns_window)
        self.assertTrue(host.attached)
        self.assertTrue(self.shell.winfo_exists())
        self.assertEqual(host._native.parent(wrapper),self.pages['source'].winfo_id())
        self.assertTrue(host._native.exists(self.videos['source'].winfo_id()))
        host.detach()
        self.assertFalse(host.owns_window)
        self.leaves['source'].pack(in_=self.home,fill='both',expand=True)
        self.shell.destroy(); self.root.update()
        self.check_picture('source')
        self.assertEqual(self.errors, [])

    def test_tk_detach_failure_preserves_content_outside_shell_until_retry(self):
        host = self.attach('source'); self.select('source')
        with patch.object(host,'_forget_window_manager',side_effect=tk.TclError('injected wm forget failure')):
            with self.assertRaisesRegex(tk.TclError,'wm forget'): host.detach()
        self.assertTrue(host.owns_window)
        self.assertFalse(host.attached)
        self.assertEqual(host._native.parent(host._wrapper),0)
        self.assertEqual(self.leaves['source'].winfo_id(),self.ids['source'][0])
        host.detach()
        self.assertFalse(host.owns_window)
        self.assertTrue(host._native.exists(self.ids['source'][1]))
        self.assertEqual(self.errors, [])

    def test_attach_failure_rolls_back_without_destroying_content_or_shell(self):
        self.leaves['source'].pack_forget()
        host = self.hosts['source'] = NativePanelHost(self.leaves['source'],self.pages['source'])
        original = host._native.set_parent
        def fail_embed(hwnd,parent):
            if parent: raise OSError('injected embed failure')
            return original(hwnd,parent)
        with patch.object(host._native,'set_parent',side_effect=fail_embed):
            with self.assertRaisesRegex(OSError,'embed'): host.attach()
        self.assertFalse(host.owns_window)
        self.assertEqual(self.leaves['source'].winfo_id(),self.ids['source'][0])
        self.assertTrue(self.shell.winfo_exists())
        host.attach(); self.select('source'); self.check_picture('source')
        self.assertEqual(self.errors, [])

    def test_existing_single_float_can_embed_and_pending_resize_is_cancelled_on_detach(self):
        leaf = self.leaves['source']; leaf.pack_forget()
        self.root.tk.call('wm','manage',leaf)
        self.root.tk.call('wm','geometry',leaf,'360x240+10000+10000')
        self.root.update()
        host = self.hosts['source'] = NativePanelHost(leaf,self.pages['source'])
        host.attach(); self.select('source'); self.check_picture('source')
        host.resize(400,300)
        first = host._resize_after
        host.resize(410,310)
        self.assertEqual(host._resize_after,first)
        host.detach()
        self.assertIsNone(host._resize_after)
        self.shell.destroy(); self.root.update()
        self.assertEqual(leaf.winfo_id(),self.ids['source'][0])
        self.assertEqual(self.errors, [])

    def test_unchanged_layout_and_repeated_visibility_do_not_touch_native_video_windows(self):
        host = self.attach('source'); self.select('source')
        with patch.object(host._native, 'position', wraps=host._native.position) as position, \
             patch.object(host._native, 'show', wraps=host._native.show) as show:
            for _ in range(50):
                host.show(); host.resize(); self.root.update()
            self.assertEqual(position.call_count, 0)
            self.assertEqual(show.call_count, 0)
            self.assertIsNone(host._resize_after)
            for _ in range(50): host.hide()
            self.root.update()
            self.assertEqual(show.call_count, 1)
            for _ in range(50): host.show()
            self.root.update()
            self.assertEqual(show.call_count, 2)
            self.assertEqual(position.call_count, 0)
            self.shell.geometry('820x510+9000+9000'); self.root.update()
            self.assertGreater(position.call_count, 0)
            self.assertTrue(all(not call.kwargs['frame_changed'] for call in position.call_args_list))
        self.check_picture('source')
        self.assertEqual(self.errors, [])

    def test_native_frame_style_refresh_happens_only_on_attach(self):
        self.leaves['source'].pack_forget()
        host = self.hosts['source'] = NativePanelHost(self.leaves['source'], self.pages['source'])
        with patch.object(host._native, 'position', wraps=host._native.position) as position:
            host.attach(); self.select('source')
            self.shell.geometry('820x510+9000+9000'); self.root.update()
            self.assertEqual(sum(bool(call.kwargs['frame_changed']) for call in position.call_args_list), 1)
        self.assertEqual(self.errors, [])


if __name__ == '__main__': unittest.main()
