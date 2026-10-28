"""Existing proxies must link without re-encoding, guessing, or pausing playback."""
from copy import deepcopy
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import external_proxy_link as matching
from exporter_core import CREATE_NO_WINDOW, ExportCancelled, MediaInfo, find_binary
from multicam_core import CameraClip, Cut, MulticamProject
from multicam_proxy import MulticamProxyMixin


class Variable:
    def __init__(self, value=None): self.value = value
    def get(self): return self.value
    def set(self, value): self.value = value


class ExistingProxyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sources = self.root/'originals'; self.sources.mkdir()
        self.proxies = self.root/'proxies'; self.proxies.mkdir()
        self.source = self.file(self.sources/'A.mov')
        self.proxy = self.file(self.proxies/'A_proxy_1080p.mov')

    def file(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'media fixture')
        return str(path)

    def workspace(self):
        ws = MulticamProxyMixin()
        ws.project = MulticamProject([CameraClip('a',1,self.source,0.,2.),
            CameraClip('b',2,self.source,4.,2.)], [Cut(0.,1)])
        ws.app = SimpleNamespace(caps=SimpleNamespace(ffprobe_path='ffprobe'),_log=Mock())
        ws.player = Mock(); ws.jobs = queue.Queue(); ws.status = Variable(); ws.proxy_var = Variable(False)
        ws._mark_dirty = Mock(); ws._configure_player = Mock(); ws._remember = Mock()
        ws.busy = False; ws.current = 1.5
        self.addCleanup(lambda: ws._proxy_link_thread.join(3) if vars(ws).get('_proxy_link_thread') else None)
        self.addCleanup(ws._reset_proxy_link)
        return ws

    def finish(self, ws):
        ws._proxy_link_thread.join(3)
        self.assertFalse(ws._proxy_link_thread.is_alive())
        while not ws.jobs.empty():
            kind, payload = ws.jobs.get_nowait()
            self.assertTrue(ws._handle_quick_proxy_job(kind,payload))

    def test_suffix_and_nested_folder_match_without_renaming(self):
        other = self.file(self.sources/'B.MP4')
        second = self.file(self.proxies/'camera2'/'B.mov')
        matched, issues = matching.match_proxy_folder([self.source,self.source,other],self.proxies)
        self.assertEqual(set(matched),{(self.source,self.proxy),(other,second)})
        self.assertEqual(issues,[])
        self.assertTrue(Path(self.proxy).exists())

    def test_duplicate_source_names_and_multiple_candidates_are_not_guessed(self):
        duplicate = self.file(self.sources/'other'/'A.mp4')
        self.assertEqual(matching.match_proxy_folder([self.source,duplicate],self.proxies)[0],[])
        self.file(self.proxies/'A_720p.mp4')
        matches, issues = matching.match_proxy_folder([self.source],self.proxies)
        self.assertEqual(matches,[]); self.assertIn('多个候选',issues[0][1])

    def test_one_proxy_cannot_bind_two_different_sources(self):
        second = self.file(self.sources/'A_proxy.mov')
        matches, issues = matching.match_proxy_folder([self.source,second],self.proxies)
        self.assertEqual(matches,[]); self.assertEqual(len(issues),2)

    def test_other_ambiguous_source_does_not_hide_a_shared_candidate(self):
        second = self.file(self.sources/'A_proxy.mov')
        self.file(self.proxies/'A_720p.mov')
        matches, issues = matching.match_proxy_folder([self.source,second],self.proxies)
        self.assertEqual(matches,[]); self.assertEqual(len(issues),2)

    def test_originals_are_never_candidates_and_cancel_is_obeyed(self):
        self.assertEqual(matching.match_proxy_folder([self.source],self.sources)[0],[])
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(ExportCancelled):
            matching.match_proxy_folder([self.source],self.proxies,cancel)

    def test_validator_reads_full_original_duration_and_allows_different_fps(self):
        original = MediaInfo(self.source,duration=10,width=3840,height=2160,fps=60)
        proxy = MediaInfo(self.proxy,duration=10,width=1920,height=1080,fps=30,video_codec='prores')
        with patch.object(matching,'_probe_media_cancel',side_effect=[original,proxy]) as probe:
            self.assertEqual(matching.validate_existing_proxy('ffprobe',self.source,self.proxy),proxy)
        self.assertEqual([call.args[1] for call in probe.call_args_list],[self.source,self.proxy])
        proxy.duration=2
        with patch.object(matching,'_probe_media_cancel',side_effect=[original,proxy]):
            with self.assertRaisesRegex(ValueError,'时长'):
                matching.validate_existing_proxy('ffprobe',self.source,self.proxy)

    def test_success_hot_swaps_all_same_source_clips_with_one_undo(self):
        ws = self.workspace()
        with patch.object(matching,'validate_existing_proxy') as validate, patch('proxy_cache.register_proxy') as register:
            ws._start_proxy_link([(self.source,self.proxy)])
            self.assertFalse(ws.busy)
            self.finish(ws)
        validate.assert_called_once()
        ws._remember.assert_called_once(); register.assert_called_once_with(self.source,self.proxy)
        self.assertTrue(all(clip.proxy_path==self.proxy for clip in ws.project.clips))
        ws._configure_player.assert_not_called()
        self.assertEqual(ws.current,1.5)
        action=ws.player.submit.call_args
        self.assertEqual(action.args,('proxy_update',));self.assertEqual(len(action.kwargs['bindings']),2)

    def test_failed_validation_preserves_previous_binding(self):
        ws=self.workspace();ws.project.clips[0].proxy_path='previous.mov'
        with patch.object(matching,'validate_existing_proxy',side_effect=ValueError('时长不一致')):
            ws._start_proxy_link([(self.source,self.proxy)]);self.finish(ws)
        self.assertEqual(ws.project.clips[0].proxy_path,'previous.mov')
        ws._remember.assert_not_called();self.assertIn('1 份未匹配',ws.status.get())

    def test_project_switch_rejects_ready_results(self):
        ws=self.workspace()
        with patch.object(matching,'validate_existing_proxy'):
            ws._start_proxy_link([(self.source,self.proxy)]);ws._proxy_link_thread.join(3)
            ws._reset_proxy_link();ws.project=deepcopy(ws.project);self.finish(ws)
        self.assertTrue(all(not c.proxy_path for c in ws.project.clips));ws._remember.assert_not_called()

    def test_edit_copy_and_manual_rebinding_while_validation_finishes(self):
        ws=self.workspace()
        with patch.object(matching,'validate_existing_proxy'),patch('proxy_cache.register_proxy'):
            ws._start_proxy_link([(self.source,self.proxy)]);ws._proxy_link_thread.join(3)
            ws.project=deepcopy(ws.project);ws.project.cuts.append(Cut(1.,2))
            ws.project.clips[0].proxy_path='new-manual.mov';ws.proxy_var.set(False)
            self.finish(ws)
        self.assertEqual(ws.project.clips[0].proxy_path,'new-manual.mov')
        self.assertEqual(ws.project.clips[1].proxy_path,self.proxy)
        self.assertEqual(ws.project.cuts[-1],Cut(1.,2))
        self.assertFalse(ws.player.submit.call_args.kwargs['use_proxies'])

    def test_file_changed_after_validation_is_not_mounted(self):
        ws=self.workspace()
        with patch.object(matching,'validate_existing_proxy'):
            ws._start_proxy_link([(self.source,self.proxy)]);ws._proxy_link_thread.join(3)
            Path(self.proxy).write_bytes(b'replaced proxy file');self.finish(ws)
        self.assertTrue(all(not c.proxy_path for c in ws.project.clips));ws._remember.assert_not_called()

    def test_cancel_waiting_validation_does_not_bind_partial_batch(self):
        ws=self.workspace();entered=threading.Event()
        def validate(binary,source,proxy,cancel):
            entered.set();self.assertTrue(cancel.wait(3));raise ExportCancelled('cancel')
        with patch.object(matching,'validate_existing_proxy',side_effect=validate):
            ws._start_proxy_link([(self.source,self.proxy)]);self.assertTrue(entered.wait(3))
            ws.cancel_proxy_link();self.finish(ws)
        self.assertFalse(ws._proxy_link_running);ws._remember.assert_not_called()
        self.assertTrue(all(not c.proxy_path for c in ws.project.clips))

    def test_batch_links_unique_files_but_reports_unmatched(self):
        ws=self.workspace();other=self.file(self.sources/'unmatched.mp4')
        ws.project.clips.append(CameraClip('c',3,other,0.,2.))
        with patch.object(matching,'validate_existing_proxy'),patch('proxy_cache.register_proxy'):
            ws._start_proxy_link(folder=str(self.proxies));self.finish(ws)
        self.assertEqual(ws.project.clips[0].proxy_path,self.proxy)
        self.assertEqual(ws.project.clips[-1].proxy_path,'')
        self.assertIn('已链接 1 份',ws.status.get());self.assertIn('1 份未匹配',ws.status.get())


