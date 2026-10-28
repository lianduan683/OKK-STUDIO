"""Ordinary programme playback receives exact signed transition pixels."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from creative_core import ColorGrade, Transition
from exporter_core import CREATE_NO_WINDOW, Capabilities, ExportCancelled, ExportSettings
from multicam_core import CameraClip, Cut, MulticamProject, render_multicam
import multicam_player as player_module
from multicam_player import MulticamPlayer
from program_edits import set_grade_on_range
from transition_preview import render_transition_previews, transition_signature, transition_windows
from test_multicam_player import _Trace, _controller_type


class TransitionPixelCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = Path(__file__).resolve().parents[1]
        cls.ffmpeg, cls.ffprobe = str(app/'bin'/'ffmpeg.exe'), str(app/'bin'/'ffprobe.exe')
        if not Path(cls.ffmpeg).is_file(): raise unittest.SkipTest('bundled FFmpeg unavailable')
        cls.temporary = tempfile.TemporaryDirectory(prefix='transition_cache_')
        cls.folder = Path(cls.temporary.name)
        for color in ('red', 'blue'):
            cls.run_command([cls.ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i',
                f'color=c={color}:s=128x72:r=10:d=4', '-an', '-c:v', 'libx264',
                '-crf', '0', '-preset', 'ultrafast', '-threads', '1', str(cls.folder/f'{color}.mp4')])

    @classmethod
    def tearDownClass(cls): cls.temporary.cleanup()

    @staticmethod
    def run_command(command):
        result = subprocess.run(command, capture_output=True, timeout=45, creationflags=CREATE_NO_WINDOW)
        if result.returncode: raise AssertionError(result.stderr.decode('utf-8', 'replace')[-2500:])
        return result.stdout

    def project(self):
        return MulticamProject(clips=[CameraClip('a', 1, str(self.folder/'red.mp4'), 0, 4),
                                     CameraClip('b', 2, str(self.folder/'blue.mp4'), 0, 4)],
            cuts=[Cut(0, 1), Cut(2, 2)], fps=10, transitions=[Transition(2, 'fade', 1)])

    def pixels(self, path):
        raw = self.run_command([self.ffmpeg, '-v', 'error', '-i', str(path), '-map', '0:v:0',
                                '-an', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'])
        size = 128*72*3
        return [tuple(raw[index+3*(36*128+64):index+3*(36*128+64)+3]) for index in range(0,len(raw),size)]

    def test_generated_cache_and_complete_export_both_show_real_blended_pixels(self):
        project = self.project()
        updates = []
        items = render_transition_previews(project, self.folder/'cache', self.ffmpeg, self.ffprobe,
                                           width=128, height=72, progress=lambda *args: updates.append(args))
        self.assertEqual([(item['start'],item['end']) for item in items],[(1.5,2.5)])
        self.assertEqual(updates, [(1,1)])
        preview = self.pixels(items[0]['path'])
        self.assertEqual(len(preview),10)
        self.assertGreater(preview[0][0],230)
        self.assertLess(preview[0][2],15)
        self.assertTrue(90 < preview[5][0] < 175 and 90 < preview[5][2] < 175, preview[5])
        self.assertGreater(preview[-1][2],200)
        output = self.folder/'export.mp4'
        def runner(command, report):
            result = subprocess.run(command,capture_output=True,timeout=45,creationflags=CREATE_NO_WINDOW)
            return result.returncode,result.stderr.decode('utf-8','replace')
        render_multicam(project,output,self.ffmpeg,self.ffprobe,
            ExportSettings(codec='libx264',preset='ultrafast',quality=0,audio='none',
                           decode_mode='cpu',bframes=0,extra_args=['-threads','1']),
            Capabilities(),runner)
        complete = self.pixels(output)
        self.assertEqual(len(complete),40)
        self.assertGreater(complete[10][0],235)
        self.assertLess(complete[10][2],10)
        self.assertTrue(90 < complete[20][0] < 175 and 90 < complete[20][2] < 175,complete[20])
        self.assertGreater(complete[30][2],235)
        self.assertLess(complete[30][0],10)

    def test_cache_reuse_invalidation_and_cancel(self):
        project = self.project()
        original = copy.deepcopy(project)
        first = render_transition_previews(project,self.folder/'reuse',self.ffmpeg,self.ffprobe,width=128,height=72)
        stamp = Path(first[0]['path']).stat().st_mtime_ns
        again = render_transition_previews(project,self.folder/'reuse',self.ffmpeg,self.ffprobe,width=128,height=72)
        self.assertEqual(first,again)
        self.assertEqual(Path(again[0]['path']).stat().st_mtime_ns,stamp)
        self.assertEqual(project,original)
        old = transition_signature(project)
        set_grade_on_range(project,2,4,ColorGrade(exposure=-1))
        self.assertNotEqual(old,transition_signature(project))
        event=threading.Event(); event.set()
        with self.assertRaises(ExportCancelled):
            render_transition_previews(project,self.folder/'cancel',self.ffmpeg,self.ffprobe,
                                       width=128,height=72,cancel_event=event)

    def test_same_camera_different_range_treatments_fade_but_identical_shots_do_not_fake_motion(self):
        project=self.project()
        project.cuts=[Cut(0,1)]
        project.edit_points=[2.]
        set_grade_on_range(project,0,2,ColorGrade(exposure=-1))
        items=render_transition_previews(project,self.folder/'same_camera',self.ffmpeg,self.ffprobe,width=128,height=72)
        pixels=self.pixels(items[0]['path'])
        self.assertLess(pixels[0][0],150)
        self.assertGreater(pixels[-1][0],225)
        self.assertTrue(pixels[0][0]<pixels[5][0]<pixels[-1][0])


class TransitionPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.trace=_Trace()
        base=_controller_type(self.trace)
        trace=self.trace
        class Controller(base):
            def wait_for_frame(self,target,**kwargs): trace.record('ready',self.window,target)
        self.patches=[patch.object(player_module,'MpvController',Controller),
                      patch.object(player_module,'_AudioController',Controller),
                      patch.object(MulticamPlayer,'TICK',.01)]
        for item in self.patches: item.start()
        self.temporary=tempfile.TemporaryDirectory(prefix='transition_player_')
        self.file=Path(self.temporary.name)/'mixed.mp4'; self.file.write_bytes(b'fixture only')
        self.events=queue.Queue()
        self.player=MulticamPlayer('mpv',{1:101,2:102},200,300,self.events,program_standby_window=201)
        self.project=MulticamProject(clips=[CameraClip('a',1,'red.mp4',0,8),CameraClip('b',2,'blue.mp4',0,8)],
            cuts=[Cut(0,1),Cut(4,2)],fps=10,master_audio='master.wav',master_duration=8,
            global_grade=ColorGrade(exposure=-1),transitions=[Transition(4,'fade',2)])
        self.player.submit('configure',project=self.project)
        self.wait(lambda:self.player._program.active)
        self.publish()
    def tearDown(self):
        self.player.close(); self.assertTrue(self.player.wait_closed(3))
        for item in reversed(self.patches): item.stop()
        self.temporary.cleanup()
    def drain(self):
        values=[]
        while not self.events.empty():
            value=self.events.get_nowait();values.append(value)
            if value[0]=='program_swap':self.player.submit('program_swap_ack',window=value[1])
        return values
    def wait(self,predicate):
        end=time.monotonic()+3
        while time.monotonic()<end:
            self.drain()
            if predicate():return
            time.sleep(.005)
        self.fail('player cache did not reach expected state')
    def publish(self,signature=None):
        self.player.submit('transition_previews',signature=signature or transition_signature(self.project),
                           items=[{'path':str(self.file),'start':3.,'end':5.}])
        self.wait(lambda:bool(self.player._transition_previews))
    def seek(self,value):
        self.player.submit('seek',time=value)
        self.wait(lambda:self.player._position==value and self.player._awaiting_program_swap is None
                  and self.player._program.key==self.player._program_spec(self.player._camera_at(value),value)[0])

    def test_ordinary_seek_reaches_cached_mix_only_inside_window_without_double_grading(self):
        self.seek(4.)
        self.assertEqual(self.player._program.key[0],'transition-preview')
        self.assertEqual(self.player._program.controller.path,str(self.file))
        self.assertEqual(self.player._audio.controller.path,'master.wav')
        uniforms=[call[2][2] for call in self.trace.snapshot()
                  if call[0]=='command' and call[1]==self.player._program.window
                  and call[2][:2]==('set_property','glsl-shader-opts')]
        if uniforms:
            self.assertTrue(all(float(uniforms[-1][f'g{i}_enabled'])==0 for i in range(3)))
        else:
            self.assertNotIn(id(self.player._program.controller),self.player._grade_engine._states,
                             'A fresh baked decoder should not install a second colour shader')
        self.seek(5.1)
        self.assertEqual(self.player._program.controller.path,'blue.mp4')
        self.seek(2.9)
        self.assertEqual(self.player._program.controller.path,'red.mp4')

    def test_camera_audition_and_live_colour_draft_temporarily_use_unbaked_picture(self):
        self.seek(4.)
        self.player.submit('audition',camera=1)
        self.wait(lambda:self.player._program.controller.path=='red.mp4')
        self.player.submit('audition',camera=None)
        self.wait(lambda:self.player._program.key[0]=='transition-preview')
        self.player.submit('grade_preview',target=('selection',3.,5.),grade=ColorGrade(exposure=1))
        self.wait(lambda:self.player._program.controller.path=='blue.mp4')
        self.player.submit('grade_preview',grade=None)
        self.wait(lambda:self.player._program.key[0]=='transition-preview')

    def test_new_edit_rejects_stale_cache_and_source_media_replacement_changes_signature(self):
        old=transition_signature(self.project)
        self.project.global_grade=ColorGrade(exposure=.5)
        self.player.submit('configure',project=self.project)
        self.wait(lambda:self.player._transition_signature==transition_signature(self.project))
        self.player.submit('transition_previews',signature=old,
            items=[{'path':str(self.file),'start':3.,'end':5.}])
        time.sleep(.05)
        self.assertEqual(self.player._transition_previews,[])
        local=copy.deepcopy(self.project)
        local.clips[0].path=str(self.file)
        before=transition_signature(local)
        self.file.write_bytes(b'replaced fixture content')
        self.assertNotEqual(before,transition_signature(local))

    def test_short_transition_is_preloaded_hidden_without_touching_master_audio(self):
        self.seek(2.5)
        self.wait(lambda:self.player._program_standby.key and self.player._program_standby.key[0]=='transition-preview')
        self.assertEqual(self.player._program.controller.path,'red.mp4')
        loads=sum(1 for call in self.trace.snapshot() if call[0]=='load' and call[2]==str(self.file))
        self.seek(3.2)
        self.assertEqual(self.player._program.key[0],'transition-preview')
        self.assertEqual(sum(1 for call in self.trace.snapshot() if call[0]=='load' and call[2]==str(self.file)),loads)
        self.assertEqual(self.player._audio.controller.path,'master.wav')


@unittest.skipUnless(os.name=='nt' and os.environ.get('OKK_TEST_MPV_GPU')=='1',
                     'set OKK_TEST_MPV_GPU=1 for short hardware-decoded transition playback')
class ActualTransitionPlaybackTests(unittest.TestCase):
    def test_ordinary_paused_and_running_programme_shows_the_real_mix(self):
        import tkinter as tk
        app=Path(__file__).resolve().parents[1]
        ffmpeg,ffprobe,mpv=(app/'bin'/name for name in ('ffmpeg.exe','ffprobe.exe','mpv.exe'))
        if not all(path.is_file() for path in (ffmpeg,ffprobe,mpv)):
            self.skipTest('bundled media tools unavailable')
        root=tk.Tk();root.title('OKK 转场预览验证')
        root.geometry('320x180+20+20')
        views=[tk.Frame(root,bg='black') for _ in range(2)]
        for view in views:view.place(x=0,y=0,relwidth=1,relheight=1)
        root.update()
        with tempfile.TemporaryDirectory(prefix='actual_transition_') as temporary:
            folder=Path(temporary)
            sources=[]
            for color in ('red','blue'):
                path=folder/f'{color}.mp4'
                TransitionPixelCacheTests.run_command([str(ffmpeg),'-v','error','-y','-f','lavfi','-i',
                    f'color=c={color}:s=320x180:r=30:d=4','-an','-c:v','libx264','-preset','ultrafast',
                    '-crf','18','-pix_fmt','yuv420p','-threads','1',str(path)])
                sources.append(path)
            audio=folder/'silent_master.wav'
            TransitionPixelCacheTests.run_command([str(ffmpeg),'-v','error','-y','-f','lavfi','-i',
                'anullsrc=r=48000:cl=stereo','-t','4','-c:a','pcm_s16le',str(audio)])
            project=MulticamProject(clips=[CameraClip('a',1,str(sources[0]),0,4),
                                          CameraClip('b',2,str(sources[1]),0,4)],
                cuts=[Cut(0,1),Cut(2,2)],fps=30,master_audio=str(audio),master_duration=4,
                transitions=[Transition(2,'fade',1)])
            items=render_transition_previews(project,folder/'cache',str(ffmpeg),str(ffprobe),width=320,height=180)
            events=queue.Queue();swaps=[];failures=[]
            player=MulticamPlayer(str(mpv),{},views[0].winfo_id(),0,events,
                                  program_standby_window=views[1].winfo_id())
            def poll(predicate,timeout=8):
                end=time.monotonic()+timeout
                while time.monotonic()<end:
                    root.update()
                    while not events.empty():
                        kind,value=events.get_nowait()
                        if kind=='program_swap':
                            next(view for view in views if view.winfo_id()==value).lift()
                            root.update_idletasks();player.submit('program_swap_ack',window=value)
                            swaps.append({'window':value,'kind':player._program.key[0],
                                          'time':player._position})
                        elif kind=='error':failures.append(value)
                    if failures:self.fail(str(failures))
                    if predicate():return
                    time.sleep(.005)
                self.fail('hardware transition preview timed out')
            def pixel():
                image=folder/'current.png'
                player._program.controller.command(['screenshot-to-file',str(image),'video'])
                picture=tk.PhotoImage(master=root,file=str(image))
                return tuple(picture.get(160,90))
            try:
                player.submit('configure',project=project)
                poll(lambda:player._program.active and player._awaiting_program_swap is None)
                player.submit('transition_previews',signature=transition_signature(project),items=items)
                poll(lambda:bool(player._transition_previews))
                player.submit('seek',time=2.)
                poll(lambda:player._program.key[0]=='transition-preview' and player._awaiting_program_swap is None)
                paused=pixel()
                self.assertTrue(85<paused[0]<175 and 85<paused[2]<175,paused)
                self.assertTrue(player._program.controller.command(['get_property','pause']))
                self.assertIn(player._program.controller.command(['get_property','hwdec-current']),('nvdec','d3d11va'))
                player.submit('seek',time=1.0)
                poll(lambda:player._program.key[0]!='transition-preview' and player._awaiting_program_swap is None
                     and player._position==1.)
                before=pixel();self.assertGreater(before[0],230);self.assertLess(before[2],15)
                player.submit('play')
                poll(lambda:player._playing and player._position>1.1)
                audio_calls=[]
                audio_command=player._audio.controller.command
                def recording(command,**kwargs):
                    audio_calls.append(command)
                    return audio_command(command,**kwargs)
                player._audio.controller.command=recording
                poll(lambda:player._program.key[0]=='transition-preview' and player._position>=1.97
                     and player._awaiting_program_swap is None)
                playing=pixel()
                self.assertGreater(playing[0],65,playing);self.assertGreater(playing[2],65,playing)
                poll(lambda:player._position>=2.7 and player._program.controller.command(['get_property','path'])==str(sources[1])
                     and player._awaiting_program_swap is None)
                after=pixel();self.assertGreater(after[2],230);self.assertLess(after[0],15)
                self.assertFalse(any(command[0] in ('loadfile','seek') or command[:2]==['set_property','pause']
                                     for command in audio_calls),audio_calls)
                (app.parent/'transition_preview_gpu_validation.json').write_text(json.dumps({
                    'paused_mix_pixel':paused,'playing_mix_pixel':playing,
                    'before_pixel':before,'after_pixel':after,'swaps':swaps,
                    'ordinary_playback_uses_transition':True,'master_audio_restarted':False,
                    'double_grade_applied':False},ensure_ascii=False,indent=2),encoding='utf-8')
            finally:
                player.close();player.wait_closed(5);root.destroy()


if __name__=='__main__': unittest.main()
