"""Original/stabilized comparison uses one clock without altering edits."""
from __future__ import annotations
import copy
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from creative_core import ColorGrade
from gyroflow_core import StabilizationSettings,StabilizedRange,cache_signature
from multicam_core import CameraClip,Cut,MulticamProject
from multicam_player import MulticamPlayer
import multicam_player as player_module
from test_multicam_player import _Trace,_controller_type


class StabilizationCompareTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='compare_player_')
        self.folder=Path(self.temp.name)
        self.trace=_Trace();self.gate=threading.Event();self.gate.set()
        trace,gate=self.trace,self.gate
        base=_controller_type(trace)
        class Controller(base):
            def wait_for_frame(self,target,**kwargs):
                trace.record('frame_wait',self.window,target)
                while not gate.wait(.01):
                    if kwargs.get('cancel_event') and kwargs['cancel_event'].is_set(): return
                trace.record('frame_ready',self.window,target)
        self.patches=[patch.object(player_module,'MpvController',Controller),
                      patch.object(player_module,'_AudioController',Controller),
                      patch.object(MulticamPlayer,'TICK',.01)]
        for item in self.patches:item.start()
        source=self.folder/'original.mp4';source.write_bytes(b'original')
        cache=self.folder/'stabilized.mp4';cache.write_bytes(b'cache')
        source2=self.folder/'camera2.mp4';source2.write_bytes(b'camera2')
        proxy=self.folder/'proxy.mp4';proxy.write_bytes(b'proxy')
        settings=StabilizationSettings(auto_sync=False)
        self.project=MulticamProject(clips=[CameraClip('a',1,str(source),-1,9,str(proxy)),
                                            CameraClip('b',2,str(source2),0,8)],
                                    cuts=[Cut(0,1),Cut(6,2)],master_audio='master.wav',master_duration=8,
                                    global_grade=ColorGrade(exposure=.2),
                                    stabilizations=[StabilizedRange(2,6,1,'a',settings,str(cache),
                                        cache_signature(str(source),1,8,settings),1,8)])
        self.events=queue.Queue();self.received=[]
        self.player=MulticamPlayer('mpv',{1:101,2:102},200,300,self.events,
            program_standby_window=201,comparison_window=400,comparison_standby_window=401)
        self.player.submit('configure',project=self.project)
        self.wait(lambda:self.player._program.active)
        self.player.submit('seek',time=3)
        self.wait(lambda:self.player._program.key and self.player._program.key[0]=='stabilized')
        self.drain()

    def tearDown(self):
        self.gate.set();self.player.close()
        stopped=self.player.wait_closed(3)
        for item in reversed(self.patches):item.stop()
        self.temp.cleanup();self.assertTrue(stopped,'comparison leaked player worker')

    def drain(self,ack_comparison=True):
        result=[]
        while not self.events.empty():
            item=self.events.get_nowait();result.append(item);self.received.append(item)
        for kind,value in result:
            if kind=='program_swap':self.player.submit('program_swap_ack',window=value)
            if kind=='comparison_swap' and ack_comparison:
                self.player.submit('comparison_swap_ack',window=value)
        return result

    def wait(self,predicate,timeout=3):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            self.drain()
            if predicate():return
            time.sleep(.005)
        self.fail('comparison condition timed out; events='+str(self.received[-6:]))

    def enable(self):
        self.player.submit('stabilization_compare',enabled=True)
        self.wait(lambda:self.player._comparison.active)
        self.drain()

    def test_optional_decoders_start_only_on_enable_and_original_bypasses_proxy(self):
        self.assertFalse(any(window in {400,401} for kind,window,_,_ in self.trace.snapshot() if kind=='create'))
        self.enable()
        self.assertEqual(self.player._comparison.controller.path,self.project.clips[0].path)
        self.assertNotEqual(self.player._comparison.controller.path,self.project.clips[0].proxy_path)
        self.assertEqual(self.player._comparison.controller.position,4)
        self.assertEqual(self.player._program.controller.position,3)
        self.assertTrue(self.player._program.controller.paused)
        self.assertTrue(self.player._comparison.controller.paused)

    def test_shared_grades_and_paused_frame_seek_remain_exact(self):
        self.enable()
        before=copy.deepcopy(self.project)
        self.player.submit('seek',time=4)
        self.wait(lambda:self.player._comparison.controller.position==5)
        self.assertEqual(self.player._program.controller.position,4)
        self.assertEqual(self.player._project,before)
        def uniforms(window):
            return [payload[2] for kind,target,payload,_ in self.trace.snapshot()
                    if kind=='command' and target==window and payload[:2]==('set_property','glsl-shader-opts')][-1]
        self.assertEqual(uniforms(self.player._program.window),uniforms(self.player._comparison.window))
        self.player.submit('grade_preview',target=('selection',2,6),grade=ColorGrade(exposure=.5))
        self.wait(lambda:uniforms(self.player._comparison.window)['g1_exposure']=='0.5')
        self.assertEqual(uniforms(self.player._program.window),uniforms(self.player._comparison.window))

    def test_toggle_and_comparison_camera_changes_leave_master_audio_running(self):
        self.player.submit('play');self.wait(lambda:self.player._playing and not self.player._audio.controller.paused)
        before=len(self.trace.snapshot());self.enable()
        self.player.submit('audition',camera=2)
        self.wait(lambda:self.player._comparison.controller.path==self.project.clips[1].path)
        audio=[payload for kind,window,payload,_ in self.trace.snapshot()[before:] if kind=='command' and window==300]
        self.assertFalse(any(payload[0] in {'seek','loadfile','stop'} or payload==('set_property','pause',True) for payload in audio),audio)
        raw=self.player._comparison.controller
        main=self.player._program.controller
        self.assertLess(abs(raw._position()-main._position()),.1)
        self.player.submit('stabilization_compare',enabled=False)
        self.wait(lambda:not self.player._comparison_enabled and self.player._comparison.controller is None)
        disposed={window for kind,window,_,_ in self.trace.snapshot() if kind=='dispose'}
        self.assertTrue({400,401}.issubset(disposed))

    def test_compare_skips_baked_transition_and_explicit_excerpt_exits_comparison(self):
        path=self.folder/'baked.mp4';path.write_bytes(b'preview')
        self.player.submit('render_preview',path=str(path),start=2,end=4,play=False)
        self.wait(lambda:self.player._program.key and self.player._program.key[0]=='render-preview')
        self.enable()
        self.wait(lambda:not self.player._is_baked_key(self.player._program.key))
        self.player._transition_previews=[(str(path),2,4)]
        self.assertEqual(self.player._program_spec(1,3)[0][0],'stabilized')
        self.player.submit('render_preview',path=str(path),start=2,end=4,play=False)
        self.wait(lambda:not self.player._comparison_enabled)
        self.wait(lambda:self.player._program.key and self.player._program.key[0]=='render-preview')

    def test_state_marks_unstabilized_interval_and_disable_releases_resources(self):
        self.enable();self.player.submit('seek',time=.5)
        self.wait(lambda:any(kind=='state' and value['comparison_enabled'] and not value['stabilization_active']
                             and value['time']==.5 for kind,value in self.received))
        self.player.submit('stabilization_compare',enabled=False)
        self.wait(lambda:self.player._comparison.controller is None)
        self.player.submit('stabilization_compare',enabled=True)
        self.wait(lambda:self.player._comparison.active)
        self.player.close();self.assertTrue(self.player.wait_closed(3))
        self.assertIsNone(self.player._comparison.controller.process)


