"""Camera reference audio retains anchored timeline positions and ambiguities."""
from __future__ import annotations
from array import array
import copy
import json
import math
from pathlib import Path
import random
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

from editor_core import AlignmentResult
from exporter_core import CREATE_NO_WINDOW,ExportCancelled,find_binary
from multicam_core import CameraClip
import reference_sync
from reference_sync import align_to_camera


class ReferenceSyncTests(unittest.TestCase):
    def setUp(self):
        self.target=CameraClip('target',2,'target.mp4',7,20)
        self.refs=[CameraClip('ref1',1,'ref1.mp4',10,40),CameraClip('ref2',1,'ref2.mp4',30,40)]

    def align(self,matches,refs=None,**options):
        progress=[]
        with patch('reference_sync._has_audio',return_value=True),patch('reference_sync.align_audio_segments',side_effect=matches) as backend:
            result=align_to_camera('ffmpeg',[self.target],refs or self.refs,ffprobe_path='ffprobe',
                progress=lambda value,message:progress.append(value),**options)
        self.assertEqual(progress,sorted(progress));self.assertEqual(progress[-1],100)
        return result,backend

    def test_absolute_offsets_include_positive_and_negative_anchor_without_mutation(self):
        for anchor,expected in ((10,-12),(-5,3)):
            reference=CameraClip('ref',1,'ref.mp4',anchor,40)
            before=copy.deepcopy((self.target,reference))
            result,backend=self.align([[AlignmentResult(-2,.95,50,'original middle')]],refs=[reference])
            self.assertEqual(result[0].offset_seconds,expected)
            self.assertEqual((self.target,reference),before)
            self.assertEqual(backend.call_args.kwargs['mode'],'fast')
            self.assertTrue(backend.call_args.kwargs['continue_on_error'])

    def test_agreeing_fragment_matches_keep_confidence(self):
        result,_=self.align([[AlignmentResult(-2,.92,50)],[AlignmentResult(18,.91,50)]])
        self.assertEqual(result[0].offset_seconds,-12)
        self.assertEqual(result[0].confidence,.92)

    def test_similar_conflicting_fragments_preserve_existing_target_position(self):
        result,_=self.align([[AlignmentResult(-2,.92,50)],[AlignmentResult(-2,.90,50)]])
        self.assertEqual((result[0].offset_seconds,result[0].confidence),(-7,0))
        self.assertIn('矛盾',result[0].engine)

    def test_weak_and_unreadable_targets_do_not_get_placed_at_zero(self):
        result,_=self.align([[AlignmentResult(-100,.05,50,'weak')],[AlignmentResult(0,0,50,'unreadable')]])
        self.assertEqual((result[0].offset_seconds,result[0].confidence),(-7,0))

    def test_absent_reference_audio_is_clear_error_and_never_calls_matcher(self):
        with patch('reference_sync._has_audio',return_value=False),patch('reference_sync.align_audio_segments') as matcher:
            with self.assertRaisesRegex(ValueError,'没有可读取的音轨'):
                align_to_camera('ffmpeg',[self.target],self.refs,ffprobe_path='ffprobe')
        matcher.assert_not_called()

    def test_cancel_propagates_and_does_not_try_next_reference(self):
        event=threading.Event()
        with patch('reference_sync._has_audio',return_value=True),patch('reference_sync.align_audio_segments',side_effect=ExportCancelled('cancelled')) as matcher:
            with self.assertRaises(ExportCancelled):
                align_to_camera('ffmpeg',[self.target],self.refs,ffprobe_path='ffprobe',cancel_event=event)
        self.assertEqual(matcher.call_count,1)

    def test_silent_reference_can_be_skipped_for_another_valid_fragment(self):
        result,_=self.align([RuntimeError('silent reference'),[AlignmentResult(18,.91,50)]])
        self.assertEqual((result[0].offset_seconds,result[0].confidence),(-12,.91))

    def test_reference_clip_is_immutable_even_if_included_as_target(self):
        reference=self.refs[0]
        with patch('reference_sync._has_audio',return_value=True),patch('reference_sync.align_audio_segments') as matcher:
            result=align_to_camera('ffmpeg',[reference],[reference],ffprobe_path='ffprobe')
        matcher.assert_not_called()
        self.assertEqual((result[0].offset_seconds,result[0].confidence),(-10,1))


class ReferenceSyncMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app=Path(__file__).resolve().parents[1]
        cls.ffmpeg=find_binary('ffmpeg',app);cls.ffprobe=find_binary('ffprobe',app)
        if not cls.ffmpeg or not cls.ffprobe:raise unittest.SkipTest('FFmpeg unavailable')
        cls.temp=tempfile.TemporaryDirectory(prefix='camera_reference_media_')
        cls.folder=Path(cls.temp.name)
        cls.rate=4000
        rng=random.Random(65519)
        levels=[rng.uniform(.04,1.)**1.5 for _ in range(900)]
        cls.signal=array('h',(round(11000*levels[min(len(levels)-1,int(index/cls.rate*10))]*(
            math.sin(2*math.pi*211*index/cls.rate)+.33*math.sin(2*math.pi*337*index/cls.rate)))
            for index in range(90*cls.rate)))
        cls.paths={}
        for key,first,last in [('refA',10,45),('targetA',20,35),('refB',50,85),('targetB',60,75)]:
            audio=cls.folder/(key+'.wav')
            with wave.open(str(audio),'wb') as output:
                output.setparams((1,2,cls.rate,0,'NONE','not compressed'))
                output.writeframes(cls.signal[first*cls.rate:last*cls.rate].tobytes())
            path=cls.folder/(key+'.mkv')
            subprocess.run([cls.ffmpeg,'-v','error','-y','-f','lavfi','-i',f'color=gray:s=32x18:r=2:d={last-first}',
                '-i',str(audio),'-map','0:v:0','-map','1:a:0','-c:v','libx264','-preset','ultrafast',
                '-c:a','pcm_s16le','-shortest',str(path)],capture_output=True,check=True,timeout=20,creationflags=CREATE_NO_WINDOW)
            cls.paths[key]=path

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def test_actual_camera_audio_reference_respects_both_anchor_signs(self):
        target=CameraClip('target',2,str(self.paths['targetA']),0,15)
        for anchor,expected in ((10,20),(-5,5)):
            reference=CameraClip('ref',1,str(self.paths['refA']),anchor,35)
            result=align_to_camera(self.ffmpeg,[target],[reference],ffprobe_path=self.ffprobe,
                                   cache_dir=self.folder/'cache')
            self.assertAlmostEqual(-result[0].offset_seconds,expected,delta=.06)
            self.assertGreaterEqual(result[0].confidence,.2)
            self.assertEqual(target.start,0);self.assertEqual(reference.start,anchor)

    def test_actual_disconnected_reference_recordings_keep_their_own_anchors(self):
        targets=[CameraClip('tA',2,str(self.paths['targetA']),0,15),CameraClip('tB',3,str(self.paths['targetB']),0,15)]
        references=[CameraClip('rA',1,str(self.paths['refA']),10,35),CameraClip('rB',1,str(self.paths['refB']),60,35)]
        before=copy.deepcopy((targets,references))
        result=align_to_camera(self.ffmpeg,targets,references,ffprobe_path=self.ffprobe,cache_dir=self.folder/'cache')
        self.assertAlmostEqual(-result[0].offset_seconds,20,delta=.06)
        self.assertAlmostEqual(-result[1].offset_seconds,70,delta=.06)
        self.assertTrue(all(item.confidence>=.2 for item in result),[item.engine for item in result])
        self.assertEqual((targets,references),before)


if __name__=='__main__':unittest.main()
