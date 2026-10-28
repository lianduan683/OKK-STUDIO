"""Proxy validation, bounded filesystem work and damaged-proxy recovery."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from exporter_core import CREATE_NO_WINDOW, MediaInfo
from creative_core import ColorGrade
from gyroflow_core import StabilizedRange, StabilizationSettings
from multicam_core import CameraClip, Cut, MulticamProject
from program_edits import set_grade_on_range
from proxy_media import PreviewProxyResolver, validate_proxy_media
import proxy_media
try:
    from . import test_multicam_player as playback
    from . import test_creative_project as creative
    from . import test_program_double_buffer as buffered
except ImportError:
    import test_multicam_player as playback
    import test_creative_project as creative
    import test_program_double_buffer as buffered


class ProxyMediaTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='okk_proxy_validation_')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root/'原片.mov'; self.source.write_bytes(b'source')
        self.proxy = self.root/'代理.mp4'; self.proxy.write_bytes(b'proxy')

    def test_lookup_checks_disk_once_per_window_and_detects_missing_proxy_later(self):
        resolver = PreviewProxyResolver(.75)
        with patch.object(proxy_media, 'find_proxy', return_value=str(self.proxy)) as find:
            with patch.object(proxy_media.time, 'monotonic', return_value=1.):
                for _ in range(100):
                    self.assertEqual(resolver(str(self.source)), str(self.proxy))
                self.assertEqual(find.call_count, 1)
            self.proxy.unlink()
            with patch.object(proxy_media.time, 'monotonic', return_value=2.):
                self.assertEqual(resolver(str(self.source)), str(self.source))
                self.assertEqual(find.call_count, 2)

    def test_explicit_missing_binding_does_not_substitute_another_proxy(self):
        with patch.object(proxy_media, 'find_proxy') as find:
            self.assertEqual(PreviewProxyResolver()(str(self.source), str(self.root/'missing.mp4')),
                             str(self.source))
            find.assert_not_called()

    def test_rejected_proxy_stays_disabled_across_edits_but_repaired_file_is_usable(self):
        resolver = PreviewProxyResolver(0)
        resolver.reject(str(self.proxy))
        resolver.reset()
        self.assertEqual(resolver(str(self.source), str(self.proxy)), str(self.source))
        self.proxy.write_bytes(b'repaired proxy with valid video')
        self.assertEqual(resolver(str(self.source), str(self.proxy)), str(self.proxy))

    def test_proxy_validation_allows_frame_rate_change_but_rejects_trim_and_bad_metadata(self):
        good = MediaInfo(str(self.proxy), duration=10., width=640, height=360, fps=25.)
        with patch.object(proxy_media, 'probe_media', return_value=good):
            self.assertIs(validate_proxy_media('ffprobe', str(self.source), str(self.proxy), 10., 60.), good)
        for changed in (replace(good, duration=8.), replace(good, duration=float('nan')),
                        replace(good, width=0)):
            with self.subTest(media=changed), patch.object(proxy_media, 'probe_media', return_value=changed):
                with self.assertRaises(ValueError):
                    validate_proxy_media('ffprobe', str(self.source), str(self.proxy), 10., 60.)
        with self.assertRaises(ValueError):
            validate_proxy_media('ffprobe', str(self.source), str(self.source), 10.)

    def test_real_ffprobe_accepts_full_proxy_and_rejects_truncated_video_and_audio_only(self):
        binary = Path(__file__).resolve().parents[1]/'bin'
        ffmpeg, ffprobe = binary/'ffmpeg.exe', binary/'ffprobe.exe'
        if not ffmpeg.is_file() or not ffprobe.is_file():
            self.skipTest('bundled FFmpeg unavailable')
        for name, seconds in (('full.mp4', 2), ('trimmed.mp4', 1)):
            result = subprocess.run([str(ffmpeg), '-v', 'error', '-y', '-f', 'lavfi', '-i',
                f'testsrc2=size=96x54:rate=25:duration={seconds}', '-c:v', 'libx264',
                '-preset', 'ultrafast', str(self.root/name)], capture_output=True,
                timeout=15, creationflags=CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
        full = validate_proxy_media(str(ffprobe), str(self.source), str(self.root/'full.mp4'), 2., 30.)
        self.assertEqual(full.fps, 25.)
        with self.assertRaisesRegex(ValueError, '不一致'):
            validate_proxy_media(str(ffprobe), str(self.source), str(self.root/'trimmed.mp4'), 2.)
        audio = self.root/'audio.wav'
        result = subprocess.run([str(ffmpeg), '-v', 'error', '-y', '-f', 'lavfi', '-i',
            'sine=frequency=440:duration=2', str(audio)], capture_output=True,
            timeout=15, creationflags=CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0)
        with self.assertRaises(ValueError):
            validate_proxy_media(str(ffprobe), str(self.source), str(audio), 2.)


class DamagedProxyPlaybackTests(playback.MulticamPlayerTests):
    def test_corrupt_proxy_falls_back_once_preserving_original_time_and_export(self):
        with tempfile.TemporaryDirectory(prefix='okk_bad_proxy_') as directory:
            root = Path(directory)
            source = root/'original.mov'; source.write_bytes(b'original')
            proxy = root/'bad-proxy.mp4'; proxy.write_bytes(b'corrupt proxy')
            project = MulticamProject([CameraClip('a', 1, str(source), -1.5, 10., str(proxy))],
                                      [Cut(0., 1)])
            before = deepcopy(project)
            self._configure(project)
            self._wait(lambda: self._loads(200)[-1][2] == str(source))
            self.player.submit('seek', time=.4)
            self._state(lambda state: state['time'] == .4 and state['ready'])
            self.assertIn(('seek', 1.9, 'absolute+exact'), [value[2] for value in self._commands(200)])
            bad_loads = [value for value in self._loads() if value[2] == str(proxy)]
            self.player.submit('configure', project=project)
            self.player.submit('seek', time=.6)
            self._state(lambda state: state['time'] == .6)
            self.assertEqual([value for value in self._loads() if value[2] == str(proxy)], bad_loads)
            self.assertEqual(project, before)
            self.assertEqual(project.segments()[0].path, str(source))
            self.assertTrue(any(kind == 'status' and '切回原片' in value for kind, value in self.received))


class EmbeddedProjectTests(unittest.TestCase):
    def test_embedded_payload_is_lossless_and_neither_mutates_nor_writes_files(self):
        fixture = creative.CreativeProjectTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        project = fixture.project()
        project.alignment_reference_camera = 1
        project.color_reference_camera = 2
        set_grade_on_range(project, 1., 2., ColorGrade(exposure=.25,
            lut_path=str(fixture.root/'LUT'/'片段.cube')))
        project.stabilizations = [StabilizedRange(1., 2., 1, 'a',
            StabilizationSettings(engine='image', gyro_path=str(fixture.root/'镜头'/'motion.gcsv'),
                                  lens_path=str(fixture.root/'镜头'/'lens.json')),
            str(fixture.root/'缓存'/'stable.mp4'), 'a'*64, 2.25, 3.25)]
        before = deepcopy(project)
        base = fixture.root/'not_created'
        payload = project.to_payload(base)
        serialized = deepcopy(payload)
        restored = MulticamProject.from_payload(payload, base)
        self.assertEqual(asdict(restored), asdict(before))
        self.assertEqual(payload, serialized)
        self.assertEqual(project, before)
        self.assertFalse(base.exists())
        invalid = deepcopy(payload); invalid['version'] = 999
        with self.assertRaises(ValueError):
            MulticamProject.from_payload(invalid, base)
        invalid = deepcopy(payload); invalid['unknown'] = 1
        with self.assertRaises(ValueError):
            MulticamProject.from_payload(invalid, base)


class DamagedProxyDoubleBufferTests(unittest.TestCase):
    setUp = buffered.DoubleBufferTests.setUp
    tearDown = buffered.DoubleBufferTests.tearDown
    wait = buffered.DoubleBufferTests.wait
    drain = buffered.DoubleBufferTests.drain

    def test_bad_proxy_keeps_old_frame_until_original_is_ready(self):
        with tempfile.TemporaryDirectory(prefix='okk_proxy_double_') as directory:
            proxy = Path(directory)/'bad-proxy.mp4'; proxy.write_bytes(b'broken')
            self.project.clips[1].proxy_path = str(proxy)
            self.player.submit('configure', project=self.project)
            self.player.submit('audition', camera=2)
            self.wait(lambda: self.player._program.active and self.player._program.controller.path == 'blue.mp4')
            self.assertTrue(self.player._program.controller.paused)
            self.assertEqual(self.player._project.clips[1].proxy_path, str(proxy))
            loads = [value for value in self.trace.snapshot() if value[0] == 'load' and value[2] == str(proxy)]
            self.assertGreaterEqual(len(loads), 1)
            self.assertLessEqual(len(loads), 2)
if __name__ == '__main__': unittest.main(verbosity=2)
