"""Real preview/export alpha pixels, project clock, latest mailbox and close."""
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
import queue
import subprocess
import time
import unittest

from exporter_core import CREATE_NO_WINDOW, probe_media
from multicam_core import CameraClip, MulticamProject
from video_stack_preview import StackPreview, preview_command, preview_segments
from video_stack_render import build_stack_command, plan_stack_segments
import test_video_stack_render as fixtures


class StackPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.StackMediaTests.setUpClass()
        cls.fixture = fixtures.StackMediaTests()
        cls.ffmpeg, cls.ffprobe = fixtures.StackMediaTests.ffmpeg, fixtures.StackMediaTests.ffprobe
        cls.controllers = []

    @classmethod
    def tearDownClass(cls):
        for controller in cls.controllers: controller.close()
        fixtures.StackMediaTests.tearDownClass()

    def render(self, command):
        completed = subprocess.run(command, capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode('utf-8', 'replace'))
        return completed.stdout

    def info(self, project):
        return {clip.path: probe_media(self.ffprobe, clip.path) for clip in project.clips}

    def whole(self, project, info):
        frames = []
        for segment in plan_stack_segments(project):
            command, _ = preview_command(segment, project, self.ffmpeg, info, 128, 72,
                                          max_fps=project.fps)
            frames.append(self.render(command))
        return b''.join(frames)

    def test_exact_paused_frame_matches_continuous_graph_at_negative_offset_edges_and_gaps(self):
        project = self.fixture.project()
        project.clips[0].path = str(self.fixture.dynamic)
        project.clips[0].start = -.2; project.clips[0].duration = .8
        project.clips[1].start = .2; project.clips[1].duration = 1.4
        project.clips.append(CameraClip('a2', 1, str(self.fixture.top), 1., .4, has_alpha=True))
        info = self.info(project); full = self.whole(project, info); stride = 128*72*4
        for frame in (0, 2, 5, 6, 9, 10, 13, 14, 16, 19):
            with self.subTest(frame=frame):
                segment = next(preview_segments(project, frame/10, single_frame=True))
                command, _ = preview_command(segment, project, self.ffmpeg, info, 128, 72, single_frame=True)
                self.assertEqual(self.render(command), full[frame*stride:(frame+1)*stride])

    def test_60fps_project_25fps_source_keeps_pause_frame_and_30fps_duration(self):
        project = self.fixture.project(); project.fps = 60
        project.clips[0].path = str(self.fixture.dynamic25); project.clips[0].start = -.017
        info = self.info(project); stride = 128*72*4; full = self.whole(project, info)
        for frame in (1, 7, 31, 59, 77):
            segment = next(preview_segments(project, frame/60, single_frame=True))
            command, rate = preview_command(segment, project, self.ffmpeg, info, 128, 72, single_frame=True)
            self.assertEqual(rate, 60)
            self.assertEqual(self.render(command), full[frame*stride:(frame+1)*stride])
        segment = next(preview_segments(project, .5))
        command, rate = preview_command(segment, project, self.ffmpeg, info, 128, 72)
        self.assertEqual(rate, 30)
        self.assertIn('fps=fps=60', command[command.index('-filter_complex')+1])
        self.assertEqual(len(self.render(command)), round(segment.duration*30)*stride)

    def test_latest_seek_cancels_old_process_and_mailbox_is_one_frame(self):
        events = queue.Queue(); controller = StackPreview(self.ffmpeg, self.ffprobe,
            lambda kind,value: events.put((kind,value)), width=128, height=72)
        self.controllers.append(controller)
        project = self.fixture.project()
        for request in range(1,31):
            controller.request(project, request/30, False, request)
            time.sleep(.002)
        deadline = time.monotonic()+12; frame = None
        while time.monotonic()<deadline:
            item = controller.take_frame()
            if item is not None:
                self.assertEqual(item['request_id'],30); frame = item; break
            time.sleep(.005)
        self.assertIsNotNone(frame)
        self.assertAlmostEqual(frame['time'], 1.)
        self.assertEqual(len(frame['rgba']),128*72*4)
        self.assertIsNone(controller.take_frame())
        self.assertFalse([value for kind,value in list(events.queue) if kind=='error'])
        controller.request(project, 0., True, 31)
        time.sleep(.04)
        began=time.monotonic(); controller.close()
        self.assertLess(time.monotonic()-began,2.)
        self.assertFalse(controller._thread.is_alive())
        self.assertIsNone(controller._process)

    def test_rgba_mailbox_replaces_without_queuing_raw_frames_and_rejects_cancelled_generation(self):
        events=[];controller=StackPreview(self.ffmpeg,self.ffprobe,lambda *value:events.append(value))
        self.controllers.append(controller)
        for index in range(50): controller._publish(0, {'request_id': 0, 'rgba': bytes([index])})
        self.assertEqual(len(events),1)
        self.assertEqual(controller.take_frame()['rgba'],bytes([49]))
        controller.cancel();controller._publish(0, {'request_id':0,'rgba':b'old'})
        self.assertIsNone(controller.take_frame())
        controller.close()

    def test_portrait_canvas_36_layer_script_and_rounded_ntsc_match_export_clock(self):
        project=self.fixture.project();project.clips[0].path=str(self.fixture.portrait)
        project.fps=59.94006;project.range_start=.5;project.range_end=1.
        segment=next(preview_segments(project,0.,single_frame=True))
        self.assertEqual(segment.start,0.)
        command,rate=preview_command(segment,project,self.ffmpeg,self.info(project),36,72,single_frame=True)
        self.assertIn('60000/1001',command)
        self.assertAlmostEqual(rate,60000/1001)
        events=[];controller=StackPreview(self.ffmpeg,self.ffprobe,lambda *value:events.append(value),width=128,height=72)
        self.controllers.append(controller)
        project.clips=[CameraClip(str(camera),camera,str(self.fixture.portrait),0,2,has_alpha=True) for camera in range(1,37)]
        project.video_stack_order=list(range(1,37))
        controller.request(project,0.,False,1)
        deadline=time.monotonic()+10;frame=None
        while time.monotonic()<deadline:
            frame=controller.take_frame()
            if frame is not None:break
            time.sleep(.005)
        self.assertIsNotNone(frame,events)
        self.assertEqual((frame['width'],frame['height']),(36,72))
        self.assertEqual(frame['rgba'][4*(36*36+18):4*(36*36+18)+4],bytes((255,0,0,255)))
        controller.close()

    def test_continuous_epoch_scrub_completes_samples_then_release_is_exact(self):
        controller=StackPreview(self.ffmpeg,self.ffprobe,lambda *value:None,width=128,height=72)
        self.controllers.append(controller);project=self.fixture.project()
        started=time.monotonic();request=0;next_request=started;frames=[]
        while time.monotonic()-started<2.:
            now=time.monotonic()
            if now>=next_request:
                request+=1;target=min(1.8,(now-started)*.8)
                controller.request(project,target,False,request,preview_epoch=700)
                next_request+=1/30
            frame=controller.take_frame()
            if frame is not None:
                self.assertEqual(frame['preview_epoch'],700)
                self.assertLessEqual(frame['request_id'],request)
                frames.append(frame)
            time.sleep(.003)
        self.assertGreaterEqual(len(frames),3,'continuous scrub starved all frames')
        request+=1;controller.request(project,1.5,False,request,preview_epoch=None)
        deadline=time.monotonic()+5;frame=None
        while time.monotonic()<deadline:
            frame=controller.take_frame()
            if frame is not None:break
            time.sleep(.005)
        self.assertIsNotNone(frame)
        self.assertEqual(frame['request_id'],request)
        self.assertIsNone(frame['preview_epoch'])
        self.assertEqual(frame['time'],1.5)
        controller.close()


if __name__=='__main__':unittest.main()
