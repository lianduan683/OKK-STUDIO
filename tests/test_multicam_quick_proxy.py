"""Background batch orchestration must not lock editing or overwrite new choices."""
from copy import deepcopy
from pathlib import Path
import queue
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from multicam_core import CameraClip, Cut, MulticamProject
from multicam_proxy import MulticamProxyMixin, _stamp
from exporter_core import ExportCancelled


class Variable:
    def __init__(self, value=None): self.value = value
    def get(self): return self.value
    def set(self, value): self.value = value


class Workspace(MulticamProxyMixin):
    def _set_quick_proxy_running(self, value): self._quick_proxy_running = value


class QuickProxyBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.sources = [self.folder/f'camera{i}.mp4' for i in range(3)]
        for source in self.sources: source.write_bytes(b'source')
        self.output = self.folder/'proxy.mp4'; self.output.write_bytes(b'complete proxy')
        self.ws = Workspace()
        w = self.ws
        w.project = MulticamProject([CameraClip(str(i),i+1,str(self.sources[i%3]),0.,10.)
                                    for i in range(9)], [Cut(0.,2)])
        w.current = 2.
        w.app = SimpleNamespace(caps=SimpleNamespace(ffmpeg_path='ffmpeg',ffprobe_path='ffprobe'),
                                _log=Mock(), working=False)
        w.jobs = queue.Queue(); w.player = Mock()
        w.status = Variable(); w.quick_proxy_status = Variable(); w.proxy_var = Variable(False)
        w._quick_proxy_token = 0; w._quick_proxy_cancel = threading.Event()
        w._quick_proxy_running = False; w._quick_proxy_thread = None
        w._configure_player = Mock(); w._mark_dirty = Mock()
        w.quick_proxy_cancel_button = Mock(); w.busy = False
        self.module = SimpleNamespace(prepare_1080p_proxy=Mock(return_value=str(self.output)))
        self.patch = patch.dict(sys.modules, {'quick_proxy': self.module})
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.addCleanup(self.shutdown)

    def shutdown(self):
        self.ws._quick_proxy_cancel.set()
        if self.ws._quick_proxy_thread: self.ws._quick_proxy_thread.join(3)

    def finish(self):
        self.ws._quick_proxy_thread.join(3)
        self.assertFalse(self.ws._quick_proxy_thread.is_alive())
        while not self.ws.jobs.empty():
            kind, payload = self.ws.jobs.get_nowait()
            self.assertTrue(self.ws._handle_quick_proxy_job(kind,payload))

    def test_nine_cameras_deduplicate_and_prioritize_program_without_locking_editor(self):
        self.assertTrue(self.ws.start_quick_proxy())
        self.assertFalse(self.ws.busy)
        self.assertFalse(self.ws.app.working)
        self.finish()
        calls = self.module.prepare_1080p_proxy.call_args_list
        self.assertEqual(len(calls),3)
        self.assertEqual(calls[0].args[0],str(self.sources[1]))
        self.assertTrue(all(clip.proxy_path == str(self.output) for clip in self.ws.project.clips))
        self.assertEqual(self.ws.player.submit.call_count,3)
        self.assertTrue(all(c.args[0]=='proxy_update' for c in self.ws.player.submit.call_args_list))
        self.assertEqual([c.path for c in self.ws.project.clips],
                         [str(self.sources[i%3]) for i in range(9)])

    def test_edit_replaces_project_object_but_does_not_lose_proxy_or_cut(self):
        self.ws.start_quick_proxy()
        self.ws.project = deepcopy(self.ws.project)
        self.ws.project.cuts.append(Cut(5.,3))
        self.finish()
        self.assertEqual(self.ws.project.cuts[-1], Cut(5.,3))
        self.assertTrue(all(c.proxy_path for c in self.ws.project.clips))

    def test_manual_rebinding_during_generation_is_preserved(self):
        self.ws.start_quick_proxy()
        self.ws.project.clips[0].proxy_path = 'manually-chosen.mov'
        self.finish()
        self.assertEqual(self.ws.project.clips[0].proxy_path, 'manually-chosen.mov')
        self.assertEqual(self.ws.project.clips[3].proxy_path, str(self.output))

    def test_new_project_discards_old_results_and_cancels_generation(self):
        self.ws.start_quick_proxy()
        self.ws._quick_proxy_thread.join(3)
        self.ws._reset_quick_proxy()
        self.ws.project = deepcopy(self.ws.project)
        self.finish()
        self.assertTrue(all(not c.proxy_path for c in self.ws.project.clips))
        self.ws.player.submit.assert_not_called()

    def test_cancel_keeps_completed_binding_and_discards_late_results(self):
        request = {'source':str(self.sources[0]),'stamp':_stamp(self.sources[0]),'clips':[('0','')]}
        self.ws._quick_proxy_running = True
        self.ws._handle_quick_proxy_job('quick_proxy_ready',(0,request,str(self.output)))
        self.ws.cancel_quick_proxy()
        request['clips'] = [('3','')]
        self.ws._handle_quick_proxy_job('quick_proxy_ready',(0,request,str(self.output)))
        self.assertEqual(self.ws.project.clips[0].proxy_path,str(self.output))
        self.assertEqual(self.ws.project.clips[3].proxy_path,'')

    def test_failure_in_one_source_does_not_block_remaining_cameras(self):
        self.module.prepare_1080p_proxy.side_effect = [RuntimeError('offline'),str(self.output),str(self.output)]
        self.ws.start_quick_proxy(); self.finish()
        self.assertEqual(self.module.prepare_1080p_proxy.call_count,3)
        self.assertFalse(self.ws._quick_proxy_running)
        self.assertIn('1 份失败',self.ws.status.get())
        self.ws.app._log.assert_called_once()

    def test_modified_source_cannot_receive_old_result(self):
        request = {'source':str(self.sources[0]),'stamp':_stamp(self.sources[0]),'clips':[('0','')]}
        self.sources[0].write_bytes(b'replaced original recording')
        self.ws._handle_quick_proxy_job('quick_proxy_ready',(0,request,str(self.output)))
        self.assertFalse(self.ws.project.clips[0].proxy_path)

    def test_nine_distinct_sources_run_one_at_a_time_and_attach_independently(self):
        sources=[self.folder/f'unique{i}.mp4' for i in range(9)]
        for source in sources:source.write_bytes(b'original')
        self.ws.project=MulticamProject([CameraClip(str(i),i+1,str(source),0.,10.)
            for i,source in enumerate(sources)],[Cut(0.,2)])
        entered=threading.Event();release=threading.Event();active=0;maximum=0
        def prepare(source,*args,**kwargs):
            nonlocal active,maximum
            active+=1;maximum=max(maximum,active);entered.set()
            try:
                self.assertTrue(release.wait(3))
                return str(self.output)
            finally:active-=1
        self.module.prepare_1080p_proxy.side_effect=prepare
        try:
            self.ws.start_quick_proxy();self.assertTrue(entered.wait(3))
            self.assertEqual(self.module.prepare_1080p_proxy.call_count,1)
            self.assertFalse(self.ws.busy);self.assertFalse(self.ws.app.working)
        finally:release.set()
        self.finish()
        self.assertEqual(maximum,1);self.assertEqual(self.module.prepare_1080p_proxy.call_count,9)
        self.assertEqual(self.ws.player.submit.call_count,9)
        self.assertTrue(all(c.proxy_path==str(self.output) for c in self.ws.project.clips))

    def test_close_cancels_active_worker_and_ignores_its_late_done(self):
        entered=threading.Event()
        def prepare(source,*args,cancel_event,**kwargs):
            entered.set();self.assertTrue(cancel_event.wait(3));raise ExportCancelled('closed')
        self.module.prepare_1080p_proxy.side_effect=prepare
        self.ws.start_quick_proxy();self.assertTrue(entered.wait(3))
        self.ws._close_quick_proxy();self.finish()
        self.assertTrue(self.ws._quick_proxy_cancel.is_set())
        self.ws._mark_dirty.assert_not_called();self.ws.player.submit.assert_not_called()
        self.assertEqual(self.module.prepare_1080p_proxy.call_count,1)

    def test_user_can_disable_proxy_preview_while_finished_files_still_attach(self):
        self.ws.start_quick_proxy();self.ws.proxy_var.set(False);self.finish()
        self.assertTrue(all(c.proxy_path for c in self.ws.project.clips))
        self.assertTrue(all(not call.kwargs['use_proxies'] for call in self.ws.player.submit.call_args_list))
        self.assertFalse(self.ws.proxy_var.get())

    def test_removed_clip_and_relinked_source_are_not_restored_by_completion(self):
        self.ws.start_quick_proxy()
        self.ws.project.clips=[clip for clip in self.ws.project.clips if clip.id!='0']
        self.ws.project.clips[0].path='replacement-source.mov'
        self.finish()
        self.assertNotIn('0',[clip.id for clip in self.ws.project.clips])
        self.assertEqual(self.ws.project.clips[0].path,'replacement-source.mov')
        self.assertEqual(self.ws.project.clips[0].proxy_path,'')

    def test_cancel_proxy_then_start_export_does_not_clear_proxy_cancellation(self):
        self.ws.app.cancel_event=threading.Event();entered=threading.Event()
        def prepare(source,*args,cancel_event,**kwargs):
            entered.set();self.assertTrue(cancel_event.wait(3));raise ExportCancelled('cancelled')
        self.module.prepare_1080p_proxy.side_effect=prepare
        self.ws.start_quick_proxy();self.assertTrue(entered.wait(3));self.ws.cancel_quick_proxy()
        self.ws.app.cancel_event.clear();self.ws.app.working=True
        self.finish()
        self.assertTrue(self.ws._quick_proxy_cancel.is_set())
        self.assertFalse(self.ws.app.cancel_event.is_set());self.assertTrue(self.ws.app.working)
        self.ws.player.submit.assert_not_called()

    def test_reset_during_encode_rejects_old_done_without_stopping_new_batch(self):
        entered=threading.Event();new_entered=threading.Event();release=threading.Event();calls=0
        def prepare(source,*args,cancel_event,**kwargs):
            nonlocal calls
            calls+=1
            if calls==1:
                entered.set();self.assertTrue(cancel_event.wait(3));raise ExportCancelled('old project')
            new_entered.set();self.assertTrue(release.wait(3));return str(self.output)
        self.module.prepare_1080p_proxy.side_effect=prepare
        try:
            self.ws.start_quick_proxy();self.assertTrue(entered.wait(3));old=self.ws._quick_proxy_thread
            self.ws._reset_quick_proxy();old.join(3);self.assertFalse(old.is_alive())
            self.ws.project=deepcopy(self.ws.project);self.ws.start_quick_proxy()
            self.assertTrue(new_entered.wait(3))
            while not self.ws.jobs.empty():
                kind,payload=self.ws.jobs.get_nowait();self.ws._handle_quick_proxy_job(kind,payload)
            self.assertTrue(self.ws._quick_proxy_running)
            self.assertTrue(all(not clip.proxy_path for clip in self.ws.project.clips))
        finally:release.set()
        self.finish()
        self.assertTrue(all(clip.proxy_path for clip in self.ws.project.clips))


if __name__ == '__main__': unittest.main()
