"""Both video backends retain bounded packets without autonomous cache pauses."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from gpu_mpv_api import Core
from mpv_controller import MpvController
from test_gpu_mpv_api import FakeLibrary


class PlaybackCachePolicyTests(unittest.TestCase):
    def assert_video_cache_policy(self, options):
        self.assertEqual(options['cache-pause'], 'no')
        self.assertEqual(options['cache-on-disk'], 'no')
        self.assertEqual(options['demuxer-max-bytes'], '32MiB')
        self.assertEqual(options['demuxer-max-back-bytes'], '8MiB')
        self.assertEqual(options['demuxer-readahead-secs'], '2')

    def test_native_initialization_disables_only_independent_buffering_pause(self):
        library = FakeLibrary()
        core = Core(library)
        try:
            options = {key: value for _, key, value in library.options}
            self.assert_video_cache_policy(options)
            self.assertEqual(options['pause'], 'yes', 'user transport pause must remain available')
            self.assertNotIn('demuxer-seekable-cache', options, 'keep MPV cache reuse policy enabled by default')
        finally:
            core.close()

    def test_external_launch_passes_same_bounded_policy_to_hardware_and_fallback_viewers(self):
        for fallback in (False, True):
            with self.subTest(fallback=fallback), patch('gpu_d3d11.preferred_adapter_name', return_value=''), \
                    patch('mpv_controller.subprocess.Popen', return_value=SimpleNamespace(poll=lambda: None)) as launch:
                controller = MpvController('mpv.exe', 123, allow_software_decode=fallback)
                with patch.object(controller, 'command', return_value='fixture-version'):
                    controller.start()
                args = launch.call_args.args[0]
                options = dict(arg[2:].split('=', 1) for arg in args if arg.startswith('--') and '=' in arg)
                self.assert_video_cache_policy(options)
                self.assertEqual(options['hwdec-software-fallback'], 'yes' if fallback else 'no')


if __name__ == '__main__':
    unittest.main()
