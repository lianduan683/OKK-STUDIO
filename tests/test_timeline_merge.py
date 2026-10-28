"""Source join model invariants and CPU FFmpeg stream-copy integration."""
import copy
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from exporter_core import CREATE_NO_WINDOW,ExportCancelled,find_binary
from creative_core import ColorGrade
from multicam_core import CameraClip,Cut,MulticamProject
from timeline_merge import (SourceMergeResult,apply_source_merge,plan_source_merge,
    render_source_merge,_probe,_validate_sources)


class SourceMergeModelTests(unittest.TestCase):
    def setUp(self):
        self.project=MulticamProject(clips=[CameraClip('a',1,'a.mp4',5,2,proxy_path='a_proxy.mp4'),
            CameraClip('b',1,'b.mp4',10,3),CameraClip('c',2,'c.mp4',0,30),
            CameraClip('d',1,'d.mp4',20,3)],cuts=[Cut(0,2),Cut(5,1),Cut(20,2)],master_duration=30)

    def test_gap_overlap_and_negative_starts_are_packed_from_the_first_start(self):
        for second_start in (10,6):
            self.project.clips[1].start=second_start
            plan=plan_source_merge(self.project,['b','a'])
            self.assertEqual(plan.source_ids,('a','b'))
            self.assertEqual(plan.shifts,(0,7-second_start))
            before=copy.deepcopy(self.project)
            result=SourceMergeResult('joined.mp4',5,(2,3))
            candidate,merged=apply_source_merge(self.project,plan,result)
            self.assertEqual((merged.id,merged.start,merged.duration,merged.proxy_path),('a',5,5,''))
            self.assertEqual(candidate.cuts,self.project.cuts)
            self.assertEqual(candidate.master_audio,self.project.master_audio)
            self.assertEqual([clip for clip in candidate.clips if clip.id in ('c','d')],self.project.clips[2:])
            self.assertEqual(self.project,before)
        self.project.clips[0].start=-2
        self.assertEqual(plan_source_merge(self.project,['a','b']).start,-2)

    def test_refuses_changed_camera_grade_stale_selection_and_cached_source_effects(self):
        self.project.camera_grades[2]=ColorGrade(contrast=1.2)
        with self.assertRaisesRegex(ValueError,'不同的机位调色'):plan_source_merge(self.project,['a','c'])
        with self.assertRaisesRegex(ValueError,'不存在'):plan_source_merge(self.project,['a','b','gone'])
        plan=plan_source_merge(self.project,['a','b'])
        self.project.clips[1].start+=1
        with self.assertRaisesRegex(ValueError,'发生变化'):
            apply_source_merge(self.project,plan,SourceMergeResult('x.mp4',5,(2,3)))
        self.project.stabilizations=[SimpleNamespace(source_id='b')]
        with self.assertRaisesRegex(ValueError,'防抖'):plan_source_merge(self.project,['a','b'])
        self.project.stabilizations=[];self.project.library_overlays=[SimpleNamespace(source_id='a')]
        with self.assertRaisesRegex(ValueError,'叠加特效'):plan_source_merge(self.project,['a','b'])

    def test_cross_track_batch_import_order_and_camera_cuts_are_retained(self):
        # Batch import assigns each DJI part a separate camera and zero start.
        project=MulticamProject(clips=[CameraClip('z-first',3,'first.mp4',0,2),
            CameraClip('a-second',8,'second.mp4',0,3),CameraClip('q-third',9,'third.mp4',0,4),
            CameraClip('unselected',9,'keep.mp4',20,3)],cuts=[Cut(0,3),Cut(2,8),Cut(4,9)],
            master_audio='master.wav',master_duration=25,alignment_reference_camera=8,
            video_stack_order=[9,8,3],video_stack_hidden=[8])
        original=copy.deepcopy(project)
        plan=plan_source_merge(project,['a-second','q-third','z-first'])
        self.assertEqual(plan.source_ids,('z-first','a-second','q-third'))
        self.assertEqual(plan.shifts,(0,2,5))
        candidate,merged=apply_source_merge(project,plan,SourceMergeResult('merged.mp4',9,(2,3,4)))
        self.assertEqual((merged.camera,merged.start),(3,0))
        self.assertEqual([cut.camera for cut in candidate.cuts],[3,3,9])
        self.assertEqual(candidate.alignment_reference_camera,3)
        self.assertEqual(candidate.video_stack_order,[9,3])
        self.assertEqual(candidate.video_stack_hidden,[])
        self.assertEqual(candidate.master_audio,'master.wav')
        self.assertEqual(candidate.master_duration,25)
        self.assertEqual(candidate.clips[-1],project.clips[-1])
        self.assertEqual(project,original)

    def test_gap_compaction_trims_old_tail_range_and_cuts(self):
        project=MulticamProject(clips=[CameraClip('a',1,'a.mp4',0,2),CameraClip('b',2,'b.mp4',10,3)],
            cuts=[Cut(0,1),Cut(10,2)],range_end=13,edit_points=[2,11])
        plan=plan_source_merge(project,['a','b'])
        candidate,merged=apply_source_merge(project,plan,SourceMergeResult('merged.mp4',5,(2,3)))
        self.assertEqual(candidate.duration,5)
        self.assertEqual(candidate.range_end,5)
        self.assertEqual(candidate.edit_points,[2])
        self.assertEqual([(cut.time,cut.camera) for cut in candidate.cuts],[(0,1)])
        candidate.validate()


