"""Recover video presentation once on return from another application."""
from __future__ import annotations

import ctypes
from functools import lru_cache
import os
import time


@lru_cache(maxsize=1)
def _foreground_api():
    if os.name != 'nt': return None
    api = ctypes.WinDLL('user32', use_last_error=True)
    api.GetForegroundWindow.argtypes = []
    api.GetForegroundWindow.restype = ctypes.c_void_p
    api.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    api.GetWindowThreadProcessId.restype = ctypes.c_ulong
    return api


def application_is_foreground():
    api = _foreground_api()
    if api is None: return None
    window = api.GetForegroundWindow()
    if not window: return None
    process = ctypes.c_ulong()
    if not api.GetWindowThreadProcessId(window, ctypes.byref(process)): return None
    # Native MPV children and floating panels all belong to this application.
    return process.value == os.getpid()


class PreviewFocusMixin:
    def _init_preview_focus(self):
        self._preview_foreground = None
        self._preview_focus_checked = 0.
        self._preview_focus_bindings = []
        for event in ('<FocusIn>', '<FocusOut>', '<Map>'):
            token = self.app.bind(event, self._preview_focus_event, add='+')
            self._preview_focus_bindings.append((event, token))

    def _preview_focus_event(self, _event=None):
        self._check_preview_foreground(force=True)

    def _check_preview_foreground(self, *, force=False):
        if not self.alive: return
        now = time.monotonic()
        if not force and now-self._preview_focus_checked < .1: return
        self._preview_focus_checked = now
        active = application_is_foreground()
        if active is None: return
        previous = self._preview_foreground
        self._preview_foreground = active
        if active and previous is False and self.player is not None:
            # The worker owns the current audio clock. A UI time label may be
            # stale after occlusion, so never turn it into a navigation seek.
            self._stack_preview_payload = None
            self.player.submit('refresh_presentation')

    def _close_preview_focus(self):
        for event, token in self._preview_focus_bindings:
            if token: self.app.unbind(event, token)
        self._preview_focus_bindings.clear()
