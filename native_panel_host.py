"""Embed a managed Tk panel in another toplevel without replacing its HWNDs.

Tk geometry managers cannot move common-parent siblings across a toplevel
boundary. Keep the panel a Tk-managed toplevel, and embed only its native
wrapper in a notebook page. Call detach() on every host before destroying
the floating group: Win32 destroys native children with their parent.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import os
import tkinter as tk


class _Windows:
    def __init__(self):
        if os.name != 'nt':
            raise RuntimeError('整组浮动面板当前需要 Windows。')
        self.api = C.WinDLL('user32', use_last_error=True)
        self.api.GetParent.argtypes = [W.HWND]
        self.api.GetParent.restype = W.HWND
        self.api.SetParent.argtypes = [W.HWND, W.HWND]
        self.api.SetParent.restype = W.HWND
        self.api.GetWindowLongPtrW.argtypes = [W.HWND, C.c_int]
        self.api.GetWindowLongPtrW.restype = C.c_ssize_t
        self.api.SetWindowLongPtrW.argtypes = [W.HWND, C.c_int, C.c_ssize_t]
        self.api.SetWindowLongPtrW.restype = C.c_ssize_t
        self.api.SetWindowPos.argtypes = [W.HWND, W.HWND, C.c_int, C.c_int, C.c_int, C.c_int, W.UINT]
        self.api.ShowWindow.argtypes = [W.HWND, C.c_int]
        self.api.IsWindow.argtypes = [W.HWND]

    def parent(self, hwnd):
        return int(self.api.GetParent(hwnd) or 0)

    def exists(self, hwnd):
        return bool(hwnd and self.api.IsWindow(hwnd))

    def style(self, hwnd, extended=False):
        return self.api.GetWindowLongPtrW(hwnd, -20 if extended else -16)

    def set_style(self, hwnd, value, extended=False):
        C.set_last_error(0)
        previous = self.api.SetWindowLongPtrW(hwnd, -20 if extended else -16, value)
        if not previous and C.get_last_error():
            raise C.WinError(C.get_last_error())

    def set_parent(self, hwnd, parent):
        C.set_last_error(0)
        previous = self.api.SetParent(hwnd, parent or None)
        if not previous and C.get_last_error():
            raise C.WinError(C.get_last_error())

    def position(self, hwnd, width, height, visible=False, *, frame_changed=False):
        # Reapplying FRAMECHANGED on every idle resize invalidates all child
        # video surfaces even when their size has not changed.
        flags = 0x0014 | (0x0020 if frame_changed else 0) | (0x0040 if visible else 0)
        if not self.api.SetWindowPos(hwnd, None, 0, 0, width, height, flags):
            raise C.WinError(C.get_last_error())

    def show(self, hwnd, visible):
        self.api.ShowWindow(hwnd, 4 if visible else 0)  # SHOWNOACTIVATE/HIDE


class NativePanelHost:
    """Own one panel's native embedding until detach() succeeds.

    The caller must remove the panel from pack/grid/place/notebook first.
    Existing wm-managed single-panel windows are accepted. ``owns_window``
    remains true after partial failure, so the caller can retain the shell
    and retry; this helper never destroys a panel, viewport or shell.
    """
    def __init__(self, widget: tk.Frame, container: tk.Misc):
        if widget.winfo_class() != 'Frame' or widget.tk is not container.tk:
            raise ValueError('浮动面板和容器必须是同一 Tk 实例内的原有 Frame。')
        self.widget, self.container = widget, container
        self._native = _Windows()
        self._leaf_hwnd = int(widget.winfo_id())
        self._wrapper = 0
        self._original_style = self._original_exstyle = None
        self._owns_window = False
        self._attached = False
        self._visible = False
        self._resize_after = None
        self._requested_size = None
        self._applied_size = None
        self._applied_visible = False
        self._frame_changed = False
        self._configure_binding = None

    @property
    def attached(self):
        return self._attached

    @property
    def owns_window(self):
        return self._owns_window

    def _assert_leaf(self):
        if not self.widget.winfo_exists() or not self._native.exists(self._leaf_hwnd):
            raise RuntimeError('原面板窗口已被销毁；不能继续浮动或停靠。')
        if int(self.widget.winfo_id()) != self._leaf_hwnd:
            raise RuntimeError('原面板 HWND 已改变；停止操作以保留现有内容。')

    def attach(self):
        if self._attached: return self
        if self._owns_window:
            raise RuntimeError('面板上次挂接未完成，请先 detach() 恢复再重试。')
        self._assert_leaf()
        manager = self.widget.winfo_manager()
        if manager not in ('', 'wm'):
            raise ValueError('挂接浮动组前请先将面板从原几何容器移出。')
        if manager != 'wm':
            self.widget.tk.call('wm', 'manage', self.widget)
        self._owns_window = True
        try:
            self.widget.update_idletasks()
            self._wrapper = self._native.parent(self._leaf_hwnd)
            if not self._wrapper or self._wrapper == int(self.container.winfo_id()):
                raise RuntimeError('未找到面板的独立窗口包装层。')
            self._original_style = self._native.style(self._wrapper)
            self._original_exstyle = self._native.style(self._wrapper, True)
            self._native.show(self._wrapper, False)
            self._native.set_style(self._wrapper, (self._original_style & ~0x80CF0000) | 0x42000000)
            self._native.set_style(self._wrapper, (self._original_exstyle & ~0x00040000) | 0x08000000, True)
            self._native.set_parent(self._wrapper, int(self.container.winfo_id()))
            self._applied_size = None
            self._applied_visible = False
            self._frame_changed = True
            self._attached = True
            self._configure_binding = self.container.bind('<Configure>', self._configured, add='+')
            self.resize()
            return self
        except Exception:
            # Keep ownership visible until a complete rollback succeeds.
            try: self.detach()
            except Exception: pass
            raise

    def _configured(self, event):
        if event.widget is self.container: self.resize()

    def resize(self, width=None, height=None):
        if not self._attached: return
        if (width is None) != (height is None):
            raise ValueError('面板尺寸需要同时提供宽和高。')
        self._requested_size = None if width is None else (max(1, int(width)), max(1, int(height)))
        if (self._desired_size() == self._applied_size and
                self._visible == self._applied_visible and not self._frame_changed):
            return
        if self._resize_after is None:
            self._resize_after = self.container.after_idle(self._apply_geometry)

    def _desired_size(self):
        return self._requested_size or (max(1, self.container.winfo_width()),
                                        max(1, self.container.winfo_height()))

    def _apply_geometry(self):
        self._resize_after = None
        if not self._attached: return
        self._assert_leaf()
        if not self.container.winfo_exists() or not self._native.exists(self._wrapper):
            raise RuntimeError('浮动容器已失效；请保留面板并重新停靠。')
        size = self._desired_size()
        if size != self._applied_size or self._frame_changed:
            self._native.position(self._wrapper, *size, frame_changed=self._frame_changed)
            self._applied_size = size
            self._frame_changed = False
        if self._visible != self._applied_visible:
            self._native.show(self._wrapper, self._visible)
            self._applied_visible = self._visible

    def show(self):
        if not self._attached: raise RuntimeError('面板尚未挂接到浮动组。')
        self._visible = True
        self.resize()

    def hide(self):
        self._visible = False
        if self._applied_visible and self._wrapper and self._native.exists(self._wrapper):
            self._native.show(self._wrapper, False)
            self._applied_visible = False

    def _cancel_geometry(self):
        if self._resize_after is not None:
            try: self.container.after_cancel(self._resize_after)
            except tk.TclError: pass
            self._resize_after = None
        if self._configure_binding is not None:
            try: self.container.unbind('<Configure>', self._configure_binding)
            except tk.TclError: pass
            self._configure_binding = None

    def _forget_window_manager(self):
        self.widget.tk.call('wm', 'forget', self.widget)

    def detach(self):
        if not self._owns_window: return
        self._cancel_geometry()
        self._assert_leaf()
        if self._wrapper and self._native.exists(self._wrapper):
            self._native.show(self._wrapper, False)
            self._applied_visible = False
            # Remove the native child before any group shell can be destroyed.
            self._native.set_parent(self._wrapper, 0)
            self._attached = False
            if self._original_style is not None:
                self._native.set_style(self._wrapper, self._original_style)
                self._native.set_style(self._wrapper, self._original_exstyle, True)
        self._forget_window_manager()
        self._assert_leaf()
        self._attached = False
        self._owns_window = False
        self._wrapper = 0
        self._visible = False
        self._applied_size = None
        self._frame_changed = False