@unittest.skipUnless(os.environ.get('OKK_TEST_MPV_GPU')=='1','set OKK_TEST_MPV_GPU=1 for original/stabilized GPU comparison')
class StabilizationCompareGpuTests(unittest.TestCase):
    def test_original_fov_and_stabilized_crop_share_clock_and_grade(self):
        import tkinter as tk
        app=Path(__file__).resolve().parents[1]
        ffmpeg,mpv=app/'bin'/'ffmpeg.exe',app/'bin'/'mpv.exe'
        root=tk.Tk();root.title('原片 / 防抖后对比验证');root.geometry('660x225+20+20')
        panels=[]
        for side in range(2):
            panel=tk.Frame(root,width=320,height=180,bg='black');panel.grid(row=0,column=side)
            fronts=[tk.Frame(panel,bg='black') for _ in range(2)]
            for frame in fronts:frame.place(x=0,y=0,relwidth=1,relheight=1)
            fronts[0].lift();panels.append(fronts)
        root.update();events=queue.Queue();captures=[]
        with tempfile.TemporaryDirectory(prefix='compare_gpu_') as name:
            folder=Path(name);source=folder/'original.mp4';cache=folder/'cropped.mp4'
            filter_text='color=red:s=320x180:r=30:d=4,drawbox=x=80:y=0:w=160:h=180:color=blue:t=fill'
            subprocess.run([str(ffmpeg),'-v','error','-y','-f','lavfi','-i',filter_text,
                            '-c:v','libx264','-preset','ultrafast','-crf','18',str(source)],capture_output=True,check=True,timeout=20)
            subprocess.run([str(ffmpeg),'-v','error','-y','-i',str(source),'-vf','crop=160:180:80:0,scale=320:180',
                            '-c:v','libx264','-preset','ultrafast','-crf','18',str(cache)],capture_output=True,check=True,timeout=20)
            settings=StabilizationSettings(auto_sync=False)
            project=MulticamProject(clips=[CameraClip('a',1,str(source),0,4)],cuts=[Cut(0,1)],fps=30,
                global_grade=ColorGrade(exposure=-.25),stabilizations=[StabilizedRange(0,4,1,'a',settings,str(cache),
                    cache_signature(str(source),0,4,settings),0,4)])
            player=MulticamPlayer(str(mpv),{},panels[1][0].winfo_id(),0,events,
                program_standby_window=panels[1][1].winfo_id(),comparison_window=panels[0][0].winfo_id(),
                comparison_standby_window=panels[0][1].winfo_id())
            errors=[]
            def poll_until(predicate,timeout=8):
                deadline=time.monotonic()+timeout
                while time.monotonic()<deadline:
                    root.update()
                    while not events.empty():
                        kind,value=events.get_nowait()
                        if kind in ('program_swap','comparison_swap'):
                            side=1 if kind=='program_swap' else 0
                            next(frame for frame in panels[side] if frame.winfo_id()==value).lift()
                            player.submit('program_swap_ack' if side else 'comparison_swap_ack',window=value)
                        elif kind=='error':errors.append(value)
                    if predicate():return
                    time.sleep(.005)
                self.fail('GPU comparison timed out: '+str(errors))
            def capture(label):
                samples={}
                for side,slot in (('original',player._comparison),('stabilized',player._program)):
                    path=folder/(label+'_'+side+'.png')
                    slot.controller.command(['screenshot-to-file',str(path),'video'])
                    image=tk.PhotoImage(master=root,file=str(path))
                    samples[side]={'left':list(image.get(20,90)),'center':list(image.get(160,90)),
                        'time':slot.controller.command(['get_property','time-pos']),
                        'hwdec':slot.controller.command(['get_property','hwdec-current'])}
                captures.append(samples);return samples
            try:
                player.submit('configure',project=project);player.submit('seek',time=1)
                player.submit('stabilization_compare',enabled=True)
                poll_until(lambda:player._comparison.active and player._program.active)
                result=capture('paused')
                self.assertGreater(result['original']['left'][0],150)
                self.assertGreater(result['stabilized']['left'][2],150)
                self.assertLess(abs(result['original']['center'][2]-result['stabilized']['center'][2]),4)
                self.assertLess(abs(result['original']['time']-result['stabilized']['time']),1/30+.001)
                player.submit('seek',time=2)
                poll_until(lambda:abs(player._comparison.controller.command(['get_property','time-pos'])-2)<.04)
                self.assertLess(abs(player._program.controller.command(['get_property','time-pos'])-2),.04)
                player.submit('play');poll_until(lambda:player._playing)
                time.sleep(.15);result=capture('playing')
                self.assertLess(abs(result['original']['time']-result['stabilized']['time']),.1)
                self.assertTrue(all(value['hwdec'] in {'nvdec','d3d11va'} for sample in captures for value in sample.values()))
                player.submit('stabilization_compare',enabled=False)
                poll_until(lambda:player._comparison.controller is None)
                self.assertFalse(errors,errors)
                (app.parent/'stabilization_compare_gpu_validation.json').write_text(json.dumps({
                    'successful':True,'original_full_fov_and_stabilized_crop':True,
                    'same_grading':True,'paused_and_playing_checked':True,'samples':captures,
                    'disabled_decoders_released':True},indent=2),encoding='utf-8')
            finally:
                player.close();player.wait_closed(5);root.destroy()


if __name__=='__main__':unittest.main()
