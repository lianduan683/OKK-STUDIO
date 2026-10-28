"""Tk-only presentation of a bounded, latest-frame alpha compositor mailbox."""
from __future__ import annotations

import tkinter as tk
import ctypes
import os
import time
import numpy as np
from editor_core import format_time


class _PhotoBlock(ctypes.Structure):
    _fields_ = [('pixels', ctypes.c_void_p), ('width', ctypes.c_int), ('height', ctypes.c_int),
                ('pitch', ctypes.c_int), ('pixel_size', ctypes.c_int), ('offset', ctypes.c_int*4)]


def _photo_api():
    """Use the Tk DLL already loaded by Python; never load an external DLL."""
    if os.name != 'nt': return None
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    kernel.GetModuleHandleW.restype = ctypes.c_void_p
    handle = kernel.GetModuleHandleW(f'tk{str(tk.TkVersion).replace(".", "")}t.dll')
    if not handle: return None
    api = ctypes.CDLL('tk', handle=handle)
    api.Tk_FindPhoto.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    api.Tk_FindPhoto.restype = ctypes.c_void_p
    api.Tk_PhotoPutBlock.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_PhotoBlock),
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    api.Tk_PhotoPutBlock.restype = ctypes.c_int
    return api


class StackPreviewUIMixin:
    def _init_stack_preview(self):
        self._stack_preview_active = False
        self._stack_preview_after = None
        self._stack_preview_payload = None
        self._stack_preview_image = None
        self._stack_preview_last_time = None
        self._stack_preview_scale = None
        self._stack_preview_last_payload = None
        self._stack_preview_draw_size = None
        self._stack_preview_resize_after = None
        self._stack_photo_api = _photo_api()

    def _build_stack_preview(self):
        self.stack_preview_label = tk.Label(self.program_surface, bg='#0e0f10', bd=0,
            text='正在准备图层画面…', fg='#a0a4aa')
        self.stack_preview_time_label = tk.Label(self.program_surface, bg='#17191c', fg='#c1c6cb',
            bd=0, padx=4, text='', font=('Cascadia Mono', 8))
        focus = getattr(self, '_focus_preview', None)
        if callable(focus):
            self.stack_preview_label.bind('<Button-1>', focus)
            self.stack_preview_time_label.bind('<Button-1>', focus)
        self.program_surface.bind('<Configure>', self._resize_stack_preview, add='+')

    def _resize_stack_preview(self, _event=None):
        if not self._stack_preview_active or self._stack_preview_last_payload is None: return
        size = (self.program_surface.winfo_width(), self.program_surface.winfo_height())
        if size == self._stack_preview_draw_size or self._stack_preview_resize_after is not None: return
        def redraw():
            self._stack_preview_resize_after = None
            if self._stack_preview_last_payload is not None:
                self._present_stack_frame(self._stack_preview_last_payload)
        self._stack_preview_resize_after = self.after(16, redraw)

    def _raise_stack_preview(self):
        if not self._stack_preview_active: return
        for view in (*self.program_views, *self.comparison_views): view.place_forget()
        self.comparison_original_label.place_forget()
        self.comparison_stabilized_label.place_forget()
        self.stack_preview_label.place(x=0, y=0, relwidth=1, relheight=1)
        self.stack_preview_label.lift()
        self.stack_preview_time_label.place(relx=1, rely=1, x=-4, y=-4, anchor='se')
        self.stack_preview_time_label.lift()

    def _set_stack_preview_mode(self, enabled):
        enabled = bool(enabled and self.player is not None and self.project.video_stack_enabled)
        self._stack_preview_active = enabled
        if self._stack_preview_after is not None:
            self.after_cancel(self._stack_preview_after)
            self._stack_preview_after = None
        if enabled:
            self._raise_stack_preview()
            self._stack_preview_after = self.after(16, self._drain_stack_preview)
        else:
            if self._stack_preview_resize_after is not None:
                self.after_cancel(self._stack_preview_resize_after)
                self._stack_preview_resize_after = None
            self._stack_preview_payload = None
            self._stack_preview_image = None
            self._stack_preview_last_time = None
            self._stack_preview_scale = None
            self._stack_preview_last_payload = None
            self._stack_preview_draw_size = None
            self.stack_preview_label.configure(image='', text='正在准备图层画面…')
            self.stack_preview_label.place_forget()
            self.stack_preview_time_label.place_forget()
            self._set_comparison_layout(self._comparison_layout_enabled)

    def _handle_stack_preview_job(self, kind, payload):
        if kind == 'stack_mode':
            self._set_stack_preview_mode(payload)
            return True
        if kind == 'stack_frame':
            # Compatibility with queue producers; the production decoder uses
            # take_stack_frame(), a single replaceable slot, without bytes here.
            if isinstance(payload, dict): self._stack_preview_payload = payload
            return True
        return False

    def _drain_stack_preview(self):
        began = time.perf_counter()
        self._stack_preview_after = None
        if not self.alive or not self._stack_preview_active or self.player is None: return
        take = getattr(self.player, 'take_stack_frame', None)
        payload = take() if callable(take) else None
        if payload is None: payload = self._stack_preview_payload
        self._stack_preview_payload = None
        if payload is not None: self._present_stack_frame(payload)
        if self.alive and self._stack_preview_active:
            delay = max(1, 16-round((time.perf_counter()-began)*1000))
            self._stack_preview_after = self.after(delay, self._drain_stack_preview)

    def _present_stack_frame(self, payload):
        if not self._stack_preview_active or not self.project.video_stack_enabled: return False
        accept = getattr(self, '_accept_stack_preview_frame', self._accept_transport_state)
        if not isinstance(payload, dict) or not accept(payload): return False
        width, height, rgba = payload.get('width'), payload.get('height'), payload.get('rgba')
        if (type(width) is not int or type(height) is not int or not 0 < width <= 7680
                or not 0 < height <= 4320 or not isinstance(rgba, bytes)
                or len(rgba) != width*height*4): return False
        # No conversion occurs until both the freshness fence and shape pass.
        available = (max(1, self.program_surface.winfo_width()), max(1, self.program_surface.winfo_height()))
        ratio = min(available[0]/width, available[1]/height)
        size = (max(1, round(width*ratio)), max(1, round(height*ratio)))
        # NumPy is already shipped by the editor. Write RGBA directly into the
        # existing Tk photo; use PPM if this Tk build exposes no C API. Keep
        # only the current image and reuse coordinates while dimensions match.
        pixels = np.frombuffer(rgba, dtype=np.uint8).reshape(height, width, 4)
        if size != (width, height):
            key = (width, height, *size)
            if self._stack_preview_scale is None or self._stack_preview_scale[0] != key:
                xs = np.minimum(width-1, np.arange(size[0])*width//size[0])
                ys = np.minimum(height-1, np.arange(size[1])*height//size[1])
                self._stack_preview_scale = (key, xs, ys)
            _, xs, ys = self._stack_preview_scale
            pixels = pixels[ys[:, None], xs, :]
        if self._stack_preview_image is None:
            self._stack_preview_image = tk.PhotoImage(width=size[0], height=size[1], master=self.stack_preview_label)
            self.stack_preview_label.configure(image=self._stack_preview_image, text='')
        image = self._stack_preview_image
        if (image.width(), image.height()) != size:
            image.configure(width=size[0], height=size[1])
        api = self._stack_photo_api
        if api is not None:
            interpreter = self.tk.interpaddr()
            handle = api.Tk_FindPhoto(interpreter, str(image).encode('ascii'))
            block = _PhotoBlock(pixels.ctypes.data, size[0], size[1], pixels.strides[0], 4,
                                (ctypes.c_int*4)(0, 1, 2, 3))
            if not handle or api.Tk_PhotoPutBlock(interpreter, handle, ctypes.byref(block), 0, 0, *size, 1):
                self._stack_photo_api = None
                api = None
        if api is None:
            ppm = f'P6\n{size[0]} {size[1]}\n255\n'.encode('ascii') + pixels[:, :, :3].tobytes()
            self._stack_preview_image.configure(data=ppm, format='PPM')
        self._stack_preview_last_time = payload.get('time')
        self.stack_preview_time_label.configure(text='画面 ' + format_time(float(payload.get('time', 0.))))
        self._stack_preview_draw_size = available
        self._stack_preview_last_payload = payload
        return True