class RealExistingProxyTests(unittest.TestCase):
    def test_preconverted_prores_links_without_encoding_and_trim_is_rejected(self):
        root=Path(__file__).resolve().parents[1]
        ffmpeg=find_binary('ffmpeg',root);ffprobe=find_binary('ffprobe',root)
        if not ffmpeg or not ffprobe:self.skipTest('FFmpeg unavailable')
        with tempfile.TemporaryDirectory(dir=root/'work') as temporary:
            folder=Path(temporary);source=folder/'original.mp4';proxy=folder/'already_converted.mov';trim=folder/'trim.mov'
            for path,duration,rate,codec in [(source,2,60,'libx264'),(proxy,2,30,'prores_ks'),(trim,1,30,'prores_ks')]:
                result=subprocess.run([ffmpeg,'-v','error','-y','-f','lavfi','-i',
                    f'testsrc2=size=192x108:rate={rate}:duration={duration}',
                    '-c:v',codec,'-threads','2',str(path)],capture_output=True,timeout=30,creationflags=CREATE_NO_WINDOW)
                self.assertEqual(result.returncode,0,result.stderr)
            stamp=proxy.stat().st_mtime_ns
            result=matching.validate_existing_proxy(ffprobe,str(source),str(proxy))
            self.assertEqual(result.video_codec,'prores');self.assertAlmostEqual(result.duration,2,places=2)
            self.assertEqual(proxy.stat().st_mtime_ns,stamp)
            with self.assertRaisesRegex(ValueError,'时长'):
                matching.validate_existing_proxy(ffprobe,str(source),str(trim))


if __name__=='__main__':unittest.main()
