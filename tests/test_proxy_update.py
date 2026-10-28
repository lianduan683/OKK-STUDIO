"""Incremental proxies keep playback state and untouched camera decoders."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from creative_core import ColorGrade
from multicam_core import CameraClip, Cut, MulticamProject
from transition_preview import transition_signature
from subtitle_core import SubtitleCue
try:
    from . import test_multicam_player as plain
    from . import test_transition_preview as baked
    from . import test_gpu_live_player as gpu
except ImportError:
    import test_multicam_player as plain
    import test_transition_preview as baked
    import test_gpu_live_player as gpu


def binding(clip, proxy):
    return {'id': clip.id, 'source': clip.path, 'proxy': str(proxy)}


class IncrementalProxyTests(unittest.TestCase):
    setUp = plain.MulticamPlayerTests.setUp
    tearDown = plain.MulticamPlayerTests.tearDown
    _wait = plain.MulticamPlayerTests._wait
    _state = plain.MulticamPlayerTests._state
    _loads = plain.MulticamPlayerTests._loads
    _commands = plain.MulticamPlayerTests._commands
    _configure = plain.MulticamPlayerTests._configure

    def project(self):
        return MulticamProject([CameraClip(str(i),i,f'camera-{i}.mp4',-1.,20.) for i in range(1,10)],
                               [Cut(0.,1)], master_audio='master.wav',master_duration=19.)

    def test_one_finished_proxy_switches_only_its_camera_and_keeps_live_draft_audio_and_clock(self):
        project=self.project()
        project.subtitles.cues=[SubtitleCue('s',0.,5.,'Caption')]
        self.player.submit('camera_windows',windows={i:100+i for i in range(1,10)})
        self._configure(project)
        self._wait(lambda:len(self.player._slots)==9 and all(slot.active for slot in self.player._slots.values()))
        self.player.submit('audition',camera=2)
        self.player.submit('grade_preview',target=('global',),grade=ColorGrade(exposure=.2))
        self.player.submit('play')
        self._state(lambda value:value['playing'] and value['time']>.02 and value['camera']==2)
        draft=deepcopy(self.player._grade_draft)
        subtitle=self.player._subtitle_ass_path
        before=len(self.trace.snapshot());position=self.player._position
        with tempfile.TemporaryDirectory(prefix='proxy_update_') as folder:
            proxy=Path(folder)/'camera-2.mp4';proxy.write_bytes(b'completed proxy')
            payload=[binding(project.clips[1],proxy)]
            self.player.submit('proxy_update',bindings=payload,use_proxies=True)
            payload[0]['proxy']='caller-mutated.mp4'
            self._wait(lambda:self.player._slots[2].controller.path==str(proxy)
                       and self.player._program.controller.path==str(proxy))
            self.assertTrue(self.player._playing)
            self.assertGreaterEqual(self.player._position,position)
            self.assertEqual(self.player._audition,2)
            self.assertEqual(self.player._grade_draft,draft)
            self.assertEqual(self.player._subtitle_ass_path,subtitle)
            changes=self.trace.snapshot()[before:]
            self.assertEqual({window for kind,window,value,_ in changes if kind=='load'},{102,200})
            self.assertFalse(any(kind=='command' and window==300 and
                (value[0] in ('seek','loadfile','stop') or value==('set_property','pause',True))
                for kind,window,value,_ in changes))
            untouched={101,103,104,105,106,107,108,109}
            self.assertFalse(any(kind=='load' or (kind=='command' and value[0]=='seek')
                for kind,window,value,_ in changes if window in untouched))
            self.assertEqual(self.player._project.segments()[0].path,project.clips[0].path)
            self.assertEqual(project.clips[1].proxy_path,'', 'worker mutated caller project')

    def test_disabled_proxies_bind_without_loading_and_stale_or_bad_batch_is_non_destructive(self):
        project=self.project()
        self._configure(project,use_proxies=False)
        self._wait(lambda:self.player._slots[1].active and self.player._slots[2].active)
        self.player.submit('play');self._state(lambda value:value['playing'])
        with tempfile.TemporaryDirectory(prefix='proxy_disabled_') as folder:
            proxy=Path(folder)/'proxy.mp4';proxy.write_bytes(b'proxy')
            before=len(self.trace.snapshot())
            self.player.submit('proxy_update',bindings=[binding(project.clips[0],proxy)],use_proxies=False)
            self._wait(lambda:self.player._project.clips[0].proxy_path==str(proxy))
            self.assertFalse(any(kind=='load' for kind,_,_,_ in self.trace.snapshot()[before:]))
            self.assertFalse(self.player._use_proxies)
            self.player.submit('proxy_update',bindings=[{'id':'1','source':'replaced.mp4','proxy':str(proxy)}],use_proxies=False)
            self.player.submit('proxy_update',bindings=[binding(project.clips[1],proxy),
                {'id':'3','source':'camera-3.mp4','proxy':str(Path(folder)/'missing.mp4')}],use_proxies=False)
            self._wait(lambda:any(kind=='error' and '代理自动挂载' in text for kind,text in self.received))
            self.assertEqual(self.player._project.clips[1].proxy_path,'')
            self.assertTrue(self.player._playing)

    def test_stack_proxy_attachment_and_disable_restart_only_composite_job(self):
        class Preview:
            def __init__(self):self.requests=[];self.cancelled=0
            def cancel(self):self.cancelled+=1
            def close(self):pass
            def update_clock(self,position):pass
            def request(self,project,position,playing,request_id,**options):
                self.requests.append((deepcopy(project),position,playing,options))
        self._wait(lambda:hasattr(self.player,'_stack_preview'))
        preview=Preview();self.player._stack_preview=preview
        self.player.submit('camera_windows',windows={})
        project=self.project();project.video_stack_enabled=True
        self._configure(project);self._wait(lambda:bool(preview.requests))
        self.player.submit('play');self._state(lambda value:value['playing'])
        with tempfile.TemporaryDirectory(prefix='stack_proxy_update_') as folder:
            proxy=Path(folder)/'ready.mp4';proxy.write_bytes(b'completed')
            before=len(preview.requests);trace_start=len(self.trace.snapshot())
            self.player.submit('proxy_update',bindings=[binding(project.clips[0],proxy)],use_proxies=True)
            self._wait(lambda:len(preview.requests)>before)
            self.assertEqual(preview.requests[-1][0].clips[0].proxy_path,str(proxy))
            self.assertTrue(preview.requests[-1][2]);self.assertTrue(preview.requests[-1][3]['use_proxies'])
            before=len(preview.requests)
            self.player.submit('proxy_update',bindings=[],use_proxies=False)
            self._wait(lambda:len(preview.requests)>before)
            self.assertFalse(preview.requests[-1][3]['use_proxies'])
            self.assertGreaterEqual(preview.cancelled,3)
            changes=self.trace.snapshot()[trace_start:]
            self.assertFalse(any(kind=='command' and window==300 and value[0] in ('seek','loadfile','stop')
                                 for kind,window,value,_ in changes))


class CachedTransitionProxyTests(unittest.TestCase):
    setUp=baked.TransitionPlaybackTests.setUp
    tearDown=baked.TransitionPlaybackTests.tearDown
    wait=baked.TransitionPlaybackTests.wait
    drain=baked.TransitionPlaybackTests.drain
    publish=baked.TransitionPlaybackTests.publish
    seek=baked.TransitionPlaybackTests.seek

    def test_baked_transition_and_rendered_programme_survive_incremental_attachment(self):
        self.seek(4.)
        before=deepcopy(self.player._transition_previews)
        key=self.player._program.key;signature=self.player._transition_signature
        proxy=Path(self.temporary.name)/'proxy.mp4';proxy.write_bytes(b'proxy')
        self.player.submit('proxy_update',bindings=[binding(self.project.clips[1],proxy)],use_proxies=True)
        self.wait(lambda:self.player._project.clips[1].proxy_path==str(proxy))
        self.assertEqual(self.player._transition_previews,before)
        self.assertEqual(self.player._transition_signature,signature)
        self.assertEqual(self.player._program.key,key)
        self.assertEqual(self.player._position,4.)
        self.assertFalse(self.player._playing)
        self.player.submit('render_preview',path=str(self.file),start=3.,end=5.,play=False)
        self.wait(lambda:self.player._render_preview is not None)
        preview=self.player._render_preview
        self.player.submit('proxy_update',bindings=[binding(self.project.clips[0],proxy)],use_proxies=True)
        self.wait(lambda:self.player._project.clips[0].proxy_path==str(proxy))
        self.assertEqual(self.player._render_preview,preview)
        self.assertEqual(self.player._transition_previews,before)


class NativeGpuProxyTests(unittest.TestCase):
    setUp=gpu.GpuLiveWorkerTests.setUp
    tearDown=gpu.GpuLiveWorkerTests.tearDown
    wait=gpu.GpuLiveWorkerTests.wait
    drain=gpu.GpuLiveWorkerTests.drain

    def test_native_transition_rebinds_only_updated_side_without_restarting_renderer_or_master(self):
        self.player.submit('seek',time=.55)
        self.wait(lambda:abs(self.player._position-.55)<1e-8 and self.player._gpu_context is not None)
        self.player.submit('grade_preview',target=('global',),grade=ColorGrade(exposure=.1))
        self.wait(lambda:self.player._grade_draft is not None)
        backend=self.backends[0]
        before=len(self.trace.snapshot());original_draft=deepcopy(self.player._grade_draft)
        with tempfile.TemporaryDirectory(prefix='native_proxy_') as folder:
            proxy=Path(folder)/'camera-b.mp4';proxy.write_bytes(b'proxy')
            self.player.submit('proxy_update',bindings=[binding(self.project.clips[1],proxy)],use_proxies=True)
            self.wait(lambda:self.player._gpu_context is not None and
                self.player._gpu_context['plan'].incoming.path==str(proxy)
                and any(controller.path==str(proxy) for controller in backend.controllers.values()))
            self.assertIs(self.backends[0],backend)
            self.assertEqual(len(self.backends),1);self.assertEqual(backend.closed,0)
            self.assertFalse(self.player._playing);self.assertAlmostEqual(self.player._position,.55)
            self.assertEqual(self.player._grade_draft,original_draft)
            changes=self.trace.snapshot()[before:]
            self.assertFalse(any(kind=='load' and value=='camera-a.mp4' for kind,window,value,_ in changes))
            self.assertFalse(any(kind=='command' and window==300 and value[0] in ('seek','loadfile','stop')
                                 for kind,window,value,_ in changes))
            self.assertEqual(self.player._project.segments()[1].path,'camera-b.mp4')


class ProxySignatureTests(unittest.TestCase):
    def test_proxy_path_never_invalidates_original_based_transition_signature(self):
        project=MulticamProject([CameraClip('a',1,'source.mp4',0.,4.)],[Cut(0.,1)])
        before=transition_signature(project)
        project.clips[0].proxy_path='different-proxy.mp4'
        self.assertEqual(transition_signature(project),before)
        project.clips[0].path='replacement-original.mp4'
        self.assertNotEqual(transition_signature(project),before)


if __name__=='__main__':unittest.main(verbosity=2)
