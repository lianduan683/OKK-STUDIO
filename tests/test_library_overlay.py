"""Real alpha/screen rendering, full-resolution delivery and cache identity."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from effects_library import OverlayAsset
from exporter_core import Capabilities, CREATE_NO_WINDOW, ExportCancelled, ExportSettings, probe_media
from library_overlay import prepare_library_overlay, overlay_cache_matches
import library_overlay
from multicam_core import CameraClip, Cut, MulticamProject, render_multicam
from multicam_player import MulticamPlayer
from proxy_media import PreviewProxyResolver


class LibraryOverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Path(__file__).resolve().parents[1]
        cls.ffmpeg, cls.ffprobe = str(cls.app/'bin/ffmpeg.exe'), str(cls.app/'bin/ffprobe.exe')
        if not Path(cls.ffmpeg).is_file(): raise unittest.SkipTest('FFmpeg unavailable')
        cls.temporary = tempfile.TemporaryDirectory(prefix='overlay_qa_', dir=cls.app/'work')
        cls.root = Path(cls.temporary.name)
        cls.source = cls.root/'source.mp4'
        cls.alpha = cls.root/'alpha.mov'
        cls.screen = cls.root/'screen.mp4'
        cls.run_command(['-f','lavfi','-i','color=c=0x204060:s=96x54:r=30:d=3',
                         '-c:v','libx264','-crf','0',str(cls.source)])
        cls.run_command(['-f','lavfi','-i','color=c=red@0.5:s=96x54:r=30:d=1,format=argb',
                         '-c:v','qtrle',str(cls.alpha)])
        cls.run_command(['-f','lavfi','-i','color=c=0x303030:s=96x54:r=30:d=1',
                         '-c:v','libx264','-crf','0',str(cls.screen)])

    @classmethod
    def tearDownClass(cls): cls.temporary.cleanup()

    @classmethod
    def run_command(cls, args):
        result = subprocess.run([cls.ffmpeg, '-v','error','-y', *args], capture_output=True,
                                timeout=35, creationflags=CREATE_NO_WINDOW)
        if result.returncode: raise AssertionError(result.stderr.decode('utf-8','replace'))
        return result.stdout

    def setUp(self):
        self.folder = self.root/self._testMethodName; self.folder.mkdir()
        self.project = MulticamProject([CameraClip('a',1,str(self.source),0.,3.,str(self.screen))],
                                      [Cut(0.,1)], fps=30.)
        self.cache_patch = patch.object(library_overlay, 'cache_dir', return_value=self.folder)
        self.cache_patch.start(); self.addCleanup(self.cache_patch.stop)

    def prepare(self, kind='alpha', project=None):
        project = project or self.project
        source = self.alpha if kind == 'alpha' else self.screen
        asset = OverlayAsset(kind,kind,kind,'测试',source.name,96,54,30.,30,1.,'fixture')
        with patch.object(library_overlay, 'library_asset_path', return_value=str(source)):
            return prepare_library_overlay(project,.5,1.5,asset,1.,self.ffmpeg,self.ffprobe)

    def pixel(self, path, at):
        return tuple(self.run_command(['-ss',str(at),'-i',str(path),'-frames:v','1',
            '-vf','scale=1:1','-pix_fmt','rgb24','-f','rawvideo','pipe:1']))

    def test_alpha_preview_export_and_undo_keep_original_and_proxy_separate(self):
        original = deepcopy(self.project)
        record = self.prepare()
        self.assertEqual(self.project, original)
        self.project.library_overlays.append(record)
        inside = self.project.media_at(1,1.,use_proxies=True)
        self.assertEqual(inside[0],record.cache_path)
        self.assertEqual(inside[2][0],'library-overlay')
        player = object.__new__(MulticamPlayer)
        player._project=self.project; player._use_proxies=True; player._proxy_resolver=PreviewProxyResolver()
        self.assertEqual(player._clip(1,1.)[1],record.cache_path)
        self.assertEqual(self.project.media_at(1,.1,use_proxies=True)[0],str(self.screen))
        output = self.folder/'delivery.mp4'
        def run(command, progress=None):
            result=subprocess.run(command,capture_output=True,timeout=35,creationflags=CREATE_NO_WINDOW)
            return result.returncode,result.stderr.decode('utf-8','replace')
        render_multicam(self.project,str(output),self.ffmpeg,self.ffprobe,
                        ExportSettings(codec='libx264',preset='veryfast',decode_mode='cpu'),Capabilities(),run)
        before, blended, after = self.pixel(output,.2), self.pixel(output,1.), self.pixel(output,2.)
        self.assertTrue(all(abs(a-b)<5 for a,b in zip(before,after)),(before,after))
        self.assertGreater(blended[0],before[0]+70)
        self.assertLess(blended[2],before[2]-20)
        info=probe_media(self.ffprobe,str(output));self.assertEqual((info.width,info.height),(96,54))
        self.assertAlmostEqual(info.duration,3.,places=2)
        self.project.library_overlays.clear()
        self.assertEqual(self.project.segments()[0].path,str(self.source))
        self.project.library_overlays.append(record)
        self.assertTrue(any(segment.path==record.cache_path for segment in self.project.segments()))

    def test_screen_is_rgb_screen_and_full_quality_cache_roundtrips(self):
        record=self.prepare('screen');self.project.library_overlays.append(record)
        source=self.pixel(self.source,1.);effect=self.pixel(record.cache_path,1.-record.source_start)
        expected=tuple(round(255-(255-value)*(255-48)/255) for value in source)
        self.assertTrue(all(abs(a-b)<9 for a,b in zip(effect,expected)),(effect,expected))
        path=self.folder/'edit.multicam.json';self.project.save(path)
        restored=MulticamProject.load(path)
        self.assertEqual(asdict(restored),asdict(self.project))
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['version'],8)
        self.assertTrue(overlay_cache_matches(restored.library_overlays[0],restored,strict=True))

    def test_original_replacement_or_missing_cache_blocks_export_instead_of_dropping_effect(self):
        local=self.folder/'source.mp4';local.write_bytes(self.source.read_bytes())
        self.project.clips[0].path=str(local)
        record=self.prepare();self.project.library_overlays.append(record)
        self.assertTrue(overlay_cache_matches(record,self.project,strict=True))
        local.write_bytes(local.read_bytes()+b'changed')
        with self.assertRaisesRegex(ValueError,'叠加缓存'):
            self.project.segments()
        self.assertEqual(self.project.library_overlays[0],record)

    def test_cancel_and_multi_camera_selection_do_not_modify_project(self):
        asset=OverlayAsset('a','a','alpha','test','alpha.mov',96,54,30.,30,1.,'fixture')
        before=deepcopy(self.project)
        cancelled=threading.Event();cancelled.set()
        with self.assertRaises(ExportCancelled):
            prepare_library_overlay(self.project,.5,1.5,asset,1.,self.ffmpeg,self.ffprobe,cancelled)
        self.assertEqual(self.project,before)
        self.project.clips.append(CameraClip('b',2,str(self.source),0.,3.))
        self.project.cuts.append(Cut(1.,2))
        with self.assertRaisesRegex(ValueError,'同一机位'):
            prepare_library_overlay(self.project,.5,1.5,asset,1.,self.ffmpeg,self.ffprobe)

    def test_ten_bit_cache_and_cpu_lossless_fallback_keep_depth_and_size(self):
        source=self.folder/'tenbit.mp4'
        self.run_command(['-f','lavfi','-i','color=c=0x204060:s=96x54:r=30:d=3',
                          '-vf','format=yuv420p10le','-c:v','libx264','-crf','0',str(source)])
        self.project.clips[0].path=str(source)
        from image_stabilization import _run
        calls=[]
        def run(command,*args,**kwargs):
            calls.append(command)
            if 'hevc_nvenc' in command:
                raise RuntimeError('fixture unavailable NVENC')
            return _run(command,*args,**kwargs)
        with patch('image_stabilization._run',side_effect=run):
            record=self.prepare('screen')
        self.assertEqual(record.encoding,'libx265 lossless')
        info=probe_media(self.ffprobe,record.cache_path)
        self.assertEqual((info.width,info.height,info.bit_depth),(96,54,10))
        self.assertEqual(len(calls),2)
        self.project.library_overlays.append(record)
        self.project.fps=60.
        with self.assertRaisesRegex(ValueError,'叠加缓存'):
            self.project.segments()

    def test_animation_begins_at_selection_even_with_transition_handles(self):
        animated = self.folder/'animated.mov'
        self.run_command(['-f','lavfi','-i','color=c=red:s=96x54:r=30:d=0.3',
                          '-f','lavfi','-i','color=c=blue:s=96x54:r=30:d=0.7',
                          '-filter_complex','[0:v][1:v]concat=n=2:v=1:a=0,format=argb[v]',
                          '-map','[v]','-c:v','qtrle',str(animated)])
        asset = OverlayAsset('animated','animated','alpha','test',animated.name,96,54,30.,30,1.,'fixture')
        # A source positioned after timeline zero also exercises the coordinate conversion.
        self.project.clips[0].start = .2
        with patch.object(library_overlay, 'library_asset_path', return_value=str(animated)):
            record = prepare_library_overlay(self.project,.7,1.7,asset,1.,self.ffmpeg,self.ffprobe)
        local_start = record.start - self.project.clips[0].start - record.source_start
        first = self.pixel(record.cache_path, local_start)
        later = self.pixel(record.cache_path, local_start + .4)
        self.assertGreater(first[0],220,first)
        self.assertLess(first[2],30,first)
        self.assertGreater(later[2],220,later)
        self.assertLess(later[0],30,later)


if __name__=='__main__':unittest.main(verbosity=2)
