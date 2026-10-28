"""Alpha gesture sampling stays bounded without weakening final seek accuracy."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from test_transport_controls import TransportHarness, RecordingPlayer
import test_timeline_drag_merge as fixture


class StackTransportTimingTests(unittest.TestCase):
    def setUp(self):
        self.ws = TransportHarness()
        self.ws.project.video_stack_enabled = True
        self.clock = patch('transport_controls.time.monotonic', side_effect=lambda: self.ws.now)
        self.clock.start()

    def tearDown(self): self.clock.stop()

    def test_both_two_second_holds_share_epoch_and_release_is_new_exact_request(self):
        for direction, key in ((1, 'Right'), (-1, 'Left')):
            with self.subTest(direction=direction):
                ws = self.ws
                ws._reset_transport_controls(); ws.current = 5.; ws.player.commands.clear()
                ws._transport_key_press(direction)
                self.assertNotIn('preview_epoch', ws.player.scrubs[0])
                ws.advance(.24)
                self.assertIsNone(ws._preview_epoch)
                ws.advance(2.01)
                samples = ws.player.scrubs[1:]
                self.assertGreater(len(samples), 40)
                epoch = ws._preview_epoch
                self.assertTrue(all(item['preview_epoch'] == epoch for item in samples))
                self.assertAlmostEqual(ws.current, 5.+direction*(2.+1/30), delta=2/30)
                current = ws.current
                sample = dict(samples[0], rgba=b'real', time=samples[0]['time'])
                self.assertFalse(ws._accept_transport_state(sample))
                self.assertTrue(ws._accept_stack_preview_frame(sample))
                self.assertEqual(ws.current, current)
                count, request = len(ws.player.scrubs), ws._transport_request_id
                ws._transport_key_release(SimpleNamespace(keysym=key))
                self.assertIsNone(ws._preview_epoch)
                self.assertFalse(ws._accept_stack_preview_frame(sample))
                final = ws.player.scrubs[-1]
                self.assertEqual(final['request_id'], request+1)
                self.assertNotIn('preview_epoch', final)
                self.assertEqual(final['time'], current)
                self.assertTrue(final['pause'])
                self.assertEqual(len(ws.player.scrubs), count+1)
                ws.advance(2.)
                self.assertEqual(len(ws.player.scrubs), count+1)

    def test_reversal_focus_loss_reset_and_close_invalidate_old_epoch(self):
        ws = self.ws
        ws._transport_key_press(1); ws.advance(.5)
        first = dict(ws.player.scrubs[-1])
        ws._transport_key_press(-1)
        self.assertFalse(ws._accept_stack_preview_frame(first))
        self.assertIsNone(ws._preview_epoch)
        ws.advance(.4)
        self.assertNotEqual(ws._preview_epoch, first['preview_epoch'])
        second = dict(ws.player.scrubs[-1])
        ws._transport_focus_out()
        self.assertIsNone(ws._preview_epoch)
        self.assertNotIn('preview_epoch', ws.player.scrubs[-1])
        self.assertFalse(ws._accept_stack_preview_frame(second))
        ws._transport_key_press(1); ws.advance(.4)
        count = len(ws.player.scrubs)
        ws._reset_transport_controls()
        self.assertEqual(len(ws.player.scrubs), count)
        self.assertIsNone(ws._preview_epoch)
        ws._transport_key_press(1); ws.advance(.4)
        count = len(ws.player.scrubs)
        ws._close_transport_controls()
        self.assertEqual(len(ws.player.scrubs), count)
        self.assertIsNone(ws._preview_epoch)

    def test_short_tap_and_ordinary_multicam_keep_original_protocol(self):
        ws = self.ws
        ws._transport_key_press(1); ws.advance(.2)
        ws._transport_key_release(SimpleNamespace(keysym='Right'))
        self.assertEqual(len(ws.player.scrubs), 1)
        self.assertNotIn('preview_epoch', ws.player.scrubs[0])
        ws.project.video_stack_enabled = False
        ws._transport_key_press(-1); ws.advance(2.)
        count = len(ws.player.scrubs)
        ws._transport_key_release(SimpleNamespace(keysym='Left'))
        self.assertEqual(len(ws.player.scrubs), count)
        self.assertTrue(all('preview_epoch' not in item for item in ws.player.scrubs))


class StackTransportTkTests(unittest.TestCase):
    setUp = fixture.TimelineDropTests.setUp
    tearDown = fixture.TimelineDropTests.tearDown

    def prepare(self):
        ws = self.workspace
        ws.project.video_stack_enabled = True
        ws.player = RecordingPlayer()
        ws._set_stack_preview_mode(True)
        return ws

    def point(self, position):
        ws = self.workspace
        return dict(x=round(ws._timeline_x(position)-ws.timeline.canvasx(0)), y=8)

    def test_ruler_motion_keeps_one_epoch_and_release_requests_accurate_last_frame(self):
        ws = self.prepare(); canvas = ws.timeline
        canvas.event_generate('<ButtonPress-1>', **self.point(1.))
        epoch = ws._preview_epoch
        for position in (2., 3., 4., 5.):
            canvas.event_generate('<B1-Motion>', **self.point(position))
        self.assertIsNotNone(epoch)
        self.assertTrue(all(item['preview_epoch'] == epoch for item in ws.player.scrubs))
        old = dict(ws.player.scrubs[0], width=2, height=2, rgba=bytes([20, 80, 140, 255])*4)
        current = ws.current
        self.assertTrue(ws._present_stack_frame(old))
        self.assertEqual(ws.current, current)
        self.assertEqual(ws._stack_preview_last_time, old['time'])
        self.assertIn('00:00:01', ws.stack_preview_time_label.cget('text'))
        canvas.event_generate('<ButtonRelease-1>', **self.point(5.))
        self.assertIsNone(ws._preview_epoch)
        self.assertNotIn('preview_epoch', ws.player.scrubs[-1])
        self.assertFalse(ws._present_stack_frame(old))
        final = dict(ws.player.scrubs[-1], width=2, height=2, rgba=bytes([140, 80, 20, 255])*4)
        self.assertTrue(ws._present_stack_frame(final))
        self.assertAlmostEqual(ws._stack_preview_last_time, 5., delta=1/30)
        self.assertEqual(self.errors, [])

    def test_escape_stops_ruler_epoch_and_future_motion_cannot_restart_it(self):
        ws = self.prepare(); canvas = ws.timeline
        canvas.event_generate('<ButtonPress-1>', **self.point(1.))
        canvas.event_generate('<B1-Motion>', **self.point(4.))
        ws._key(SimpleNamespace(widget=canvas, state=0, keysym='Escape', char=''))
        self.assertIsNone(ws._preview_epoch)
        self.assertFalse(ws._timeline_scrubbing)
        self.assertNotIn('preview_epoch', ws.player.scrubs[-1])
        count = len(ws.player.scrubs)
        canvas.event_generate('<B1-Motion>', **self.point(5.))
        self.assertEqual(len(ws.player.scrubs), count)
        self.assertEqual(self.errors, [])

    def test_real_focus_loss_finishes_ruler_sampling_and_releases_geometry(self):
        ws = self.prepare(); canvas = ws.timeline
        canvas.event_generate('<ButtonPress-1>', **self.point(1.))
        canvas.event_generate('<B1-Motion>', **self.point(3.))
        target = ws.current
        self.assertIsNotNone(ws._preview_epoch)
        self.root.event_generate('<FocusOut>')
        self.assertIsNone(ws._preview_epoch)
        self.assertIsNone(ws._timeline_drag_geometry)
        self.assertFalse(ws._timeline_scrubbing)
        self.assertNotIn('preview_epoch', ws.player.scrubs[-1])
        self.assertEqual(ws.player.scrubs[-1]['time'], target)
        self.assertEqual(self.errors, [])


if __name__ == '__main__': unittest.main()
