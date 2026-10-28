"""Persistent layer order, alpha metadata and backwards-compatible projects."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from exporter_core import _media_info_from_payload, _media_probe_command
from multicam_core import CameraClip, Cut, MulticamProject
from video_stack import pixel_format_has_alpha, prune_stack, stream_has_alpha


class StackModelTests(unittest.TestCase):
    def project(self):
        return MulticamProject(clips=[CameraClip(str(camera), camera, f'camera{camera}.mov', 0, 5,
            has_alpha=camera == 2) for camera in (1, 2, 36)], cuts=[Cut(0, 1)],
            video_stack_enabled=True, video_stack_order=[36, 2, 1], video_stack_hidden=[1])

    def test_order_uses_top_to_bottom_and_appends_new_tracks(self):
        project = self.project()
        self.assertEqual(project.stacked_cameras(), [36, 2])
        self.assertEqual(project.stacked_cameras(include_hidden=True), [36, 2, 1])
        project.clips.append(CameraClip('new', 7, 'new.mov', 0, 5))
        self.assertEqual(project.stacked_cameras(), [36, 2, 7])
        self.assertEqual(project.camera_at(2), 1)  # Switching edit retained when leaving stack mode.

    def test_roundtrip_order_hidden_alpha_and_no_payload_mutation(self):
        project = self.project()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'layers.multicam.json'
            project.save(path)
            restored = MulticamProject.load(path)
            self.assertEqual(restored.video_stack_order, [36, 2, 1])
            self.assertEqual(restored.video_stack_hidden, [1])
            self.assertTrue(restored.video_stack_enabled)
            self.assertTrue(restored.clips[1].has_alpha)
            payload = restored.to_payload(folder)
            before = deepcopy(payload)
            self.assertEqual(payload['version'], 8)
            MulticamProject.from_payload(payload, folder)
            self.assertEqual(payload, before)

    def test_legacy_v7_defaults_to_switching_without_losing_camera_numbers(self):
        payload = self.project().to_payload('.')
        payload['version'] = 7
        for key in ('video_stack_enabled', 'video_stack_order', 'video_stack_hidden'):
            payload.pop(key)
        for clip in payload['clips']: clip.pop('has_alpha')
        restored = MulticamProject.from_payload(payload, '.')
        self.assertFalse(restored.video_stack_enabled)
        self.assertEqual(restored.stacked_cameras(), [1, 2, 36])
        self.assertTrue(all(not clip.has_alpha for clip in restored.clips))

    def test_invalid_layer_references_and_boolean_numbers_are_rejected(self):
        project = self.project()
        for field, value in (('video_stack_enabled', 1), ('video_stack_order', [1, 1]),
                              ('video_stack_order', [True]), ('video_stack_order', [99]),
                              ('video_stack_hidden', [2, 2]), ('video_stack_hidden', [99])):
            with self.subTest(field=field, value=value):
                bad = deepcopy(project)
                setattr(bad, field, value)
                with self.assertRaises(ValueError): bad.validate()
        project.clips[0].has_alpha = 1
        with self.assertRaises(ValueError): project.validate()

    def test_explicit_delete_prunes_only_removed_camera_references(self):
        project = self.project()
        project.clips = [clip for clip in project.clips if clip.camera != 1]
        prune_stack(project)
        self.assertEqual(project.video_stack_order, [36, 2])
        self.assertEqual(project.video_stack_hidden, [])
        project.cuts.clear()
        project.validate()


class AlphaMetadataTests(unittest.TestCase):
    def test_alpha_formats_and_padding_only_rgb_are_distinguished(self):
        for value in ('rgba', 'argb', 'bgra64le', 'yuva444p12le', 'gbrap10le', 'ya16le', 'pal8'):
            self.assertTrue(pixel_format_has_alpha(value), value)
        for value in ('yuv420p', 'rgb24', 'rgb0', '0rgb', 'gbrp10le', 'unknown', None):
            self.assertFalse(pixel_format_has_alpha(value), value)

    def test_vp9_alpha_mode_tag_survives_probe_even_when_pixfmt_is_yuv420p(self):
        stream = {'codec_type': 'video', 'codec_name': 'vp9', 'pix_fmt': 'yuv420p',
                  'width': 1920, 'height': 1080, 'duration': '4', 'tags': {'ALPHA_MODE': '1'}}
        self.assertTrue(stream_has_alpha(stream))
        info = _media_info_from_payload('overlay.webm', {'streams': [stream]})
        self.assertTrue(info.has_alpha)
        self.assertIn('alpha_mode', ' '.join(_media_probe_command('ffprobe', 'overlay.webm')))

    def test_no_video_is_never_marked_as_alpha(self):
        info = _media_info_from_payload('audio.wav', {'streams': [{'codec_type': 'audio',
            'codec_name': 'pcm_s16le', 'duration': '2'}]}, allow_audio_only=True)
        self.assertFalse(info.has_alpha)


if __name__ == '__main__': unittest.main()