class SourceMergeValidationTests(unittest.TestCase):
    def test_format_audio_presence_and_partial_source_are_checked_before_output(self):
        with tempfile.TemporaryDirectory() as directory:
            paths=[Path(directory)/name for name in ('first.mp4','second.mp4')]
            for path in paths:path.write_bytes(b'fixture')
            project=MulticamProject(clips=[CameraClip(str(i),1,str(path),i*3,1) for i,path in enumerate(paths)])
            plan=plan_source_merge(project,['0','1'])
            video={'codec_name':'h264','width':320,'height':180,'r_frame_rate':'30/1',
                'avg_frame_rate':'30/1','time_base':'1/15360','duration':'1.0','start_time':'0','extradata_hash':'abc'}
            audio={'codec_name':'aac','sample_rate':'48000','channels':2,'time_base':'1/48000','duration':'1','start_time':'0'}
            def check(second,pattern):
                with patch('timeline_merge._probe',side_effect=[({'format':{'duration':'1'}},video,[audio]),second]):
                    with self.assertRaisesRegex(ValueError,pattern):
                        _validate_sources(plan,'ffprobe',None,None)
            check(({'format':{'duration':'1'}},dict(video,width=640),[audio]),'画面宽度')
            check(({'format':{'duration':'1'}},video,[]),'音轨数量')
            check(({'format':{'duration':'2'}},dict(video,duration='2'),[audio]),'时间线时长')
            check(({'format':{'duration':'1'}},video,[dict(audio,start_time='.2')]),'音画起点')
            check(({'format':{'duration':'1'}},dict(video,extradata_hash='other'),[audio]),'编码参数')


class SourceMergeFFmpegTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg=find_binary('ffmpeg',Path(__file__).resolve().parents[1])
        cls.ffprobe=find_binary('ffprobe',Path(__file__).resolve().parents[1])
        if not cls.ffmpeg or not cls.ffprobe:raise unittest.SkipTest('FFmpeg not installed')

    def encode(self,output,*,audio=False,segments=False,rate='30',seconds=None):
        args=[self.ffmpeg,'-v','error','-nostdin','-y','-f','lavfi','-i','testsrc2=size=160x90:rate='+rate]
        if audio:args+=['-f','lavfi','-i','sine=frequency=440:sample_rate=48000']
        args+=['-t',str(seconds or ('3' if segments else '1')),'-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p',
            '-g','30','-keyint_min','30','-sc_threshold','0','-bf','0','-threads','1']
        if audio:args+=['-c:a','aac','-b:a','128k']
        if segments:args+=['-f','segment','-segment_time','1','-reset_timestamps','1']
        args.append(str(output))
        subprocess.run(args,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=CREATE_NO_WINDOW,timeout=30)

    def test_silent_full_sources_merge_without_encoding_or_touching_originals(self):
        with tempfile.TemporaryDirectory(prefix='join_test_') as directory:
            root=Path(directory);first=root/"第一段's.mp4";second=root/'second.mp4'
            self.encode(first);second.write_bytes(first.read_bytes())
            project=MulticamProject(clips=[CameraClip('a',1,str(first),2,1),CameraClip('b',1,str(second),5,1)])
            plan=plan_source_merge(project,['a','b']);reports=[]
            originals={path:path.read_bytes() for path in (first,second)}
            result=render_source_merge(plan,self.ffmpeg,self.ffprobe,root/'merged_media',progress=lambda *value:reports.append(value))
            self.assertAlmostEqual(result.duration,2,places=2)
            self.assertEqual(Path(result.path).suffix,'.mp4')
            self.assertEqual(reports[-1][0],100)
            _,video,audio=_probe(self.ffprobe,result.path,None)
            self.assertEqual(video['codec_name'],'h264');self.assertEqual(audio,[])
            self.assertEqual(video['nb_frames'],'60')
            for path,data in originals.items():self.assertEqual(path.read_bytes(),data)
            candidate,clip=apply_source_merge(project,plan,result)
            self.assertEqual(len(candidate.clips),1);self.assertEqual(clip.start,2)
            self.assertFalse(list((root/'merged_media').glob('*.pending*')))

    def test_audio_recording_split_parts_keep_streams_and_cancel_leaves_no_pending(self):
        with tempfile.TemporaryDirectory(prefix='join_audio_') as directory:
            root=Path(directory);self.encode(root/'part%02d.mp4',audio=True,segments=True)
            paths=sorted(root.glob('part*.mp4'));clips=[]
            for index,path in enumerate(paths):
                payload,_,_=_probe(self.ffprobe,path,None)
                clips.append(CameraClip(str(index),index+1,str(path),0,float(payload['format']['duration'])))
            plan=plan_source_merge(MulticamProject(clips=clips),[clip.id for clip in clips])
            result=render_source_merge(plan,self.ffmpeg,self.ffprobe,root/'merged_media')
            _,video,audio=_probe(self.ffprobe,result.path,None)
            self.assertEqual(video['nb_frames'],'90');self.assertEqual(len(audio),1)
            self.assertEqual(audio[0]['codec_name'],'aac')
            self.assertAlmostEqual(result.duration,sum(clip.duration for clip in clips),delta=.067)
            candidate,merged=apply_source_merge(MulticamProject(clips=clips),plan,result)
            self.assertEqual(len(candidate.clips),1)
            self.assertEqual(merged.camera,1)
            event=threading.Event()
            def cancel(percent,phase):
                if percent>=10:event.set()
            with self.assertRaises(ExportCancelled):
                render_source_merge(plan,self.ffmpeg,self.ffprobe,root/'cancelled',cancel_event=event,progress=cancel)
            self.assertFalse((root/'cancelled').exists() and any((root/'cancelled').iterdir()))

    def test_fractional_frame_rate_and_time_base_survive_stream_copy(self):
        with tempfile.TemporaryDirectory(prefix='join_fractional_') as directory:
            root=Path(directory);first=root/'first.mp4';second=root/'second.mp4'
            self.encode(first,rate='30000/1001',seconds='1.001')
            second.write_bytes(first.read_bytes())
            payload,source,_=_probe(self.ffprobe,first,None)
            duration=float(payload['format']['duration'])
            project=MulticamProject(fps=30000/1001,clips=[CameraClip('a',1,str(first),0,duration),
                CameraClip('b',2,str(second),10,duration)])
            result=render_source_merge(plan_source_merge(project,['b','a']),self.ffmpeg,self.ffprobe,root/'merged_media')
            _,merged,_=_probe(self.ffprobe,result.path,None)
            self.assertEqual(merged['avg_frame_rate'],'30000/1001')
            self.assertEqual(merged['time_base'],source['time_base'])
            self.assertEqual(merged['nb_frames'],'60')
            self.assertAlmostEqual(result.duration,2.002,places=3)


if __name__=='__main__':unittest.main()
