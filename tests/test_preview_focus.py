"""Returning to the app refreshes video once without navigating from stale UI."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from preview_focus import PreviewFocusMixin


class FocusHarness(PreviewFocusMixin):
    def __init__(self):
        self.app = Mock()
        self.player = Mock()
        self.alive = True
        self.current = 1.  # Deliberately stale relative to a playing worker.
        self._stack_preview_payload = {'time': 1.}
        self._init_preview_focus()


class PreviewFocusTests(unittest.TestCase):
    def test_return_refreshes_once_without_using_ui_time_or_clearing_cache(self):
        w = FocusHarness()
        with patch('preview_focus.application_is_foreground', side_effect=[True, False, False, True, True, True]):
            for _ in range(6): w._preview_focus_event()
        w.player.submit.assert_called_once_with('refresh_presentation')
        self.assertEqual(w.current, 1.)
        self.assertIsNone(w._stack_preview_payload)

    def test_internal_focus_changes_and_initial_mapping_do_not_refresh(self):
        w = FocusHarness()
        with patch('preview_focus.application_is_foreground', return_value=True):
            for _ in range(30): w._preview_focus_event(SimpleNamespace())
        w.player.submit.assert_not_called()

    def test_missing_foreground_handle_retains_state_and_poll_is_throttled(self):
        w = FocusHarness()
        with patch('preview_focus.time.monotonic', return_value=10.), \
             patch('preview_focus.application_is_foreground', return_value=False) as read:
            for _ in range(50): w._check_preview_foreground()
            read.assert_called_once()
        with patch('preview_focus.application_is_foreground', return_value=None): w._preview_focus_event()
        self.assertFalse(w._preview_foreground)
        with patch('preview_focus.application_is_foreground', return_value=True): w._preview_focus_event()
        w.player.submit.assert_called_once_with('refresh_presentation')

    def test_close_removes_only_own_bindings(self):
        w = FocusHarness()
        w._close_preview_focus()
        self.assertEqual(w.app.unbind.call_count, 3)
        self.assertEqual(w._preview_focus_bindings, [])


if __name__ == '__main__': unittest.main()
